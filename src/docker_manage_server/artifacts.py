from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import tarfile
from typing import Any

from pydantic import BaseModel

from .models import FileEntry


class ArchiveReview(BaseModel):
    app_name: str
    server_paths: tuple[str, ...]
    files: tuple[FileEntry, ...]
    env_text: str
    compose_text: str


def extract_and_review(archive_path: Path, extracted_dir: Path) -> ArchiveReview:
    archive_path = Path(archive_path).resolve()
    extracted_dir = Path(extracted_dir).resolve()
    if not archive_path.is_file():
        raise ValueError(f"archive does not exist: {archive_path}")
    if extracted_dir.exists():
        shutil.rmtree(extracted_dir)
    extracted_dir.mkdir(mode=0o700, parents=True)

    seen: set[str] = set()
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                relative = _safe_member_name(member.name)
                if relative is None or relative in seen:
                    raise ValueError(f"unsafe archive member: {member.name!r}")
                seen.add(relative)
                _validate_member(member, extracted_dir, relative)
                archive.extract(member, path=extracted_dir)
    except (tarfile.TarError, OSError) as exc:
        raise ValueError(f"invalid archive: {exc}") from exc

    required = {"manifest.json", "checksums.sha256", "compose.yaml", ".env"}
    missing = sorted(path for path in required if not (extracted_dir / path).is_file())
    if missing:
        raise ValueError(f"archive missing required files: {', '.join(missing)}")
    _verify_checksums(extracted_dir)

    try:
        manifest = json.loads((extracted_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid manifest.json: {exc}") from exc
    app_name = manifest.get("app_name") if isinstance(manifest, dict) else None
    if not isinstance(app_name, str) or not _safe_app_name(app_name):
        raise ValueError("manifest app_name is unsafe")
    server_paths = _manifest_server_paths(manifest)

    return ArchiveReview(
        app_name=app_name,
        server_paths=server_paths,
        files=list_files(extracted_dir),
        env_text=(extracted_dir / ".env").read_text(encoding="utf-8"),
        compose_text=(extracted_dir / "compose.yaml").read_text(encoding="utf-8"),
    )


def list_files(extracted_dir: Path) -> tuple[FileEntry, ...]:
    root = Path(extracted_dir).resolve()
    entries: list[FileEntry] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            kind = "directory"
            size = 0
        elif stat.S_ISLNK(mode):
            kind = "symlink"
            size = len(os.readlink(path))
        elif stat.S_ISREG(mode):
            kind = "file"
            size = path.stat().st_size
        else:
            kind = "other"
            size = 0
        entries.append(FileEntry(path=relative, kind=kind, size=size))
    return tuple(entries)


def overlay_directory(source: Path, target: Path) -> None:
    source = Path(source).resolve()
    target = Path(target).resolve()
    if not source.is_dir():
        raise ValueError(f"overlay source is not a directory: {source}")
    target.mkdir(mode=0o750, parents=True, exist_ok=True)
    for source_path in sorted(source.rglob("*")):
        relative = source_path.relative_to(source)
        target_path = target / relative
        mode = source_path.lstat().st_mode
        if stat.S_ISDIR(mode):
            if target_path.exists() and not target_path.is_dir():
                target_path.unlink()
            if not target_path.exists():
                target_path.mkdir(parents=True, exist_ok=False)
                target_path.chmod(stat.S_IMODE(mode))
        elif stat.S_ISLNK(mode):
            if target_path.exists() or target_path.is_symlink():
                _remove_path(target_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.symlink_to(os.readlink(source_path))
        elif stat.S_ISREG(mode):
            if target_path.exists() and target_path.is_dir():
                shutil.rmtree(target_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
        else:
            raise ValueError(f"unsupported overlay member: {relative}")


def prepare_server_directories(
    deployment_dir: Path,
    server_paths: tuple[str, ...],
) -> None:
    root = Path(deployment_dir).resolve()
    for value in server_paths:
        path = PurePosixPath(value)
        if path.is_absolute():
            continue
        if (
            ".." in path.parts
            or len(path.parts) < 2
            or path.parts[0] != "files"
        ):
            raise ValueError(f"server path is outside deployment directory: {value}")
        target = root.joinpath(*path.parts)
        if not target.resolve(strict=False).is_relative_to(root):
            raise ValueError(f"server path is outside deployment directory: {value}")
        if target.exists() or target.is_symlink():
            continue
        target.mkdir(parents=True, exist_ok=False)
        target.chmod(0o777)


def write_checksums(root: Path) -> None:
    root = Path(root).resolve()
    lines = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name != "checksums.sha256":
            relative = path.relative_to(root).as_posix()
            lines.append(f"{_sha256(path)}  {relative}\n")
    destination = root / "checksums.sha256"
    partial = root / ".checksums.sha256.partial"
    partial.write_text("".join(lines), encoding="utf-8")
    partial.replace(destination)


def _verify_checksums(root: Path) -> None:
    checksums_path = root / "checksums.sha256"
    declared: dict[str, str] = {}
    for line_number, line in enumerate(
        checksums_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        digest, separator, relative = line.partition("  ")
        if (
            not separator
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or _safe_member_name(relative) is None
            or relative in declared
        ):
            raise ValueError(f"invalid checksum line: {line_number}")
        declared[relative] = digest

    actual = {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.name != "checksums.sha256"
    }
    if set(actual) != set(declared):
        raise ValueError("checksum file list does not match archive files")
    for relative, expected in declared.items():
        if actual[relative] != expected:
            raise ValueError(f"checksum mismatch: {relative}")


def _validate_member(member: tarfile.TarInfo, root: Path, relative: str) -> None:
    mode = member.mode
    if member.ischr() or member.isblk() or member.isfifo() or stat.S_ISSOCK(mode):
        raise ValueError(f"unsafe archive member: {member.name!r}")
    destination = root / relative
    if not destination.parent.resolve(strict=False).is_relative_to(root):
        raise ValueError(f"unsafe archive member: {member.name!r}")
    if member.issym():
        target = posixpath.normpath(posixpath.join(posixpath.dirname(relative), member.linkname))
        if _safe_member_name(target) is None or not (root / target).resolve(strict=False).is_relative_to(root):
            raise ValueError(f"unsafe archive member: {member.name!r}")
    elif member.islnk():
        target = _safe_member_name(member.linkname)
        if target is None or not (root / target).resolve(strict=False).is_relative_to(root):
            raise ValueError(f"unsafe archive member: {member.name!r}")
    elif not (member.isdir() or member.isfile()):
        raise ValueError(f"unsafe archive member: {member.name!r}")


def _safe_member_name(value: str) -> str | None:
    if not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


def _safe_app_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value))


def _manifest_server_paths(manifest: dict[str, Any]) -> tuple[str, ...]:
    values = manifest.get("server_paths", [])
    if not isinstance(values, list):
        raise ValueError("manifest server_paths is unsafe")

    normalized: list[str] = []
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or "\\" in value
            or "\x00" in value
        ):
            raise ValueError("manifest server_paths is unsafe")
        path = PurePosixPath(value)
        if ".." in path.parts:
            raise ValueError("manifest server_paths is unsafe")
        if not path.is_absolute() and (
            len(path.parts) < 2 or path.parts[0] != "files"
        ):
            raise ValueError("manifest server_paths is unsafe")
        rendered = path.as_posix()
        if rendered not in normalized:
            normalized.append(rendered)
    return tuple(normalized)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
