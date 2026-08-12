# 运行资源生命周期操作 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为独立 Docker 容器和 Docker Compose 项目增加安全、同步、按资源类型隔离的启动、停止、重启和删除操作。

**Architecture:** `DockerRuntime` 只执行固定 Docker SDK/Compose CLI 动作；新增 `RuntimeLifecycleService` 统一执行归属、存在性和实时状态校验，并固定使用完整容器 ID。API 与 Web 路由共享该服务，详情模板只渲染当前状态允许的动作。

**Tech Stack:** Python 3.11、Docker SDK for Python、Docker Compose v2 CLI、FastAPI、Jinja2、pytest、原生 JavaScript

## Global Constraints

- 独立容器使用 Docker SDK；Compose 项目始终使用 `docker compose --project-name <项目名> <动作>`。
- 独立容器运行中禁止删除，不使用 `docker rm -f`。
- Compose 删除固定使用 `down`，不传 `--volumes`，保留命名卷和数据。
- Compose 启动固定使用 `start`，不使用 `up -d`，不创建缺失容器。
- 不提供 Compose 项目内容器的单独生命周期操作。
- 启动只允许已停止资源；停止、重启只允许运行中资源；Compose 删除允许任意状态。
- 所有动作同步执行，不增加后台任务、批量操作、自动重试或进度轮询。
- 所有命令使用参数数组且 `shell=False`；项目名必须先通过实时 inventory 确认。
- 所有变更直接提交到用户指定的 `main` 分支，不推送远端。

---

### Task 1: Docker 与 Compose 固定生命周期动作

**Files:**
- Modify: `src/docker_manage_server/docker_runtime.py:105-156`
- Modify: `tests/unit/test_docker_runtime.py`

**Interfaces:**
- Produces: `start_container(container_id: str) -> None`
- Produces: `stop_container(container_id: str) -> None`
- Produces: `restart_container(container_id: str) -> None`
- Produces: `remove_container(container_id: str) -> None`
- Produces: `start_compose_project(project_name: str) -> None`
- Produces: `stop_compose_project(project_name: str) -> None`
- Produces: `restart_compose_project(project_name: str) -> None`
- Produces: `remove_compose_project(project_name: str) -> None`
- Errors: 统一抛出已有 `ContainerNotFoundError` 或 `DockerRuntimeError`。

- [ ] **Step 1: 写独立容器动作失败测试**

在 `tests/unit/test_docker_runtime.py` 增加真实方法调用断言：

```python
@pytest.mark.parametrize(
    ("method_name", "container_method", "expected_kwargs"),
    [
        ("start_container", "start", {}),
        ("stop_container", "stop", {}),
        ("restart_container", "restart", {}),
        ("remove_container", "remove", {"force": False, "v": False}),
    ],
)
def test_container_lifecycle_uses_docker_sdk(
    method_name, container_method, expected_kwargs
):
    calls = []
    container = SimpleNamespace(
        **{container_method: lambda **kwargs: calls.append(kwargs)}
    )
    runtime = DockerRuntime(
        client=SimpleNamespace(
            containers=SimpleNamespace(get=lambda _container_id: container)
        )
    )
    getattr(runtime, method_name)("immutable-id")
    assert calls == [expected_kwargs]


def test_container_lifecycle_maps_sdk_failure():
    def fail():
        raise DockerException("operation failed")

    runtime = DockerRuntime(
        client=SimpleNamespace(
            containers=SimpleNamespace(
                get=lambda _container_id: SimpleNamespace(start=fail)
            )
        )
    )
    with pytest.raises(DockerRuntimeError, match="operation failed"):
        runtime.start_container("immutable-id")
```

- [ ] **Step 2: 写 Compose 动作失败测试**

