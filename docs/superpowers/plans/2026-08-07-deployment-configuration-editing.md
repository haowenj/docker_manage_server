# Deployment Configuration Editing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户在部署前或部署失败后安全编辑 `.env`、`compose.yaml` 和部署目录权限规则，并能够重新部署。

**Architecture:** 新建聚焦的 `deployment_config.py` 负责目录规则规范化、任务可编辑性判断、目标路径校验和目录权限应用；`DeploymentService` 负责在任务锁内校验并事务化保存候选配置，并扩展现有部署状态机支持失败重试。API 和服务端渲染页面只做输入适配与展示，Compose 语义校验统一通过 `DockerRuntime.compose_config()` 调用现有 Docker Compose CLI。

**Tech Stack:** Python 3.11、FastAPI、Pydantic、Jinja2、原生 JavaScript/CSS、pytest、Docker Compose CLI

## Global Constraints

- 只允许编辑成功解压工作区中的 `.env` 和 `compose.yaml`，不提供任意文件编辑器。
- 目录规则只允许稳定部署根目录内的安全 POSIX 相对路径；禁止绝对路径、反斜杠、NUL、`..`、符号链接组件和普通文件冲突。
- 权限只接受 `0000`–`0777`；只 `chmod` 明确规则对应的目录本身，不递归、不 `chown`、不调用 shell。
- 保存编辑配置时不修改稳定部署目录；目录创建和权限设置只发生在用户明确部署或重新部署时。
- 原始 `archive.tar.gz` 永远不修改；保存成功后重建解压工作区的 `checksums.sha256`。
- 已成功解压的部署失败任务可以编辑或原样重试；上传或解压失败任务不可编辑、不可重试。
- 部署失败不自动回滚，不删除稳定部署目录中的既有数据。
- 保留现有同源写请求边界、HTML 自动转义和“仅限受信任内网”的产品边界。
- 不新增第三方依赖，继续支持 Python `>=3.11`。

---

## File Map

- Create: `src/docker_manage_server/deployment_config.py` — 目录规则、可编辑状态、路径校验和权限应用的领域逻辑。
- Create: `src/docker_manage_server/templates/deployments/edit.html` — 独立部署配置编辑页。
- Create: `tests/unit/test_deployment_config.py` — 目录规则、路径边界、权限和状态判断单元测试。
- Modify: `src/docker_manage_server/models.py` — 新增 `DirectoryRule`、`FailurePhase` 和任务兼容字段。
- Modify: `src/docker_manage_server/artifacts.py` — 提供工作区校验和重建函数，移除固定 `0777` 的旧目录准备职责。
- Modify: `src/docker_manage_server/docker_runtime.py` — 新增候选 Compose 配置校验命令。
- Modify: `src/docker_manage_server/deployment.py` — 事务化编辑、任务锁、失败阶段和重新部署状态机。
- Modify: `src/docker_manage_server/api.py` — 配置更新 API、review 元数据和失败重试入口。
- Modify: `src/docker_manage_server/web.py` — 编辑页面、表单保存、详情页目录状态和失败重试入口。
- Modify: `src/docker_manage_server/web_views.py` — 编辑时间、可编辑性和目录展示模型。
- Modify: `src/docker_manage_server/templates/deployments/detail.html` — 编辑/重试按钮和目录规则预览。
- Modify: `src/docker_manage_server/static/js/app.js` — 目录规则增删、预设和 `r/w/x` 转换。
- Modify: `src/docker_manage_server/static/css/app.css` — 编辑表单和响应式目录规则布局。
- Modify: `tests/unit/test_artifacts.py`, `tests/unit/test_docker_runtime.py`, `tests/unit/test_deployment.py` — 文件系统、运行时和服务状态机测试。
- Modify: `tests/api/test_deployment_api.py`, `tests/web/test_deployments.py`, `tests/web/test_security.py`, `tests/web/conftest.py` — API、页面、同源安全和测试运行时覆盖。
- Modify: `README.md` — 记录编辑、目录权限和失败重试操作流程。

---

### Task 1: Directory Rule Model and Task Eligibility

**Files:**
- Create: `src/docker_manage_server/deployment_config.py`
- Create: `tests/unit/test_deployment_config.py`
- Modify: `src/docker_manage_server/models.py`
- Modify: `tests/unit/test_storage.py`

**Interfaces:**
- Produces: `DirectoryRule(path: str, mode: str)` and `FailurePhase.UPLOAD | FailurePhase.DEPLOY`.
- Produces: `normalize_directory_rules(rules: Sequence[DirectoryRule]) -> tuple[DirectoryRule, ...]`.
- Produces: `effective_directory_rules(task: DeploymentTask) -> tuple[DirectoryRule, ...]`.
- Produces: `is_recoverable_failure(task: DeploymentTask) -> bool`, `can_edit_task(task) -> bool`, and `can_retry_task(task) -> bool`.

- [ ] **Step 1: Write failing model, normalization, compatibility, and eligibility tests**

Create `tests/unit/test_deployment_config.py` with these cases:

```python
from pathlib import Path

import pytest

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
    extracted.mkdir(parents=True)
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
```

Append this storage compatibility test to `tests/unit/test_storage.py`:

```python
def test_old_task_json_defaults_configuration_editing_fields(tmp_path: Path):
    store = TaskStore(tmp_path)
    store.create("legacy-config", "legacy.tar.gz")
    state_path = tmp_path / "tasks/legacy-config.json"
    body = json.loads(state_path.read_text(encoding="utf-8"))
    body.pop("directory_rules", None)
    body.pop("failure_phase", None)
    body.pop("edited_at", None)
    state_path.write_text(json.dumps(body), encoding="utf-8")

    loaded = store.get("legacy-config")

    assert loaded.directory_rules is None
    assert loaded.failure_phase is None
    assert loaded.edited_at is None
```

Also add this invalid-mode assertion to `tests/unit/test_deployment_config.py`:

```python
from pydantic import ValidationError


@pytest.mark.parametrize("mode", ("777", "0888", "1777", "-001", "rwxrwxrwx"))
def test_directory_rule_rejects_nonstandard_mode(mode: str):
    with pytest.raises(ValidationError):
        DirectoryRule(path="data", mode=mode)
```

- [ ] **Step 2: Run tests to verify the new interfaces are missing**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_deployment_config.py tests/unit/test_storage.py
```

Expected: collection fails because `deployment_config`, `DirectoryRule`, and `FailurePhase` do not exist.

- [ ] **Step 3: Add the models and pure domain functions**

Add to `models.py`:

```python
class FailurePhase(str, Enum):
    UPLOAD = "upload"
    DEPLOY = "deploy"


class DirectoryRule(BaseModel):
    path: str
    mode: str = Field(pattern=r"^0[0-7]{3}$")
```

Add these fields to `DeploymentTask`:

```python
directory_rules: tuple[DirectoryRule, ...] | None = None
failure_phase: FailurePhase | None = None
edited_at: datetime | None = None
```

Create `deployment_config.py` with:

```python
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
```

- [ ] **Step 4: Run the focused tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_deployment_config.py tests/unit/test_storage.py
```

