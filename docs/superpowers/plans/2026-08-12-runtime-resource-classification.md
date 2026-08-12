# Runtime Resource Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将运行概览和运行管理按 Compose 项目与独立容器分类展示，并提供 Compose 项目详情、项目范围内容器详情弹框、日志和终端入口。

**Architecture:** `DockerRuntime` 负责执行固定的 `docker compose ls --all --format json` 命令并解析原始项目记录；新增 `RuntimeInventoryService` 一次读取 Compose 项目和 Docker 容器，根据 `com.docker.compose.*` 标签完成合并、分类、排序和归属验证。FastAPI 页面和 WebSocket 路由只消费该聚合服务，Compose 页面使用独立路由和模板，独立容器继续使用 `/containers/{container_id}` 页面族。

**Tech Stack:** Python 3.11、FastAPI、Docker SDK for Python 7.x、Docker Compose CLI、Pydantic/dataclasses、Jinja2、原生 HTML `<dialog>`、原生 JavaScript、pytest。

## Global Constraints

- 本轮只实现只读分类、详情、日志和终端，不实现启动、停止、重启或删除。
- Compose 项目通过 `docker compose ls --all --format json` 枚举，必须包含全部停止项目。
- Compose 项目是未来生命周期操作主体，项目内容器不得出现单容器生命周期操作入口。
- Compose 内容器不得进入独立容器列表；即使 Compose CLI 失败，也必须继续根据标签隔离。
- 首页依次展示最近部署任务、Compose 项目和独立容器，每个模块最多 5 项。
- 部署任务按更新时间倒序；Compose 项目按运行中优先、项目名排序；独立容器按运行中优先、创建时间倒序。
- `/runtime` 展示完整 Compose 项目和独立容器列表；旧 `/containers` 列表地址重定向到 `/runtime`。
- Compose 内容器详情使用只读弹框；日志和终端使用 Compose 专属新标签页。
- Compose 内容器日志和终端请求必须在服务端验证容器确实属于 URL 中的项目。
- Docker daemon 不可用时首页保留部署任务，`/runtime` 返回 503；Compose CLI 单独失败只局部降级。
- 不新增前端框架、外部 CDN、运行状态缓存或持久化历史。
- 保留现有同源检查、CSP、安全响应头、宿主机端口和数据目录行为。
- Git 操作只使用本地 `git`；本计划执行阶段不安装或调用 `gh` 或其他平台 CLI。

---

## File Structure

### Create

- `src/docker_manage_server/runtime_inventory.py` — 运行资源只读模型、Compose/容器聚合、分类、排序和归属验证的唯一边界。
- `src/docker_manage_server/templates/runtime/list.html` — “运行管理”完整列表页。
- `src/docker_manage_server/templates/compose_projects/detail.html` — Compose 项目详情与内容器只读弹框。
- `src/docker_manage_server/templates/compose_projects/logs.html` — 带项目上下文的容器日志页。
- `src/docker_manage_server/templates/compose_projects/terminal.html` — 带项目上下文的容器终端页。
- `tests/unit/test_runtime_inventory.py` — 聚合、排序、降级和归属验证单元测试。
- `tests/web/test_runtime.py` — 运行管理列表和兼容重定向测试。
- `tests/web/test_compose_projects.py` — Compose 详情、弹框、日志和终端页面测试。

### Modify

- `src/docker_manage_server/docker_runtime.py` — 新增 Compose 列表记录、错误类型和固定命令执行/JSON 解析。
- `src/docker_manage_server/api.py` — 构造共享 inventory service，增加 Compose 日志与终端端点，抽取共享终端转发函数。
- `src/docker_manage_server/web.py` — 首页聚合、运行管理、Compose 详情与 Compose 工具页路由。
- `src/docker_manage_server/web_views.py` — Compose 项目视图、容器服务名、运行指标和排序后数据的展示转换。
- `src/docker_manage_server/templates/base.html` — 导航改为“运行管理”并链接 `/runtime`。
- `src/docker_manage_server/templates/dashboard.html` — 首页三模块布局和局部错误提示。
- `src/docker_manage_server/templates/containers/detail.html` — 保持独立容器页面语义和返回入口。
- `src/docker_manage_server/templates/containers/logs.html` — 保持独立容器上下文。
- `src/docker_manage_server/templates/containers/terminal.html` — 保持独立容器上下文。
- `src/docker_manage_server/static/js/app.js` — 原生 dialog 打开/关闭交互。
- `src/docker_manage_server/static/css/app.css` — 三模块布局、Compose 详情和响应式 dialog 样式。
- `src/docker_manage_server/static/js/terminal.js` — 无需分叉，仅继续读取页面提供的 `data-terminal-url`。
- `pyproject.toml` — 打包新增 `runtime` 和 `compose_projects` 模板目录。
- `tests/web/conftest.py` — 扩展 fake runtime 的 Compose 项目数据和容器对象映射。
- `tests/web/test_dashboard.py` — 首页三模块、5 项限制和降级行为。
- `tests/web/test_containers.py` — 旧列表重定向、独立容器页面回归和 Compose 容器隔离。
- `tests/api/test_container_api.py` — Compose 专属日志和终端归属验证。
- `tests/web/test_package_resources.py` — 新模板包资源验证。
- `README.md` — 更新页面入口、分类语义和 Compose 工具页说明。

---

### Task 1: Compose Project Enumeration in DockerRuntime

**Files:**
- Modify: `src/docker_manage_server/docker_runtime.py:1-175`
- Modify: `tests/unit/test_docker_runtime.py:1-77`

**Interfaces:**
- Consumes: existing `DockerRuntime._run(argv: list[str], cwd: Path) -> Any` behavior.
- Produces: `ComposeProjectRecord(name: str, status: str, config_files: tuple[str, ...])`.
- Produces: `ComposeListError(DockerRuntimeError)`.
- Produces: `DockerRuntime.list_compose_projects() -> tuple[ComposeProjectRecord, ...]`.

- [ ] **Step 1: Write failing fixed-command and JSON parsing tests**

Append tests that prove the command is fixed, does not use a shell, accepts empty output, and normalizes comma-separated config paths:

```python
import json

from docker_manage_server.docker_runtime import ComposeProjectRecord


def test_list_compose_projects_uses_all_json_and_no_shell(tmp_path):
    calls = []
    payload = [
        {
            "Name": "mall-stack",
            "Status": "running(3)",
            "ConfigFiles": "/srv/mall/compose.yaml,/srv/mall/compose.prod.yaml",
        }
    ]

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload).encode(),
            stderr=b"",
        )

    runtime = DockerRuntime(client=SimpleNamespace(), command_runner=runner)
    assert runtime.list_compose_projects() == (
        ComposeProjectRecord(
            name="mall-stack",
            status="running(3)",
            config_files=(
                "/srv/mall/compose.yaml",
                "/srv/mall/compose.prod.yaml",
            ),
        ),
    )
    assert calls[0][0] == ["docker", "compose", "ls", "--all", "--format", "json"]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["timeout"] == 1800


def test_list_compose_projects_accepts_empty_json_array():
    runtime = DockerRuntime(
        client=SimpleNamespace(),
        command_runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=b"[]\n", stderr=b""
        ),
    )
    assert runtime.list_compose_projects() == ()
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_docker_runtime.py::test_list_compose_projects_uses_all_json_and_no_shell \
  tests/unit/test_docker_runtime.py::test_list_compose_projects_accepts_empty_json_array
```