```python
@pytest.mark.parametrize(
    ("method_name", "subcommand"),
    [
        ("start_compose_project", "start"),
        ("stop_compose_project", "stop"),
        ("restart_compose_project", "restart"),
        ("remove_compose_project", "down"),
    ],
)
def test_compose_lifecycle_uses_project_name_empty_directory_and_no_shell(
    method_name, subcommand
):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")

    runtime = DockerRuntime(client=SimpleNamespace(), command_runner=runner)
    getattr(runtime, method_name)("mall")

    argv, kwargs = calls[0]
    assert argv == ["docker", "compose", "--project-name", "mall", subcommand]
    assert kwargs["shell"] is False
    assert Path(kwargs["cwd"]).name.startswith("docker-manage-compose-")
    assert not Path(kwargs["cwd"]).exists()


def test_compose_lifecycle_maps_nonzero_exit():
    runtime = DockerRuntime(
        client=SimpleNamespace(),
        command_runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"compose failed"
        ),
    )
    with pytest.raises(DockerRuntimeError, match="compose failed"):
        runtime.stop_compose_project("mall")
```

并补充 `from pathlib import Path`。

- [ ] **Step 3: 运行测试并确认 RED**

运行：

```bash
.venv/bin/python -m pytest -q tests/unit/test_docker_runtime.py -k lifecycle
```

预期：因八个生命周期方法不存在而失败。

- [ ] **Step 4: 实现最小 Docker SDK 动作**

在 `DockerRuntime` 中增加：

```python
    def start_container(self, container_id: str) -> None:
        self._container_action(container_id, "start")

    def stop_container(self, container_id: str) -> None:
        self._container_action(container_id, "stop")

    def restart_container(self, container_id: str) -> None:
        self._container_action(container_id, "restart")

    def remove_container(self, container_id: str) -> None:
        self._container_action(container_id, "remove", force=False, v=False)

    def _container_action(
        self, container_id: str, action: str, **kwargs: Any
    ) -> None:
        container = self.get_container(container_id)
        try:
            getattr(container, action)(**kwargs)
        except DockerException as exc:
            raise DockerRuntimeError(str(exc)) from exc
```

- [ ] **Step 5: 实现最小 Compose 固定动作**

增加 `import tempfile`，并在 `DockerRuntime` 中增加：

```python
    def start_compose_project(self, project_name: str) -> None:
        self._compose_project_action(project_name, "start")

    def stop_compose_project(self, project_name: str) -> None:
        self._compose_project_action(project_name, "stop")

    def restart_compose_project(self, project_name: str) -> None:
        self._compose_project_action(project_name, "restart")

    def remove_compose_project(self, project_name: str) -> None:
        self._compose_project_action(project_name, "down")

    def _compose_project_action(self, project_name: str, action: str) -> None:
        with tempfile.TemporaryDirectory(
            prefix="docker-manage-compose-"
        ) as directory:
            result = self._run(
                [
                    "docker",
                    "compose",
                    "--project-name",
                    project_name,
                    action,
                ],
                Path(directory),
            )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise DockerRuntimeError(
                detail or f"docker compose {action} exited {result.returncode}"
            )
```

- [ ] **Step 6: 运行单元测试并确认 GREEN**

运行：

```bash
.venv/bin/python -m pytest -q tests/unit/test_docker_runtime.py
```

预期：全部通过。

- [ ] **Step 7: 提交**

```bash
git add src/docker_manage_server/docker_runtime.py tests/unit/test_docker_runtime.py
git commit -m "feat: execute runtime lifecycle actions"
```

### Task 2: 统一归属与状态边界服务

**Files:**
- Create: `src/docker_manage_server/runtime_lifecycle.py`
- Create: `tests/unit/test_runtime_lifecycle.py`

**Interfaces:**
- Consumes: Task 1 的八个 `DockerRuntime` 动作方法。
- Consumes: `RuntimeInventoryService.require_standalone_container()` 和 `find_project()`。
- Produces: `RuntimeActionConflictError(RuntimeError)`。
- Produces: `RuntimeResourceNotFoundError(RuntimeError)`。
- Produces: `RuntimeLifecycleService` 八个同名资源动作方法；非删除返回动作后的资源快照，删除返回动作前的稳定身份字典。

- [ ] **Step 1: 创建失败测试及可变 FakeRuntime**

创建 `tests/unit/test_runtime_lifecycle.py`，定义含以下行为的 `FakeRuntime`：

