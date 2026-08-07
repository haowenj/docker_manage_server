from __future__ import annotations

from io import BytesIO
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

import docker_manage_server.artifacts as artifacts
from docker_manage_server.deployment_config import can_retry_task
from docker_manage_server.deployment import (
    DeploymentConfigurationError,
    DeploymentConfigurationTooLargeError,
    DeploymentService,
    DeploymentStateError,
)
from docker_manage_server.docker_runtime import DockerRuntimeError
from docker_manage_server.models import DirectoryRule, FailurePhase, TaskStatus
from docker_manage_server.storage import TaskStore


class FakeRuntime:
    def __init__(
        self,
        *,
        compose_returncode: int = 0,
        compose_observer=None,
        compose_returncodes: list[int] | None = None,
        compose_config_returncode: int = 0,
        compose_config_stderr: bytes = b"invalid compose",
    ):
        self.calls: list[str] = []
        self.compose_returncode = compose_returncode
        self.compose_returncodes = list(compose_returncodes or [])
        self.compose_observer = compose_observer
        self.compose_config_returncode = compose_config_returncode
        self.compose_config_stderr = compose_config_stderr

    def load_image(self, image_tar: Path, cwd: Path):
        self.calls.append("load")
        return SimpleNamespace(returncode=0, stdout=b"loaded", stderr=b"")

    def compose_up(self, cwd: Path):
        if self.compose_observer is not None:
            self.compose_observer(cwd)
        self.calls.append("compose")
        returncode = (
            self.compose_returncodes.pop(0)
            if self.compose_returncodes
            else self.compose_returncode
        )
        return SimpleNamespace(
            returncode=returncode,
            stdout=b"started" if returncode == 0 else b"",
            stderr=b"failed" if returncode else b"",
        )

    def compose_config(self, project_dir: Path, compose_file: Path, env_file: Path):
        self.calls.append("config")
        return SimpleNamespace(
            returncode=self.compose_config_returncode,
            stdout=b"",
            stderr=self.compose_config_stderr if self.compose_config_returncode else b"",
        )


def make_service(tmp_path: Path, runtime=None) -> DeploymentService:
    return DeploymentService(TaskStore(tmp_path), runtime or FakeRuntime())


def test_upload_ends_in_pending_review(tmp_path, valid_archive):
    service = make_service(tmp_path)
    with valid_archive.open("rb") as archive:
        task = service.upload("task-1", archive, "demo.tar.gz")
    assert task.status.value == "pending_review"
    assert task.app_name == "demo"
    assert task.server_paths == ("files/sqlite",)


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


def test_deploy_prepares_missing_server_directory_before_compose(
    tmp_path, valid_archive
):
    observed: list[tuple[bool, int | None]] = []

    def observe(cwd: Path) -> None:
        directory = cwd / "files/sqlite"
        mode = stat.S_IMODE(directory.stat().st_mode) if directory.exists() else None
        observed.append((directory.is_dir(), mode))

    service = make_service(tmp_path, FakeRuntime(compose_observer=observe))
    with valid_archive.open("rb") as archive:
        service.upload("task-1", archive, "demo.tar.gz")

    task = service.deploy("task-1")

    assert task.status.value == "deployed"
    assert observed == [(True, 0o777)]


def test_failed_compose_persists_failed_task_and_error(tmp_path, valid_archive):
    service = make_service(tmp_path, FakeRuntime(compose_returncode=1))
    with valid_archive.open("rb") as archive:
        service.upload("task-1", archive, "demo.tar.gz")

    task = service.deploy("task-1")

    assert task.status.value == "failed"
    assert "failed" in task.error
    assert service.store.get("task-1").status.value == "failed"


