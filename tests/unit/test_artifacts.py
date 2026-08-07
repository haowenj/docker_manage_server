import io
import stat
import tarfile
from pathlib import Path

import pytest

import docker_manage_server.artifacts as artifacts
from docker_manage_server.artifacts import extract_and_review, overlay_directory


def test_extract_review_reads_manifest_env_and_compose(valid_archive: Path, tmp_path: Path):
    review = extract_and_review(valid_archive, tmp_path / "extracted")
    assert review.app_name == "demo"
    assert review.server_paths == ("files/sqlite",)
    assert "SECRET=value" in review.env_text
    assert "services:" in review.compose_text


def test_path_traversal_archive_is_rejected(tmp_path: Path):
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("../../outside.txt")
        info.size = 1
        bundle.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="unsafe archive member"):
        extract_and_review(archive, tmp_path / "extracted")


def test_unsafe_manifest_app_name_is_rejected(unsafe_app_name_archive: Path, tmp_path: Path):
    with pytest.raises(ValueError, match="app_name is unsafe"):
        extract_and_review(unsafe_app_name_archive, tmp_path / "extracted")


def test_unsafe_manifest_server_path_is_rejected(
    unsafe_server_path_archive: Path, tmp_path: Path
):
    with pytest.raises(ValueError, match="manifest server_paths is unsafe"):
        extract_and_review(unsafe_server_path_archive, tmp_path / "extracted")


def test_overlay_does_not_delete_files_missing_from_new_package(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "compose.yaml").write_text("new", encoding="utf-8")
    (target / "compose.yaml").write_text("old", encoding="utf-8")
    (target / "files/data.db").parent.mkdir()
    (target / "files/data.db").write_text("keep", encoding="utf-8")
    overlay_directory(source, target)
    assert (target / "compose.yaml").read_text(encoding="utf-8") == "new"
    assert (target / "files/data.db").read_text(encoding="utf-8") == "keep"


def test_overlay_preserves_mode_for_new_directory(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source_directory = source / "files/sqlite"
    source_directory.mkdir(parents=True)
    source_directory.chmod(0o777)
    target.mkdir()

    overlay_directory(source, target)

    assert stat.S_IMODE((target / "files/sqlite").stat().st_mode) == 0o777


def test_overlay_preserves_mode_of_existing_directory(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source_directory = source / "files/sqlite"
    target_directory = target / "files/sqlite"
    source_directory.mkdir(parents=True)
    source_directory.chmod(0o777)
    target_directory.mkdir(parents=True)
    target_directory.chmod(0o700)

    overlay_directory(source, target)

    assert stat.S_IMODE(target_directory.stat().st_mode) == 0o700


def test_write_checksums_matches_all_regular_workspace_files(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".env").write_text("A=2\n", encoding="utf-8")
    (root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (root / "checksums.sha256").write_text("stale\n", encoding="utf-8")

    artifacts.write_checksums(root)
    artifacts._verify_checksums(root)

    entries = (root / "checksums.sha256").read_text(encoding="utf-8")
    assert "  .env\n" in entries
    assert "  compose.yaml\n" in entries
    assert "  checksums.sha256\n" not in entries