```python
class FakeRuntime:
    def __init__(self, containers=(), projects=()):
        self.containers = [dict(item) for item in containers]
        self.projects = list(projects)
        self.calls = []

    def list_containers(self):
        return self.containers

    def list_compose_projects(self):
        return tuple(self.projects)

    def get_serialized_container(self, container_id):
        for item in self.containers:
            if item["id"] == container_id or item.get("name") == container_id:
                return dict(item)
        raise ContainerNotFoundError(container_id)

    def start_container(self, container_id):
        self.calls.append(("start_container", container_id))
        self._container(container_id)["running"] = True

    def stop_container(self, container_id):
        self.calls.append(("stop_container", container_id))
        self._container(container_id)["running"] = False

    def restart_container(self, container_id):
        self.calls.append(("restart_container", container_id))

    def remove_container(self, container_id):
        self.calls.append(("remove_container", container_id))
        self.containers.remove(self._container(container_id))

    def start_compose_project(self, name):
        self.calls.append(("start_compose_project", name))
        self._set_project_running(name, True)

    def stop_compose_project(self, name):
        self.calls.append(("stop_compose_project", name))
        self._set_project_running(name, False)

    def restart_compose_project(self, name):
        self.calls.append(("restart_compose_project", name))

    def remove_compose_project(self, name):
        self.calls.append(("remove_compose_project", name))
        self.projects = [item for item in self.projects if item.name != name]
        self.containers = [
            item
            for item in self.containers
            if item.get("labels", {}).get("com.docker.compose.project") != name
        ]
```

辅助方法 `_container()` 按完整 ID 返回同一字典，`_set_project_running()` 同时更新项目记录状态和项目内容器 `running` 字段。

- [ ] **Step 2: 写独立容器规则失败测试**

覆盖以下断言：

```python
def test_container_actions_use_validated_immutable_id_and_return_latest_state():
    runtime = FakeRuntime(
        containers=[container("immutable", name="alias", running=False)]
    )
    service = RuntimeLifecycleService(
        runtime, RuntimeInventoryService(runtime)
    )
    assert service.start_container("alias")["running"] is True
    assert runtime.calls == [("start_container", "immutable")]


@pytest.mark.parametrize(
    ("method", "running"),
    [
        ("start_container", True),
        ("stop_container", False),
        ("restart_container", False),
        ("remove_container", True),
    ],
)
def test_container_actions_reject_invalid_state(method, running):
    runtime = FakeRuntime(containers=[container("direct", running=running)])
    service = RuntimeLifecycleService(runtime, RuntimeInventoryService(runtime))
    with pytest.raises(RuntimeActionConflictError):
        getattr(service, method)("direct")
    assert runtime.calls == []


def test_container_actions_hide_compose_managed_container():
    runtime = FakeRuntime(
        containers=[container("compose-web", running=True, project="mall")]
    )
    service = RuntimeLifecycleService(runtime, RuntimeInventoryService(runtime))
    with pytest.raises(ContainerNotFoundError):
        service.stop_container("compose-web")
```

`container()` 辅助函数返回完整序列化字段，并支持 `name`、`running`、`project`。

- [ ] **Step 3: 写 Compose 规则失败测试**

```python
def test_compose_actions_validate_project_and_return_latest_state():
    runtime = FakeRuntime(
        projects=[ComposeProjectRecord("mall", "exited(1)", ())],
        containers=[container("mall-web", running=False, project="mall")],
    )
    service = RuntimeLifecycleService(runtime, RuntimeInventoryService(runtime))
    assert service.start_compose_project("mall").running is True
    assert runtime.calls == [("start_compose_project", "mall")]


@pytest.mark.parametrize(
    ("method", "status", "running"),
    [
        ("start_compose_project", "running(1)", True),
        ("stop_compose_project", "exited(1)", False),
        ("restart_compose_project", "exited(1)", False),
    ],
)
def test_compose_actions_reject_invalid_state(method, status, running):
    runtime = FakeRuntime(
        projects=[ComposeProjectRecord("mall", status, ())],
        containers=[container("mall-web", running=running, project="mall")],
    )
    service = RuntimeLifecycleService(runtime, RuntimeInventoryService(runtime))
    with pytest.raises(RuntimeActionConflictError):
        getattr(service, method)("mall")
    assert runtime.calls == []


def test_compose_delete_allows_running_and_missing_project_is_hidden():
    runtime = FakeRuntime(
        projects=[ComposeProjectRecord("mall", "running(1)", ())],
        containers=[container("mall-web", running=True, project="mall")],
    )
    service = RuntimeLifecycleService(runtime, RuntimeInventoryService(runtime))
    assert service.remove_compose_project("mall") == {"name": "mall"}
    with pytest.raises(RuntimeResourceNotFoundError):
        service.remove_compose_project("missing")
```

