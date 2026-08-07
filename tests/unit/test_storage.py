from datetime import datetime, timezone
import json
from pathlib import Path

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