Expected: all focused tests pass.

- [ ] **Step 5: Commit the domain model**

```bash
git add src/docker_manage_server/models.py src/docker_manage_server/deployment_config.py tests/unit/test_deployment_config.py tests/unit/test_storage.py
git commit -m "feat: add deployment directory rule model"
```

---

### Task 2: Safe Directory Application and Workspace Checksums

**Files:**
- Modify: `src/docker_manage_server/deployment_config.py`
- Modify: `src/docker_manage_server/artifacts.py`
- Modify: `tests/unit/test_deployment_config.py`
- Modify: `tests/unit/test_artifacts.py`

**Interfaces:**
- Consumes: `DirectoryRule` and `normalize_directory_rules()` from Task 1.
- Produces: `validate_directory_targets(root: Path, rules: Sequence[DirectoryRule]) -> None`.
- Produces: `apply_directory_rules(root: Path, rules: Sequence[DirectoryRule]) -> None`.
- Produces: `write_checksums(root: Path) -> None`.

- [ ] **Step 1: Write failing path-state, chmod, non-recursive, parent-mode, and checksum tests**

Append to `tests/unit/test_deployment_config.py`:

```python
import os
import stat

from docker_manage_server.deployment_config import (
    apply_directory_rules,
    validate_directory_targets,
)


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
```

Replace the fixed-permission `prepare_server_directories` tests in `tests/unit/test_artifacts.py` with a checksum test:

```python
def test_write_checksums_matches_all_regular_workspace_files(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".env").write_text("A=2\n", encoding="utf-8")
    (root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (root / "checksums.sha256").write_text("stale\n", encoding="utf-8")

    artifacts.write_checksums(root)
    artifacts._verify_checksums(root)

    entries = (root / "checksums.sha256").read_text(encoding="utf-8")
    assert "  .env\n" in entries
    assert "  compose.yaml\n" in entries
    assert "checksums.sha256" not in entries
```

- [ ] **Step 2: Run tests and confirm old behavior is insufficient**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_deployment_config.py tests/unit/test_artifacts.py
```

Expected: failures show the new functions are absent and the old helper preserves an existing directory mode instead of applying an explicit rule.

- [ ] **Step 3: Implement target validation, two-phase directory application, and checksum writing**

Add to `deployment_config.py`:

```python
import stat
from pathlib import Path


def validate_directory_targets(
    root: Path,
    rules: Sequence[DirectoryRule],
) -> None:
    root = Path(root)
    for rule in normalize_directory_rules(rules):
        current = root
        for part in PurePosixPath(rule.path).parts:
            current = current / part
            if current.is_symlink():
                raise ConfigurationValidationError(
                    f"directory path contains symbolic link: {rule.path}"
                )
            if current.exists() and not current.is_dir():
                raise ConfigurationValidationError(
                    f"directory path component is not a directory: {rule.path}"
                )