Expected: collection fails because `ComposeProjectRecord` and `list_compose_projects` do not exist.

- [ ] **Step 3: Implement the record and successful parser**

Add `json` and `PurePath`-free string parsing; config paths are display-only:

```python
import json


class ComposeListError(DockerRuntimeError):
    pass


@dataclass(frozen=True)
class ComposeProjectRecord:
    name: str
    status: str
    config_files: tuple[str, ...]


def _config_files(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
```

Add this method to `DockerRuntime`:

```python
def list_compose_projects(self) -> tuple[ComposeProjectRecord, ...]:
    try:
        result = self._run(
            ["docker", "compose", "ls", "--all", "--format", "json"],
            Path.cwd(),
        )
    except DockerRuntimeError as exc:
        raise ComposeListError(str(exc)) from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ComposeListError(detail or f"docker compose ls exited {result.returncode}")
    try:
        payload = json.loads(result.stdout.decode("utf-8") or "[]")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ComposeListError(f"invalid docker compose ls JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ComposeListError("invalid docker compose ls JSON: expected a list")
    records = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("Name"), str):
            raise ComposeListError("invalid docker compose ls project record")
        records.append(
            ComposeProjectRecord(
                name=item["Name"],
                status=str(item.get("Status") or "unknown"),
                config_files=_config_files(item.get("ConfigFiles")),
            )
        )
    return tuple(records)
```

- [ ] **Step 4: Add failing command-error and malformed-JSON tests**

```python
import pytest

from docker_manage_server.docker_runtime import ComposeListError


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (SimpleNamespace(returncode=1, stdout=b"", stderr=b"compose unavailable"), "compose unavailable"),
        (SimpleNamespace(returncode=0, stdout=b"not-json", stderr=b""), "invalid docker compose ls JSON"),
        (SimpleNamespace(returncode=0, stdout=b"{}", stderr=b""), "expected a list"),
    ],
)
def test_list_compose_projects_maps_failures(result, message):
    runtime = DockerRuntime(
        client=SimpleNamespace(),
        command_runner=lambda *_args, **_kwargs: result,
    )
    with pytest.raises(ComposeListError, match=message):
        runtime.list_compose_projects()


def test_list_compose_projects_maps_command_execution_failure():
    def runner(*_args, **_kwargs):
        raise OSError("docker not found")

    runtime = DockerRuntime(client=SimpleNamespace(), command_runner=runner)
    with pytest.raises(ComposeListError, match="docker not found"):
        runtime.list_compose_projects()
```

- [ ] **Step 5: Run Docker runtime tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_docker_runtime.py
```

Expected: all tests in `test_docker_runtime.py` pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/docker_manage_server/docker_runtime.py tests/unit/test_docker_runtime.py
git commit -m "feat: enumerate compose projects"
```

---

### Task 2: Runtime Inventory Aggregation and Ownership Boundary

**Files:**
- Create: `src/docker_manage_server/runtime_inventory.py`
- Create: `tests/unit/test_runtime_inventory.py`
- Modify: `src/docker_manage_server/docker_runtime.py:54-63`
- Modify: `tests/unit/test_docker_runtime.py`

**Interfaces:**
- Consumes: `DockerRuntime.list_compose_projects() -> tuple[ComposeProjectRecord, ...]`.
- Consumes: `DockerRuntime.list_containers() -> list[dict[str, Any]]`.
- Consumes: `DockerRuntime.get_container(container_id: str) -> Any` and `DockerRuntime._serialize_container(container) -> dict[str, Any]`.
- Produces: `ComposeProject`, `RuntimeOverview`, `RuntimeInventoryService.load()`, `find_project()`, `require_project_container()`, and `require_standalone_container()`.

- [ ] **Step 1: Write failing aggregation and sorting tests**

Create `tests/unit/test_runtime_inventory.py` with a fake runtime and explicit mixed input:

```python
from docker_manage_server.docker_runtime import ComposeListError, ComposeProjectRecord
from docker_manage_server.runtime_inventory import RuntimeInventoryService


def container(
    container_id: str,
    *,
    running: bool,
    created: str,
    project: str | None = None,
    service: str | None = None,
):
    labels = {}
    if project:
        labels["com.docker.compose.project"] = project
        labels["com.docker.compose.service"] = service or "web"
    return {
        "id": container_id,
        "short_id": container_id,
        "name": container_id,
        "created": created,
        "running": running,
        "status": "running" if running else "exited",
        "labels": labels,
        "ports": {},
        "mounts": [],
        "networks": {},
    }


class FakeRuntime:
    def __init__(self, projects, containers):
        self.projects = projects
        self.containers = containers

    def list_compose_projects(self):
        return self.projects

    def list_containers(self):
        return self.containers


def test_inventory_groups_compose_and_sorts_standalone_containers():
    runtime = FakeRuntime(
        projects=(
            ComposeProjectRecord("stopped-empty", "exited(0)", ("/srv/empty/compose.yaml",)),
            ComposeProjectRecord("alpha", "running(1)", ("/srv/alpha/compose.yaml",)),
        ),
        containers=[
            container("old-running", running=True, created="2026-01-01T00:00:00Z"),
            container("alpha-web", running=True, created="2026-03-01T00:00:00Z", project="alpha"),
            container("new-running", running=True, created="2026-02-01T00:00:00Z"),
            container("new-stopped", running=False, created="2026-04-01T00:00:00Z"),
        ],
    )

    overview = RuntimeInventoryService(runtime).load()

    assert [project.name for project in overview.compose_projects] == ["alpha", "stopped-empty"]
    assert [item["id"] for item in overview.standalone_containers] == [
        "new-running",
        "old-running",
        "new-stopped",
    ]
    assert overview.compose_projects[0].containers[0]["compose_service"] == "web"
    assert overview.compose_projects[1].container_count == 0
```

- [ ] **Step 2: Run the aggregation test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_runtime_inventory.py::test_inventory_groups_compose_and_sorts_standalone_containers
```

Expected: import fails because `runtime_inventory.py` does not exist.

- [ ] **Step 3: Implement immutable view models and the aggregator**

Create `runtime_inventory.py` with these public shapes:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .docker_runtime import (
    ComposeListError,
    ComposeProjectRecord,
    ContainerNotFoundError,
    DockerRuntime,
    DockerRuntimeError,
)


COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
COMPOSE_CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"
UNREGISTERED_STATUS = "未被 Compose CLI 发现"


@dataclass(frozen=True)
class ComposeProject:
    name: str
    status: str
    config_files: tuple[str, ...]
    containers: tuple[dict[str, Any], ...]

    @property
    def running_containers(self) -> int:
        return sum(bool(item.get("running")) for item in self.containers)

    @property
    def container_count(self) -> int:
        return len(self.containers)

    @property
    def running(self) -> bool:
        return self.running_containers > 0 or self.status.casefold().startswith("running")


@dataclass(frozen=True)
class RuntimeOverview:
    compose_projects: tuple[ComposeProject, ...] = ()
    standalone_containers: tuple[dict[str, Any], ...] = ()
    compose_error: str | None = None
    docker_error: str | None = None


def _split_config_files(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


class RuntimeInventoryService:
    def __init__(self, runtime: DockerRuntime):
        self.runtime = runtime

    def load(self) -> RuntimeOverview:
        try:
            containers = self.runtime.list_containers()
        except DockerRuntimeError as exc:
            return RuntimeOverview(docker_error=str(exc))

        compose_error = None
        try:
            records = self.runtime.list_compose_projects()
        except ComposeListError as exc:
            records = ()
            compose_error = str(exc)

        projects: dict[str, dict[str, Any]] = {
            record.name: {
                "status": record.status,
                "config_files": record.config_files,
                "containers": [],
            }
            for record in records
        }
        standalone = []
        for item in containers:
            labels = item.get("labels") if isinstance(item.get("labels"), dict) else {}
            project_name = labels.get(COMPOSE_PROJECT_LABEL)
            if not isinstance(project_name, str) or not project_name:
                standalone.append(item)
                continue
            entry = projects.setdefault(
                project_name,
                {
                    "status": UNREGISTERED_STATUS,
                    "config_files": _split_config_files(labels.get(COMPOSE_CONFIG_FILES_LABEL)),
                    "containers": [],
                },
            )
            enriched = dict(item)
            enriched["compose_project"] = project_name
            enriched["compose_service"] = labels.get(COMPOSE_SERVICE_LABEL) or "—"
            entry["containers"].append(enriched)

        compose_projects = tuple(
            sorted(
                (
                    ComposeProject(
                        name=name,
                        status=value["status"],
                        config_files=tuple(value["config_files"]),
                        containers=tuple(value["containers"]),
                    )
                    for name, value in projects.items()
                ),
                key=lambda project: (not project.running, project.name.casefold()),
            )
        )
        standalone.sort(key=lambda item: str(item.get("created") or ""), reverse=True)
        standalone.sort(key=lambda item: not bool(item.get("running")))
        return RuntimeOverview(
            compose_projects=compose_projects,
            standalone_containers=tuple(standalone),
            compose_error=compose_error,
        )
```

