from pathlib import Path
import stat

import pytest
from pydantic import ValidationError

from docker_manage_server.deployment_config import (
    ConfigurationValidationError,
    apply_directory_rules,
    can_delete_task,
    can_edit_task,
    can_retry_task,
    effective_directory_rules,
    normalize_directory_rules,
    validate_directory_targets,
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


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (TaskStatus.UPLOADED, False),
        (TaskStatus.EXTRACTING, False),
        (TaskStatus.DEPLOYING, False),
        (TaskStatus.PENDING_REVIEW, True),
        (TaskStatus.FAILED, True),
        (TaskStatus.DEPLOYED, True),
    ],
)
def test_delete_permission_depends_only_on_non_active_status(
    tmp_path: Path,
    status: TaskStatus,
    expected: bool,
):
    assert can_delete_task(make_task(tmp_path, status=status)) is expected


def test_applies_mode_to_new_and_existing_directories_without_recursing(tmp_path: Path):
    root = tmp_path / "deployments/demo"
    existing = root / "data/existing"
    child = existing / "keep.txt"
    existing.mkdir(parents=True)
    existing.chmod(0o755)
    child.write_text("keep", encoding="utf-8")
    child.chmod(0o600)

    apply_directory_rules(
        root,
        (
            DirectoryRule(path="data/new", mode="0770"),
            DirectoryRule(path="data/existing", mode="0700"),
        ),
    )

    assert stat.S_IMODE((root / "data/new").stat().st_mode) == 0o770
    assert stat.S_IMODE(existing.stat().st_mode) == 0o700
    assert stat.S_IMODE(child.stat().st_mode) == 0o600


def test_only_explicit_parent_rule_controls_parent_mode(tmp_path: Path):
    root = tmp_path / "deployments/demo"
    apply_directory_rules(
        root,
        (
            DirectoryRule(path="data/mysql", mode="0770"),
            DirectoryRule(path="data", mode="0750"),
        ),
    )
    assert stat.S_IMODE((root / "data/mysql").stat().st_mode) == 0o770
    assert stat.S_IMODE((root / "data").stat().st_mode) == 0o750


def test_rejects_symlink_component_and_file_conflict(tmp_path: Path):
    root = tmp_path / "deployments/demo"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "occupied").write_text("file", encoding="utf-8")

    with pytest.raises(ConfigurationValidationError, match="symbolic link"):
        validate_directory_targets(
            root, (DirectoryRule(path="link/data", mode="0770"),)
        )
    with pytest.raises(ConfigurationValidationError, match="not a directory"):
        validate_directory_targets(
            root, (DirectoryRule(path="occupied/data", mode="0770"),)
        )
