from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import shutil
from threading import Lock
from typing import Any, BinaryIO

from .artifacts import (
    extract_and_review,
    overlay_directory,
    write_checksums,
)
from .deployment_config import (
    ConfigurationValidationError,
    apply_directory_rules,
    can_edit_task,
    can_retry_task,
    effective_directory_rules,
    normalize_directory_rules,
    validate_directory_targets,
)
from .docker_runtime import DockerRuntime, DockerRuntimeError
from .models import DeploymentTask, DirectoryRule, FailurePhase, TaskStatus
from .storage import TaskStore


ENV_MAX_BYTES = 1024 * 1024
COMPOSE_MAX_BYTES = 2 * 1024 * 1024


class DeploymentStateError(RuntimeError):
    pass


class DeploymentCommandError(RuntimeError):
    pass


class DeploymentConfigurationError(RuntimeError):
    pass


class DeploymentConfigurationTooLargeError(DeploymentConfigurationError):
    pass


class DeploymentService:
    def __init__(self, store: TaskStore, runtime: DockerRuntime):
        self.store = store
        self.runtime = runtime
        self._lock_guard = Lock()
        self._app_locks: dict[str, Lock] = {}
        self._task_locks: dict[str, Lock] = {}

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
            task.directory_rules = tuple(
                DirectoryRule(path=value, mode="0777")
                for value in review.server_paths
                if not PurePosixPath(value).is_absolute()
            )
            task.failure_phase = None
            task.deployment_dir = self.store.deployment_dir(review.app_name)
            self.store.save(task)
            return task
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.failure_phase = FailurePhase.UPLOAD
            task.error = str(exc)
            self.store.save(task)
            raise

    def deploy(self, task_id: str) -> DeploymentTask:
        with self._task_lock(task_id):
            task = self.store.get(task_id)
            if can_retry_task(task):
                task = self._begin_deploy_locked(task_id)
            elif task.status is not TaskStatus.DEPLOYING:
                raise DeploymentStateError(
                    f"task {task_id} cannot deploy from status {task.status.value}"
                )
            if not task.app_name or task.deployment_dir is None:
                raise DeploymentStateError(f"task {task_id} has no deployment target")
            with self._app_lock(task.app_name):
                try:
                    deployment_dir = task.deployment_dir
                    rules = effective_directory_rules(task)
                    validate_directory_targets(task.extracted_dir, rules)
                    validate_directory_targets(deployment_dir, rules)
                    overlay_directory(task.extracted_dir, deployment_dir)
                    apply_directory_rules(deployment_dir, rules)
                    image_tar = deployment_dir / "images.tar"
                    if image_tar.is_file():
                        load_result = self.runtime.load_image(image_tar, deployment_dir)
                        task.command_output += self._format_output("docker load", load_result)
                        self._require_success("docker load", load_result)
                    compose_result = self.runtime.compose_up(deployment_dir)
                    task.command_output += self._format_output("docker compose", compose_result)
                    self._require_success("docker compose", compose_result)
                    task.status = TaskStatus.DEPLOYED
                    task.failure_phase = None
                except Exception as exc:
                    task.status = TaskStatus.FAILED
                    task.failure_phase = FailurePhase.DEPLOY
                    task.error = str(exc)
                return self.store.save(task)

    def _begin_deploy_locked(self, task_id: str) -> DeploymentTask:
        task = self.store.get(task_id)
        if not can_retry_task(task):
            raise DeploymentStateError(
                f"task {task_id} cannot deploy from status {task.status.value}"
            )
        if not task.app_name or task.deployment_dir is None:
            raise DeploymentStateError(f"task {task_id} has no deployment target")
        task.status = TaskStatus.DEPLOYING
        task.command_output = ""
        task.error = None
        task.failure_phase = None
        return self.store.save(task)

    def begin_deploy(self, task_id: str) -> DeploymentTask:
        with self._task_lock(task_id):
            return self._begin_deploy_locked(task_id)

    def edit_configuration(
        self,
        task_id: str,
        env_text: str,
        compose_text: str,
        directory_rules: Sequence[DirectoryRule],
    ) -> DeploymentTask:
        with self._task_lock(task_id):
            task = self.store.get(task_id)
            if not can_edit_task(task):
                raise DeploymentStateError(
                    f"task {task_id} cannot be edited from status {task.status.value}"
                )
            if task.deployment_dir is None:
                raise DeploymentStateError(f"task {task_id} has no deployment target")
            try:
                env_bytes = env_text.encode("utf-8")
                compose_bytes = compose_text.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise DeploymentConfigurationError(
                    "configuration text must be valid UTF-8"
                ) from exc
            if len(env_bytes) > ENV_MAX_BYTES:
                raise DeploymentConfigurationTooLargeError(".env exceeds 1 MiB")
            if len(compose_bytes) > COMPOSE_MAX_BYTES:
                raise DeploymentConfigurationTooLargeError("compose.yaml exceeds 2 MiB")
            try:
                rules = normalize_directory_rules(directory_rules)
                validate_directory_targets(task.deployment_dir, rules)
                validate_directory_targets(task.extracted_dir, rules)
            except ConfigurationValidationError as exc:
                raise DeploymentConfigurationError(str(exc)) from exc

            env_path = task.extracted_dir / ".env"
            compose_path = task.extracted_dir / "compose.yaml"
            checksum_path = task.extracted_dir / "checksums.sha256"
            candidate_env = task.extracted_dir / ".env.candidate"
            candidate_compose = task.extracted_dir / ".compose.candidate.yaml"
            checksum_partial = task.extracted_dir / ".checksums.sha256.partial"
            snapshots = {
                env_path: env_path.read_bytes(),
                compose_path: compose_path.read_bytes(),
                checksum_path: checksum_path.read_bytes(),
            }
            try:
                candidate_env.write_bytes(env_bytes)
                candidate_compose.write_bytes(compose_bytes)
                try:
                    result = self.runtime.compose_config(
                        task.extracted_dir,
                        candidate_compose,
                        candidate_env,
                    )
                except DockerRuntimeError as exc:
                    raise DeploymentConfigurationError(
                        f"compose validation failed: {exc}"
                    ) from exc
                if result.returncode != 0:
                    detail = _as_text(getattr(result, "stderr", b""))
                    raise DeploymentConfigurationError(
                        f"invalid compose configuration: {detail or result.returncode}"
                    )
                candidate_env.replace(env_path)
                candidate_compose.replace(compose_path)
                write_checksums(task.extracted_dir)
                task.directory_rules = rules
                task.edited_at = datetime.now(timezone.utc)
                task.status = TaskStatus.PENDING_REVIEW
                task.error = None
                task.failure_phase = None
                return self.store.save(task)
            except Exception:
                for path, content in snapshots.items():
                    partial = path.with_name(f".{path.name}.restore")
                    partial.write_bytes(content)
                    partial.replace(path)
                raise
            finally:
                for path in (candidate_env, candidate_compose, checksum_partial):
                    if path.exists():
                        path.unlink()

    def discard(self, task_id: str) -> DeploymentTask:
        with self._task_lock(task_id):
            task = self.store.get(task_id)
            if task.status in {
                TaskStatus.EXTRACTING,
                TaskStatus.DEPLOYING,
                TaskStatus.DEPLOYED,
            }:
                raise DeploymentStateError(
                    f"task {task_id} cannot be discarded from status {task.status.value}"
                )
            discarded = task.model_copy(update={"status": TaskStatus.DISCARDED})
            self.store.delete(task_id)
            return discarded

    def _app_lock(self, app_name: str) -> Lock:
        with self._lock_guard:
            return self._app_locks.setdefault(app_name, Lock())

    def _task_lock(self, task_id: str) -> Lock:
        with self._lock_guard:
            return self._task_locks.setdefault(task_id, Lock())

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