- [ ] **Step 4: Write failing CLI degradation and label-only project tests**

Add a fake that raises `ComposeListError`, then assert tagged containers remain grouped and never become standalone:

```python
def test_inventory_uses_labels_when_compose_cli_fails():
    runtime = FakeRuntime(
        projects=(),
        containers=[
            container("orphan-web", running=False, created="2026-01-01T00:00:00Z", project="orphan"),
            container("direct", running=True, created="2026-02-01T00:00:00Z"),
        ],
    )

    def fail():
        raise ComposeListError("compose plugin unavailable")

    runtime.list_compose_projects = fail
    overview = RuntimeInventoryService(runtime).load()

    assert overview.compose_error == "compose plugin unavailable"
    assert [project.name for project in overview.compose_projects] == ["orphan"]
    assert overview.compose_projects[0].status == "未被 Compose CLI 发现"
    assert [item["id"] for item in overview.standalone_containers] == ["direct"]
```

- [ ] **Step 5: Add ownership lookup tests and implementation**

Use real serialized mappings for lookup without rerunning the full Compose list:

```python
def test_require_project_and_standalone_container_enforce_labels():
    compose_item = container(
        "compose-web",
        running=True,
        created="2026-01-01T00:00:00Z",
        project="mall",
    )
    standalone_item = container(
        "direct",
        running=True,
        created="2026-01-01T00:00:00Z",
    )
    runtime = FakeRuntime((), [])
    runtime.get_serialized_container = lambda container_id: {
        "compose-web": compose_item,
        "direct": standalone_item,
    }[container_id]
    inventory = RuntimeInventoryService(runtime)

    assert inventory.require_project_container("mall", "compose-web")["id"] == "compose-web"
    assert inventory.require_standalone_container("direct")["id"] == "direct"

    for project_name, container_id in (("other", "compose-web"), ("mall", "direct")):
        try:
            inventory.require_project_container(project_name, container_id)
        except ContainerNotFoundError:
            pass
        else:
            raise AssertionError("ownership mismatch must look like not-found")

    try:
        inventory.require_standalone_container("compose-web")
    except ContainerNotFoundError:
        pass
    else:
        raise AssertionError("compose container must not enter standalone pages")
```

Add to `DockerRuntime`:

```python
def get_serialized_container(self, container_id: str) -> dict[str, Any]:
    return self._serialize_container(self.get_container(container_id))
```

Add to `RuntimeInventoryService`:

```python
def find_project(self, name: str) -> ComposeProject | None:
    overview = self.load()
    if overview.docker_error:
        raise DockerRuntimeError(overview.docker_error)
    return next((project for project in overview.compose_projects if project.name == name), None)

def require_project_container(self, project_name: str, container_id: str) -> dict[str, Any]:
    item = self.runtime.get_serialized_container(container_id)
    labels = item.get("labels") if isinstance(item.get("labels"), dict) else {}
    if labels.get(COMPOSE_PROJECT_LABEL) != project_name:
        raise ContainerNotFoundError(container_id)
    enriched = dict(item)
    enriched["compose_project"] = project_name
    enriched["compose_service"] = labels.get(COMPOSE_SERVICE_LABEL) or "—"
    return enriched

def require_standalone_container(self, container_id: str) -> dict[str, Any]:
    item = self.runtime.get_serialized_container(container_id)
    labels = item.get("labels") if isinstance(item.get("labels"), dict) else {}
    if labels.get(COMPOSE_PROJECT_LABEL):
        raise ContainerNotFoundError(container_id)
    return item
```

- [ ] **Step 6: Normalize Docker SDK list failures**

Add a test that makes `client.containers.list` raise `DockerException`, then change `list_containers` to map it to `DockerRuntimeError`. The final method must be:

```python
def list_containers(self) -> list[dict[str, Any]]:
    try:
        containers = self.client.containers.list(all=True)
    except DockerException as exc:
        raise DockerRuntimeError(str(exc)) from exc
    return [self._serialize_container(container) for container in containers]
```

- [ ] **Step 7: Run inventory and Docker runtime unit tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_runtime_inventory.py \
  tests/unit/test_docker_runtime.py
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit Task 2**

```bash
git add \
  src/docker_manage_server/runtime_inventory.py \
  src/docker_manage_server/docker_runtime.py \
  tests/unit/test_runtime_inventory.py \
  tests/unit/test_docker_runtime.py
git commit -m "feat: classify runtime resources"
```

---

### Task 3: Dashboard and Runtime Management Page

**Files:**
- Create: `src/docker_manage_server/templates/runtime/list.html`
- Create: `tests/web/test_runtime.py`
- Modify: `src/docker_manage_server/api.py:48-92`
- Modify: `src/docker_manage_server/web.py:144-312`
- Modify: `src/docker_manage_server/web_views.py:1-75`
- Modify: `src/docker_manage_server/templates/base.html:15-22`
- Modify: `src/docker_manage_server/templates/dashboard.html`
- Modify: `src/docker_manage_server/static/css/app.css`
- Modify: `tests/web/conftest.py`
- Modify: `tests/web/test_dashboard.py`
- Modify: `tests/web/test_containers.py:26-32,99-104`

**Interfaces:**
- Consumes: `RuntimeInventoryService.load() -> RuntimeOverview`.
- Produces: `compose_project_view(project: ComposeProject) -> dict[str, Any]`.
- Produces: `runtime_metrics(tasks, overview) -> dict[str, int]`.
- Produces: `GET /runtime` and compatibility `GET /containers -> 307 /runtime`.

- [ ] **Step 1: Extend the Web fake runtime with Compose input**

In `tests/web/conftest.py`, add:

```python
from docker_manage_server.docker_runtime import ComposeListError, ComposeProjectRecord


class WebFakeRuntime:
    def __init__(self):
        self.compose_error = None
        self.compose_projects = ()
        self.container_objects = {}

    def list_compose_projects(self):
        if self.compose_error:
            raise ComposeListError(self.compose_error)
        return self.compose_projects

    def get_serialized_container(self, container_id):
        for item in self.containers:
            if item["id"] == container_id:
                return item
        raise ContainerNotFoundError(container_id)
```

