from __future__ import annotations

from pathlib import Path
import shutil
from threading import Lock
from typing import Any, BinaryIO

from .artifacts import extract_and_review, overlay_directory, prepare_server_directories
from .docker_runtime import DockerRuntime
from .models import DeploymentTask, TaskStatus
from .storage import TaskStore


class DeploymentStateError(RuntimeError):
    pass


class DeploymentCommandError(RuntimeError):
    pass


class DeploymentService:
    def __init__(self, store: TaskStore, runtime: DockerRuntime):
        self.store = store
        self.runtime = runtime
        self._lock_guard = Lock()
        self._app_locks: dict[str, Lock] = {}

    def upload(self, task_id: str, archive: BinaryIO, filename: str) -> DeploymentTask:
        task = self.store.create(task_id, filename)
        archive_path = task.package_dir / "archive.tar.gz"
        try:
            with archive_path.open("wb") as destination:
                shutil.copyfileobj(archive, destination)
            task.status = TaskStatus.EXTRACTING
            self.store.save(task)
            review = extract_and_review(archive_path, task.extracted_dir)
            task.status = TaskStatus.PENDING_REVIEW
            task.app_name = review.app_name
            task.server_paths = review.server_paths
            task.deployment_dir = self.store.deployment_dir(review.app_name)
            self.store.save(task)
            return task
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            self.store.save(task)
            raise

    def deploy(self, task_id: str) -> DeploymentTask:
        task = self.store.get(task_id)
        if task.status is not TaskStatus.PENDING_REVIEW:
            raise DeploymentStateError(
                f"task {task_id} cannot deploy from status {task.status.value}"
            )
        if not task.app_name or task.deployment_dir is None:
            raise DeploymentStateError(f"task {task_id} has no deployment target")

        lock = self._app_lock(task.app_name)
        with lock:
            task = self.store.get(task_id)
            if task.status is not TaskStatus.PENDING_REVIEW:
                raise DeploymentStateError(
                    f"task {task_id} cannot deploy from status {task.status.value}"
                )
            task.status = TaskStatus.DEPLOYING
            task.command_output = ""
            task.error = None
            self.store.save(task)
            try:
                deployment_dir = task.deployment_dir
                assert deployment_dir is not None
                overlay_directory(task.extracted_dir, deployment_dir)
                prepare_server_directories(deployment_dir, task.server_paths)
                image_tar = deployment_dir / "images.tar"
                if image_tar.is_file():
                    load_result = self.runtime.load_image(image_tar, deployment_dir)
                    task.command_output += self._format_output("docker load", load_result)
                    self._require_success("docker load", load_result)
                compose_result = self.runtime.compose_up(deployment_dir)
                task.command_output += self._format_output("docker compose", compose_result)
                self._require_success("docker compose", compose_result)
                task.status = TaskStatus.DEPLOYED
                self.store.save(task)
                return task
            except Exception as exc:
                task.status = TaskStatus.FAILED
                task.error = str(exc)
                self.store.save(task)
                return task

    def discard(self, task_id: str) -> DeploymentTask:
        task = self.store.get(task_id)
        if task.status in {TaskStatus.EXTRACTING, TaskStatus.DEPLOYING, TaskStatus.DEPLOYED}:
            raise DeploymentStateError(
                f"task {task_id} cannot be discarded from status {task.status.value}"
            )
        discarded = task.model_copy(update={"status": TaskStatus.DISCARDED})
        self.store.delete(task_id)
        return discarded

    def _app_lock(self, app_name: str) -> Lock:
        with self._lock_guard:
            return self._app_locks.setdefault(app_name, Lock())

    @staticmethod
    def _require_success(name: str, result: Any) -> None:
        if result.returncode != 0:
            stderr = _as_text(getattr(result, "stderr", b""))
            raise DeploymentCommandError(f"{name} failed: {stderr or result.returncode}")

    @staticmethod
    def _format_output(name: str, result: Any) -> str:
        stdout = _as_text(getattr(result, "stdout", b""))
        stderr = _as_text(getattr(result, "stderr", b""))
        return f"[{name}]\n{stdout}{stderr}"


def _as_text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