- [ ] **Step 4: 运行测试并确认 RED**

运行：

```bash
.venv/bin/python -m pytest -q tests/unit/test_runtime_lifecycle.py
```

预期：因 `runtime_lifecycle` 模块不存在而失败。

- [ ] **Step 5: 实现 `RuntimeLifecycleService`**

创建 `src/docker_manage_server/runtime_lifecycle.py`，包含：

```python
from __future__ import annotations

from typing import Any, Callable

from .docker_runtime import ContainerNotFoundError, DockerRuntime
from .runtime_inventory import ComposeProject, RuntimeInventoryService


class RuntimeActionConflictError(RuntimeError):
    pass


class RuntimeResourceNotFoundError(RuntimeError):
    pass


class RuntimeLifecycleService:
    def __init__(self, runtime: DockerRuntime, inventory: RuntimeInventoryService):
        self.runtime = runtime
        self.inventory = inventory

    def start_container(self, container_id: str) -> dict[str, Any]:
        return self._container_action(container_id, "start", expected_running=False)

    def stop_container(self, container_id: str) -> dict[str, Any]:
        return self._container_action(container_id, "stop", expected_running=True)

    def restart_container(self, container_id: str) -> dict[str, Any]:
        return self._container_action(container_id, "restart", expected_running=True)

    def remove_container(self, container_id: str) -> dict[str, Any]:
        item = self.inventory.require_standalone_container(container_id)
        if item.get("running"):
            raise RuntimeActionConflictError("运行中的独立容器必须先停止")
        immutable_id = str(item["id"])
        self.runtime.remove_container(immutable_id)
        return {"id": immutable_id, "name": item.get("name") or immutable_id}

    def _container_action(
        self, container_id: str, action: str, expected_running: bool
    ) -> dict[str, Any]:
        item = self.inventory.require_standalone_container(container_id)
        if bool(item.get("running")) is not expected_running:
            raise RuntimeActionConflictError("容器当前状态不允许此操作")
        immutable_id = str(item["id"])
        method: Callable[[str], None] = getattr(
            self.runtime, f"{action}_container"
        )
        method(immutable_id)
        return self.inventory.require_standalone_container(immutable_id)

    def start_compose_project(self, name: str) -> ComposeProject:
        return self._compose_action(name, "start", expected_running=False)

    def stop_compose_project(self, name: str) -> ComposeProject:
        return self._compose_action(name, "stop", expected_running=True)

    def restart_compose_project(self, name: str) -> ComposeProject:
        return self._compose_action(name, "restart", expected_running=True)

    def remove_compose_project(self, name: str) -> dict[str, str]:
        self._require_project(name)
        self.runtime.remove_compose_project(name)
        return {"name": name}

    def _compose_action(
        self, name: str, action: str, expected_running: bool
    ) -> ComposeProject:
        project = self._require_project(name)
        if project.running is not expected_running:
            raise RuntimeActionConflictError("Compose 项目当前状态不允许此操作")
        method: Callable[[str], None] = getattr(
            self.runtime, f"{action}_compose_project"
        )
        method(name)
        return self._require_project(name)

    def _require_project(self, name: str) -> ComposeProject:
        project = self.inventory.find_project(name)
        if project is None:
            raise RuntimeResourceNotFoundError(name)
        return project
```

- [ ] **Step 6: 运行测试并确认 GREEN**

运行：

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_runtime_lifecycle.py \
  tests/unit/test_runtime_inventory.py
```

预期：全部通过。

- [ ] **Step 7: 提交**

```bash
git add src/docker_manage_server/runtime_lifecycle.py \
  tests/unit/test_runtime_lifecycle.py