Insert the three attributes at the end of the existing `__init__`, and insert both methods after the existing `list_containers` method. Leave the existing `available`, deployment command, Compose validation and terminal fake methods unchanged. Import `ContainerNotFoundError` alongside `ComposeListError`.

- [ ] **Step 2: Write failing homepage classification and five-item tests**

Replace the old dashboard container assertion with explicit three-module data. Create six entries per category and assert only the expected five render:

```python
def test_dashboard_renders_three_runtime_modules_with_five_items(web_context):
    client, store, runtime = web_context
    for index in range(6):
        task = store.create(f"task-{index}", f"task-{index}.tar.gz")
        task.status = TaskStatus.PENDING_REVIEW
        task.app_name = f"task-app-{index}"
        store.save(task)

    runtime.compose_projects = tuple(
        ComposeProjectRecord(f"project-{index}", "running(1)", ())
        for index in range(6)
    )
    runtime.containers = [
        {
            "id": f"direct-{index}",
            "short_id": f"direct-{index}",
            "name": f"direct-{index}",
            "image": "demo:latest",
            "created": f"2026-08-{index + 1:02d}T00:00:00Z",
            "status": "running",
            "running": True,
            "ports": {},
            "labels": {},
            "mounts": [],
            "networks": {},
        }
        for index in range(6)
    ]

    response = client.get("/")

    assert response.status_code == 200
    assert "最近部署任务" in response.text
    assert "Compose 项目" in response.text
    assert "独立容器" in response.text
    assert response.text.count('data-dashboard-task-row') == 5
    assert response.text.count('data-dashboard-compose-row') == 5
    assert response.text.count('data-dashboard-container-row') == 5
    assert 'href="/runtime"' in response.text
```

- [ ] **Step 3: Write failing `/runtime` and redirect tests**

Create `tests/web/test_runtime.py`:

```python
def test_runtime_page_lists_compose_projects_and_standalone_containers(web_context):
    client, _store, runtime = web_context
    runtime.compose_projects = (
        ComposeProjectRecord("mall", "running(1)", ("/srv/mall/compose.yaml",)),
    )
    runtime.containers = [
        {
            "id": "mall-web",
            "short_id": "mall-web",
            "name": "mall-web",
            "image": "mall/web:1",
            "created": "2026-08-01T00:00:00Z",
            "status": "running",
            "running": True,
            "ports": {},
            "labels": {"com.docker.compose.project": "mall"},
            "mounts": [],
            "networks": {},
        },
        {
            "id": "direct",
            "short_id": "direct",
            "name": "direct",
            "image": "alpine:3.21",
            "created": "2026-08-02T00:00:00Z",
            "status": "exited",
            "running": False,
            "ports": {},
            "labels": {},
            "mounts": [],
            "networks": {},
        },
    ]

    response = client.get("/runtime")
    assert response.status_code == 200
    assert 'href="/compose-projects/mall"' in response.text
    assert 'href="/containers/direct"' in response.text
    assert 'href="/containers/mall-web"' not in response.text


def test_old_container_list_redirects_to_runtime(web_context):
    client, _store, _runtime = web_context
    response = client.get("/containers", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/runtime"
```

- [ ] **Step 4: Run the new web tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/web/test_dashboard.py::test_dashboard_renders_three_runtime_modules_with_five_items \
  tests/web/test_runtime.py
```

Expected: failures because the inventory is not injected and `/runtime` does not exist.

- [ ] **Step 5: Inject RuntimeInventoryService once in create_app**

In `api.py`, construct and expose one service:

```python
inventory = RuntimeInventoryService(runtime)
deployment = DeploymentService(store, runtime)

app.state.inventory = inventory
app.state.deployment = deployment

app.include_router(create_web_router(store, deployment, runtime, inventory))
```

Add the imports and change `create_web_router` to accept:

```python
def create_web_router(
    store: TaskStore,
    deployment: DeploymentService,
    runtime: DockerRuntime,
    inventory: RuntimeInventoryService,
) -> APIRouter:
```

- [ ] **Step 6: Implement view conversion and metrics**

In `web_views.py`, add:

```python
from .runtime_inventory import ComposeProject, RuntimeOverview


def compose_project_view(project: ComposeProject) -> dict[str, Any]:
    return {
        "project": project,
        "status_label": project.status,
        "running": project.running,
        "container_count": project.container_count,
        "running_containers": project.running_containers,
        "containers": [container_view(item) for item in project.containers],
    }


def runtime_metrics(
    tasks: Sequence[DeploymentTask],
    overview: RuntimeOverview,
) -> dict[str, int]:
    all_containers = [
        item
        for project in overview.compose_projects
        for item in project.containers
    ] + list(overview.standalone_containers)
    return {
        "compose_projects": len(overview.compose_projects),
        "standalone_containers": len(overview.standalone_containers),
        "containers": len(all_containers),
        "running": sum(bool(item.get("running")) for item in all_containers),
        "failed": sum(task.status is TaskStatus.FAILED for task in tasks),
    }
```

Extend `container_view` with `compose_service`:

```python
"compose_service": item.get("compose_service") or "—",
```

Remove the old `dashboard_metrics` after all callers switch to `runtime_metrics`.

- [ ] **Step 7: Implement dashboard, `/runtime`, and redirect routes**

Dashboard obtains `overview = inventory.load()`, slices each list independently, and always renders HTTP 200. Add:

```python
"tasks": [task_view(task) for task in tasks[:5]],
"compose_projects": [
    compose_project_view(project) for project in overview.compose_projects[:5]
],
"standalone_containers": [
    container_view(item) for item in overview.standalone_containers[:5]
],
"metrics": runtime_metrics(tasks, overview),
"compose_error": overview.compose_error,
"docker_error": overview.docker_error,
```

Add routes before `/containers/{container_id}`:

```python
@router.get("/runtime", response_class=HTMLResponse)
def runtime_page(request: Request):
    overview = inventory.load()
    if overview.docker_error:
        return _web_error(request, 503, "Docker daemon 不可用", overview.docker_error)
    return templates.TemplateResponse(
        request=request,
        name="runtime/list.html",
        context={
            "page_title": "运行管理",
            "active_nav": "runtime",
            "compose_projects": [
                compose_project_view(project) for project in overview.compose_projects
            ],
            "standalone_containers": [
                container_view(item) for item in overview.standalone_containers
            ],
            "compose_error": overview.compose_error,
        },
    )


@router.get("/containers")
def legacy_container_list():
    return RedirectResponse("/runtime", status_code=307)
```

- [ ] **Step 8: Implement the three-module templates and navigation**

Update `base.html` navigation to:

```html
<a class="{{ 'active' if active_nav == 'runtime' else '' }}" href="/runtime">运行管理</a>
```

In `dashboard.html`, keep the page actions and render three panels. Every loop row must carry the test marker:

```html
<section class="panel">
  <div class="panel-heading"><h2>最近部署任务</h2><a href="/deployments">查看全部</a></div>
  <div class="table-scroll"><table>
    <thead><tr><th>应用</th><th>状态</th><th>更新时间</th></tr></thead>
    <tbody>{% for entry in tasks %}
      <tr data-dashboard-task-row><td><a href="/deployments/{{ entry.task.task_id }}">{{ entry.task.app_name or entry.task.original_filename }}</a></td><td>{% with status_value=entry.status_value, status_label=entry.status_label %}{% include "components/status_badge.html" %}{% endwith %}</td><td>{{ entry.updated_at }}</td></tr>
    {% else %}<tr><td colspan="3" class="empty-cell">暂无部署任务</td></tr>{% endfor %}</tbody>
  </table></div>