def test_edit_configuration_updates_workspace_rules_checksum_and_state(
    tmp_path, valid_archive
):
    service = make_service(tmp_path)
    with valid_archive.open("rb") as archive:
        service.upload("task-1", archive, "demo.tar.gz")

    task = service.edit_configuration(
        "task-1",
        "SECRET=changed\n",
        "services:\n  web:\n    image: changed:latest\n",
        (DirectoryRule(path="data/mysql", mode="0770"),),
    )

    assert task.status is TaskStatus.PENDING_REVIEW
    assert task.failure_phase is None
    assert task.error is None
    assert task.edited_at is not None
    assert task.directory_rules == (DirectoryRule(path="data/mysql", mode="0770"),)
    assert (task.extracted_dir / ".env").read_text(encoding="utf-8") == "SECRET=changed\n"
    assert not (task.extracted_dir / ".env.candidate").exists()
    assert not (task.extracted_dir / ".compose.candidate.yaml").exists()
    artifacts._verify_checksums(task.extracted_dir)


def test_edit_configuration_recovers_deploy_failure_and_preserves_output(
    tmp_path, valid_archive
):
    service = make_service(tmp_path)
    with valid_archive.open("rb") as archive:
        task = service.upload("task-1", archive, "demo.tar.gz")
    task.status = TaskStatus.FAILED
    task.failure_phase = FailurePhase.DEPLOY
    task.error = "compose failed"
    task.command_output = "old diagnostic output"
    service.store.save(task)

    edited = service.edit_configuration(
        "task-1", "A=2\n", "services: {}\n", ()
    )

    assert edited.status is TaskStatus.PENDING_REVIEW
    assert edited.failure_phase is None
    assert edited.error is None
    assert edited.command_output == "old diagnostic output"


def test_edit_configuration_validation_failure_preserves_everything(
    tmp_path, valid_archive
):
    runtime = FakeRuntime(compose_config_returncode=1)
    service = make_service(tmp_path, runtime)
    with valid_archive.open("rb") as archive:
        before = service.upload("task-1", archive, "demo.tar.gz")
    env_before = (before.extracted_dir / ".env").read_bytes()
    compose_before = (before.extracted_dir / "compose.yaml").read_bytes()
    checksum_before = (before.extracted_dir / "checksums.sha256").read_bytes()

    with pytest.raises(DeploymentConfigurationError, match="invalid compose"):
        service.edit_configuration("task-1", "BROKEN=1\n", "not compose", ())

    after = service.store.get("task-1")
    assert (after.extracted_dir / ".env").read_bytes() == env_before
    assert (after.extracted_dir / "compose.yaml").read_bytes() == compose_before
    assert (after.extracted_dir / "checksums.sha256").read_bytes() == checksum_before
    assert after.directory_rules == before.directory_rules
    assert after.edited_at is None
    assert not (after.extracted_dir / ".env.candidate").exists()
    assert not (after.extracted_dir / ".compose.candidate.yaml").exists()


def test_edit_configuration_wraps_compose_runtime_error(
    tmp_path, valid_archive, monkeypatch
):
    service = make_service(tmp_path)
    with valid_archive.open("rb") as archive:
        service.upload("task-1", archive, "demo.tar.gz")

    def unavailable(*_args):
        raise DockerRuntimeError("docker compose unavailable")

    monkeypatch.setattr(service.runtime, "compose_config", unavailable)
    with pytest.raises(DeploymentConfigurationError, match="unavailable"):
        service.edit_configuration("task-1", "A=1\n", "services: {}\n", ())


def test_edit_configuration_rejects_oversized_env(tmp_path, valid_archive):
    service = make_service(tmp_path)
    with valid_archive.open("rb") as archive:
        service.upload("task-1", archive, "demo.tar.gz")
    with pytest.raises(DeploymentConfigurationTooLargeError):
        service.edit_configuration(
            "task-1", "A" * (1024 * 1024 + 1), "services: {}\n", ()
        )


def test_edit_configuration_rejects_text_that_cannot_encode_as_utf8(
    tmp_path, valid_archive
):
    service = make_service(tmp_path)
    with valid_archive.open("rb") as archive:
        service.upload("task-1", archive, "demo.tar.gz")
    with pytest.raises(DeploymentConfigurationError, match="UTF-8"):
        service.edit_configuration("task-1", "VALUE=\ud800\n", "services: {}\n", ())


