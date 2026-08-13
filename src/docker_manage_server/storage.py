from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
import os
import re
import shutil
import stat
from pathlib import Path

from .models import DeploymentTask, TaskStatus


_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class TaskStore:
    def __init__(
        self,
        data_dir: Path,
        clock: Callable[[], datetime] | None = None,
    ):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.data_dir = Path(data_dir)
        self.packages_dir = self.data_dir / "packages"
        self.tasks_dir = self.data_dir / "tasks"
        self.deployments_dir = self.data_dir / "deployments"
        self.packages_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.deployments_dir.mkdir(parents=True, exist_ok=True)

    def create(self, task_id: str, original_filename: str) -> DeploymentTask:
        self._validate_task_id(task_id)
        state_path = self._state_path(task_id)
        if state_path.exists():
            raise ValueError(f"task already exists: {task_id}")
        package_dir = self.packages_dir / task_id
        package_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        now = self._clock()
        task = DeploymentTask(
            task_id=task_id,
            status=TaskStatus.UPLOADED,
            original_filename=original_filename,
            package_dir=package_dir,
            extracted_dir=package_dir / "extracted",
            created_at=now,
            updated_at=now,
        )
        self._write(task)
        return task

    def save(self, task: DeploymentTask) -> DeploymentTask:
        now = self._clock()
        task.created_at = task.created_at or now
        task.updated_at = now
        return self._write(task)

    def _write(self, task: DeploymentTask) -> DeploymentTask:
        self._validate_task_id(task.task_id)
        destination = self._state_path(task.task_id)
        partial = destination.with_name(f".{destination.name}.partial")
        partial.write_text(task.model_dump_json(indent=2), encoding="utf-8")
        partial.replace(destination)
        return task

    def get(self, task_id: str) -> DeploymentTask:
        self._validate_task_id(task_id)
        path = self._state_path(task_id)
        if not path.is_file():
            raise KeyError(task_id)
        task = DeploymentTask.model_validate_json(path.read_text(encoding="utf-8"))
        if task.created_at is None or task.updated_at is None:
            fallback = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            task.created_at = task.created_at or fallback
            task.updated_at = task.updated_at or fallback
        return task

    def list(self) -> tuple[DeploymentTask, ...]:
        tasks = tuple(self.get(path.stem) for path in self.tasks_dir.glob("*.json"))
        minimum = datetime.min.replace(tzinfo=timezone.utc)
        return tuple(
            sorted(
                tasks,
                key=lambda task: (task.updated_at or minimum, task.task_id),
                reverse=True,
            )
        )

    def delete(self, task_id: str) -> None:
        self._validate_task_id(task_id)
        package_dir = self.packages_dir / task_id
        state_path = self._state_path(task_id)
        if package_dir.exists():
            resolved_package = package_dir.resolve()
            if resolved_package.parent != self.packages_dir.resolve():
                raise ValueError("refusing to delete outside packages directory")
            shutil.rmtree(resolved_package)
        if state_path.exists():
            state_path.unlink()

    def package_size_bytes(self, task_id: str) -> int:
        root = self.package_dir(task_id)
        try:
            root_mode = root.lstat().st_mode
        except FileNotFoundError:
            return 0
        if not stat.S_ISDIR(root_mode):
            raise OSError("task package path is not a directory")

        total = 0

        def handle_walk_error(exc: OSError) -> None:
            if isinstance(exc, FileNotFoundError):
                return
            raise exc

        for directory, dirnames, filenames in os.walk(
            root,
            topdown=True,
            onerror=handle_walk_error,
            followlinks=False,
        ):
            directory_path = Path(directory)
            safe_directories = []
            for name in dirnames:
                path = directory_path / name
                try:
                    mode = path.lstat().st_mode
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(mode):
                    safe_directories.append(name)
            dirnames[:] = safe_directories
            for name in filenames:
                path = directory_path / name
                try:
                    file_stat = path.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(file_stat.st_mode):
                    total += file_stat.st_size
        return total

    def package_dir(self, task_id: str) -> Path:
        self._validate_task_id(task_id)
        return self.packages_dir / task_id

    def extracted_dir(self, task_id: str) -> Path:
        return self.package_dir(task_id) / "extracted"

    def deployment_dir(self, app_name: str) -> Path:
        return self.deployments_dir / app_name

    def _state_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not _TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"unsafe task id: {task_id}")