</section>
<div class="content-grid">
  <section class="panel">
    <div class="panel-heading"><h2>Compose 项目</h2><a href="/runtime">查看全部</a></div>
    {% if compose_error %}<div class="alert alert-warning">Compose 项目读取失败：{{ compose_error }}</div>{% endif %}
    <div class="table-scroll"><table><thead><tr><th>项目</th><th>容器</th><th>状态</th></tr></thead><tbody>{% for entry in compose_projects %}
      <tr data-dashboard-compose-row><td><a href="/compose-projects/{{ entry.project.name }}">{{ entry.project.name }}</a></td><td>{{ entry.running_containers }}/{{ entry.container_count }}</td><td><span class="status {{ 'status-running' if entry.running else 'status-stopped' }}">{{ entry.status_label }}</span></td></tr>
    {% else %}<tr><td colspan="3" class="empty-cell">暂无 Compose 项目</td></tr>{% endfor %}</tbody></table></div>
  </section>
  <section class="panel">
    <div class="panel-heading"><h2>独立容器</h2><a href="/runtime">查看全部</a></div>
    <div class="table-scroll"><table><thead><tr><th>名称</th><th>镜像</th><th>状态</th></tr></thead><tbody>{% for container in standalone_containers %}
      <tr data-dashboard-container-row><td><a href="/containers/{{ container.item.id }}">{{ container.name }}</a></td><td>{{ container.image }}</td><td><span class="status {{ 'status-running' if container.running else 'status-stopped' }}">{{ container.status_label }}</span></td></tr>
    {% else %}<tr><td colspan="3" class="empty-cell">暂无独立容器</td></tr>{% endfor %}</tbody></table></div>
  </section>
