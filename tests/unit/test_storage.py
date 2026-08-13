from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from docker_manage_server.models import TaskStatus
from docker_manage_server.storage import TaskStore


def test_create_task_persists_json(tmp_path: Path):
    store = TaskStore(tmp_path)
    task = store.create("task-1", "demo.tar.gz")
    assert task.status is TaskStatus.UPLOADED
    assert store.get("task-1").original_filename == "demo.tar.gz"
    assert store.package_dir("task-1").is_dir()


def test_delete_task_removes_only_task_directory(tmp_path: Path):
    store = TaskStore(tmp_path)
    store.create("task-1", "demo.tar.gz")
    store.create("task-2", "other.tar.gz")
    store.delete("task-1")
    assert not (tmp_path / "packages/task-1").exists()
    assert (tmp_path / "packages/task-2").exists()


def test_package_size_sums_regular_files_and_ignores_symlinks(tmp_path: Path):
    store = TaskStore(tmp_path)
    task = store.create("sized", "demo.tar.gz")
    (task.package_dir / "archive.tar.gz").write_bytes(b"1234")
    nested = task.package_dir / "extracted"
    nested.mkdir()
    (nested / "compose.yaml").write_bytes(b"123456")
    external = tmp_path / "external"
    external.mkdir()
    (external / "large.bin").write_bytes(b"x" * 100)
    (task.package_dir / "file-link").symlink_to(external / "large.bin")
    (task.package_dir / "dir-link").symlink_to(
        external,
        target_is_directory=True,
    )

    assert store.package_size_bytes("sized") == 10


def test_package_size_returns_zero_for_missing_directory(tmp_path: Path):
    store = TaskStore(tmp_path)

    assert store.package_size_bytes("missing") == 0


def test_package_size_skips_file_that_disappears(tmp_path: Path, monkeypatch):
    store = TaskStore(tmp_path)
    task = store.create("changing", "demo.tar.gz")
    target = task.package_dir / "vanishing.bin"
    target.write_bytes(b"123")
    real_lstat = Path.lstat

    def vanish(path):
        if path == target:
            raise FileNotFoundError(path)
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", vanish)

    assert store.package_size_bytes("changing") == 0


def test_package_size_propagates_read_error(tmp_path: Path, monkeypatch):
    store = TaskStore(tmp_path)
    task = store.create("blocked", "demo.tar.gz")
    target = task.package_dir / "blocked.bin"
    target.write_bytes(b"123")
    real_lstat = Path.lstat

    def blocked(path):
        if path == target:
            raise PermissionError("blocked")
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", blocked)

    with pytest.raises(PermissionError, match="blocked"):
        store.package_size_bytes("blocked")


def test_delete_can_retry_after_state_file_unlink_failure(
    tmp_path: Path,
    monkeypatch,
):
    store = TaskStore(tmp_path)
    store.create("partial", "demo.tar.gz")
    state_path = tmp_path / "tasks/partial.json"
    real_unlink = Path.unlink

    def fail_state_once(path, *args, **kwargs):
        if path == state_path:
            raise PermissionError("blocked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_state_once)
    with pytest.raises(PermissionError, match="blocked"):
        store.delete("partial")
    assert not store.package_dir("partial").exists()
    assert state_path.is_file()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    store.delete("partial")
    assert not state_path.exists()


def test_save_and_reload_status(tmp_path: Path):
    store = TaskStore(tmp_path)
    task = store.create("task-1", "demo.tar.gz")
    task.status = TaskStatus.PENDING_REVIEW
    store.save(task)
    assert store.get("task-1").status is TaskStatus.PENDING_REVIEW


def test_list_returns_tasks_by_latest_update(tmp_path: Path):
    moments = iter(
        (
            datetime(2026, 8, 7, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 7, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 8, 7, 1, 2, tzinfo=timezone.utc),
        )
    )
    store = TaskStore(tmp_path, clock=lambda: next(moments))
    first = store.create("first", "first.tar.gz")
    second = store.create("second", "second.tar.gz")
    first.status = TaskStatus.PENDING_REVIEW
    store.save(first)

    assert [task.task_id for task in store.list()] == ["first", "second"]
    assert first.created_at == datetime(2026, 8, 7, 1, 0, tzinfo=timezone.utc)
    assert first.updated_at == datetime(2026, 8, 7, 1, 2, tzinfo=timezone.utc)
    assert second.created_at == second.updated_at


def test_old_task_json_uses_state_file_mtime(tmp_path: Path):
    store = TaskStore(tmp_path)
    store.create("legacy", "legacy.tar.gz")
    state_path = tmp_path / "tasks/legacy.json"
    body = json.loads(state_path.read_text(encoding="utf-8"))
    body.pop("created_at")
    body.pop("updated_at")
    state_path.write_text(json.dumps(body), encoding="utf-8")

    loaded = store.get("legacy")

    assert loaded.created_at is not None
    assert loaded.updated_at == loaded.created_at


def test_old_task_json_defaults_configuration_editing_fields(tmp_path: Path):
    store = TaskStore(tmp_path)
    store.create("legacy-config", "legacy.tar.gz")
    state_path = tmp_path / "tasks/legacy-config.json"
    body = json.loads(state_path.read_text(encoding="utf-8"))
    body.pop("directory_rules", None)
    body.pop("failure_phase", None)
    body.pop("edited_at", None)
    state_path.write_text(json.dumps(body), encoding="utf-8")

    loaded = store.get("legacy-config")

    assert loaded.directory_rules is None
    assert loaded.failure_phase is None
    assert loaded.edited_at is None
