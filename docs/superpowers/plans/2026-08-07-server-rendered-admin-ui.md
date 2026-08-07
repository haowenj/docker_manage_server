# Docker Manage Server 服务端渲染管理台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Docker Manage Server 增加明亮侧边栏风格的 Jinja2 服务端渲染管理台，覆盖部署归档上传/审核/部署、任务状态、容器详情、日志和在线终端，同时保持现有 API 兼容。

**Architecture:** `create_app()` 继续创建唯一的存储、部署和 Docker Runtime 实例，并把它们注入新增的 Web Router；HTML 首屏和普通操作由 Jinja2 与 Post/Redirect/Get 完成，只有部署状态、日志和 WebSocket 终端使用本地 JavaScript。模板、CSS、脚本、xterm.js 和许可证作为 Python 包数据随 Docker 镜像与离线归档交付。

**Tech Stack:** Python 3.11+、FastAPI、Starlette、Jinja2 3.1、Pydantic 2、pytest、原生 JavaScript、`@xterm/xterm 6.0.0`、`@xterm/addon-fit 0.11.0`、Docker Compose。

## Global Constraints

- 保留全部现有 `/api/*` 路由、响应字段和状态码；任务时间字段不得出现在现有 API payload 中。
- 首版不增加鉴权、数据库、容器生命周期操作、Node.js 运行时或前端构建步骤。
- 页面资源不得引用公网 CDN，必须随 Python wheel、Docker 镜像和离线归档交付。
- 所有用户或 Docker 提供的文本必须经过 Jinja2 自动转义或使用 DOM `textContent` 写入。
- 普通操作使用服务端表单和 303 重定向；JavaScript 仅用于确认、状态轮询、日志刷新和终端。
- 宿主机端口保持 `6308`，容器端口保持 `8000`，数据路径保持 `/data/docker-manage-server`，Docker Socket 保持 `/var/run/docker.sock`。
- 每个生产代码行为必须先有失败测试；不得在未观察 RED 的情况下写实现。
- `.docker-manage/`、`.superpowers/` 和现有未跟踪 `uv.lock` 不得提交。

---

### Task 1: 为部署任务增加可排序时间与列表读取

**Files:**
- Modify: `src/docker_manage_server/models.py`
- Modify: `src/docker_manage_server/storage.py`
- Modify: `src/docker_manage_server/api.py`
- Modify: `tests/unit/test_storage.py`
- Modify: `tests/api/test_deployment_api.py`

**Interfaces:**
- `DeploymentTask.created_at: datetime | None`
- `DeploymentTask.updated_at: datetime | None`
- `TaskStore(data_dir: Path, clock: Callable[[], datetime] | None = None)`
- `TaskStore.list() -> tuple[DeploymentTask, ...]`
- `_task_payload()` 继续返回旧 API 字段，不包含 `created_at` 和 `updated_at`。

- [ ] **Step 1: 写任务时间与列表的失败测试**

在 `tests/unit/test_storage.py` 添加：

```python
from datetime import datetime, timedelta, timezone
import json


def test_list_returns_tasks_by_latest_update(tmp_path: Path):
    moments = iter(
        (
            datetime(2026, 8, 7, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 7, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 8, 7, 1, 2, tzinfo=timezone.utc),
        )
    )
    store = TaskStore(tmp_path, clock=lambda: next(moments))
    first = store.create("first", "first.tar.gz")
    second = store.create("second", "second.tar.gz")
    first.status = TaskStatus.PENDING_REVIEW
    store.save(first)

    assert [task.task_id for task in store.list()] == ["first", "second"]
    assert first.created_at == datetime(2026, 8, 7, 1, 0, tzinfo=timezone.utc)
    assert first.updated_at == datetime(2026, 8, 7, 1, 2, tzinfo=timezone.utc)
    assert second.created_at == second.updated_at


def test_old_task_json_uses_state_file_mtime(tmp_path: Path):
    store = TaskStore(tmp_path)
    task = store.create("legacy", "legacy.tar.gz")
    state_path = tmp_path / "tasks/legacy.json"
    body = json.loads(state_path.read_text(encoding="utf-8"))
    body.pop("created_at")
    body.pop("updated_at")
    state_path.write_text(json.dumps(body), encoding="utf-8")

    loaded = store.get("legacy")

    assert loaded.created_at is not None
    assert loaded.updated_at == loaded.created_at
```

在 `tests/api/test_deployment_api.py` 的上传测试中添加：

```python
    assert "created_at" not in response.json()
    assert "updated_at" not in response.json()
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
uv run pytest -q tests/unit/test_storage.py::test_list_returns_tasks_by_latest_update tests/unit/test_storage.py::test_old_task_json_uses_state_file_mtime tests/api/test_deployment_api.py::test_upload_returns_pending_review
```

Expected: FAIL，原因分别为 `TaskStore` 不接受 `clock`、没有 `list()`，任务模型没有时间字段。

- [ ] **Step 3: 实现时间字段与列表**

在 `models.py` 添加：

```python
from datetime import datetime


class DeploymentTask(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    task_id: str
    status: TaskStatus
    original_filename: str
    package_dir: Path
    extracted_dir: Path
    deployment_dir: Path | None = None
    app_name: str | None = None
    error: str | None = None
    command_output: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
```

在 `storage.py` 使用以下接口：

```python
from collections.abc import Callable
from datetime import datetime, timezone


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

    def _write(self, task: DeploymentTask) -> DeploymentTask:
        self._validate_task_id(task.task_id)
        destination = self._state_path(task.task_id)
        partial = destination.with_name(f".{destination.name}.partial")
        partial.write_text(task.model_dump_json(indent=2), encoding="utf-8")
        partial.replace(destination)
        return task
```

在 `api.py` 保持 API payload 不变：

```python
def _task_payload(task: DeploymentTask) -> dict[str, Any]:
    return task.model_dump(
        mode="json",
        exclude={"created_at", "updated_at"},
    )
```

- [ ] **Step 4: 运行 Task 1 测试与回归测试**

Run:

```bash
uv run pytest -q tests/unit/test_storage.py tests/api/test_deployment_api.py
```

Expected: PASS，且现有 API payload 不增加时间字段。

- [ ] **Step 5: 提交 Task 1**

```bash
git add src/docker_manage_server/models.py src/docker_manage_server/storage.py src/docker_manage_server/api.py tests/unit/test_storage.py tests/api/test_deployment_api.py
git commit -m "feat: list deployment task history"
```

---

### Task 2: 建立可打包的 Jinja2 Web 基础与首页

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/docker_manage_server/api.py`
- Create: `src/docker_manage_server/web.py`
- Create: `src/docker_manage_server/web_views.py`
- Create: `src/docker_manage_server/templates/base.html`
- Create: `src/docker_manage_server/templates/dashboard.html`
- Create: `src/docker_manage_server/templates/components/status_badge.html`
- Create: `src/docker_manage_server/static/css/app.css`
- Create: `src/docker_manage_server/static/js/app.js`
- Create: `tests/web/conftest.py`
- Create: `tests/web/test_dashboard.py`

**Interfaces:**
- `create_web_router(store: TaskStore, deployment: DeploymentService, runtime: DockerRuntime) -> APIRouter`
- `task_view(task: DeploymentTask) -> dict[str, Any]`
- `container_view(item: Mapping[str, Any]) -> dict[str, Any]`
- `dashboard_metrics(tasks, containers) -> dict[str, int]`
- `GET /` 返回服务端渲染首页；Docker 失败时仍返回 200。

- [ ] **Step 1: 写首页与包资源失败测试**

创建 `tests/web/conftest.py`：

```python
from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from docker_manage_server.api import create_app
from docker_manage_server.config import Settings
from docker_manage_server.storage import TaskStore


class WebFakeRuntime:
    def __init__(self):
        self.available = True
        self.containers = [
            {
                "id": "abc123",
                "short_id": "abc123",
                "name": "server",
                "image": "demo/server:1",
                "status": "running",
                "running": True,
                "ports": {"8000/tcp": [{"HostPort": "6308"}]},
                "labels": {},
                "mounts": [],
                "networks": {},
                "raw_attrs": {"State": {"Running": True}},
            }
        ]

    def ping(self):
        return self.available

    def list_containers(self):
        if not self.available:
            from docker_manage_server.docker_runtime import DockerRuntimeError

            raise DockerRuntimeError("daemon offline")
        return self.containers

    def get_container(self, container_id):
        return SimpleNamespace(id=container_id, attrs={})

    def load_image(self, image_tar, cwd):
        return SimpleNamespace(returncode=0, stdout=b"loaded\n", stderr=b"")

    def compose_up(self, cwd):
        return SimpleNamespace(returncode=0, stdout=b"started\n", stderr=b"")