</div>
```

Create `runtime/list.html` with two panels. The Compose table columns are 项目、状态、运行容器、配置文件; each project name links to `/compose-projects/{{ entry.project.name }}` and config paths are joined with `<br>`. The independent table columns are 名称、镜像、状态、端口、短 ID and reuse the exact independent-container row above plus `ports_text` and `item.short_id`. Render `compose_error` as an `.alert-warning` above the Compose table. The file contains no loop over project containers, so no Compose container can link to `/containers/{id}`.

- [ ] **Step 9: Update responsive CSS and degradation tests**

Reuse `.content-grid` for the two runtime panels. Add only focused status styles:

```css
.status-partial { color: #92400e; background: #fef3c7; }
.status-unknown { color: #475569; background: #e2e8f0; }
```

Update the offline dashboard test to assert deployment tasks remain and both runtime areas display the Docker error. Add a Compose-only failure test that sets `runtime.compose_error`, asserts HTTP 200, the warning text, a label-derived Compose project, and the independent container list.

- [ ] **Step 10: Run dashboard/runtime/navigation tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/web/test_dashboard.py \
  tests/web/test_runtime.py \
  tests/web/test_containers.py \
  tests/web/test_security.py
```

Expected: all selected tests pass, including old independent container detail pages.

- [ ] **Step 11: Commit Task 3**

```bash
git add \
  src/docker_manage_server/api.py \
  src/docker_manage_server/web.py \
  src/docker_manage_server/web_views.py \
  src/docker_manage_server/templates/base.html \
  src/docker_manage_server/templates/dashboard.html \
  src/docker_manage_server/templates/runtime/list.html \
  src/docker_manage_server/static/css/app.css \
  tests/web/conftest.py \
  tests/web/test_dashboard.py \
  tests/web/test_runtime.py \
  tests/web/test_containers.py
git commit -m "feat: classify dashboard runtime resources"
```

---

### Task 4: Compose Project Detail and Read-Only Container Dialogs

**Files:**
- Create: `src/docker_manage_server/templates/compose_projects/detail.html`
- Create: `tests/web/test_compose_projects.py`
- Modify: `src/docker_manage_server/web.py`
- Modify: `src/docker_manage_server/static/js/app.js`
- Modify: `src/docker_manage_server/static/css/app.css`
- Modify: `pyproject.toml`
- Modify: `tests/web/test_package_resources.py`

**Interfaces:**
- Consumes: `RuntimeInventoryService.find_project(name) -> ComposeProject | None`.
- Consumes: `compose_project_view(project) -> dict[str, Any]` and `container_view(item)`.
- Produces: `GET /compose-projects/{project_name}`.
- Produces: `data-dialog-open`, `data-container-dialog`, and `data-dialog-close` DOM contract.

- [ ] **Step 1: Write failing project detail and empty-project tests**

Create `tests/web/test_compose_projects.py`:

```python
from docker_manage_server.docker_runtime import ComposeProjectRecord


def test_compose_project_detail_renders_container_dialog_and_tools(web_context):
    client, _store, runtime = web_context
    runtime.compose_projects = (
        ComposeProjectRecord("mall", "running(1)", ("/srv/mall/compose.yaml",)),
    )
    runtime.containers = [
        {
            "id": "mall-web",
            "short_id": "mall-web",
            "name": "mall-web-1",
            "image": "mall/web:1",
            "created": "2026-08-01T00:00:00Z",
            "status": "running",
            "running": True,
            "ports": {"8000/tcp": [{"HostPort": "6308"}]},
            "labels": {
                "com.docker.compose.project": "mall",
                "com.docker.compose.service": "web",
            },
            "mounts": [{"Source": "/srv/data", "Destination": "/data", "Type": "bind"}],
            "networks": {"mall_default": {"IPAddress": "172.20.0.2"}},
        }
    ]

    response = client.get("/compose-projects/mall")

    assert response.status_code == 200
    assert "mall-web-1" in response.text
    assert "web" in response.text
    assert 'data-dialog-open="container-mall-web"' in response.text
    assert 'data-container-dialog="container-mall-web"' in response.text
    assert 'href="/compose-projects/mall/containers/mall-web/logs"' in response.text
    assert 'href="/compose-projects/mall/containers/mall-web/terminal"' in response.text
    assert 'href="/containers/mall-web"' not in response.text
    assert "启动" not in response.text
    assert "停止" not in response.text
    assert "重启" not in response.text
    assert "删除" not in response.text


def test_compose_project_detail_allows_empty_stopped_project(web_context):
    client, _store, runtime = web_context
    runtime.compose_projects = (
        ComposeProjectRecord("empty", "exited(0)", ("/srv/empty/compose.yaml",)),
    )
    response = client.get("/compose-projects/empty")
    assert response.status_code == 200
    assert "暂无容器" in response.text


def test_unknown_compose_project_returns_404(web_context):
    client, _store, _runtime = web_context
    response = client.get("/compose-projects/missing")
    assert response.status_code == 404
    assert "找不到 Compose 项目" in response.text
```

- [ ] **Step 2: Run the detail tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest -q tests/web/test_compose_projects.py
```

Expected: 404 or template-not-found failures because the route and template do not exist.

- [ ] **Step 3: Add the Compose detail route**

Add before any generic container routes:

```python
@router.get("/compose-projects/{project_name}", response_class=HTMLResponse)
def compose_project_detail(request: Request, project_name: str):
    try:
        project = inventory.find_project(project_name)
    except DockerRuntimeError as exc:
        return _web_error(request, 503, "Docker daemon 不可用", str(exc))
    if project is None:
        return _web_error(request, 404, "找不到 Compose 项目", project_name)
    return templates.TemplateResponse(
        request=request,
        name="compose_projects/detail.html",
        context={
            "page_title": project.name,
            "active_nav": "runtime",
            "compose_project": compose_project_view(project),
        },
    )
```

- [ ] **Step 4: Create the server-rendered detail and dialogs**

The template must:

- show project status, config files, total container count and running count;
- list every project container with name, service, image and status;
- use a button with `data-dialog-open="container-{{ container.item.id }}"`;
- use one native `<dialog data-container-dialog="container-{{ container.item.id }}">` per container;
- render ports, mounts, networks and labels with ordinary escaped Jinja expressions;
- link logs and terminal to Compose routes with `target="_blank" rel="noopener"`;
- contain no lifecycle form or button.

The dialog skeleton is:

```html
<button class="button button-secondary" type="button" data-dialog-open="container-{{ container.item.id }}">查看详情</button>
<dialog class="container-dialog" data-container-dialog="container-{{ container.item.id }}">
  <div class="dialog-heading">
    <h2>{{ container.name }}</h2>
    <button class="button button-secondary" type="button" data-dialog-close>关闭</button>
  </div>
  <dl class="definition-grid">
    <dt>Compose 服务</dt><dd>{{ container.compose_service }}</dd>
    <dt>镜像</dt><dd>{{ container.image }}</dd>
    <dt>状态</dt><dd>{{ container.status_label }}</dd>
    <dt>端口</dt><dd>{{ container.ports_text }}</dd>
  </dl>
  <section class="dialog-section"><h3>挂载</h3><pre>{% for mount in container.item.mounts %}{{ mount.Source or "—" }} → {{ mount.Destination or "—" }} ({{ mount.Type or "unknown" }})
{% else %}暂无挂载{% endfor %}</pre></section>
  <section class="dialog-section"><h3>网络</h3><pre>{% for name, network in container.item.networks.items() %}{{ name }}  {{ network.IPAddress or "—" }}
{% else %}暂无网络{% endfor %}</pre></section>
  <section class="dialog-section"><h3>标签</h3><pre>{% for key, value in container.item.labels|dictsort %}{{ key }}={{ value }}
{% else %}暂无标签{% endfor %}</pre></section>
</dialog>
```

- [ ] **Step 5: Add dialog JavaScript and CSS**

Append to `app.js`:

```javascript
document.querySelectorAll("[data-dialog-open]").forEach((button) => {
  button.addEventListener("click", () => {
    const dialog = document.querySelector(
      `[data-container-dialog="${CSS.escape(button.dataset.dialogOpen)}"]`,
    );
    if (dialog) dialog.showModal();
  });
});

document.querySelectorAll("[data-container-dialog]").forEach((dialog) => {
  dialog.querySelector("[data-dialog-close]")?.addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
});
```

Add focused CSS:

```css
.container-dialog { width: min(760px, calc(100% - 32px)); max-height: calc(100vh - 48px); padding: 20px; color: var(--text); background: var(--surface); border: 1px solid var(--border); border-radius: 12px; }
.container-dialog::backdrop { background: rgb(15 23 42 / 45%); }
.dialog-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.dialog-section { margin-top: 18px; }
```

At `max-width: 760px`, make `.dialog-heading` align to the start and wrap.

- [ ] **Step 6: Package and verify new templates**

Add to `[tool.setuptools.package-data]`:

```toml
"templates/runtime/*.html",
"templates/compose_projects/*.html",
```

Extend `test_package_resources.py`:

```python
def test_runtime_templates_are_packaged():
    package = files("docker_manage_server")
    for path in (
        "templates/runtime/list.html",
        "templates/compose_projects/detail.html",
    ):
        assert package.joinpath(path).is_file()
```

- [ ] **Step 7: Run Compose detail and package tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/web/test_compose_projects.py \
  tests/web/test_package_resources.py \
  tests/web/test_security.py
```

Expected: all selected tests pass, including HTML escaping security regressions.

- [ ] **Step 8: Commit Task 4**

```bash
git add \
  src/docker_manage_server/web.py \
  src/docker_manage_server/templates/compose_projects/detail.html \
  src/docker_manage_server/static/js/app.js \
  src/docker_manage_server/static/css/app.css \
  pyproject.toml \
  tests/web/test_compose_projects.py \
  tests/web/test_package_resources.py
git commit -m "feat: add compose project details"
```

---

### Task 5: Compose-Scoped Logs and Terminal

**Files:**
- Create: `src/docker_manage_server/templates/compose_projects/logs.html`
- Create: `src/docker_manage_server/templates/compose_projects/terminal.html`
- Modify: `src/docker_manage_server/api.py:212-267`
- Modify: `src/docker_manage_server/web.py`
- Modify: `tests/api/test_container_api.py`
- Modify: `tests/web/test_compose_projects.py`
- Modify: `tests/web/test_containers.py`
- Modify: `tests/web/test_security.py`
- Modify: `tests/web/test_package_resources.py`

**Interfaces:**
- Consumes: `RuntimeInventoryService.require_project_container(project_name, container_id)`.
- Consumes: `RuntimeInventoryService.require_standalone_container(container_id)`.
- Produces: `GET /compose-projects/{project}/containers/{container}/logs`.
- Produces: `GET /compose-projects/{project}/containers/{container}/terminal`.
- Produces: `GET /api/compose-projects/{project}/containers/{container}/logs`.
- Produces: `WS /api/compose-projects/{project}/containers/{container}/terminal`.
- Produces: shared `_serve_terminal(websocket, runtime, container_id)` after WebSocket acceptance.

- [ ] **Step 1: Write failing Compose page and API ownership tests**

Append to `tests/web/test_compose_projects.py`:

```python
def test_compose_log_and_terminal_pages_keep_project_context(web_context):
    client, _store, runtime = web_context
    runtime.containers = [compose_container_fixture("mall", "mall-web", running=True)]

    logs = client.get("/compose-projects/mall/containers/mall-web/logs")
    terminal = client.get("/compose-projects/mall/containers/mall-web/terminal")

    assert logs.status_code == 200
    assert 'data-log-url="/api/compose-projects/mall/containers/mall-web/logs"' in logs.text
    assert 'href="/compose-projects/mall"' in logs.text
    assert "mall / web / mall-web" in logs.text
    assert terminal.status_code == 200
    assert 'data-terminal-url="/api/compose-projects/mall/containers/mall-web/terminal"' in terminal.text
    assert 'href="/compose-projects/mall"' in terminal.text


def test_compose_tool_pages_hide_cross_project_container(web_context):
    client, _store, runtime = web_context
    runtime.containers = [compose_container_fixture("other", "other-web", running=True)]
    for suffix in ("logs", "terminal"):
        response = client.get(f"/compose-projects/mall/containers/other-web/{suffix}")
        assert response.status_code == 404
        assert "找不到容器" in response.text
        assert "other" not in response.text
```

Define `compose_container_fixture` once at the top of the test file; it must return the full mapping used in Task 4 with project/service labels.

Append to `tests/api/test_container_api.py`:

```python
def test_compose_logs_validate_project_ownership(client):
    allowed = client.get("/api/compose-projects/mall/containers/mall-web/logs")
    hidden = client.get("/api/compose-projects/other/containers/mall-web/logs")
    assert allowed.status_code == 200
    assert allowed.text == "hello\n"
    assert hidden.status_code == 404


def test_compose_terminal_hides_cross_project_container(client):
    with client.websocket_connect(
        "/api/compose-projects/other/containers/mall-web/terminal"
    ) as websocket:
        message = websocket.receive_json()
    assert message["error"] == "container_not_found"
```

Update the API fake so `get_serialized_container("mall-web")` returns a running item labeled for project `mall` and `logs` returns `hello\n`.

- [ ] **Step 2: Run focused tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/web/test_compose_projects.py \
  tests/api/test_container_api.py
```

Expected: new routes return 404 and the WebSocket route is absent.

- [ ] **Step 3: Add Compose tool-page routes and templates**

Create a helper in `web.py`:

```python
def _compose_container_page(
    request: Request,
    inventory: RuntimeInventoryService,
    project_name: str,
    container_id: str,
    template_name: str,
) -> HTMLResponse:
    try:
        item = inventory.require_project_container(project_name, container_id)
    except ContainerNotFoundError:
        return _web_error(request, 404, "找不到容器", container_id)
    except DockerRuntimeError as exc:
        return _web_error(request, 503, "Docker daemon 不可用", str(exc))
    container = container_view(item)
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "page_title": container["name"],
            "active_nav": "runtime",
            "project_name": project_name,
            "container": container,
        },
    )