def test_edit_configuration_rejects_deployed_and_upload_failed_tasks(
    tmp_path, valid_archive
):
    service = make_service(tmp_path)
    with valid_archive.open("rb") as archive:
        task = service.upload("task-1", archive, "demo.tar.gz")
    for status, phase in (
        (TaskStatus.DEPLOYED, None),
        (TaskStatus.FAILED, FailurePhase.UPLOAD),
    ):
        task.status = status
        task.failure_phase = phase
        service.store.save(task)
        with pytest.raises(DeploymentStateError):
            service.edit_configuration("task-1", "A=1\n", "services: {}\n", ())


def test_edit_configuration_store_failure_restores_workspace(
    tmp_path, valid_archive, monkeypatch
):
    service = make_service(tmp_path)
    with valid_archive.open("rb") as archive:
        task = service.upload("task-1", archive, "demo.tar.gz")
    paths = (
        task.extracted_dir / ".env",
        task.extracted_dir / "compose.yaml",
        task.extracted_dir / "checksums.sha256",
    )
    before = {path: path.read_bytes() for path in paths}
    real_save = service.store.save

    def fail_edited_save(candidate):
        if candidate.edited_at is not None:
            raise OSError("state write failed")
        return real_save(candidate)

    monkeypatch.setattr(service.store, "save", fail_edited_save)
    with pytest.raises(OSError, match="state write failed"):
        service.edit_configuration("task-1", "A=2\n", "services: {}\n", ())

    assert {path: path.read_bytes() for path in paths} == before
    assert service.store.get("task-1").edited_at is None


def test_upload_initializes_relative_manifest_directory_rules(tmp_path, valid_archive):
    service = make_service(tmp_path)
    with valid_archive.open("rb") as archive:
        task = service.upload("task-1", archive, "demo.tar.gz")
    assert task.directory_rules == (
        DirectoryRule(path="files/sqlite", mode="0777"),
    )
    assert task.failure_phase is None


def test_retry_updates_existing_directory_mode_before_compose(tmp_path, valid_archive):
    observed = []

    def observe(cwd: Path):
        target = cwd / "data/mysql"
        observed.append(stat.S_IMODE(target.stat().st_mode))

    runtime = FakeRuntime(compose_returncodes=[1, 0], compose_observer=observe)
    service = make_service(tmp_path, runtime)
    with valid_archive.open("rb") as archive:
        task = service.upload("task-1", archive, "demo.tar.gz")
    task.directory_rules = (DirectoryRule(path="data/mysql", mode="0770"),)
    service.store.save(task)

    first = service.deploy("task-1")
    assert first.status is TaskStatus.FAILED
    assert first.failure_phase is FailurePhase.DEPLOY
    assert first.deployment_dir is not None
    (first.deployment_dir / "data/mysql").chmod(0o755)

    second = service.deploy("task-1")
    assert second.status is TaskStatus.DEPLOYED
    assert observed == [0o770, 0o770]


def test_upload_failure_records_upload_phase(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(Exception):
        service.upload("task-1", BytesIO(b"broken"), "broken.tar.gz")
    task = service.store.get("task-1")
    assert task.status is TaskStatus.FAILED
    assert task.failure_phase is FailurePhase.UPLOAD
    assert can_retry_task(task) is False


def test_begin_deploy_persists_deploying_and_blocks_duplicate_queue(
    tmp_path, valid_archive
):
    service = make_service(tmp_path)
    with valid_archive.open("rb") as archive:
        service.upload("task-1", archive, "demo.tar.gz")
    queued = service.begin_deploy("task-1")
    assert queued.status is TaskStatus.DEPLOYING
    assert service.store.get("task-1").status is TaskStatus.DEPLOYING
    with pytest.raises(DeploymentStateError):
        service.begin_deploy("task-1")