git commit -m "feat: enforce runtime lifecycle boundaries"
```

### Task 3: REST API 生命周期端点

**Files:**
- Modify: `src/docker_manage_server/api.py:49-295`
- Modify: `tests/api/test_container_api.py`

**Interfaces:**
- Consumes: Task 2 的 `RuntimeLifecycleService`、`RuntimeActionConflictError`、`RuntimeResourceNotFoundError`。
- Produces: 八个设计文档指定的 REST API 端点。
- Status mapping: 404 资源/归属错误，409 状态冲突，503 Docker/Compose 运行错误。

- [ ] **Step 1: 扩展 API FakeRuntime 并写独立容器失败测试**

让 `ContainerApiRuntime` 的 `get_serialized_container()` 支持运行中的独立容器别名 `direct`，返回完整 ID `sha256:direct-immutable-id`；增加动作调用记录并在 start/stop 时更新状态。

增加测试：

```python
def test_container_lifecycle_api_uses_immutable_id_and_explicit_methods(client):
    stopped = client.post("/api/containers/direct/stop")
    started = client.post("/api/containers/direct/start")
    restarted = client.post("/api/containers/direct/restart")
    stopped_again = client.post("/api/containers/direct/stop")
    removed = client.delete("/api/containers/direct")

    assert [response.status_code for response in (
        stopped, started, restarted, stopped_again, removed
    )] == [200, 200, 200, 200, 200]
    assert client.app.state.test_runtime.lifecycle_calls == [
        ("stop_container", "sha256:direct-immutable-id"),
        ("start_container", "sha256:direct-immutable-id"),
        ("restart_container", "sha256:direct-immutable-id"),
        ("stop_container", "sha256:direct-immutable-id"),
        ("remove_container", "sha256:direct-immutable-id"),
    ]
    assert removed.json()["deleted"] is True


def test_container_lifecycle_api_rejects_state_and_compose_ownership(client):
    assert client.delete("/api/containers/direct").status_code == 409
    assert client.post("/api/containers/stopped/stop").status_code == 409
    assert client.post("/api/containers/mall-web/stop").status_code == 404
```

- [ ] **Step 2: 写 Compose API 失败测试**

让 FakeRuntime 在项目动作后同步更新 `ComposeProjectRecord` 和 `mall-web` 状态，然后增加：

```python
def test_compose_lifecycle_api_uses_explicit_project_actions(client):
    stopped = client.post("/api/compose-projects/mall/stop")
    started = client.post("/api/compose-projects/mall/start")
    restarted = client.post("/api/compose-projects/mall/restart")
    removed = client.delete("/api/compose-projects/mall")

    assert [response.status_code for response in (
        stopped, started, restarted, removed
    )] == [200, 200, 200, 200]
    assert client.app.state.test_runtime.lifecycle_calls[-4:] == [
        ("stop_compose_project", "mall"),
        ("start_compose_project", "mall"),
        ("restart_compose_project", "mall"),
        ("remove_compose_project", "mall"),
    ]
    assert removed.json() == {"deleted": True, "name": "mall"}


def test_compose_lifecycle_api_rejects_state_and_missing_project(client):
    assert client.post("/api/compose-projects/mall/start").status_code == 409
    assert client.post("/api/compose-projects/missing/stop").status_code == 404
```

- [ ] **Step 3: 运行测试并确认 RED**

运行：

```bash
.venv/bin/python -m pytest -q tests/api/test_container_api.py -k lifecycle
```

预期：端点返回 404/405，测试失败。

- [ ] **Step 4: 在应用中创建共享生命周期服务**

在 `create_app()` 中：

```python
lifecycle = RuntimeLifecycleService(runtime, inventory)
app.state.lifecycle = lifecycle
```

将其传给 `create_web_router(...)`。导入 Task 2 的服务与异常类型。

- [ ] **Step 5: 增加 API 错误映射与八个明确端点**

定义私有执行辅助函数，将 `ContainerNotFoundError`/`RuntimeResourceNotFoundError` 映射为 404，`RuntimeActionConflictError` 映射为 409，`DockerRuntimeError` 映射为 503。

非删除容器端点返回 `{"item": lifecycle.<action>_container(container_id)}`；删除返回：

```python
identity = lifecycle.remove_container(container_id)
return {"deleted": True, **identity}
```

非删除 Compose 端点返回：

```python
project = lifecycle.<action>_compose_project(project_name)
return {
    "item": {
        "name": project.name,
        "status": project.status,
        "running": project.running,
        "container_count": project.container_count,
        "running_containers": project.running_containers,
    }
}
```

Compose 删除返回 `{"deleted": True, "name": project_name}`。

- [ ] **Step 6: 运行 API 测试并确认 GREEN**

运行：

```bash
.venv/bin/python -m pytest -q \
  tests/api/test_container_api.py \
  tests/web/test_security.py