```

Add the two GET routes. Create `compose_projects/logs.html` with this complete body:

```html
{% extends "base.html" %}
{% block content %}
<section class="panel tool-page-panel log-panel" data-log-viewer data-log-url="/api/compose-projects/{{ project_name }}/containers/{{ container.item.id }}/logs">
  <div class="panel-heading"><div><h2>{{ project_name }} / {{ container.compose_service }} / {{ container.name }}</h2><span class="status {{ 'status-running' if container.running else 'status-stopped' }}">{{ container.status_label }}</span></div><a class="button button-secondary" href="/compose-projects/{{ project_name }}">返回 Compose 项目</a></div>
  <div class="toolbar"><label>行数 <select data-log-tail><option>100</option><option>500</option><option value="all">全部</option></select></label><label><input type="checkbox" data-log-timestamps> 显示时间戳</label><button class="button button-secondary" type="button" data-log-refresh>刷新日志</button></div>
  <pre class="log-output-viewport" data-log-output>点击“刷新日志”读取日志。</pre>
</section>
{% endblock %}
```

Create `compose_projects/terminal.html` with this complete body:

```html
{% extends "base.html" %}
{% block head %}<link rel="stylesheet" href="/static/vendor/xterm/xterm.css">{% endblock %}
{% block content %}
<section class="panel tool-page-panel terminal-panel" data-terminal-url="/api/compose-projects/{{ project_name }}/containers/{{ container.item.id }}/terminal" data-terminal-command="/bin/sh">
  <div class="panel-heading"><div><h2>{{ project_name }} / {{ container.compose_service }} / {{ container.name }}</h2><span class="status {{ 'status-running' if container.running else 'status-stopped' }}">{{ container.status_label }}</span></div><a class="button button-secondary" href="/compose-projects/{{ project_name }}">返回 Compose 项目</a></div>
  {% if not container.running %}<div class="alert alert-warning">容器未运行，无法连接终端。</div>{% endif %}
  <div class="toolbar"><span data-terminal-status>尚未连接</span><button class="button button-primary" type="button" data-terminal-connect {{ 'disabled' if not container.running else '' }}>连接终端</button></div>
  <div class="terminal-viewport" data-terminal-viewport></div>
</section>
{% endblock %}
{% block scripts %}<script type="module" src="/static/js/terminal.js"></script>{% endblock %}
```

Compose project names use Docker Compose's URL-safe project-name character set; Jinja still escapes them. Use these exact project-scoped links and never use the project name for file access.

- [ ] **Step 4: Add Compose-scoped log API**

Add:

```python
@app.get("/api/compose-projects/{project_name}/containers/{container_id}/logs")
def get_compose_logs(
    project_name: str,
    container_id: str,
    tail: str = "all",
    timestamps: bool = False,
) -> PlainTextResponse:
    try:
        inventory.require_project_container(project_name, container_id)
        output = runtime.logs(container_id, tail=tail, timestamps=timestamps)
    except ContainerNotFoundError as exc:
        raise HTTPException(status_code=404, detail="container not found") from exc
    except DockerRuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return PlainTextResponse(output.decode("utf-8", errors="replace"))
```

- [ ] **Step 5: Refactor terminal relay once and add Compose WebSocket**

Replace the duplicated session creation/relay block in the existing terminal endpoint with the following shared function, then call it after origin validation and `await websocket.accept()`:

```python
async def _serve_terminal(
    websocket: WebSocket,
    runtime: DockerRuntime,
    container_id: str,
    command: str,
) -> None:
    try:
        session = runtime.create_terminal(container_id, shlex.split(command))
    except ContainerNotFoundError:
        await websocket.send_json({"error": "container_not_found"})
        await websocket.close(code=1008)
        return
    except ContainerNotRunningError:
        await websocket.send_json({"error": "container_not_running"})
        await websocket.close(code=1008)
        return
    except DockerRuntimeError as exc:
        await websocket.send_json({"error": "docker_runtime_error", "detail": str(exc)})
        await websocket.close(code=1011)
        return

    reader = asyncio.create_task(_relay_terminal_output(websocket, session.socket))
    writer = asyncio.create_task(_relay_terminal_input(websocket, runtime, session))
    try:
        await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        runtime.close_terminal(session)
        for task in (reader, writer):
            if not task.done():
                task.cancel()
        await asyncio.gather(reader, writer, return_exceptions=True)
        await _close_websocket(websocket)
