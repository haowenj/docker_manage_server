from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from docker_manage_server.deployment import DeploymentService
from docker_manage_server.docker_runtime import DockerRuntime
from docker_manage_server.storage import TaskStore


class FakeRuntime:
    def __init__(self, *, compose_returncode: int = 0):
        self.calls: list[str] = []
        self.compose_returncode = compose_returncode

    def load_image(self, image_tar: Path, cwd: Path):
        self.calls.append("load")
        return SimpleNamespace(returncode=0, stdout=b"loaded", stderr=b"")

    def compose_up(self, cwd: Path):
        self.calls.append("compose")
        return SimpleNamespace(
            returncode=self.compose_returncode,
            stdout=b"started" if self.compose_returncode == 0 else b"",
            stderr=b"failed" if self.compose_returncode else b"",
        )


def make_service(tmp_path: Path, runtime=None) -> DeploymentService:
    return DeploymentService(TaskStore(tmp_path), runtime or FakeRuntime())


def test_upload_ends_in_pending_review(tmp_path, valid_archive):
    service = make_service(tmp_path)
    with valid_archive.open("rb") as archive:
        task = service.upload("task-1", archive, "demo.tar.gz")
    assert task.status.value == "pending_review"
    assert task.app_name == "demo"


def test_discard_physically_removes_pending_task(tmp_path, valid_archive):
    service = make_service(tmp_path)
    with valid_archive.open("rb") as archive:
        service.upload("task-1", archive, "demo.tar.gz")
    service.discard("task-1")
    assert not (tmp_path / "packages/task-1").exists()
    assert not (tmp_path / "tasks/task-1.json").exists()


def test_deploy_loads_image_then_runs_compose_without_deleting_old_bind_data(
    tmp_path, valid_archive_with_files
):
    runtime = FakeRuntime()
    service = make_service(tmp_path, runtime)
    with valid_archive_with_files.open("rb") as archive:
        service.upload("task-1", archive, "demo.tar.gz")
    (tmp_path / "deployments/demo/files/old.db").parent.mkdir(parents=True)
    (tmp_path / "deployments/demo/files/old.db").write_text("keep", encoding="utf-8")

    task = service.deploy("task-1")

    assert task.status.value == "deployed"
    assert (tmp_path / "deployments/demo/files/old.db").read_text(encoding="utf-8") == "keep"
    assert runtime.calls == ["load", "compose"]


def test_failed_compose_persists_failed_task_and_error(tmp_path, valid_archive):
    service = make_service(tmp_path, FakeRuntime(compose_returncode=1))
    with valid_archive.open("rb") as archive:
        service.upload("task-1", archive, "demo.tar.gz")

    task = service.deploy("task-1")

    assert task.status.value == "failed"
    assert "failed" in task.error
    assert service.store.get("task-1").status.value == "failed"
