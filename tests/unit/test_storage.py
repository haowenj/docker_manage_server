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
