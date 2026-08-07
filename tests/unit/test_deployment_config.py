from pathlib import Path

import pytest
from pydantic import ValidationError

from docker_manage_server.deployment_config import (
    ConfigurationValidationError,
    can_edit_task,
    can_retry_task,
    effective_directory_rules,
    normalize_directory_rules,
)
from docker_manage_server.models import (
    DeploymentTask,
    DirectoryRule,
    FailurePhase,
    TaskStatus,
)


def make_task(tmp_path: Path, **updates) -> DeploymentTask:
    extracted = tmp_path / "packages/task/extracted"
    extracted.mkdir(parents=True, exist_ok=True)
    (extracted / ".env").write_text("A=1\n", encoding="utf-8")
    (extracted / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    values = {
        "task_id": "task",
        "status": TaskStatus.PENDING_REVIEW,
        "original_filename": "demo.tar.gz",
        "package_dir": extracted.parent,
        "extracted_dir": extracted,
        "deployment_dir": tmp_path / "deployments/demo",
        "app_name": "demo",
    }
    values.update(updates)
    return DeploymentTask(**values)


def test_normalizes_safe_relative_paths_and_preserves_order():
    rules = normalize_directory_rules(
        (
            DirectoryRule(path="./data/mysql", mode="0770"),
            DirectoryRule(path="logs/./nginx", mode="0755"),
        )
    )
    assert rules == (
        DirectoryRule(path="data/mysql", mode="0770"),
        DirectoryRule(path="logs/nginx", mode="0755"),
    )


@pytest.mark.parametrize(
    "path",
    ("", ".", "/data/mysql", "../outside", "data/../outside", "data\\mysql", "bad\x00name"),
)
def test_rejects_unsafe_directory_paths(path: str):
    with pytest.raises(ConfigurationValidationError):
        normalize_directory_rules((DirectoryRule(path=path, mode="0770"),))


def test_rejects_duplicate_normalized_paths():
    with pytest.raises(ConfigurationValidationError, match="duplicate"):
        normalize_directory_rules(
            (
                DirectoryRule(path="./data/mysql", mode="0770"),
                DirectoryRule(path="data/mysql", mode="0755"),
            )
        )


@pytest.mark.parametrize("mode", ("777", "0888", "1777", "-001", "rwxrwxrwx"))
def test_directory_rule_rejects_nonstandard_mode(mode: str):
    with pytest.raises(ValidationError):
        DirectoryRule(path="data", mode=mode)


def test_old_task_converts_only_relative_server_paths(tmp_path: Path):
    task = make_task(
        tmp_path,
        directory_rules=None,
        server_paths=("files/sqlite", "/srv/external"),
    )
    assert effective_directory_rules(task) == (
        DirectoryRule(path="files/sqlite", mode="0777"),
    )


def test_explicit_empty_rules_do_not_restore_legacy_paths(tmp_path: Path):
    task = make_task(
        tmp_path,
        directory_rules=(),
        server_paths=("files/sqlite",),
    )
    assert effective_directory_rules(task) == ()


def test_pending_and_deploy_failure_are_editable_and_retryable(tmp_path: Path):
    pending = make_task(tmp_path)
    failed = pending.model_copy(
        update={"status": TaskStatus.FAILED, "failure_phase": FailurePhase.DEPLOY}
    )
    for task in (pending, failed):
        assert can_edit_task(task) is True
        assert can_retry_task(task) is True


def test_upload_failure_is_not_editable_or_retryable(tmp_path: Path):
    task = make_task(
        tmp_path,
        status=TaskStatus.FAILED,
        failure_phase=FailurePhase.UPLOAD,
    )
    assert can_edit_task(task) is False
    assert can_retry_task(task) is False