```

预期：全部通过，包括现有同源安全测试。

- [ ] **Step 7: 提交**

```bash
git add src/docker_manage_server/api.py tests/api/test_container_api.py
git commit -m "feat: expose runtime lifecycle api"
```

### Task 4: Web 路由与详情页状态按钮

**Files:**
- Modify: `src/docker_manage_server/web.py:170-436`
- Modify: `src/docker_manage_server/templates/containers/detail.html`
- Modify: `src/docker_manage_server/templates/compose_projects/detail.html`
- Modify: `src/docker_manage_server/static/css/app.css`
- Modify: `tests/web/conftest.py`
- Modify: `tests/web/test_containers.py`
- Modify: `tests/web/test_compose_projects.py`
- Modify: `tests/web/test_security.py`

**Interfaces:**
- Consumes: Task 2 的共享 `RuntimeLifecycleService`。
- Produces: 八个设计文档指定的 HTML POST 路由。
- Produces: 启动无确认；停止、重启、删除使用 `data-confirm`。

- [ ] **Step 1: 扩展 WebFakeRuntime 并写独立容器按钮失败测试**

给 `WebFakeRuntime` 增加与 Task 3 相同的可变生命周期方法和 `lifecycle_calls`。

在 `tests/web/test_containers.py` 增加：

```python
def test_running_container_detail_shows_stop_restart_only(web_context):
    client, _store, _runtime = web_context
    response = client.get("/containers/abc123")
    assert 'action="/containers/abc123/stop"' in response.text
    assert 'action="/containers/abc123/restart"' in response.text
    assert 'action="/containers/abc123/start"' not in response.text
    assert 'action="/containers/abc123/delete"' not in response.text
    assert "确认停止此独立容器？" in response.text
    assert "确认重启此独立容器？" in response.text


def test_stopped_container_detail_shows_start_delete_only(web_context):
    client, _store, runtime = web_context
    runtime.containers[0]["running"] = False
    runtime.containers[0]["status"] = "exited"
    response = client.get("/containers/abc123")
    assert 'action="/containers/abc123/start"' in response.text
    assert 'action="/containers/abc123/delete"' in response.text
    assert 'action="/containers/abc123/stop"' not in response.text
    assert 'action="/containers/abc123/restart"' not in response.text
    assert 'action="/containers/abc123/start" data-confirm' not in response.text
    assert "确认删除此已停止的独立容器？" in response.text
```

- [ ] **Step 2: 写 Compose 按钮失败测试并更新旧边界断言**

将现有 Compose 详情测试中“完全没有启动/停止/重启/删除”改为只断言弹框内部没有生命周期表单，并增加：

```python
def test_running_compose_detail_shows_stop_restart_delete(web_context):
    client, _store, runtime = web_context
    runtime.compose_projects = (
        ComposeProjectRecord("mall", "running(1)", ()),
    )
    runtime.containers = [compose_container_fixture()]
    response = client.get("/compose-projects/mall")
    for action in ("stop", "restart", "delete"):
        assert f'action="/compose-projects/mall/{action}"' in response.text
    assert 'action="/compose-projects/mall/start"' not in response.text
    assert "保留命名卷和数据" in response.text


def test_stopped_compose_detail_shows_start_delete(web_context):
    client, _store, runtime = web_context
    runtime.compose_projects = (
        ComposeProjectRecord("mall", "exited(1)", ()),
    )
    runtime.containers = [compose_container_fixture(running=False)]
    response = client.get("/compose-projects/mall")
    assert 'action="/compose-projects/mall/start"' in response.text
    assert 'action="/compose-projects/mall/delete"' in response.text
    assert 'action="/compose-projects/mall/stop"' not in response.text
    assert 'action="/compose-projects/mall/restart"' not in response.text