@pytest.fixture
def web_context(tmp_path):
    runtime = WebFakeRuntime()
    store = TaskStore(tmp_path)
    app = create_app(
        settings=Settings(data_dir=tmp_path),
        store=store,
        runtime=runtime,
    )
    return TestClient(app), store, runtime
```

创建 `tests/web/test_dashboard.py`：

```python
from importlib.resources import files

from docker_manage_server.models import TaskStatus


def test_dashboard_renders_tasks_and_containers(web_context):
    client, store, _runtime = web_context
    task = store.create("task-1", "demo.tar.gz")
    task.status = TaskStatus.PENDING_REVIEW
    task.app_name = "demo"
    store.save(task)

    response = client.get("/")

    assert response.status_code == 200
    assert "Docker Manage" in response.text
    assert "demo" in response.text
    assert "server" in response.text
    assert 'href="/deployments"' in response.text
    assert 'href="/containers"' in response.text


def test_dashboard_degrades_when_docker_is_offline(web_context):
    client, _store, runtime = web_context
    runtime.available = False

    response = client.get("/")

    assert response.status_code == 200
    assert "Docker daemon 不可用" in response.text


def test_template_and_static_resources_are_package_data():
    package = files("docker_manage_server")
    assert package.joinpath("templates/base.html").is_file()
    assert package.joinpath("static/css/app.css").is_file()
    assert package.joinpath("static/js/app.js").is_file()
```

- [ ] **Step 2: 运行首页测试并确认 RED**

Run:

```bash
uv run pytest -q tests/web/test_dashboard.py
```

Expected: FAIL，根路径仍为 404，模板和静态资源不存在。

- [ ] **Step 3: 添加依赖与 package data**

在 `pyproject.toml` 的运行依赖加入：

```toml
"jinja2>=3.1,<4",
```

并添加：

```toml
[tool.setuptools.package-data]
docker_manage_server = [
    "templates/*.html",
    "templates/components/*.html",
    "templates/deployments/*.html",
    "templates/containers/*.html",
    "templates/errors/*.html",
    "static/css/*.css",
    "static/js/*.js",
    "static/vendor/xterm/*.mjs",
    "static/vendor/xterm/*.css",
    "static/vendor/xterm/*.txt",
    "static/vendor/xterm/*.json",
]
```

- [ ] **Step 4: 实现视图辅助函数和首页路由**

`web_views.py` 提供以下稳定结构：

```python
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .models import DeploymentTask, TaskStatus

STATUS_LABELS = {
    TaskStatus.UPLOADED: "已上传",
    TaskStatus.EXTRACTING: "正在解压",
    TaskStatus.PENDING_REVIEW: "待审核",
    TaskStatus.DEPLOYING: "部署中",
    TaskStatus.DEPLOYED: "已部署",
    TaskStatus.DISCARDED: "已丢弃",
    TaskStatus.FAILED: "失败",
}


def task_view(task: DeploymentTask) -> dict[str, Any]:
    return {
        "task": task,
        "status_value": task.status.value,
        "status_label": STATUS_LABELS[task.status],
        "created_at": _format_time(task.created_at),
        "updated_at": _format_time(task.updated_at),
    }


def container_view(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "item": item,
        "name": item.get("name") or item.get("short_id") or "未知容器",
        "image": item.get("image") or "—",
        "running": bool(item.get("running")),
        "status_label": "运行中" if item.get("running") else str(item.get("status") or "已停止"),
        "ports_text": _format_ports(item.get("ports")),
    }


def dashboard_metrics(
    tasks: Sequence[DeploymentTask],
    containers: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    return {
        "containers": len(containers),
        "running": sum(bool(item.get("running")) for item in containers),
        "pending_review": sum(task.status is TaskStatus.PENDING_REVIEW for task in tasks),
        "deployed": sum(task.status is TaskStatus.DEPLOYED for task in tasks),
        "failed": sum(task.status is TaskStatus.FAILED for task in tasks),
    }


def _format_time(value: datetime | None) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S") if value else "—"


def _format_ports(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "—"
    rendered = []
    for container_port, bindings in sorted(value.items()):
        if not bindings:
            rendered.append(str(container_port))
            continue
        hosts = ", ".join(
            f"{item.get('HostIp') or '0.0.0.0'}:{item.get('HostPort')}"
            for item in bindings
            if isinstance(item, dict)
        )
        rendered.append(f"{hosts} → {container_port}")
    return "; ".join(rendered)
```

创建 `web.py`，先写入完整导入与模板对象：

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .deployment import DeploymentService
from .docker_runtime import DockerRuntime, DockerRuntimeError
from .storage import TaskStore
from .web_views import container_view, dashboard_metrics, task_view

PACKAGE_ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))
```

随后创建 Router；首页捕获 `DockerRuntimeError` 并降级：

```python
def create_web_router(
    store: TaskStore,
    deployment: DeploymentService,
    runtime: DockerRuntime,
) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        tasks = store.list()
        docker_error = None
        try:
            containers = runtime.list_containers()
        except DockerRuntimeError as exc:
            containers = []
            docker_error = str(exc)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "page_title": "运行概览",
                "active_nav": "dashboard",
                "tasks": [task_view(task) for task in tasks[:5]],
                "containers": [container_view(item) for item in containers[:5]],
                "metrics": dashboard_metrics(tasks, containers),
                "docker_error": docker_error,
            },
        )

    return router
```

在 `api.py` 中挂载并注入同一实例：

```python
from fastapi.staticfiles import StaticFiles
from .web import PACKAGE_ROOT, create_web_router

app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")
app.include_router(create_web_router(store, deployment, runtime))
```

- [ ] **Step 5: 创建基础模板和明亮首页**

`base.html` 使用以下完整骨架；不得添加内联脚本：

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ page_title }} · Docker Manage</title>
  <link rel="stylesheet" href="{{ url_for('static', path='/css/app.css') }}">
  {% block head %}{% endblock %}
</head>
<body>
  <div class="trust-warning">未启用鉴权，仅限受信任内网</div>
  <div class="app-shell">
    <aside class="sidebar">
      <a class="brand" href="/">Docker Manage</a>
      <nav class="sidebar-nav" aria-label="主导航">
        <a class="{{ 'active' if active_nav == 'dashboard' else '' }}" href="/">运行概览</a>
        <a class="{{ 'active' if active_nav == 'deployments' else '' }}" href="/deployments">部署任务</a>
        <a class="{{ 'active' if active_nav == 'containers' else '' }}" href="/containers">容器管理</a>
      </nav>
    </aside>
    <main class="main-content">
      <header class="page-header"><h1>{{ page_title }}</h1></header>
      {% block content %}{% endblock %}
    </main>
  </div>
  <script defer src="{{ url_for('static', path='/js/app.js') }}"></script>
  {% block scripts %}{% endblock %}
</body>
</html>
```

`components/status_badge.html`：

```html
<span class="status status-{{ status_value }}">{{ status_label }}</span>
```

`dashboard.html`：

```html
{% extends "base.html" %}
{% block content %}
<div class="page-actions">
  <p class="page-intro">查看部署任务与当前 Docker 容器状态。</p>
  <a class="button button-primary" href="/deployments">上传部署归档</a>
</div>
{% if docker_error %}
<div class="alert alert-danger">Docker daemon 不可用：{{ docker_error }}</div>
{% endif %}
<section class="metric-grid" aria-label="运行统计">
  <article class="metric"><span>全部容器</span><strong>{{ metrics.containers }}</strong></article>
  <article class="metric"><span>运行中</span><strong>{{ metrics.running }}</strong></article>
  <article class="metric"><span>待审核</span><strong>{{ metrics.pending_review }}</strong></article>
  <article class="metric"><span>已部署</span><strong>{{ metrics.deployed }}</strong></article>
  <article class="metric"><span>失败任务</span><strong>{{ metrics.failed }}</strong></article>
