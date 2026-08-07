from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath

from .models import DeploymentTask, DirectoryRule, FailurePhase, TaskStatus


class ConfigurationValidationError(ValueError):
    pass


def normalize_directory_rules(
    rules: Sequence[DirectoryRule],
) -> tuple[DirectoryRule, ...]:
    normalized: list[DirectoryRule] = []
    seen: set[str] = set()
    for rule in rules:
        value = rule.path
        if not value or value == "." or "\\" in value or "\x00" in value:
            raise ConfigurationValidationError(f"unsafe directory path: {value!r}")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ConfigurationValidationError(f"unsafe directory path: {value!r}")
        rendered = path.as_posix()
        if rendered in {"", "."}:
            raise ConfigurationValidationError(f"unsafe directory path: {value!r}")
        if rendered in seen:
            raise ConfigurationValidationError(f"duplicate directory path: {rendered}")
        seen.add(rendered)
        normalized.append(DirectoryRule(path=rendered, mode=rule.mode))
    return tuple(normalized)


def effective_directory_rules(task: DeploymentTask) -> tuple[DirectoryRule, ...]:
    if task.directory_rules is not None:
        return task.directory_rules
    converted: list[DirectoryRule] = []
    for value in task.server_paths:
        path = PurePosixPath(value)
        if path.is_absolute():
            continue
        try:
            converted.extend(
                normalize_directory_rules((DirectoryRule(path=value, mode="0777"),))
            )
        except ConfigurationValidationError:
            continue
    return tuple(converted)


def is_recoverable_failure(task: DeploymentTask) -> bool:
    if task.status is not TaskStatus.FAILED:
        return False
    if task.failure_phase is FailurePhase.UPLOAD:
        return False
    if task.failure_phase not in {None, FailurePhase.DEPLOY}:
        return False
    return bool(
        task.app_name
        and task.deployment_dir is not None
        and task.extracted_dir.is_dir()
        and (task.extracted_dir / ".env").is_file()
        and (task.extracted_dir / "compose.yaml").is_file()
    )


def can_edit_task(task: DeploymentTask) -> bool:
    return task.status is TaskStatus.PENDING_REVIEW or is_recoverable_failure(task)


def can_retry_task(task: DeploymentTask) -> bool:
    return task.status is TaskStatus.PENDING_REVIEW or is_recoverable_failure(task)