```

- [ ] **Step 3: 写 Web 动作与错误映射失败测试**

分别覆盖：

```python
def test_container_web_actions_redirect_to_detail_or_runtime(web_context):
    client, _store, runtime = web_context
    assert client.post("/containers/abc123/stop").status_code == 303
    assert client.post("/containers/abc123/start").status_code == 303
    assert client.post("/containers/abc123/restart").status_code == 303
    assert client.post("/containers/abc123/stop").status_code == 303
    deleted = client.post("/containers/abc123/delete")
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/runtime"


def test_compose_web_actions_redirect_to_detail_or_runtime(web_context):
    client, _store, runtime = web_context
    runtime.compose_projects = (
        ComposeProjectRecord("mall", "running(1)", ()),
    )
    runtime.containers = [compose_container_fixture()]
    assert client.post("/compose-projects/mall/stop").status_code == 303
    assert client.post("/compose-projects/mall/start").status_code == 303
    assert client.post("/compose-projects/mall/restart").status_code == 303
    deleted = client.post("/compose-projects/mall/delete")
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/runtime"
```

再覆盖运行中容器删除得到 409、Compose 容器走独立动作得到 404、FakeRuntime 抛 `DockerRuntimeError("offline")` 得到 503。

- [ ] **Step 4: 运行测试并确认 RED**

运行：

```bash
.venv/bin/python -m pytest -q \
  tests/web/test_containers.py \
  tests/web/test_compose_projects.py \
  tests/web/test_security.py
```

预期：动作表单和 POST 路由不存在，测试失败。

- [ ] **Step 5: 在模板中增加按状态显示的动作表单**

独立容器详情页的 `.container-heading-actions` 中：

```html
{% if container.running %}
<form method="post" action="/containers/{{ container.item.id }}/stop" data-confirm="确认停止此独立容器？"><button class="button button-secondary" type="submit">停止</button></form>
<form method="post" action="/containers/{{ container.item.id }}/restart" data-confirm="确认重启此独立容器？"><button class="button button-secondary" type="submit">重启</button></form>
{% else %}
<form method="post" action="/containers/{{ container.item.id }}/start"><button class="button button-primary" type="submit">启动</button></form>
<form method="post" action="/containers/{{ container.item.id }}/delete" data-confirm="确认删除此已停止的独立容器？该操作不可恢复。"><button class="button button-danger" type="submit">删除</button></form>
{% endif %}
```

Compose 项目详情顶部使用同样结构；运行中显示 stop/restart/delete，停止时显示 start/delete。Compose 删除确认文本为：

```text
确认删除此 Compose 项目？将删除项目容器和网络，但保留命名卷和数据。
```

- [ ] **Step 6: 增加八个 Web POST 路由与错误映射**

更新 `create_web_router(..., lifecycle: RuntimeLifecycleService)`。使用私有 `_runtime_action_error()` 将：

```text
ContainerNotFoundError / RuntimeResourceNotFoundError -> 404
RuntimeActionConflictError -> 409
DockerRuntimeError -> 503
```

启动/停止/重启成功返回原详情的 `303`；删除成功返回 `/runtime` 的 `303`。每个公开路由固定调用一个明确服务方法，不接收 action 参数。

- [ ] **Step 7: 调整动作表单布局**

在 CSS 中增加：

```css
.container-heading-actions form { display: inline-flex; margin: 0; }
```

移动端继续复用现有 `.container-heading-actions` 换行规则。

- [ ] **Step 8: 补同源 POST 安全测试并运行 GREEN**

在 `tests/web/test_security.py` 增加跨域请求到 `/containers/abc123/stop` 和 `/compose-projects/mall/stop`，断言 403 且 FakeRuntime 没有动作记录。

运行：

```bash
.venv/bin/python -m pytest -q \
  tests/web/test_containers.py \
  tests/web/test_compose_projects.py \
  tests/web/test_security.py
```

预期：全部通过。

- [ ] **Step 9: 提交**

```bash
git add src/docker_manage_server/web.py \
  src/docker_manage_server/templates/containers/detail.html \
  src/docker_manage_server/templates/compose_projects/detail.html \
  src/docker_manage_server/static/css/app.css \
  tests/web/conftest.py tests/web/test_containers.py \
  tests/web/test_compose_projects.py tests/web/test_security.py