</section>
<div class="content-grid">
  <section class="panel">
    <div class="panel-heading"><h2>最近部署任务</h2><a href="/deployments">查看全部</a></div>
    <div class="table-scroll">
      <table>
        <thead><tr><th>应用</th><th>状态</th><th>更新时间</th></tr></thead>
        <tbody>
        {% for entry in tasks %}
          <tr>
            <td><a href="/deployments/{{ entry.task.task_id }}">{{ entry.task.app_name or entry.task.original_filename }}</a></td>
            <td>{% with status_value=entry.status_value, status_label=entry.status_label %}{% include "components/status_badge.html" %}{% endwith %}</td>
            <td>{{ entry.updated_at }}</td>
          </tr>
        {% else %}
          <tr><td colspan="3" class="empty-cell">暂无部署任务</td></tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </section>
  <section class="panel">
    <div class="panel-heading"><h2>容器</h2><a href="/containers">查看全部</a></div>
    <div class="table-scroll">
      <table>
        <thead><tr><th>名称</th><th>镜像</th><th>状态</th></tr></thead>
        <tbody>
        {% for container in containers %}
          <tr>
            <td><a href="/containers/{{ container.item.id }}">{{ container.name }}</a></td>
            <td>{{ container.image }}</td>
            <td><span class="status {{ 'status-running' if container.running else 'status-stopped' }}">{{ container.status_label }}</span></td>
          </tr>
        {% else %}
          <tr><td colspan="3" class="empty-cell">暂无容器</td></tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </section>
