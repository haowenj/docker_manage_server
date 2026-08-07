from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest


def _write_archive(
    root: Path,
    archive_path: Path,
    *,
    include_files: bool = False,
    app_name: str = "demo",
    env_text: str = "SECRET=value\n",
    server_paths: tuple[str, ...] = ("./files/sqlite",),
) -> Path:
    payload = root / "payload"
    payload.mkdir(parents=True)
    (payload / ".env").write_text(env_text, encoding="utf-8")
    (payload / "compose.yaml").write_text(
        "services:\n  web:\n    image: demo:latest\n", encoding="utf-8"
    )
    (payload / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "app_name": app_name,
                "server_paths": list(server_paths),
            }
        ),
        encoding="utf-8",
    )
    if include_files:
        (payload / "files").mkdir()
        (payload / "files/data.db").write_text("new", encoding="utf-8")
        (payload / "images.tar").write_bytes(b"fake image archive")

    checksum_lines = []
    for path in sorted(item for item in payload.rglob("*") if item.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        checksum_lines.append(f"{digest}  {path.relative_to(payload).as_posix()}\n")
    (payload / "checksums.sha256").write_text("".join(checksum_lines), encoding="utf-8")

    with tarfile.open(archive_path, "w:gz") as bundle:
        for path in sorted(payload.rglob("*")):
            bundle.add(path, arcname=path.relative_to(payload).as_posix(), recursive=False)
    return archive_path


@pytest.fixture
def valid_archive(tmp_path: Path) -> Path:
    return _write_archive(tmp_path / "valid", tmp_path / "demo.tar.gz")


@pytest.fixture
def valid_archive_with_files(tmp_path: Path) -> Path:
    return _write_archive(
        tmp_path / "valid-with-files", tmp_path / "demo-with-files.tar.gz", include_files=True
    )


@pytest.fixture
def unsafe_app_name_archive(tmp_path: Path) -> Path:
    return _write_archive(
        tmp_path / "unsafe-app", tmp_path / "unsafe-app.tar.gz", app_name="demo name"
    )


@pytest.fixture
def unsafe_server_path_archive(tmp_path: Path) -> Path:
    return _write_archive(
        tmp_path / "unsafe-server-path",
        tmp_path / "unsafe-server-path.tar.gz",
        server_paths=("../outside",),
    )


@pytest.fixture
def html_injection_archive(tmp_path: Path) -> Path:
    return _write_archive(
        tmp_path / "html-injection",
        tmp_path / "html-injection.tar.gz",
        env_text="VALUE=<script>alert('x')</script>\n",
    )