```

Both WebSocket endpoints must reject cross-origin requests before acceptance. The Compose endpoint then accepts, validates ownership, returns a generic `container_not_found` on mismatch, and only then calls `_serve_terminal`:

```python
@app.websocket(
    "/api/compose-projects/{project_name}/containers/{container_id}/terminal"
)
async def compose_terminal(
    websocket: WebSocket,
    project_name: str,
    container_id: str,
    command: str = "/bin/sh",
):
    if not origin_matches_host(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        inventory.require_project_container(project_name, container_id)
    except ContainerNotFoundError:
        await websocket.send_json({"error": "container_not_found"})
        await websocket.close(code=1008)
        return
    except DockerRuntimeError as exc:
        await websocket.send_json({"error": "docker_runtime_error", "detail": str(exc)})
        await websocket.close(code=1011)
        return
    await _serve_terminal(websocket, runtime, container_id, command)
```

- [ ] **Step 6: Restrict independent Web pages and terminal to standalone containers**

Change `_container_page` to use `inventory.require_standalone_container` rather than directly serializing `runtime.get_container`. Pass `inventory` into every independent detail/log/terminal page call.

Before serving `/api/containers/{container_id}/terminal`, call `inventory.require_standalone_container(container_id)` after acceptance. On a Compose-labeled container, send `container_not_found` and close with 1008. Keep `/api/containers`, `/api/containers/{id}`, and `/api/containers/{id}/logs` response compatibility unchanged because they are public read APIs, but no Compose page links to them.

Add web and WebSocket tests proving `/containers/mall-web`, its tool pages, and the independent terminal WebSocket do not expose a Compose container.

- [ ] **Step 7: Add cross-origin test for Compose WebSocket**

In `tests/web/test_security.py`, add this complete test:

```python
def test_cross_origin_compose_terminal_is_rejected(web_context):
    client, _store, _runtime = web_context
    with pytest.raises(WebSocketDisconnect) as captured:
        with client.websocket_connect(
            "/api/compose-projects/mall/containers/mall-web/terminal",
            headers={"Origin": "https://evil.example"},
        ):
            pass
    assert captured.value.code == 1008
```

- [ ] **Step 8: Package the Compose tool templates**

Extend `test_runtime_templates_are_packaged` to include:

```python
"templates/compose_projects/logs.html",
"templates/compose_projects/terminal.html",
```

- [ ] **Step 9: Run terminal, API, web, and security tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/api/test_container_api.py \
  tests/unit/test_terminal_io.py \
  tests/web/test_compose_projects.py \
  tests/web/test_containers.py \
  tests/web/test_security.py \
  tests/web/test_package_resources.py
```

Expected: all selected tests pass; Compose ownership mismatch is consistently hidden as not-found.

- [ ] **Step 10: Commit Task 5**

```bash
git add \
  src/docker_manage_server/api.py \
  src/docker_manage_server/web.py \
  src/docker_manage_server/templates/compose_projects/logs.html \
  src/docker_manage_server/templates/compose_projects/terminal.html \
  tests/api/test_container_api.py \
  tests/unit/test_terminal_io.py \
  tests/web/test_compose_projects.py \
  tests/web/test_containers.py \
  tests/web/test_security.py \
  tests/web/test_package_resources.py
git commit -m "feat: add compose scoped container tools"
```

---

### Task 6: Documentation, Real-Environment Coverage, and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `tests/integration/test_real_docker.py`

**Interfaces:**
- Consumes: completed runtime classification, routes, templates, APIs and WebSocket paths.
- Produces: user-facing documentation and final verified implementation.

- [ ] **Step 1: Add a Docker-available integration test for classification**

Extend `tests/integration/test_real_docker.py` without creating or deleting containers. The test may skip when no Docker daemon is available and must verify every existing Docker container is classified exactly once:

```python
def test_real_runtime_inventory_classifies_every_container(tmp_path):
    from docker_manage_server.docker_runtime import DockerRuntime
    from docker_manage_server.runtime_inventory import RuntimeInventoryService

    runtime = DockerRuntime()
    raw = runtime.list_containers()
    overview = RuntimeInventoryService(runtime).load()

    assert overview.docker_error is None
    classified = [
        item["id"]
        for project in overview.compose_projects
        for item in project.containers
    ] + [item["id"] for item in overview.standalone_containers]
    assert sorted(classified) == sorted(item["id"] for item in raw)
    assert len(classified) == len(set(classified))
```

Decorate the test with the file's existing `@pytest.mark.skipif(not docker_available, reason="Docker daemon unavailable")` marker.

- [ ] **Step 2: Update README with exact navigation and management semantics**

Replace the “启动后访问” and container-operation description with this content, while retaining the existing deployment flow and no-authentication warning:

```markdown
启动后访问：

- 运行概览：`http://服务器IP:${DOCKER_MANAGE_SERVER_PORT}/`
- 部署任务：`http://服务器IP:${DOCKER_MANAGE_SERVER_PORT}/deployments`
- 运行管理：`http://服务器IP:${DOCKER_MANAGE_SERVER_PORT}/runtime`
- API 文档：`http://服务器IP:${DOCKER_MANAGE_SERVER_PORT}/docs`

运行概览依次展示最近 5 个部署任务、前 5 个 Compose 项目和前 5 个独立容器。运行管理展示完整列表。Compose 项目通过 `docker compose ls --all` 读取，因此已停止但仍存在的项目也会保留。旧的 `/containers` 列表地址会跳转到 `/runtime`。

Compose 项目详情是项目内容器的统一入口。容器详情为只读弹框；日志和终端使用带项目上下文的独立页面。Compose 内容器不提供单容器启动、停止、重启或删除。没有 Compose 项目标签的独立容器继续使用独立容器详情、日志和终端页面。

服务端镜像必须同时提供 Docker CLI、Docker Compose 插件和 Docker Socket 访问。Docker daemon 不可用时运行管理不可用；Compose 列表命令单独失败时，页面仍显示独立容器，并根据容器标签隔离 Compose 容器。
```

Confirm the resulting README covers all of these statements:

- 首页三个模块和每类前 5 项；
- `/runtime` replaces the old container list and `/containers` redirects;
- Compose projects include stopped projects from `docker compose ls --all`;
- Compose project detail owns container discovery;
- Compose container detail is read-only, logs and terminal use project-scoped pages;
- independent containers keep their existing details, logs and terminal pages;
- lifecycle actions remain out of scope;
- Docker daemon and Compose CLI must both be present in the service image, while Compose CLI failure is locally degraded.

Keep the existing security warning that the first release has no authentication.

- [ ] **Step 3: Run formatting-neutral source checks**

Run:

```bash
git diff --check
.venv/bin/python -m compileall -q src tests
```

Expected: both commands exit 0 with no syntax or whitespace errors.

- [ ] **Step 4: Run the complete test suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: all tests pass; Docker-dependent integration tests may report explicit skips only when the daemon is unavailable.

- [ ] **Step 5: Validate the server Compose configuration**

Run:

```bash
DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server \
DOCKER_MANAGE_SERVER_PORT=6308 \
docker compose config --quiet
```

Expected: exit 0, with no Compose validation output.

- [ ] **Step 6: Manually exercise live routes when Docker is available**

Start the service using the documented Compose command, then verify:

```bash
curl --fail http://localhost:6308/api/health
curl --fail http://localhost:6308/
curl --fail http://localhost:6308/runtime
```

Expected: health JSON reports Docker connected; `/` and `/runtime` return HTML 200. In a browser, open one existing Compose project and one independent container when each exists. Verify a stopped Compose project remains listed when the host has one. Do not create, stop, restart or delete resources just to satisfy this manual check; record unavailable sample categories as not exercised.

- [ ] **Step 7: Commit documentation and integration coverage**

```bash
git add README.md tests/integration/test_real_docker.py
git commit -m "docs: describe runtime resource management"
```

- [ ] **Step 8: Inspect final scope before handoff**

Run:

```bash
git status --short --branch
git log --oneline --decorate -8
git diff origin/main...HEAD --stat
```

Expected: no uncommitted implementation files; local branch contains the planned focused commits; no lifecycle action endpoint or button was introduced.

---

## Final Acceptance Checklist

- [ ] 首页顺序是部署任务、Compose 项目、独立容器，且每块最多 5 项。
- [ ] `/runtime` 展示全部 Compose 项目与独立容器，`/containers` 重定向到它。
- [ ] `docker compose ls --all --format json` 的停止项目和零容器项目都可见。
- [ ] Compose CLI 局部失败不影响独立容器显示，标签容器不会误归独立。
- [ ] Compose 项目详情包含项目状态、配置路径、计数和全部容器。
- [ ] Compose 内容器只读详情使用 dialog，不链接到独立容器详情。
- [ ] Compose 日志和终端页面使用项目专属路径和返回入口。
- [ ] 跨项目容器详情、日志和终端访问统一返回不泄露归属的 not-found。
- [ ] 独立容器原有详情、日志和终端仍可用，Compose 容器不能进入这些 Web 页面。
- [ ] 没有新增启动、停止、重启、删除、Compose 编辑、缓存或历史统计。
- [ ] 完整 pytest、compileall、`git diff --check` 和 `docker compose config --quiet` 通过。