def apply_directory_rules(
    root: Path,
    rules: Sequence[DirectoryRule],
) -> None:
    normalized = normalize_directory_rules(rules)
    validate_directory_targets(root, normalized)
    root.mkdir(parents=True, exist_ok=True)
    for rule in normalized:
        target = root.joinpath(*PurePosixPath(rule.path).parts)
        target.mkdir(parents=True, exist_ok=True)
    validate_directory_targets(root, normalized)
    for rule in sorted(
        normalized,
        key=lambda item: len(PurePosixPath(item.path).parts),
        reverse=True,
    ):
        target = root.joinpath(*PurePosixPath(rule.path).parts)
        mode = target.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ConfigurationValidationError(
                f"directory target changed during preparation: {rule.path}"
            )
        descriptor = os.open(
            target,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            os.fchmod(descriptor, int(rule.mode, 8))
        finally:
            os.close(descriptor)
```

Add to `artifacts.py` and call it from `_verify_checksums()` test setup as shown above:

```python
def write_checksums(root: Path) -> None:
    root = Path(root).resolve()
    lines = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name != "checksums.sha256":
            relative = path.relative_to(root).as_posix()
            lines.append(f"{_sha256(path)}  {relative}\n")
    destination = root / "checksums.sha256"
    partial = root / ".checksums.sha256.partial"
    partial.write_text("".join(lines), encoding="utf-8")
    partial.replace(destination)
```

Keep the existing `prepare_server_directories()` temporarily so `deployment.py` remains importable between commits. Task 5 switches the production caller and removes the old helper and its import in the same commit.

- [ ] **Step 4: Run focused filesystem tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_deployment_config.py tests/unit/test_artifacts.py
```

Expected: all focused tests pass.

- [ ] **Step 5: Commit the filesystem primitives**

```bash
git add src/docker_manage_server/deployment_config.py src/docker_manage_server/artifacts.py tests/unit/test_deployment_config.py tests/unit/test_artifacts.py
git commit -m "feat: apply safe deployment directory rules"
```

---

### Task 3: Candidate Compose Validation Runtime

**Files:**
- Modify: `src/docker_manage_server/docker_runtime.py`
- Modify: `tests/unit/test_docker_runtime.py`
- Modify: `tests/web/conftest.py`
- Modify: `tests/api/test_deployment_api.py`

**Interfaces:**
- Produces: `DockerRuntime.compose_config(project_dir: Path, compose_file: Path, env_file: Path) -> Any`.
- Test runtimes produce the same method and configurable `compose_config_returncode`/`stderr` behavior.

- [ ] **Step 1: Write a failing exact-argv test**

Add to `tests/unit/test_docker_runtime.py`:

```python
def test_compose_config_uses_candidate_files_project_directory_and_no_shell(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    compose_file = tmp_path / ".compose.candidate.yaml"
    env_file = tmp_path / ".env.candidate"
    result = DockerRuntime(
        client=SimpleNamespace(), command_runner=runner
    ).compose_config(tmp_path, compose_file, env_file)

    assert result.returncode == 0
    assert calls[0][0] == [
        "docker", "compose",
        "--project-directory", str(tmp_path),
        "--env-file", str(env_file),
        "-f", str(compose_file),
        "config", "--quiet",
    ]
    assert calls[0][1]["cwd"] == str(tmp_path)
    assert calls[0][1]["shell"] is False
```

- [ ] **Step 2: Run the runtime test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_docker_runtime.py::test_compose_config_uses_candidate_files_project_directory_and_no_shell
```

Expected: FAIL because `DockerRuntime` has no `compose_config` method.

- [ ] **Step 3: Implement the runtime method and update fakes**

Add to `DockerRuntime`:

```python
def compose_config(
    self,
    project_dir: Path,
    compose_file: Path,
    env_file: Path,
) -> Any:
    return self._run(
        [
            "docker", "compose",
            "--project-directory", str(project_dir),
            "--env-file", str(env_file),
            "-f", str(compose_file),
            "config", "--quiet",
        ],
        project_dir,
    )
```

Add `compose_config()` to `WebFakeRuntime` and `ApiFakeRuntime`; return `SimpleNamespace(returncode=0, stdout=b"", stderr=b"")`. Give `WebFakeRuntime` writable `compose_config_returncode` and `compose_config_stderr` attributes so Web error tests can force validation failure.

- [ ] **Step 4: Run runtime and fixture smoke tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_docker_runtime.py tests/api/test_deployment_api.py tests/web/test_deployments.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the Compose validator**

```bash
git add src/docker_manage_server/docker_runtime.py tests/unit/test_docker_runtime.py tests/web/conftest.py tests/api/test_deployment_api.py
git commit -m "feat: validate candidate compose configuration"
```

---

### Task 4: Transactional Configuration Editing Service

**Files:**
- Modify: `src/docker_manage_server/deployment.py`
- Modify: `tests/unit/test_deployment.py`

**Interfaces:**
- Consumes: Task 1 eligibility/rule functions, Task 2 target/checksum functions, and Task 3 `compose_config()`.
- Produces: `DeploymentService.edit_configuration(task_id: str, env_text: str, compose_text: str, directory_rules: Sequence[DirectoryRule]) -> DeploymentTask`.
- Produces: `DeploymentConfigurationError` and `DeploymentConfigurationTooLargeError`.
- Fixed limits: `.env` 1 MiB and Compose 2 MiB, measured as UTF-8 bytes.

- [ ] **Step 1: Write failing success, validation, size, rollback, and state tests**

Add these test imports, extend `FakeRuntime` with `compose_config_returncode`, `compose_config_stderr`, and `compose_config()`, then add the cases below:

```python
import pytest

import docker_manage_server.artifacts as artifacts
from docker_manage_server.deployment import (
    DeploymentConfigurationError,
    DeploymentConfigurationTooLargeError,
    DeploymentStateError,
)
from docker_manage_server.models import DirectoryRule, FailurePhase, TaskStatus
```

Extend the existing fake with:

```python
def __init__(
    self,
    *,
    compose_returncode: int = 0,
    compose_observer=None,
    compose_config_returncode: int = 0,
    compose_config_stderr: bytes = b"invalid compose",
):
    self.calls: list[str] = []
    self.compose_returncode = compose_returncode
    self.compose_observer = compose_observer
    self.compose_config_returncode = compose_config_returncode
    self.compose_config_stderr = compose_config_stderr

def compose_config(self, project_dir: Path, compose_file: Path, env_file: Path):
    self.calls.append("config")
    return SimpleNamespace(
        returncode=self.compose_config_returncode,
        stdout=b"",
        stderr=self.compose_config_stderr if self.compose_config_returncode else b"",
    )
```

Update assertions that previously expected `runtime.calls == ["load", "compose"]` to expect `config` only when `edit_configuration()` was called; upload and deploy alone do not invoke candidate validation.

```python
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
    artifacts._verify_checksums(task.extracted_dir)


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
        service.edit_configuration(
            "task-1", "BROKEN=1\n", "not compose", (),
        )

    after = service.store.get("task-1")
    assert (after.extracted_dir / ".env").read_bytes() == env_before
    assert (after.extracted_dir / "compose.yaml").read_bytes() == compose_before
    assert (after.extracted_dir / "checksums.sha256").read_bytes() == checksum_before
    assert after.directory_rules == before.directory_rules
    assert after.edited_at is None


def test_edit_configuration_rejects_oversized_env(tmp_path, valid_archive):
    service = make_service(tmp_path)
    with valid_archive.open("rb") as archive:
        service.upload("task-1", archive, "demo.tar.gz")
    with pytest.raises(DeploymentConfigurationTooLargeError):
        service.edit_configuration(
            "task-1", "A" * (1024 * 1024 + 1), "services: {}\n", (),
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
```

Add this rollback test:

```python
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
        service.edit_configuration(
            "task-1", "A=2\n", "services: {}\n", (),
        )

    assert {path: path.read_bytes() for path in paths} == before
    assert service.store.get("task-1").edited_at is None
```

- [ ] **Step 2: Run the service tests and verify failures**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_deployment.py
```

Expected: failures identify missing edit exceptions, missing task locks, and missing `edit_configuration()`.

- [ ] **Step 3: Implement task locks, candidate validation, atomic replacement, and rollback**

In `deployment.py`:

```python
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import write_checksums
from .deployment_config import (
    ConfigurationValidationError,
    can_edit_task,
    normalize_directory_rules,
    validate_directory_targets,
)
from .models import DirectoryRule, FailurePhase

ENV_MAX_BYTES = 1024 * 1024
COMPOSE_MAX_BYTES = 2 * 1024 * 1024


class DeploymentConfigurationError(RuntimeError):
    pass


class DeploymentConfigurationTooLargeError(DeploymentConfigurationError):
    pass
```

Add `_task_locks` beside `_app_locks`, add `_task_lock(task_id)` using `_lock_guard`, and implement `edit_configuration()` with this exact order:

```python
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
            assert task.deployment_dir is not None
            validate_directory_targets(task.deployment_dir, rules)
            validate_directory_targets(task.extracted_dir, rules)
        except ConfigurationValidationError as exc:
            raise DeploymentConfigurationError(str(exc)) from exc

        env_path = task.extracted_dir / ".env"
        compose_path = task.extracted_dir / "compose.yaml"
        checksum_path = task.extracted_dir / "checksums.sha256"
        candidate_env = task.extracted_dir / ".env.candidate"
        candidate_compose = task.extracted_dir / ".compose.candidate.yaml"
        snapshots = {
            env_path: env_path.read_bytes(),
            compose_path: compose_path.read_bytes(),
            checksum_path: checksum_path.read_bytes(),
        }
        try:
            candidate_env.write_text(env_text, encoding="utf-8")
            candidate_compose.write_text(compose_text, encoding="utf-8")
            result = self.runtime.compose_config(
                task.extracted_dir, candidate_compose, candidate_env
            )
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
            task.failure_phase = None
            task.error = None
            return self.store.save(task)
        except Exception:
            for path, content in snapshots.items():
                partial = path.with_name(f".{path.name}.restore")
                partial.write_bytes(content)
                partial.replace(path)
            raise
        finally:
            for path in (
                candidate_env,
                candidate_compose,
                task.extracted_dir / ".checksums.sha256.partial",
            ):
                if path.exists():
                    path.unlink()
```

`TaskStore.save()` already writes through a partial file followed by `replace()`, so a failed state write leaves the prior task JSON intact; do not call `save()` again during rollback because that would change `updated_at`. Convert `ConfigurationValidationError` raised before Compose execution into `DeploymentConfigurationError`. Preserve `DeploymentConfigurationTooLargeError` without wrapping so HTTP layers can map it to `413`.

- [ ] **Step 4: Run focused service and checksum tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_deployment.py tests/unit/test_artifacts.py tests/unit/test_deployment_config.py
```

Expected: all selected tests pass, including byte-for-byte rollback.

- [ ] **Step 5: Commit transactional editing**

```bash
git add src/docker_manage_server/deployment.py tests/unit/test_deployment.py
git commit -m "feat: save edited deployment configuration"
```

---

### Task 5: Upload Conversion, Deployment Retry, and Explicit Chmod

**Files:**
- Modify: `src/docker_manage_server/deployment.py`
- Modify: `tests/unit/test_deployment.py`

**Interfaces:**
- Consumes: `effective_directory_rules()`, `apply_directory_rules()`, `can_retry_task()`, and task locks.
- Changes: `DeploymentService.upload()` records `failure_phase` and initializes `directory_rules`.
- Produces: `DeploymentService.begin_deploy(task_id: str) -> DeploymentTask`, which synchronously persists `deploying` before a background task is scheduled.
- Changes: `DeploymentService.deploy()` accepts pending and recoverable failed tasks and applies directory rules before Docker commands.

- [ ] **Step 1: Write failing upload conversion, existing chmod, direct retry, and upload-failure tests**

Add these imports and cases to `tests/unit/test_deployment.py`:

```python
from io import BytesIO

from docker_manage_server.deployment_config import can_retry_task
```

```python
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
```

Update `FakeRuntime` so `compose_returncodes` is consumed per call; retain the single `compose_returncode` constructor form for older tests.

- [ ] **Step 2: Run deployment tests and verify state/retry failures**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_deployment.py
```

Expected: upload lacks initialized rules/failure phases, existing directory remains unchanged, and the second deploy is rejected from `failed`.

- [ ] **Step 3: Integrate rules and retry into upload/deploy**

In `upload()` after archive review succeeds:

```python
task.directory_rules = tuple(
    DirectoryRule(path=value, mode="0777")
    for value in review.server_paths
    if not PurePosixPath(value).is_absolute()
)
task.failure_phase = None
```

In the upload exception block set `task.failure_phase = FailurePhase.UPLOAD`.

Add the locked state helper and public queue transition:

```python
def _begin_deploy_locked(self, task_id: str) -> DeploymentTask:
    task = self.store.get(task_id)
    if not can_retry_task(task):
        raise DeploymentStateError(
            f"task {task_id} cannot deploy from status {task.status.value}"
        )
    task.status = TaskStatus.DEPLOYING
    task.command_output = ""
    task.error = None
    task.failure_phase = None
    return self.store.save(task)


def begin_deploy(self, task_id: str) -> DeploymentTask:
    with self._task_lock(task_id):
        return self._begin_deploy_locked(task_id)
```

This synchronous transition prevents edit/discard/duplicate-deploy requests from winning after a Web/API response has queued background work.

Start `deploy()` with this state/lock structure, then keep the existing command body inside the app lock:

```python
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
```

The directory-specific sequence in that method is:

```python
rules = effective_directory_rules(task)
validate_directory_targets(task.extracted_dir, rules)
validate_directory_targets(deployment_dir, rules)
overlay_directory(task.extracted_dir, deployment_dir)
apply_directory_rules(deployment_dir, rules)
```

In the deployment exception block set `task.failure_phase = FailurePhase.DEPLOY` before saving. On success keep `failure_phase=None`.

Remove `prepare_server_directories()` from `artifacts.py`, delete its import from `deployment.py`, and import `apply_directory_rules` from `deployment_config.py`. This keeps the removal and the caller migration in one passing commit.

Wrap `discard()` in the same task lock and reload the task inside the lock. A task already marked `deploying` remains non-discardable.

- [ ] **Step 4: Run deployment and artifact regressions**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_deployment.py tests/unit/test_artifacts.py tests/unit/test_deployment_config.py
```

Expected: all selected tests pass; runtime call order remains `load` then `compose`, and directory observation occurs before Compose.

- [ ] **Step 5: Commit retryable deployment behavior**

```bash
git add src/docker_manage_server/deployment.py tests/unit/test_deployment.py
git commit -m "feat: retry failed deployments with directory rules"
```

---

### Task 6: Configuration JSON API and Review Metadata

**Files:**
- Modify: `src/docker_manage_server/api.py`
- Modify: `tests/api/test_deployment_api.py`
- Modify: `tests/web/test_security.py`

**Interfaces:**
- Consumes: `DeploymentService.edit_configuration()`, `can_edit_task()`, `can_retry_task()`, and `effective_directory_rules()`.
- Produces: `PUT /api/deployment-tasks/{task_id}/configuration`.
- Changes: review JSON returns `directories`, `editable`, `retryable`, and `edited_at`.

- [ ] **Step 1: Write failing API success, validation, metadata, retry, and same-origin tests**

Add `SimpleNamespace`, `FailurePhase`, and `TaskStatus` imports to `tests/api/test_deployment_api.py`. Add this method to `ApiFakeRuntime` so TestClient can execute the background retry:

```python
def compose_up(self, cwd):
    return SimpleNamespace(returncode=0, stdout=b"started\n", stderr=b"")
```

Then add:

```python
def test_update_configuration_and_review_metadata(client, valid_archive):
    task_id = upload(client, valid_archive)["task_id"]
    response = client.put(
        f"/api/deployment-tasks/{task_id}/configuration",
        json={
            "env": "SECRET=changed\n",
            "compose": "services:\n  web:\n    image: changed:latest\n",
            "directories": [{"path": "data/mysql", "mode": "0770"}],
        },
    )
    assert response.status_code == 200
    review = client.get(f"/api/deployment-tasks/{task_id}/review").json()
    assert review["env"] == "SECRET=changed\n"
    assert review["directories"] == [
        {"path": "data/mysql", "mode": "0770", "exists": False}
    ]
    assert review["editable"] is True
    assert review["retryable"] is True
    assert review["edited_at"] is not None


def test_update_configuration_maps_errors(client, valid_archive):
    task_id = upload(client, valid_archive)["task_id"]
    unsafe = client.put(
        f"/api/deployment-tasks/{task_id}/configuration",
        json={"env": "A=1\n", "compose": "services: {}\n", "directories": [{"path": "../bad", "mode": "0770"}]},
    )
    assert unsafe.status_code == 422
    oversized = client.put(
        f"/api/deployment-tasks/{task_id}/configuration",
        json={"env": "A" * (1024 * 1024 + 1), "compose": "services: {}\n", "directories": []},
    )
    assert oversized.status_code == 413


def test_api_allows_retryable_failed_task_and_rejects_upload_failure(
    client, valid_archive
):
    task_id = upload(client, valid_archive)["task_id"]
    app = client.app
    task = app.state.store.get(task_id)
    task.status = TaskStatus.FAILED
    task.failure_phase = FailurePhase.DEPLOY
    app.state.store.save(task)
    retry = client.post(f"/api/deployment-tasks/{task_id}/deploy")
    assert retry.status_code == 202

    task.status = TaskStatus.FAILED
    task.failure_phase = FailurePhase.UPLOAD
    app.state.store.save(task)
    blocked = client.post(f"/api/deployment-tasks/{task_id}/deploy")
    assert blocked.status_code == 409
```

Add a cross-origin `PUT` assertion to `tests/web/test_security.py` using `Origin: https://evil.example`, expecting `403` before any update.

- [ ] **Step 2: Run API and security tests to verify routes are missing**

Run:

```bash
.venv/bin/python -m pytest -q tests/api/test_deployment_api.py tests/web/test_security.py
```

Expected: configuration `PUT` returns `405`, review metadata is absent, and failed deploy retry returns `409`.

- [ ] **Step 3: Add the payload model, response helper, update route, and retry gate**

Add in `api.py`:

```python
from pydantic import BaseModel

from .deployment_config import can_edit_task, can_retry_task, effective_directory_rules
from .deployment import (
    DeploymentConfigurationError,
    DeploymentConfigurationTooLargeError,
)
from .models import DirectoryRule


class DeploymentConfigurationPayload(BaseModel):
    env: str
    compose: str
    directories: tuple[DirectoryRule, ...] = ()
```

Add a helper that returns directory existence without following links:

```python
def _directory_payload(task: DeploymentTask) -> list[dict[str, Any]]:
    root = task.deployment_dir
    return [
        {
            "path": rule.path,
            "mode": rule.mode,
            "exists": bool(root and (root / rule.path).is_dir() and not (root / rule.path).is_symlink()),
        }
        for rule in effective_directory_rules(task)
    ]
```

Implement the update route:

```python
@app.put("/api/deployment-tasks/{task_id}/configuration")
def update_deployment_configuration(
    task_id: str,
    payload: DeploymentConfigurationPayload,
) -> dict[str, Any]:
    try:
        task = deployment.edit_configuration(
            task_id,
            payload.env,
            payload.compose,
            payload.directories,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="deployment task not found") from exc
    except DeploymentConfigurationTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except DeploymentStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DeploymentConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _task_payload(task)
```

Extend the review return object with:

```python
"directories": _directory_payload(task),
"editable": can_edit_task(task),
"retryable": can_retry_task(task),
"edited_at": task.edited_at.isoformat() if task.edited_at else None,
```

Change the deploy route to persist the queued state before registering background work:

```python
try:
    task = deployment.begin_deploy(task_id)
except KeyError as exc:
    raise HTTPException(status_code=404, detail="deployment task not found") from exc
except DeploymentStateError as exc:
    raise HTTPException(status_code=409, detail=str(exc)) from exc
background_tasks.add_task(deployment.deploy, task_id)
return _task_payload(task)
```

The background service accepts the already-persisted `deploying` state and performs the operation under the same task lock.

- [ ] **Step 4: Run API/security tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/api/test_deployment_api.py tests/web/test_security.py
```

Expected: all selected tests pass, including cross-origin rejection and `413` mapping.

- [ ] **Step 5: Commit API support**

```bash
git add src/docker_manage_server/api.py tests/api/test_deployment_api.py tests/web/test_security.py
git commit -m "feat: expose deployment configuration API"
```

---

### Task 7: Server-Rendered Edit Workflow and Detail Metadata

**Files:**
- Create: `src/docker_manage_server/templates/deployments/edit.html`
- Modify: `src/docker_manage_server/web.py`
- Modify: `src/docker_manage_server/web_views.py`
- Modify: `src/docker_manage_server/templates/deployments/detail.html`
- Modify: `tests/web/test_deployments.py`

**Interfaces:**
- Consumes: service editing and Task 6 eligibility/rule functions.
- Produces: `GET/POST /deployments/{task_id}/edit`.
- Produces: edit context keys `env_text`, `compose_text`, `directories`, and `edit_error`.

- [ ] **Step 1: Write failing page, save, validation-preservation, state, retry, and escaping tests**

Add to `tests/web/test_deployments.py`:

```python
import json

from docker_manage_server.models import FailurePhase


def upload_web(client, archive) -> str:
    response = client.post(
        "/deployments",
        files={"file": ("demo.tar.gz", archive.read_bytes(), "application/gzip")},
        follow_redirects=False,
    )
    return response.headers["location"].rsplit("/", 1)[1]


def test_edit_page_shows_config_and_manifest_directory_rule(web_context, valid_archive):
    client, _store, _runtime = web_context
    task_id = upload_web(client, valid_archive)
    detail = client.get(f"/deployments/{task_id}")
    assert "编辑配置" in detail.text
    assert "files/sqlite" in detail.text
    assert "0777" in detail.text

    edit = client.get(f"/deployments/{task_id}/edit")
    assert edit.status_code == 200
    assert "SECRET=value" in edit.text
    assert "data-directory-editor" in edit.text


def test_edit_form_saves_and_redirects_to_detail(web_context, valid_archive):
    client, store, _runtime = web_context
    task_id = upload_web(client, valid_archive)
    response = client.post(
        f"/deployments/{task_id}/edit",
        data={
            "env": "SECRET=changed\n",
            "compose": "services:\n  web:\n    image: changed:latest\n",
            "directories_json": json.dumps([{"path": "data/mysql", "mode": "0770"}]),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/deployments/{task_id}"
    task = store.get(task_id)
    assert task.directory_rules[0].path == "data/mysql"


def test_edit_form_error_keeps_submitted_text_escaped(web_context, valid_archive):
    client, _store, runtime = web_context
    task_id = upload_web(client, valid_archive)
    runtime.compose_config_returncode = 1
    runtime.compose_config_stderr = b"invalid compose"
    response = client.post(
        f"/deployments/{task_id}/edit",
        data={
            "env": "VALUE=<script>alert(1)</script>\n",
            "compose": "broken",
            "directories_json": "[]",
        },
    )
    assert response.status_code == 422
    assert "invalid compose" in response.text
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_deploy_failure_detail_allows_edit_and_retry(web_context, valid_archive):
    client, store, _runtime = web_context
    task_id = upload_web(client, valid_archive)
    task = store.get(task_id)
    task.status = TaskStatus.FAILED
    task.failure_phase = FailurePhase.DEPLOY
    task.error = "compose failed"
    store.save(task)
    detail = client.get(f"/deployments/{task_id}")
    assert "编辑并重试" in detail.text
    assert "重新部署" in detail.text
    retry = client.post(f"/deployments/{task_id}/deploy", follow_redirects=False)
    assert retry.status_code == 303


def test_upload_failure_has_no_edit_or_retry_actions(web_context, valid_archive):
    client, store, _runtime = web_context
    task_id = upload_web(client, valid_archive)
    task = store.get(task_id)
    task.status = TaskStatus.FAILED
    task.failure_phase = FailurePhase.UPLOAD
    store.save(task)
    detail = client.get(f"/deployments/{task_id}")
    assert "编辑并重试" not in detail.text
    assert "重新部署" not in detail.text
    assert client.get(f"/deployments/{task_id}/edit").status_code == 409
```

- [ ] **Step 2: Run Web tests and verify missing routes/UI**

Run:

```bash
.venv/bin/python -m pytest -q tests/web/test_deployments.py
```

Expected: edit GET/POST return `404`, detail lacks rule/actions, and failed retry returns `409`.

- [ ] **Step 3: Implement Web context, routes, template, and detail actions**

In `web_views.py`, extend `task_view()` with:

```python
"edited_at": _format_time(task.edited_at),
"editable": can_edit_task(task),
"retryable": can_retry_task(task),
```

In `web.py`, add `json`, `Form`, Pydantic `ValidationError`, `DirectoryRule`, and the configuration eligibility/service exception imports. Add these complete context helpers before `create_web_router()`:

```python
def _directory_views(task) -> list[dict[str, Any]]:
    root = task.deployment_dir
    rows = []
    for rule in effective_directory_rules(task):
        target = root / rule.path if root is not None else None
        rows.append(
            {
                "path": rule.path,
                "mode": rule.mode,
                "exists": bool(
                    target
                    and target.is_dir()
                    and not target.is_symlink()
                ),
            }
        )
    return rows


def _configuration_context(
    task,
    env_text: str | None = None,
    compose_text: str | None = None,
    directories: list[dict[str, Any]] | None = None,
    edit_error: str | None = None,
) -> dict[str, Any]:
    if directories is None:
        directories = [
            rule.model_dump(mode="json")
            for rule in effective_directory_rules(task)
        ]
    return {
        "page_title": f"编辑 {task.app_name or task.original_filename}",
        "active_nav": "deployments",
        "task_view": task_view(task),
        "env_text": (
            _read_optional(task.extracted_dir / ".env")
            if env_text is None else env_text
        ),
        "compose_text": (
            _read_optional(task.extracted_dir / "compose.yaml")
            if compose_text is None else compose_text
        ),
        "directories": directories,
        "edit_error": edit_error,
    }
```

Add the routes:

```python
@router.get("/deployments/{task_id}/edit", response_class=HTMLResponse)
def edit_deployment(request: Request, task_id: str):
    try:
        task = store.get(task_id)
    except KeyError:
        return _web_error(request, 404, "找不到部署任务", task_id)
    if not can_edit_task(task):
        return _web_error(request, 409, "任务当前状态不允许编辑", task.status.value)
    return templates.TemplateResponse(
        request=request,
        name="deployments/edit.html",
        context=_configuration_context(task),
    )


@router.post("/deployments/{task_id}/edit", response_class=HTMLResponse)
def save_deployment_edit(
    request: Request,
    task_id: str,
    env: str = Form(...),
    compose: str = Form(...),
    directories_json: str = Form("[]"),
):
    try:
        task = store.get(task_id)
        raw = json.loads(directories_json)
        directories = tuple(DirectoryRule.model_validate(item) for item in raw)
        deployment.edit_configuration(task_id, env, compose, directories)
    except KeyError:
        return _web_error(request, 404, "找不到部署任务", task_id)
    except DeploymentStateError as exc:
        return _web_error(request, 409, "任务当前状态不允许编辑", str(exc))
    except DeploymentConfigurationTooLargeError as exc:
        return templates.TemplateResponse(
            request=request,
            name="deployments/edit.html",
            context=_configuration_context(task, env, compose, raw, str(exc)),
            status_code=413,
        )
    except (json.JSONDecodeError, ValidationError, DeploymentConfigurationError) as exc:
        return templates.TemplateResponse(
            request=request,
            name="deployments/edit.html",
            context=_configuration_context(task, env, compose, raw if "raw" in locals() else [], str(exc)),
            status_code=422,
        )
    return RedirectResponse(f"/deployments/{task_id}", status_code=303)
```

Create `edit.html` with this initial server-rendered structure; Task 8 fills in advanced permission controls and JavaScript:

```html
{% extends "base.html" %}
{% block content %}
{% set task = task_view.task %}
{% if edit_error %}<div class="alert alert-danger">{{ edit_error }}</div>{% endif %}
<form method="post" action="/deployments/{{ task.task_id }}/edit" data-directory-editor>
  <section class="panel editor-grid">
    <label class="field">
      <span>.env</span>
      <textarea class="code-editor" name="env" spellcheck="false">{{ env_text }}</textarea>
    </label>
    <label class="field">
      <span>compose.yaml</span>
      <textarea class="code-editor" name="compose" spellcheck="false">{{ compose_text }}</textarea>
    </label>
  </section>
  <section class="panel">
    <div class="panel-heading">
      <div><h2>部署目录准备</h2><p class="muted">路径相对于部署目录；权限只作用于目录本身，不递归修改内部内容。</p></div>
      <button class="button button-secondary" type="button" data-directory-add>添加目录</button>
    </div>
    <input type="hidden" name="directories_json" data-directory-json>
    <script type="application/json" data-directory-initial>{{ directories|tojson }}</script>
    <div class="directory-rule-list" data-directory-list></div>
    <template data-directory-row-template>
      <div class="directory-rule">
        <label class="field"><span>相对路径</span><input type="text" data-directory-path></label>
        <label class="field"><span>权限</span><select data-directory-mode>
          <option value="0700">0700</option><option value="0750">0750</option>
          <option value="0755">0755</option><option value="0770">0770</option>
          <option value="0775">0775</option><option value="0777">0777</option>
        </select></label>
        <button class="button button-danger" type="button" data-directory-remove>删除</button>
      </div>
    </template>
  </section>
  <div class="button-row">
    <a class="button button-secondary" href="/deployments/{{ task.task_id }}">取消</a>
    <button class="button button-primary" type="submit">保存并校验</button>
  </div>
</form>
{% endblock %}
```

Add `"directories": _directory_views(task)` to the detail-page context. Update `detail.html` to:

- use `task_view.editable` and `task_view.retryable` for actions;
- label failed edit as “编辑并重试” and deploy as “重新部署”;
- render a “部署目录准备” table with each `path`, `mode`, and target existence label;
- show `task_view.edited_at` only when `task.edited_at` is not null.

Use this action block and directory panel in `detail.html`:

```html
{% if task_view.editable or task_view.retryable %}
<div class="button-row">
  {% if task_view.editable %}
  <a class="button button-secondary" href="/deployments/{{ task.task_id }}/edit">
    {{ "编辑并重试" if task_view.status_value == "failed" else "编辑配置" }}
  </a>
  {% endif %}
  {% if task_view.retryable %}
  <form method="post" action="/deployments/{{ task.task_id }}/deploy" data-confirm="确认部署此配置？">
    <button class="button button-primary" type="submit">
      {{ "重新部署" if task_view.status_value == "failed" else "确认部署" }}
    </button>
  </form>
  {% endif %}
  <form method="post" action="/deployments/{{ task.task_id }}/discard" data-confirm="确认丢弃此任务？该操作不可恢复。">
    <button class="button button-danger" type="submit">丢弃任务</button>
  </form>
</div>
{% endif %}

<section class="panel">
  <div class="panel-heading"><h2>部署目录准备</h2><span class="muted">{{ directories|length }} 项</span></div>
  <div class="table-scroll"><table>
    <thead><tr><th>相对路径</th><th>权限</th><th>当前状态</th></tr></thead>
    <tbody>
    {% for directory in directories %}
      <tr><td>{{ directory.path }}</td><td>{{ directory.mode }}</td><td>{{ "已存在" if directory.exists else "尚未创建" }}</td></tr>
    {% else %}
      <tr><td class="empty-cell" colspan="3">没有配置部署目录。</td></tr>
    {% endfor %}
    </tbody>
  </table></div>
</section>
```

Add `<dt>最近编辑</dt><dd>{{ task_view.edited_at }}</dd>` inside the definition grid only when `task.edited_at` is set.

Replace the Web deploy preflight and queue call with:

```python
try:
    deployment.begin_deploy(task_id)
except KeyError:
    return _web_error(request, 404, "找不到部署任务", task_id)
except DeploymentStateError as exc:
    return _web_error(request, 409, "任务当前状态不允许部署", str(exc))
background_tasks.add_task(deployment.deploy, task_id)
return RedirectResponse(f"/deployments/{task_id}", status_code=303)
```

- [ ] **Step 4: Run Web and package resource tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/web/test_deployments.py tests/web/test_package_resources.py
```

Expected: all selected tests pass and `edit.html` is available through installed package resources because `templates/deployments/*.html` is already included.

- [ ] **Step 5: Commit the server-rendered workflow**

```bash
git add src/docker_manage_server/web.py src/docker_manage_server/web_views.py src/docker_manage_server/templates/deployments/detail.html src/docker_manage_server/templates/deployments/edit.html tests/web/test_deployments.py
git commit -m "feat: add deployment configuration editor page"
```

---

### Task 8: Permission Editor Behavior, Styling, Documentation, and Full Verification

**Files:**
- Modify: `src/docker_manage_server/static/js/app.js`
- Modify: `src/docker_manage_server/static/css/app.css`
- Modify: `tests/web/test_deployments.py`
- Modify: `tests/web/test_package_resources.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `edit.html` data attributes and JSON fields from Task 7.
- Produces: preset/select and owner/group/other `r/w/x` controls synchronized to `directories_json` on submit.

- [ ] **Step 1: Add failing markup/resource assertions for the complete permission editor**

Extend the edit-page test to assert these stable hooks are present:

```python
assert 'data-directory-add' in edit.text
assert 'data-directory-mode' in edit.text
assert 'data-permission-bit="0400"' in edit.text
assert 'data-permission-bit="0001"' in edit.text
assert "0700" in edit.text
assert "0750" in edit.text
assert "0755" in edit.text
assert "0770" in edit.text
assert "0775" in edit.text
assert "0777" in edit.text
assert "不递归修改" in edit.text
```

Extend `tests/web/test_package_resources.py`:

```python
def test_deployment_editor_assets_are_packaged():
    package = files("docker_manage_server")
    assert package.joinpath("templates/deployments/edit.html").is_file()
    script = package.joinpath("static/js/app.js").read_text(encoding="utf-8")
    assert "data-directory-editor" in script
```

- [ ] **Step 2: Run focused tests and verify missing hooks**

Run:

```bash
.venv/bin/python -m pytest -q tests/web/test_deployments.py tests/web/test_package_resources.py
```

Expected: failures identify the permission hook/preset markup and JavaScript behavior not yet present.

- [ ] **Step 3: Implement editor JavaScript, responsive CSS, and README flow**

In `app.js`, initialize every `[data-directory-editor]` by:

1. parsing `[data-directory-initial]` JSON;
2. cloning `[data-directory-row-template]` for each rule;
3. converting a four-digit mode into the nine checkboxes using each checkbox's octal `data-permission-bit`;
4. recomputing the four-digit mode with `modeValue = bitwiseOr.toString(8).padStart(4, "0")` after any checkbox change;
5. applying selected preset values to checkboxes and the visible mode;
6. marking `0777` rows with a visible warning;
7. removing rows only after their remove button is clicked;
8. serializing non-empty `{path, mode}` rows into the hidden `directories_json` immediately before form submission.

Use this implementation:

```javascript
document.querySelectorAll("[data-directory-editor]").forEach((editor) => {
  const list = editor.querySelector("[data-directory-list]");
  const template = editor.querySelector("[data-directory-row-template]");
  const initialNode = editor.querySelector("[data-directory-initial]");
  const serialized = editor.querySelector("[data-directory-json]");
  const addButton = editor.querySelector("[data-directory-add]");

  const readMode = (row) => {
    let value = 0;
    row.querySelectorAll("[data-permission-bit]").forEach((checkbox) => {
      if (checkbox.checked) value |= Number.parseInt(checkbox.dataset.permissionBit, 8);
    });
    return value.toString(8).padStart(4, "0");
  };

  const renderMode = (row, mode) => {
    const numeric = Number.parseInt(mode, 8);
    row.querySelectorAll("[data-permission-bit]").forEach((checkbox) => {
      const bit = Number.parseInt(checkbox.dataset.permissionBit, 8);
      checkbox.checked = (numeric & bit) === bit;
    });
    const preset = row.querySelector("[data-directory-mode]");
    preset.value = Array.from(preset.options).some((option) => option.value === mode)
      ? mode
      : "";
    row.querySelector("[data-directory-mode-value]").textContent = mode;
    row.querySelector("[data-permission-warning]").hidden = mode !== "0777";
  };

  const addRule = (rule = { path: "", mode: "0755" }) => {
    const fragment = template.content.cloneNode(true);
    const row = fragment.querySelector("[data-directory-rule]");
    row.querySelector("[data-directory-path]").value = rule.path || "";
    renderMode(row, rule.mode || "0755");
    row.querySelector("[data-directory-mode]").addEventListener("change", (event) => {
      if (event.target.value) renderMode(row, event.target.value);
    });
    row.querySelectorAll("[data-permission-bit]").forEach((checkbox) => {
      checkbox.addEventListener("change", () => renderMode(row, readMode(row)));
    });
    row.querySelector("[data-directory-remove]").addEventListener("click", () => row.remove());
    list.appendChild(fragment);
  };

  let initial = [];
  try {
    initial = JSON.parse(initialNode.textContent || "[]");
  } catch (_error) {
    initial = [];
  }
  if (initial.length) initial.forEach(addRule);
  else addRule();

  addButton.addEventListener("click", () => addRule());
  editor.addEventListener("submit", () => {
    const rules = Array.from(list.querySelectorAll("[data-directory-rule]"))
      .map((row) => ({
        path: row.querySelector("[data-directory-path]").value.trim(),
        mode: readMode(row),
      }))
      .filter((rule) => rule.path);
    serialized.value = JSON.stringify(rules);
  });
});
```

Use these exact preset descriptions in `edit.html`:

```text
0700 仅所有者可访问
0750 所有者可写，用户组可读和进入
0755 所有人可读和进入，仅所有者可写
0770 所有者和用户组可写
0775 所有者和用户组可写，其他用户可读和进入
0777 所有人可写（仅在确有需要时使用）
```

Replace the Task 7 directory row template with this complete markup:

```html
<template data-directory-row-template>
  <article class="directory-rule" data-directory-rule>
    <label class="field">
      <span>相对路径</span>
      <input type="text" placeholder="例如 data/mysql" data-directory-path>
    </label>
    <label class="field">
      <span>常用权限</span>
      <select data-directory-mode>
        <option value="">自定义</option>
        <option value="0700">0700 仅所有者可访问</option>
        <option value="0750">0750 所有者可写，用户组可读和进入</option>
        <option value="0755">0755 所有人可读和进入，仅所有者可写</option>
        <option value="0770">0770 所有者和用户组可写</option>
        <option value="0775">0775 所有者和用户组可写，其他用户可读和进入</option>
        <option value="0777">0777 所有人可写（仅在确有需要时使用）</option>
      </select>
    </label>
    <div class="field">
      <span>当前权限</span>
      <output class="permission-mode" data-directory-mode-value>0755</output>
    </div>
    <details class="permission-details">
      <summary>精细设置 r/w/x</summary>
      <div class="permission-grid">
        <strong></strong><strong>读 r</strong><strong>写 w</strong><strong>进入 x</strong>
        <span>所有者</span>
        <label><input type="checkbox" data-permission-bit="0400">r</label>
        <label><input type="checkbox" data-permission-bit="0200">w</label>
        <label><input type="checkbox" data-permission-bit="0100">x</label>
        <span>用户组</span>
        <label><input type="checkbox" data-permission-bit="0040">r</label>
        <label><input type="checkbox" data-permission-bit="0020">w</label>
        <label><input type="checkbox" data-permission-bit="0010">x</label>
        <span>其他用户</span>
        <label><input type="checkbox" data-permission-bit="0004">r</label>
        <label><input type="checkbox" data-permission-bit="0002">w</label>
        <label><input type="checkbox" data-permission-bit="0001">x</label>
      </div>
    </details>
    <p class="permission-warning" data-permission-warning hidden>0777 允许所有用户写入，请确认这是应用真正需要的权限。</p>
    <button class="button button-danger" type="button" data-directory-remove>删除</button>
  </article>
</template>
```

Add this CSS before the existing media query:

```css
.editor-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; }
.field { display: grid; align-content: start; gap: 7px; min-width: 0; }
.field input[type="text"], .code-editor { width: 100%; color: var(--text); background: #fff; border: 1px solid #cbd5e1; border-radius: 8px; }
.field input[type="text"] { min-height: 38px; padding: 7px 10px; font: inherit; }
.code-editor { min-height: 360px; padding: 14px; resize: vertical; font: 13px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.directory-rule-list { display: grid; gap: 14px; }
.directory-rule { display: grid; grid-template-columns: minmax(220px, 1.3fr) minmax(240px, 1fr) auto auto; align-items: end; gap: 12px; padding: 16px; border: 1px solid var(--border); border-radius: 10px; }
.permission-mode { min-height: 38px; padding: 9px 12px; color: #1e293b; background: #f8fafc; border: 1px solid var(--border); border-radius: 8px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.permission-details { grid-column: 1 / -1; }
.permission-details summary { cursor: pointer; color: var(--primary); font-weight: 650; }
.permission-grid { display: grid; grid-template-columns: minmax(90px, 1fr) repeat(3, minmax(64px, .6fr)); gap: 8px; margin-top: 12px; align-items: center; }
.permission-grid label { display: flex; align-items: center; gap: 6px; }
.permission-warning { grid-column: 1 / -1; margin: 0; color: var(--warning); }
```

Add inside the existing `@media (max-width: 760px)` block:

```css
.editor-grid, .directory-rule { grid-template-columns: 1fr; }
.directory-rule .button, form[data-directory-editor] > .button-row .button { width: 100%; }
.permission-grid { grid-template-columns: minmax(80px, 1fr) repeat(3, minmax(48px, .6fr)); }
```

Replace README deployment-flow steps 3–5 with:

```markdown
3. 页面审核文件树、完整 `.env` 和 `compose.yaml`；需要时进入“编辑配置”。
4. 编辑页可以修改 `.env`、`compose.yaml`，并添加部署根目录内的“相对目录 + `0000`–`0777` 权限”规则。保存时先运行 Compose 配置校验，不会提前修改正式部署目录。
5. 用户确认部署后，服务端把内容合并到稳定目录，创建规则中缺失的目录，并对明确配置的目录本身执行 `chmod`；不会递归修改内部文件或子目录。
6. 如果存在 `images.tar`，先执行 `docker load`，再从稳定目录执行 `docker compose up -d`。
7. Compose 部署失败的任务可以编辑后重新部署，也可以原样重试；上传或解压失败的任务不能重试。
```

- [ ] **Step 4: Run the entire test and configuration suite**

Run:

```bash
.venv/bin/python -m pytest -q
DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server DOCKER_MANAGE_SERVER_PORT=6308 docker compose config
git diff --check
```

Expected:

- pytest reports all tests passing, with real-Docker tests skipped only when the daemon is unavailable;
- Compose output publishes `6308:8000` and mounts `/data/docker-manage-server` at the identical container path;
- `git diff --check` prints no errors.

- [ ] **Step 5: Build distributions and verify packaged editor resources**

Run:

```bash
uv build --wheel -o dist
.venv/bin/python -c "from importlib.resources import files; import docker_manage_server as p; assert files(p).joinpath('templates/deployments/edit.html').is_file(); assert files(p).joinpath('static/js/app.js').is_file()"
```

Expected: wheel build succeeds and the Python assertion exits with status `0`.

- [ ] **Step 6: Commit the completed user experience**

```bash
git add src/docker_manage_server/static/js/app.js src/docker_manage_server/static/css/app.css tests/web/test_deployments.py tests/web/test_package_resources.py README.md
git commit -m "feat: complete deployment permission editor"
```

---

## Final Review Checklist

- [ ] Every edited configuration path is guarded by task state and a task-level lock.
- [ ] Candidate Compose validation runs before replacing `.env` or `compose.yaml`.
- [ ] A failed save restores `.env`, Compose, checksum, and task JSON.
- [ ] Directory rules cannot escape through absolute paths, `..`, file conflicts, or symlinks.
- [ ] Existing target directories receive explicit `chmod`; child contents remain unchanged.
- [ ] Directory rules run before image load and Compose startup.
- [ ] Deployment-stage failures are editable/retryable; upload-stage failures are not.
- [ ] Original archives remain unchanged and edited workspaces have valid checksums.
- [ ] Web and JSON inputs are escaped/validated server-side and cross-origin writes remain blocked.
- [ ] Full pytest, Compose config, package resource, and whitespace checks pass.