</div>
{% endblock %}
```

`app.css` 首版写入以下完整样式，后续任务只在明确指出的位置追加终端样式：

```css
:root {
  --bg: #f8fafc;
  --surface: #ffffff;
  --border: #e2e8f0;
  --text: #172033;
  --muted: #64748b;
  --primary: #2563eb;
  --success: #15803d;
  --warning: #b45309;
  --danger: #b91c1c;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

* { box-sizing: border-box; }
body { margin: 0; color: var(--text); background: var(--bg); }
a { color: var(--primary); text-decoration: none; }
a:hover { text-decoration: underline; }
.trust-warning { min-height: 36px; padding: 8px 20px; color: #854d0e; background: #fef9c3; border-bottom: 1px solid #fde68a; text-align: center; font-size: 14px; }
.app-shell { display: grid; grid-template-columns: 240px minmax(0, 1fr); min-height: calc(100vh - 36px); }
.sidebar { position: sticky; top: 0; align-self: start; min-height: calc(100vh - 36px); padding: 28px 20px; background: var(--surface); border-right: 1px solid var(--border); }
.brand { display: block; margin: 0 12px 28px; color: var(--text); font-size: 21px; font-weight: 750; }
.sidebar-nav { display: grid; gap: 6px; }
.sidebar-nav a { padding: 11px 12px; color: #475569; border-radius: 8px; font-weight: 600; }
.sidebar-nav a:hover, .sidebar-nav a.active { color: #1d4ed8; background: #eff6ff; text-decoration: none; }
.main-content { min-width: 0; padding: 32px; }
.page-header h1 { margin: 0 0 22px; font-size: 28px; }
.page-actions, .panel-heading, .button-row, .toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.page-actions { margin-bottom: 22px; }
.page-intro, .muted { color: var(--muted); }
.metric-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 16px; margin-bottom: 22px; }
.metric, .panel { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 1px 2px rgb(15 23 42 / 4%); }
.metric { padding: 18px; }
.metric span { display: block; color: var(--muted); font-size: 14px; }
.metric strong { display: block; margin-top: 8px; font-size: 28px; }
.content-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; }
.panel { min-width: 0; padding: 20px; margin-bottom: 20px; }
.panel h2 { margin: 0; font-size: 18px; }
.panel-heading { margin-bottom: 16px; }
.table-scroll, pre { overflow: auto; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 12px 10px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }
th { color: var(--muted); font-size: 13px; font-weight: 650; }
tbody tr:last-child td { border-bottom: 0; }
.empty-cell { padding: 28px 10px; color: var(--muted); text-align: center; }
.button { display: inline-flex; align-items: center; justify-content: center; min-height: 38px; padding: 8px 14px; border: 1px solid transparent; border-radius: 8px; font: inherit; font-weight: 650; cursor: pointer; }
.button:hover { text-decoration: none; }
.button-primary { color: #fff; background: var(--primary); }
.button-secondary { color: #334155; background: #fff; border-color: #cbd5e1; }
.button-danger { color: #fff; background: var(--danger); }
.button:disabled { cursor: wait; opacity: .6; }
.status { display: inline-flex; padding: 4px 8px; border-radius: 999px; font-size: 13px; font-weight: 650; }
.status-running, .status-deployed { color: #166534; background: #dcfce7; }
.status-pending_review, .status-uploaded, .status-extracting, .status-deploying { color: #92400e; background: #fef3c7; }
.status-failed, .status-stopped { color: #991b1b; background: #fee2e2; }
.status-discarded { color: #475569; background: #e2e8f0; }
.alert { margin-bottom: 18px; padding: 12px 14px; border: 1px solid; border-radius: 8px; }
.alert-danger { color: #991b1b; background: #fef2f2; border-color: #fecaca; }
.alert-warning { color: #854d0e; background: #fffbeb; border-color: #fde68a; }
.upload-form { display: flex; align-items: end; gap: 12px; flex-wrap: wrap; }
label { color: #334155; font-size: 14px; font-weight: 600; }
input[type="file"], select { min-height: 38px; padding: 7px 10px; color: var(--text); background: #fff; border: 1px solid #cbd5e1; border-radius: 8px; }
.definition-grid { display: grid; grid-template-columns: 160px minmax(0, 1fr); gap: 10px 18px; margin: 0; }
.definition-grid dt { color: var(--muted); }
.definition-grid dd { min-width: 0; margin: 0; overflow-wrap: anywhere; }
pre { max-width: 100%; margin: 12px 0 0; padding: 14px; color: #1e293b; background: #f8fafc; border: 1px solid var(--border); border-radius: 8px; font: 13px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre; }
.file-list { max-height: 320px; overflow: auto; margin: 0; padding-left: 22px; }
.stack { display: grid; gap: 12px; }
.error-panel { max-width: 760px; }
.terminal-viewport { min-height: 360px; padding: 8px; background: #fff; border: 1px solid var(--border); border-radius: 8px; }

@media (max-width: 760px) {
  .app-shell { display: block; }
  .sidebar { position: static; width: auto; min-height: 0; padding: 18px 16px; border-right: 0; border-bottom: 1px solid var(--border); }
  .brand { margin: 0 0 14px; }
  .sidebar-nav { display: flex; overflow-x: auto; }
  .sidebar-nav a { white-space: nowrap; }
  .main-content { padding: 22px 16px; }
  .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .content-grid { grid-template-columns: 1fr; }
  .table-scroll { overflow-x: auto; }
  .page-actions, .panel-heading, .toolbar { align-items: flex-start; flex-direction: column; }
  .definition-grid { grid-template-columns: 1fr; gap: 4px; }
  .definition-grid dd { margin-bottom: 10px; }
}
```

`app.js` 首个版本只绑定带确认信息的表单：

```javascript
document.querySelectorAll("form[data-confirm]").forEach((element) => {
  element.addEventListener("submit", (event) => {
    if (!window.confirm(element.dataset.confirm)) event.preventDefault();
  });
});
```

- [ ] **Step 6: 运行首页、静态资源和完整测试**

Run:

```bash
uv run pytest -q tests/web/test_dashboard.py
uv run pytest -q
```

Expected: 首页测试通过，现有测试无回归。

- [ ] **Step 7: 提交 Task 2**

```bash
git add pyproject.toml src/docker_manage_server/api.py src/docker_manage_server/web.py src/docker_manage_server/web_views.py src/docker_manage_server/templates src/docker_manage_server/static/css/app.css src/docker_manage_server/static/js/app.js tests/web
git commit -m "feat: add server-rendered dashboard"
```

---

### Task 3: 增加部署任务上传、审核、部署与丢弃页面

**Files:**
- Modify: `src/docker_manage_server/web.py`
- Create: `src/docker_manage_server/templates/deployments/list.html`
- Create: `src/docker_manage_server/templates/deployments/detail.html`
- Create: `src/docker_manage_server/templates/errors/error.html`
- Modify: `src/docker_manage_server/static/js/app.js`
- Modify: `tests/conftest.py`
- Create: `tests/web/test_deployments.py`

**Interfaces:**
- `GET /deployments`
- `POST /deployments`
- `GET /deployments/{task_id}`
- `POST /deployments/{task_id}/deploy`
- `POST /deployments/{task_id}/discard`

- [ ] **Step 1: 写部署页面失败测试**

先对 `tests/conftest.py` 的 `_write_archive()` 做以下精确改动：

```diff
 def _write_archive(
     root: Path,
     archive_path: Path,
     *,
     include_files: bool = False,
     app_name: str = "demo",
+    env_text: str = "SECRET=value\n",
 ) -> Path:
     payload = root / "payload"
     payload.mkdir(parents=True)
-    (payload / ".env").write_text("SECRET=value\n", encoding="utf-8")
+    (payload / ".env").write_text(env_text, encoding="utf-8")
```

再添加 fixture：

```python


@pytest.fixture
def html_injection_archive(tmp_path: Path) -> Path:
    return _write_archive(
        tmp_path / "html-injection",
        tmp_path / "html-injection.tar.gz",
        env_text="VALUE=<script>alert('x')</script>\n",
    )
```

然后创建 `tests/web/test_deployments.py`，复用 `web_context`、`valid_archive` 和新 fixture：

```python
from docker_manage_server.models import TaskStatus


def test_upload_redirects_to_server_rendered_review(web_context, valid_archive):
    client, _store, _runtime = web_context
    response = client.post(
        "/deployments",
        files={"file": ("demo.tar.gz", valid_archive.read_bytes(), "application/gzip")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    detail = client.get(response.headers["location"])
    assert detail.status_code == 200
    assert "SECRET=value" in detail.text
    assert "services:" in detail.text
    assert "确认部署" in detail.text
    assert f'data-task-poll-url="/api/deployment-tasks/{response.headers["location"].rsplit("/", 1)[1]}"' in detail.text


def test_review_escapes_archive_text(web_context, html_injection_archive):
    client, _store, _runtime = web_context
    response = client.post(
        "/deployments",
        files={
            "file": (
                "demo.tar.gz",
                html_injection_archive.read_bytes(),
                "application/gzip",
            )
        },
        follow_redirects=False,
    )
    detail = client.get(response.headers["location"])
    assert "<script>" not in detail.text
    assert "&lt;script&gt;" in detail.text


def test_invalid_upload_renders_422(web_context):
    client, _store, _runtime = web_context
    response = client.post(
        "/deployments",
        files={"file": ("broken.tar.gz", b"broken", "application/gzip")},
    )
    assert response.status_code == 422
    assert "归档校验失败" in response.text


def test_deploy_and_discard_use_303(web_context, valid_archive):
    client, store, _runtime = web_context
    uploaded = client.post(
        "/deployments",
        files={"file": ("demo.tar.gz", valid_archive.read_bytes(), "application/gzip")},
        follow_redirects=False,
    )
    task_id = uploaded.headers["location"].rsplit("/", 1)[1]
    deploy = client.post(f"/deployments/{task_id}/deploy", follow_redirects=False)
    assert deploy.status_code == 303
    blocked = client.post(f"/deployments/{task_id}/deploy")
    assert blocked.status_code == 409
    assert "任务当前状态不允许部署" in blocked.text

    second = store.create("discard-me", "discard.tar.gz")
    second.status = TaskStatus.PENDING_REVIEW
    store.save(second)
    discard = client.post("/deployments/discard-me/discard", follow_redirects=False)
    assert discard.status_code == 303
    assert discard.headers["location"] == "/deployments"

    missing = client.get("/deployments/not-found")
    assert missing.status_code == 404
    assert "找不到部署任务" in missing.text
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
uv run pytest -q tests/web/test_deployments.py
```

Expected: FAIL，所有 Web 部署路由尚不存在。

- [ ] **Step 3: 实现部署 Web 路由**

在 `web.py` 增加以下导入：

```python
from uuid import uuid4

from fastapi import BackgroundTasks, File, UploadFile
from fastapi.responses import RedirectResponse

from .artifacts import list_files
from .deployment import DeploymentStateError
from .models import TaskStatus
```

先在模块级增加共享辅助函数：

```python
def _deployment_list_context(store: TaskStore) -> dict[str, Any]:
    return {
        "page_title": "部署任务",
        "active_nav": "deployments",
        "tasks": [task_view(task) for task in store.list()],
        "upload_error": None,
    }


def _web_error(
    request: Request,
    status_code: int,
    title: str,
    detail: str,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="errors/error.html",
        context={
            "page_title": title,
            "active_nav": None,
            "status_code": status_code,
            "error_title": title,
            "error_detail": detail,
        },
        status_code=status_code,
    )


def _read_optional(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
```

然后把以下闭包路由原样插到 `create_web_router()` 现有 `return router` 之前；缩进属于代码，不得移到模块级：

```python
    @router.get("/deployments", response_class=HTMLResponse)
    def deployments_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="deployments/list.html",
            context=_deployment_list_context(store),
        )

    @router.post("/deployments", response_class=HTMLResponse)
    def upload_archive(request: Request, file: UploadFile = File(...)):
        task_id = uuid4().hex
        try:
            deployment.upload(task_id, file.file, file.filename or "archive.tar.gz")
        except Exception as exc:
            return templates.TemplateResponse(
                request=request,
                name="deployments/list.html",
                context={
                    **_deployment_list_context(store),
                    "upload_error": f"归档校验失败：{exc}",
                },
                status_code=422,
            )
        return RedirectResponse(f"/deployments/{task_id}", status_code=303)

    @router.get("/deployments/{task_id}", response_class=HTMLResponse)
    def deployment_detail(request: Request, task_id: str):
        try:
            task = store.get(task_id)
        except KeyError:
            return _web_error(request, 404, "找不到部署任务", task_id)
        files = list_files(task.extracted_dir) if task.extracted_dir.is_dir() else ()
        env_text = _read_optional(task.extracted_dir / ".env")
        compose_text = _read_optional(task.extracted_dir / "compose.yaml")
        return templates.TemplateResponse(
            request=request,
            name="deployments/detail.html",
            context={
                "page_title": task.app_name or task.original_filename,
                "active_nav": "deployments",
                "task_view": task_view(task),
                "files": files,
                "env_text": env_text,
                "compose_text": compose_text,
            },
        )

    @router.post("/deployments/{task_id}/deploy")
    def deploy_archive(
        request: Request,
        task_id: str,
        background_tasks: BackgroundTasks,
    ):
        try:
            task = store.get(task_id)
        except KeyError:
            return _web_error(request, 404, "找不到部署任务", task_id)
        if task.status is not TaskStatus.PENDING_REVIEW:
            return _web_error(request, 409, "任务当前状态不允许部署", task.status.value)
        background_tasks.add_task(deployment.deploy, task_id)
        return RedirectResponse(f"/deployments/{task_id}", status_code=303)

    @router.post("/deployments/{task_id}/discard")
    def discard_archive(request: Request, task_id: str):
        try:
            deployment.discard(task_id)
        except KeyError:
            return _web_error(request, 404, "找不到部署任务", task_id)
        except DeploymentStateError as exc:
            return _web_error(request, 409, "任务当前状态不允许丢弃", str(exc))
        return RedirectResponse("/deployments", status_code=303)
```

不得用 `HTTPException` 处理页面错误，否则会返回 JSON。`errors/error.html` 使用以下内容：

```html
{% extends "base.html" %}
{% block content %}
<section class="panel error-panel">
  <p class="status status-failed">HTTP {{ status_code }}</p>
  <h2>{{ error_title }}</h2>
  <pre>{{ error_detail }}</pre>
  <p><a class="button button-secondary" href="/">返回管理台</a></p>
</section>
{% endblock %}
```

- [ ] **Step 4: 实现部署模板和轮询**

创建 `deployments/list.html`：

```html
{% extends "base.html" %}
{% block content %}
{% if upload_error %}<div class="alert alert-danger">{{ upload_error }}</div>{% endif %}
<section class="panel">
  <div class="panel-heading"><h2>上传离线部署归档</h2></div>
  <form class="upload-form" action="/deployments" method="post" enctype="multipart/form-data">
    <label>选择 .tar.gz 文件 <input type="file" name="file" accept=".tar.gz,application/gzip" required></label>
    <button class="button button-primary" type="submit">上传并审核</button>
  </form>
</section>
<section class="panel">
  <div class="panel-heading"><h2>部署任务</h2><span class="muted">按最近更新时间排序</span></div>
  <div class="table-scroll">
    <table>
      <thead><tr><th>应用</th><th>归档</th><th>状态</th><th>创建时间</th><th>更新时间</th><th></th></tr></thead>
      <tbody>
      {% for entry in tasks %}
        <tr>
          <td>{{ entry.task.app_name or "—" }}</td>
          <td>{{ entry.task.original_filename }}</td>
          <td>{% with status_value=entry.status_value, status_label=entry.status_label %}{% include "components/status_badge.html" %}{% endwith %}</td>
          <td>{{ entry.created_at }}</td>
          <td>{{ entry.updated_at }}</td>
          <td><a href="/deployments/{{ entry.task.task_id }}">详情</a></td>
        </tr>
      {% else %}
        <tr><td colspan="6" class="empty-cell">暂无部署任务，请先上传离线部署归档。</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</section>
{% endblock %}
```

创建 `deployments/detail.html`：

```html
{% extends "base.html" %}
{% block content %}
{% set task = task_view.task %}
<section class="panel" data-task-poll-url="/api/deployment-tasks/{{ task.task_id }}" data-task-status="{{ task_view.status_value }}">
  <div class="panel-heading">
    <h2>{{ task.app_name or task.original_filename }}</h2>
    <span class="status status-{{ task_view.status_value }}" data-task-status-label>{{ task_view.status_label }}</span>
  </div>
  <dl class="definition-grid">
    <dt>任务 ID</dt><dd>{{ task.task_id }}</dd>
    <dt>归档文件</dt><dd>{{ task.original_filename }}</dd>
    <dt>创建时间</dt><dd>{{ task_view.created_at }}</dd>
    <dt>更新时间</dt><dd>{{ task_view.updated_at }}</dd>
    <dt>部署目录</dt><dd>{{ task.deployment_dir or "—" }}</dd>
  </dl>
  {% if task_view.status_value == "pending_review" %}
  <div class="button-row">
    <form method="post" action="/deployments/{{ task.task_id }}/deploy" data-confirm="确认部署此归档？">
      <button class="button button-primary" type="submit">确认部署</button>
    </form>
    <form method="post" action="/deployments/{{ task.task_id }}/discard" data-confirm="确认丢弃此任务？该操作不可恢复。">
      <button class="button button-danger" type="submit">丢弃任务</button>
    </form>
  </div>
  {% endif %}
</section>
{% if task.error %}<section class="panel"><h2>错误</h2><pre data-task-error>{{ task.error }}</pre></section>{% endif %}
<section class="panel">
  <div class="panel-heading"><h2>归档文件</h2><span class="muted">{{ files|length }} 项</span></div>
  <ul class="file-list">
  {% for file in files %}<li>{{ file.path }} <span class="muted">{{ file.kind }} · {{ file.size }} B</span></li>
  {% else %}<li class="muted">没有可审核的文件。</li>{% endfor %}
  </ul>
</section>
<section class="panel">
  <div class="alert alert-warning">.env 可能包含密码、令牌等敏感信息，请勿向无关人员展示。</div>
  <h2>.env</h2><pre>{{ env_text or "文件不存在" }}</pre>
</section>
<section class="panel"><h2>compose.yaml</h2><pre>{{ compose_text or "文件不存在" }}</pre></section>
<section class="panel"><h2>命令输出</h2><pre data-task-output>{{ task.command_output or "暂无命令输出" }}</pre></section>
{% endblock %}
```

把 `app.js` 替换为以下完整版本：

```javascript
document.querySelectorAll("form[data-confirm]").forEach((element) => {
  element.addEventListener("submit", (event) => {
    if (!window.confirm(element.dataset.confirm)) event.preventDefault();
  });
});

const taskRoot = document.querySelector("[data-task-poll-url]");
if (taskRoot && taskRoot.dataset.taskStatus === "deploying") {
  const labels = {
    deploying: "部署中",
    deployed: "已部署",
    failed: "失败",
  };
  const status = taskRoot.querySelector("[data-task-status-label]");
  const output = document.querySelector("[data-task-output]");
  const error = document.querySelector("[data-task-error]");

  const timer = window.setInterval(async () => {
    try {
      const response = await fetch(taskRoot.dataset.taskPollUrl, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const task = await response.json();
      status.textContent = labels[task.status] || task.status;
      taskRoot.dataset.taskStatus = task.status;
      if (output) output.textContent = task.command_output || "暂无命令输出";
      if (error) error.textContent = task.error || "";
      if (task.status === "deployed" || task.status === "failed") {
        window.clearInterval(timer);
        window.location.reload();
      }
    } catch (_error) {
      status.textContent = "状态刷新失败";
    }
  }, 2000);
}
```

- [ ] **Step 5: 运行部署 Web 测试和 API 回归**

Run:

```bash
uv run pytest -q tests/web/test_deployments.py tests/api/test_deployment_api.py tests/unit/test_deployment.py
```

Expected: PASS。

- [ ] **Step 6: 提交 Task 3**

```bash
git add src/docker_manage_server/web.py src/docker_manage_server/templates/deployments src/docker_manage_server/templates/errors src/docker_manage_server/static/js/app.js tests/conftest.py tests/web/test_deployments.py
git commit -m "feat: manage deployments from web ui"
```

---

### Task 4: 增加容器列表、详情与日志面板

**Files:**
- Modify: `src/docker_manage_server/web.py`
- Create: `src/docker_manage_server/templates/containers/list.html`
- Create: `src/docker_manage_server/templates/containers/detail.html`
- Modify: `src/docker_manage_server/static/js/app.js`
- Create: `tests/web/test_containers.py`

**Interfaces:**
- `GET /containers`
- `GET /containers/{container_id}`
- 日志面板继续调用 `GET /api/containers/{container_id}/logs`。

- [ ] **Step 1: 写容器页面失败测试**

创建 `tests/web/test_containers.py`：

```python
from docker_manage_server.docker_runtime import ContainerNotFoundError, DockerRuntimeError


def test_container_list_is_server_rendered(web_context):
    client, _store, _runtime = web_context
    response = client.get("/containers")
    assert response.status_code == 200
    assert "server" in response.text
    assert "demo/server:1" in response.text
    assert "0.0.0.0:6308" in response.text


def test_container_detail_exposes_logs_and_terminal_targets(web_context):
    client, _store, runtime = web_context
    runtime.get_container = lambda _container_id: type(
        "Container",
        (),
        {
            "id": "abc123",
            "short_id": "abc123",
            "name": "server",
            "status": "running",
            "ports": {"8000/tcp": [{"HostPort": "6308"}]},
            "labels": {"app": "demo"},
            "image": type("Image", (), {"tags": ["demo/server:1"]})(),
            "attrs": {
                "State": {"Running": True},
                "Config": {"Image": "demo/server:1"},
                "Mounts": [],
                "NetworkSettings": {"Networks": {}},
            },
        },
    )()
    response = client.get("/containers/abc123")
    assert response.status_code == 200
    assert 'data-log-url="/api/containers/abc123/logs"' in response.text
    assert 'data-terminal-url="/api/containers/abc123/terminal"' in response.text


def test_container_pages_render_html_errors(web_context):
    client, _store, runtime = web_context
    runtime.get_container = lambda _value: (_ for _ in ()).throw(ContainerNotFoundError("x"))
    missing = client.get("/containers/missing")
    assert missing.status_code == 404
    assert "找不到容器" in missing.text

    runtime.list_containers = lambda: (_ for _ in ()).throw(DockerRuntimeError("offline"))
    unavailable = client.get("/containers")
    assert unavailable.status_code == 503
    assert "offline" in unavailable.text
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
uv run pytest -q tests/web/test_containers.py
```

Expected: FAIL，容器 Web 路由不存在。

- [ ] **Step 3: 实现容器 Web 路由**

在 `web.py` 的 Docker Runtime 导入中加入 `ContainerNotFoundError`，然后把以下闭包路由插到 `create_web_router()` 的 `return router` 之前：

```python
    @router.get("/containers", response_class=HTMLResponse)
    def containers_page(request: Request):
        try:
            items = runtime.list_containers()
        except DockerRuntimeError as exc:
            return _web_error(request, 503, "Docker daemon 不可用", str(exc))
        return templates.TemplateResponse(
            request=request,
            name="containers/list.html",
            context={
                "page_title": "容器管理",
                "active_nav": "containers",
                "containers": [container_view(item) for item in items],
            },
        )

    @router.get("/containers/{container_id}", response_class=HTMLResponse)
    def container_detail(request: Request, container_id: str):
        try:
            item = DockerRuntime._serialize_container(runtime.get_container(container_id))
        except ContainerNotFoundError:
            return _web_error(request, 404, "找不到容器", container_id)
        except DockerRuntimeError as exc:
            return _web_error(request, 503, "Docker daemon 不可用", str(exc))
        return templates.TemplateResponse(
            request=request,
            name="containers/detail.html",
            context={
                "page_title": item.get("name") or item.get("short_id"),
                "active_nav": "containers",
                "container": container_view(item),
            },
        )
```

- [ ] **Step 4: 实现模板和安全日志刷新**

创建 `containers/list.html`：

```html
{% extends "base.html" %}
{% block content %}
<section class="panel">
  <div class="panel-heading"><h2>Docker 容器</h2><span class="muted">{{ containers|length }} 个</span></div>
  <div class="table-scroll">
    <table>
      <thead><tr><th>名称</th><th>镜像</th><th>状态</th><th>端口</th><th>短 ID</th></tr></thead>
      <tbody>
      {% for container in containers %}
        <tr>
          <td><a href="/containers/{{ container.item.id }}">{{ container.name }}</a></td>
          <td>{{ container.image }}</td>
          <td><span class="status {{ 'status-running' if container.running else 'status-stopped' }}">{{ container.status_label }}</span></td>
          <td>{{ container.ports_text }}</td>
          <td><code>{{ container.item.short_id }}</code></td>
        </tr>
      {% else %}
        <tr><td colspan="5" class="empty-cell">暂无容器。</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</section>
{% endblock %}
```

创建 `containers/detail.html`：

```html
{% extends "base.html" %}
{% block content %}
<section class="panel">
  <div class="panel-heading">
    <h2>{{ container.name }}</h2>
    <span class="status {{ 'status-running' if container.running else 'status-stopped' }}">{{ container.status_label }}</span>
  </div>
  <dl class="definition-grid">
    <dt>容器 ID</dt><dd>{{ container.item.id }}</dd>
    <dt>镜像</dt><dd>{{ container.image }}</dd>
    <dt>命令</dt><dd>{{ container.item.command or "—" }}</dd>
    <dt>端口</dt><dd>{{ container.ports_text }}</dd>
    <dt>创建时间</dt><dd>{{ container.item.created or "—" }}</dd>
  </dl>
</section>
<div class="content-grid">
  <section class="panel"><h2>挂载</h2><pre>{% for mount in container.item.mounts %}{{ mount.Source or "—" }} → {{ mount.Destination or "—" }} ({{ mount.Type or "unknown" }})
{% else %}暂无挂载{% endfor %}</pre></section>
  <section class="panel"><h2>网络</h2><pre>{% for name, network in container.item.networks.items() %}{{ name }}  {{ network.IPAddress or "—" }}
{% else %}暂无网络{% endfor %}</pre></section>
</div>
<section class="panel"><h2>标签</h2><pre>{% for key, value in container.item.labels|dictsort %}{{ key }}={{ value }}
{% else %}暂无标签{% endfor %}</pre></section>
<section class="panel log-panel" data-log-viewer data-log-url="/api/containers/{{ container.item.id }}/logs">
  <div class="panel-heading"><h2>日志</h2></div>
  <div class="toolbar">
    <label>行数 <select data-log-tail><option>100</option><option>500</option><option value="all">全部</option></select></label>
    <label><input type="checkbox" data-log-timestamps> 显示时间戳</label>
    <button class="button button-secondary" type="button" data-log-refresh>刷新日志</button>
  </div>
  <pre data-log-output>点击“刷新日志”读取日志。</pre>
</section>
<section class="panel terminal-panel" data-terminal-url="/api/containers/{{ container.item.id }}/terminal" data-terminal-command="/bin/sh">
  <div class="panel-heading"><h2>在线终端</h2><span data-terminal-status>尚未连接</span></div>
  <button class="button button-primary" type="button" data-terminal-connect>连接终端</button>
  <div class="terminal-viewport" data-terminal-viewport></div>
</section>
{% endblock %}
```

在 `app.js` 末尾追加以下代码；所有日志只写入 `textContent`：

```javascript
document.querySelectorAll("[data-log-viewer]").forEach((viewer) => {
  const tail = viewer.querySelector("[data-log-tail]");
  const timestamps = viewer.querySelector("[data-log-timestamps]");
  const refresh = viewer.querySelector("[data-log-refresh]");
  const output = viewer.querySelector("[data-log-output]");

  refresh.addEventListener("click", async () => {
    refresh.disabled = true;
    output.textContent = "正在读取日志…";
    const query = new URLSearchParams({
      tail: tail.value,
      timestamps: String(timestamps.checked),
    });
    try {
      const response = await fetch(`${viewer.dataset.logUrl}?${query}`, {
        headers: { Accept: "text/plain" },
      });
      const text = await response.text();
      if (!response.ok) throw new Error(text || `HTTP ${response.status}`);
      output.textContent = text || "日志为空。";
    } catch (error) {
      output.textContent = `日志读取失败：${error.message}`;
    } finally {
      refresh.disabled = false;
    }
  });
});
```

- [ ] **Step 5: 运行容器页面与 API 回归测试**

Run:

```bash
uv run pytest -q tests/web/test_containers.py tests/api/test_container_api.py tests/unit/test_docker_runtime.py
```

Expected: PASS。

- [ ] **Step 6: 提交 Task 4**

```bash
git add src/docker_manage_server/web.py src/docker_manage_server/templates/containers src/docker_manage_server/static/js/app.js tests/web/test_containers.py
git commit -m "feat: add container web views"
```

---

### Task 5: 离线交付 xterm.js 并连接现有终端 WebSocket

**Files:**
- Create: `src/docker_manage_server/static/js/terminal.js`
- Create: `src/docker_manage_server/static/vendor/xterm/xterm.mjs`
- Create: `src/docker_manage_server/static/vendor/xterm/addon-fit.mjs`
- Create: `src/docker_manage_server/static/vendor/xterm/xterm.css`
- Create: `src/docker_manage_server/static/vendor/xterm/LICENSE-xterm.txt`
- Create: `src/docker_manage_server/static/vendor/xterm/LICENSE-addon-fit.txt`
- Create: `src/docker_manage_server/static/vendor/xterm/versions.json`
- Modify: `src/docker_manage_server/templates/containers/detail.html`
- Modify: `tests/web/test_containers.py`
- Create: `tests/web/test_package_resources.py`

**Interfaces:**
- `terminal.js` 从本站 ES modules 导入 `Terminal` 与 `FitAddon`。
- 终端元素使用 `data-terminal-url`、`data-terminal-command`、`data-terminal-connect` 和 `data-terminal-status`。

- [ ] **Step 1: 写终端资源失败测试**

```python
from importlib.resources import files
import json


def test_vendored_terminal_resources_are_present_and_pinned():
    vendor = files("docker_manage_server").joinpath("static/vendor/xterm")
    for name in (
        "xterm.mjs",
        "addon-fit.mjs",
        "xterm.css",
        "LICENSE-xterm.txt",
        "LICENSE-addon-fit.txt",
    ):
        assert vendor.joinpath(name).is_file()
    versions = json.loads(vendor.joinpath("versions.json").read_text(encoding="utf-8"))
    assert versions == {
        "@xterm/xterm": "6.0.0",
        "@xterm/addon-fit": "0.11.0",
    }
```

在容器详情测试中增加：

```python
    assert 'type="module"' in response.text
    assert "/static/js/terminal.js" in response.text
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
uv run pytest -q tests/web/test_package_resources.py tests/web/test_containers.py::test_container_detail_exposes_logs_and_terminal_targets
```

Expected: FAIL，vendor 文件和 `terminal.js` 不存在。

- [ ] **Step 3: 从固定 npm 包机械复制官方分发文件**

使用临时目录下载并解压；不要提交 `.tgz`、source map、TypeScript 源码或 npm metadata：

```bash
vendor_tmp=$(mktemp -d /tmp/docker-manage-xterm.XXXXXX)
npm pack --pack-destination "$vendor_tmp" @xterm/xterm@6.0.0
npm pack --pack-destination "$vendor_tmp" @xterm/addon-fit@0.11.0
mkdir -p "$vendor_tmp/xterm" "$vendor_tmp/fit" src/docker_manage_server/static/vendor/xterm
tar -xzf "$vendor_tmp/xterm-xterm-6.0.0.tgz" -C "$vendor_tmp/xterm"
tar -xzf "$vendor_tmp/xterm-addon-fit-0.11.0.tgz" -C "$vendor_tmp/fit"
install -m 0644 "$vendor_tmp/xterm/package/lib/xterm.mjs" src/docker_manage_server/static/vendor/xterm/xterm.mjs
install -m 0644 "$vendor_tmp/xterm/package/css/xterm.css" src/docker_manage_server/static/vendor/xterm/xterm.css
install -m 0644 "$vendor_tmp/xterm/package/LICENSE" src/docker_manage_server/static/vendor/xterm/LICENSE-xterm.txt
install -m 0644 "$vendor_tmp/fit/package/lib/addon-fit.mjs" src/docker_manage_server/static/vendor/xterm/addon-fit.mjs
install -m 0644 "$vendor_tmp/fit/package/LICENSE" src/docker_manage_server/static/vendor/xterm/LICENSE-addon-fit.txt
```

通过 `apply_patch` 创建：

```json
{
  "@xterm/xterm": "6.0.0",
  "@xterm/addon-fit": "0.11.0"
}
```

- [ ] **Step 4: 实现终端浏览器连接**

创建完整的 `terminal.js`：

```javascript
import { Terminal } from "/static/vendor/xterm/xterm.mjs";
import { FitAddon } from "/static/vendor/xterm/addon-fit.mjs";

const encoder = new TextEncoder();

function terminalUrl(root) {
  const url = new URL(root.dataset.terminalUrl, window.location.href);
  url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("command", root.dataset.terminalCommand || "/bin/sh");
  return url;
}

function connect(root, viewport, status, button) {
  button.disabled = true;
  status.textContent = "正在连接…";
  viewport.replaceChildren();

  const terminal = new Terminal({
    convertEol: true,
    cursorBlink: true,
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    fontSize: 14,
    theme: {
      background: "#ffffff",
      foreground: "#1e293b",
      cursor: "#2563eb",
      selectionBackground: "#bfdbfe",
    },
  });
  const fitAddon = new FitAddon();
  terminal.loadAddon(fitAddon);
  terminal.open(viewport);
  fitAddon.fit();

  const socket = new WebSocket(terminalUrl(root));
  socket.binaryType = "arraybuffer";

  const sendResize = () => {
    if (socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({
      type: "resize",
      width: terminal.cols,
      height: terminal.rows,
    }));
  };

  const resizeObserver = new ResizeObserver(() => {
    fitAddon.fit();
    sendResize();
  });
  resizeObserver.observe(viewport);

  socket.addEventListener("open", () => {
    status.textContent = "已连接";
    fitAddon.fit();
    sendResize();
    terminal.focus();
  });

  socket.addEventListener("message", async (event) => {
    if (event.data instanceof ArrayBuffer) {
      terminal.write(new Uint8Array(event.data));
      return;
    }
    if (event.data instanceof Blob) {
      terminal.write(new Uint8Array(await event.data.arrayBuffer()));
      return;
    }
    terminal.write(String(event.data));
  });

  terminal.onData((data) => {
    if (socket.readyState === WebSocket.OPEN) {
      socket.send(encoder.encode(data));
    }
  });

  socket.addEventListener("error", () => {
    status.textContent = "终端连接错误";
  });

  socket.addEventListener("close", (event) => {
    resizeObserver.disconnect();
    status.textContent = event.code === 1000
      ? "连接已关闭"
      : `连接已关闭（代码 ${event.code}）`;
    button.disabled = false;
  });

  window.addEventListener("beforeunload", () => socket.close(), { once: true });
}

const root = document.querySelector("[data-terminal-url]");
if (root) {
  const button = root.querySelector("[data-terminal-connect]");
  const viewport = root.querySelector("[data-terminal-viewport]");
  const status = root.querySelector("[data-terminal-status]");
  button.addEventListener("click", () => connect(root, viewport, status, button));
}
```

在 `containers/detail.html` 的 `{% extends %}` 后加入本地样式块，并在文件末尾加入本地模块块：

```html
{% block head %}<link rel="stylesheet" href="/static/vendor/xterm/xterm.css">{% endblock %}

{% block scripts %}<script type="module" src="/static/js/terminal.js"></script>{% endblock %}
```

- [ ] **Step 5: 运行资源、终端 I/O 和完整测试**

Run:

```bash
uv run pytest -q tests/web/test_package_resources.py tests/web/test_containers.py tests/unit/test_terminal_io.py tests/api/test_container_api.py
uv run pytest -q
```

Expected: PASS。

- [ ] **Step 6: 提交 Task 5**

```bash
git add src/docker_manage_server/static/js/terminal.js src/docker_manage_server/static/vendor/xterm src/docker_manage_server/templates/containers/detail.html tests/web/test_package_resources.py tests/web/test_containers.py
git commit -m "feat: add offline web terminal"
```

---

### Task 6: 增加 CSP 与 HTTP/WebSocket 同源边界

**Files:**
- Create: `src/docker_manage_server/security.py`
- Modify: `src/docker_manage_server/api.py`
- Create: `tests/web/test_security.py`

**Interfaces:**
- `origin_matches_host(origin: str | None, host: str | None) -> bool`
- HTTP unsafe 请求：Origin 缺失时兼容；存在且 host 不匹配时返回 403。
- WebSocket：Origin 存在且不匹配时以 code `1008` 拒绝。
- 所有响应包含 CSP、`X-Content-Type-Options: nosniff` 和 `Referrer-Policy: no-referrer`。

- [ ] **Step 1: 写安全失败测试**

创建 `tests/web/test_security.py`：

```python
import pytest
from starlette.websockets import WebSocketDisconnect


def test_responses_include_security_headers(web_context):
    client, _store, _runtime = web_context
    response = client.get("/")
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_cross_origin_form_is_rejected_but_missing_origin_is_allowed(web_context):
    client, _store, _runtime = web_context
    rejected = client.post(
        "/deployments",
        headers={"Origin": "https://evil.example"},
        files={"file": ("x.tar.gz", b"broken", "application/gzip")},
    )
    assert rejected.status_code == 403

    compatible = client.post(
        "/api/deployment-tasks",
        files={"file": ("x.tar.gz", b"broken", "application/gzip")},
    )
    assert compatible.status_code == 422

    same_origin = client.post(
        "/deployments",
        headers={"Origin": "http://testserver"},
        files={"file": ("x.tar.gz", b"broken", "application/gzip")},
    )
    assert same_origin.status_code == 422


def test_cross_origin_terminal_is_rejected(web_context):
    client, _store, _runtime = web_context
    with pytest.raises(WebSocketDisconnect) as captured:
        with client.websocket_connect(
            "/api/containers/abc/terminal",
            headers={"Origin": "https://evil.example"},
        ):
            pass
    assert captured.value.code == 1008
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
uv run pytest -q tests/web/test_security.py
```

Expected: FAIL，尚无安全头或同源检查。

- [ ] **Step 3: 实现同源辅助函数与 HTTP middleware**

`security.py`：

```python
from urllib.parse import urlsplit

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSP = (
    "default-src 'self'; "
    "script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


def origin_matches_host(origin: str | None, host: str | None) -> bool:
    if origin is None:
        return True
    if not host:
        return False
    parsed = urlsplit(origin)
    return parsed.scheme in {"http", "https"} and parsed.netloc.casefold() == host.casefold()
```

`style-src` 仅为 xterm 运行时计算的行列尺寸保留 `'unsafe-inline'`；`script-src` 必须继续只允许 `'self'`，模板中不增加内联脚本。

在 `create_app()` 加 middleware：跨源 `/api/` 返回 JSON 403，其余返回纯文本 403；正常响应统一补安全头。不要把 CSP 写成模板 meta，确保 API 与静态资源也有响应头。

在 `api.py` 增加导入：

```python
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse

from .security import CSP, UNSAFE_METHODS, origin_matches_host
```

在 `create_app()` 中、路由注册之前增加：

```python
    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        host = request.headers.get("host")
        if (
            request.method in UNSAFE_METHODS
            and not origin_matches_host(origin, host)
        ):
            if request.url.path.startswith("/api/"):
                response = JSONResponse(
                    {"detail": "cross-origin request rejected"},
                    status_code=403,
                )
            else:
                response = PlainTextResponse(
                    "cross-origin request rejected",
                    status_code=403,
                )
        else:
            response = await call_next(request)

        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
```

- [ ] **Step 4: 在接受 WebSocket 前校验 Origin**

```python
    @app.websocket("/api/containers/{container_id}/terminal")
    async def terminal(websocket: WebSocket, container_id: str, command: str = "/bin/sh"):
        if not origin_matches_host(
            websocket.headers.get("origin"),
            websocket.headers.get("host"),
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
```

把这段 guard 插入现有 `await websocket.accept()` 之前；guard 之后从 `await websocket.accept()` 到 reader/writer 清理的现有终端实现逐行保持不变。

- [ ] **Step 5: 运行安全测试和全部 API 回归**

Run:

```bash
uv run pytest -q tests/web/test_security.py tests/api
uv run pytest -q
```

Expected: PASS；无 Origin 的 API 测试保持通过。

- [ ] **Step 6: 提交 Task 6**

```bash
git add src/docker_manage_server/security.py src/docker_manage_server/api.py tests/web/test_security.py
git commit -m "feat: secure web management surface"
```

---

### Task 7: 完成视觉、文档、wheel、Docker 与离线归档验证

**Files:**
- Modify: `README.md`
- Modify: `.gitignore`
- Test: all tests

**Interfaces:**
- `/` 为最终入口。
- `.superpowers/` 加入 `.gitignore`；已存在 mockup 不提交。
- 最终 Docker 与离线包参数保持 Global Constraints 中的固定值。

- [ ] **Step 1: 验证明亮主题静态契约**

Run:

```bash
rg -n -- '--bg: #f8fafc|--surface: #ffffff|--primary: #2563eb|grid-template-columns: 240px|@media \(max-width: 760px\)' src/docker_manage_server/static/css/app.css
rg -n '未启用鉴权，仅限受信任内网|sidebar-nav|table-scroll' src/docker_manage_server/templates/base.html src/docker_manage_server/templates
```

Expected: 第一条命令命中五项明亮/响应式规则；第二条命令命中信任警告、导航和可横向滚动表格结构。若任一缺失，回到引入该模板或样式的任务修正并重跑该任务测试，不在此处做未规划的视觉补丁。

- [ ] **Step 2: 更新 README 与 gitignore**

README 启动章节增加：

```markdown
启动后访问：

- 管理台：`http://服务器IP:${DOCKER_MANAGE_SERVER_PORT}/`
- API 文档：`http://服务器IP:${DOCKER_MANAGE_SERVER_PORT}/docs`

管理台首版不含鉴权，可上传并部署归档、读取 `.env`、查看日志并打开容器终端，只能暴露在受信任内网。
```

`.gitignore` 增加：

```gitignore
/.superpowers/
```

- [ ] **Step 3: 运行全量测试、wheel 检查和静态扫描**

Run:

```bash
uv run pytest -q
uv build
uv run python -m zipfile -l dist/docker_manage_server-0.1.0-py3-none-any.whl
rg -n "https?://" src/docker_manage_server/templates src/docker_manage_server/static --glob '!**/vendor/**'
rg --pcre2 -n '<script(?![^>]*\bsrc=)[^>]*>' src/docker_manage_server/templates
```

Expected:

- 全部 pytest PASS；真实 Docker 条件测试只允许按既有条件跳过。
- wheel 列表包含全部模板、CSS、JS、xterm modules 和许可证。
- 静态扫描不发现公网资源或内联脚本。

- [ ] **Step 4: 本地运行并做浏览器视觉验收**

Run:

```bash
DATA_DIR="$PWD/data" uv run uvicorn docker_manage_server.main:app --host 127.0.0.1 --port 6308
```

使用浏览器逐页检查 `/`、`/deployments`、任务详情、`/containers` 和容器详情；验证桌面与窄屏、日志刷新和终端。不得把本地测试上传到生产数据目录。

验收尺寸和结果固定为：

- 桌面 `1440×900`：白色左侧栏固定、浅灰工作区、白色卡片、蓝色主按钮；
- 窄屏 `390×844`：侧栏转顶部横向导航、统计卡片两列、表格与 `<pre>` 可横向滚动；
- 所有页面顶部可见“未启用鉴权，仅限受信任内网”；
- 状态均有中文文本；空任务、空容器、Docker 离线、上传失败和部署失败均有明确反馈；
- 有运行容器时，日志刷新显示纯文本，终端为白底亮色且可输入、调整尺寸；
- 浏览器控制台无 CSP、模板资源 404 或 JavaScript 异常。

- [ ] **Step 5: 构建并验证 Docker 镜像**

Run:

```bash
DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server DOCKER_MANAGE_SERVER_PORT=6308 docker compose config --format json
docker build --platform linux/amd64 -t docker-manage-server:web-ui .
docker run --rm --entrypoint python docker-manage-server:web-ui -c "from importlib.resources import files; p=files('docker_manage_server'); assert p.joinpath('templates/base.html').is_file(); assert p.joinpath('static/js/terminal.js').is_file()"
```

Expected: Compose 渲染 `6308:8000/tcp` 和同路径数据 mount；镜像为 `linux/amd64` 且包含 Web 资源。

- [ ] **Step 6: 提交 Task 7**

```bash
git add .gitignore README.md
git commit -m "docs: document web management console"
```

- [ ] **Step 7: 使用 package-docker-app 重新生成离线包**

严格按技能执行新的 inspect → answers → plan 流程，答案使用：

```text
COMPOSE_TIMEOUT_SECONDS=1800
DATA_DIR=/data/docker-manage-server
DOCKER_HOST=unix:///var/run/docker.sock
DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server
DOCKER_MANAGE_SERVER_PORT=6308
PIP_ROOT_USER_ACTION=ignore
PYTHONDONTWRITEBYTECODE=1
PYTHONUNBUFFERED=1
port.server.8000/tcp.expose=yes
port.server.8000/tcp.host=6308
data bind=keep_server_path
docker socket bind=keep_server_path
platform=linux/amd64
```

展示完整计划与精确 plan hash，取得用户明确确认后才能 package。最终验证：

- 外层 SHA-256；
- 内部 `checksums.sha256`；
- `.env` 的数据目录和端口；
- Compose 渲染为 `6308:8000/tcp`；
- manifest server paths 为 `/data/docker-manage-server` 和 `/var/run/docker.sock`；
- 镜像平台为 `linux/amd64`；
- 归档不包含运行数据内容；
- 打包后的服务访问 `/` 返回管理台。

---

## Plan Self-Review Checklist

- [x] 每个设计要求至少对应一个任务。
- [x] 生产代码均在对应失败测试之后实现。
- [x] 时间字段不会改变现有 API payload。
- [x] Web 页面直接调用业务服务，不通过 HTTP 回调自身 API。
- [x] xterm 版本、分发文件、许可证和离线交付方式明确。
- [x] CSP、HTTP Origin 与 WebSocket Origin 均有自动化测试。
- [x] 无占位符、未定义接口或模糊的“类似实现”。
- [x] Docker 与离线包固定参数完整保留。
