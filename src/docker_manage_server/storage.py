from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .models import DeploymentTask, TaskStatus


_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class TaskStore:
    def __init__(self, data_dir: Path):
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
        task = DeploymentTask(
            task_id=task_id,
            status=TaskStatus.UPLOADED,
            original_filename=original_filename,
            package_dir=package_dir,
            extracted_dir=package_dir / "extracted",
        )
        self.save(task)
        return task

    def save(self, task: DeploymentTask) -> DeploymentTask:
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
        return DeploymentTask.model_validate_json(path.read_text(encoding="utf-8"))

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