git commit -m "feat: manage runtime resources from detail pages"
```

### Task 5: 真实 Docker 集成、文档和完整验证

**Files:**
- Modify: `tests/integration/test_real_docker.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 1-4 的完整生命周期能力。
- Produces: 唯一临时独立容器的实机生命周期证据；不改变本机已有 Compose 项目。

- [ ] **Step 1: 写安全的真实 Docker 生命周期测试**

在 `tests/integration/test_real_docker.py` 增加 `uuid4`、Docker `ImageNotFound`/`NotFound` 导入，并实现：

```python
@pytest.mark.skipif(not docker_available, reason="Docker daemon unavailable")
def test_real_standalone_container_lifecycle():
    from uuid import uuid4

    from docker.errors import ImageNotFound, NotFound

    from docker_manage_server.docker_runtime import DockerRuntime

    runtime = DockerRuntime()
    image = "alpine:3.21"
    try:
        runtime.client.images.get(image)
    except ImageNotFound:
        pytest.skip(f"local test image unavailable: {image}")

    name = f"docker-manage-lifecycle-{uuid4().hex}"
    created = runtime.client.containers.create(
        image,
        ["sh", "-c", "while true; do sleep 1; done"],
        name=name,
        labels={"docker-manage.test": "runtime-lifecycle"},
    )
    try:
        runtime.start_container(created.id)
        assert runtime.get_serialized_container(created.id)["running"] is True
        runtime.restart_container(created.id)
        assert runtime.get_serialized_container(created.id)["running"] is True
        runtime.stop_container(created.id)
        assert runtime.get_serialized_container(created.id)["running"] is False
        runtime.remove_container(created.id)
        with pytest.raises(NotFound):
            runtime.client.containers.get(created.id)
    finally:
        try:
            runtime.client.containers.get(created.id).remove(force=True, v=True)
        except NotFound:
            pass
```

只允许清理带本测试唯一名称与标签的容器，不执行任何 Compose 变更命令。

- [ ] **Step 2: 运行真实集成测试**

运行：

```bash
.venv/bin/python -m pytest -q \
  tests/integration/test_real_docker.py::test_real_standalone_container_lifecycle
```

预期：本机已有 `alpine:3.21` 时通过，否则明确 SKIP；不得拉取镜像。

- [ ] **Step 3: 更新 README 操作说明**

在运行管理说明后增加：

```markdown
独立容器详情页提供启动、停止、重启和删除操作。运行中的独立容器必须先停止才能删除，不会执行强制删除。

Compose 项目详情页以项目为单位执行 `docker compose start`、`stop`、`restart` 和 `down`。删除项目会删除项目容器与网络，但不使用 `--volumes`，因此保留命名卷和数据。Compose 项目内容器不提供单独生命周期操作。
```

- [ ] **Step 4: 浏览器只对临时假数据验证 UI**

启动现有 `WebFakeRuntime` 页面，验证：

```text
运行中独立容器：停止、重启；无删除
已停止独立容器：启动、删除
运行中 Compose：停止、重启、删除
已停止 Compose：启动、删除
停止/重启/删除弹出准确确认文本
删除成功跳转 /runtime
Compose 容器弹框内没有生命周期操作
```

浏览器验证不得连接或改变本机真实 Compose 项目。

- [ ] **Step 5: 执行完整验证**

运行：

```bash
git diff --check
.venv/bin/python -m compileall -q src tests
node --check src/docker_manage_server/static/js/app.js
.venv/bin/python -m pytest -q
DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server \
  DOCKER_MANAGE_SERVER_PORT=6308 \
  docker compose config --quiet
git status --short
```

预期：无 diff 错误、编译与 JavaScript 语法成功、所有测试通过或仅安全条件不满足的真实 Docker 测试 SKIP、Compose 配置有效、仅存在 Task 5 的计划内修改。

- [ ] **Step 6: 提交**

```bash
git add README.md tests/integration/test_real_docker.py
git commit -m "docs: describe runtime lifecycle operations"
```

- [ ] **Step 7: 请求独立代码复核并修正阻塞问题**

复核范围从本计划前提交 `abea117` 到最新 HEAD，重点检查：归属越权、TOCTOU、状态绕过、Compose 命令注入、删除卷、强制删除、API/Web 错误映射和测试真实性。修复所有 Critical/Important 问题后重新执行 Step 5。
