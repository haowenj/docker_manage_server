# Compose 容器详情弹框修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 确保部署升级后 Compose 容器“查看详情”始终加载当前脚本，并在不依赖 `CSS.escape()` 的浏览器中打开原生弹框。

**Architecture:** 在模板辅助层为本地静态资源计算稳定的内容摘要，并将摘要作为资源 URL 的 `v` 参数；Compose 详情模板使用固定 DOM `id` 关联按钮和弹框，前端通过 `getElementById()` 打开。保留现有原生 `dialog` 与关闭交互。

**Tech Stack:** Python 3.11、FastAPI、Jinja2、原生 JavaScript、pytest、Codex Browser

## Global Constraints

- 只修复静态资源缓存失效和弹框定位兼容性。
- 不引入外部 JavaScript 依赖或资源构建步骤。
- 保留原生 `dialog`、关闭按钮、点击遮罩关闭和 `aria-labelledby`。
- 不增加容器生命周期操作，不修改 Compose 项目或容器数据模型。
- 所有变更直接提交到用户指定的 `main` 分支，不推送远端。

---

### Task 1: 内容版本化静态资源 URL

**Files:**
- Modify: `src/docker_manage_server/web.py`
- Modify: `src/docker_manage_server/templates/base.html`
- Test: `tests/web/test_dashboard.py`

**Interfaces:**
- Produces: `static_asset_version(path: str) -> str`，返回包内静态文件内容 SHA-256 的前 12 个十六进制字符。
- Consumes: Jinja2 模板全局函数 `static_asset_version`。

- [ ] **Step 1: 写失败测试**

在 `tests/web/test_dashboard.py` 增加：

```python
from docker_manage_server.web import static_asset_version


def test_base_template_versions_local_static_assets(web_context):
    client, _store, _runtime = web_context
    response = client.get("/")
    assert (
        f'/static/css/app.css?v={static_asset_version("css/app.css")}'
        in response.text
    )
    assert (
        f'/static/js/app.js?v={static_asset_version("js/app.js")}'
        in response.text
    )


def test_static_asset_version_changes_with_content(tmp_path, monkeypatch):
    asset = tmp_path / "app.js"
    asset.write_text("one", encoding="utf-8")
    monkeypatch.setattr("docker_manage_server.web.STATIC_ROOT", tmp_path)
    first = static_asset_version("app.js")
    asset.write_text("two", encoding="utf-8")
    assert static_asset_version("app.js") != first
```

- [ ] **Step 2: 运行测试并确认 RED**

运行：

```bash
.venv/bin/python -m pytest -q \
  tests/web/test_dashboard.py::test_base_template_versions_local_static_assets \
  tests/web/test_dashboard.py::test_static_asset_version_changes_with_content
```

预期：因 `static_asset_version` 尚不存在而失败。

- [ ] **Step 3: 最小实现资源摘要函数与模板参数**

在 `src/docker_manage_server/web.py` 中定义：

```python
from hashlib import sha256

STATIC_ROOT = PACKAGE_ROOT / "static"


def static_asset_version(path: str) -> str:
    return sha256((STATIC_ROOT / path).read_bytes()).hexdigest()[:12]


templates.env.globals["static_asset_version"] = static_asset_version
```

在 `src/docker_manage_server/templates/base.html` 中将资源 URL 改为：

```html
<link rel="stylesheet" href="{{ url_for('static', path='/css/app.css') }}?v={{ static_asset_version('css/app.css') }}">
<script defer src="{{ url_for('static', path='/js/app.js') }}?v={{ static_asset_version('js/app.js') }}"></script>
```

- [ ] **Step 4: 运行测试并确认 GREEN**

运行：

```bash
.venv/bin/python -m pytest -q tests/web/test_dashboard.py
```

预期：该文件全部通过。

- [ ] **Step 5: 提交**

```bash
git add src/docker_manage_server/web.py \
  src/docker_manage_server/templates/base.html \
  tests/web/test_dashboard.py
git commit -m "fix: version browser assets by content"
```

### Task 2: 移除弹框定位的 `CSS.escape()` 依赖

**Files:**
- Modify: `src/docker_manage_server/templates/compose_projects/detail.html`
- Modify: `src/docker_manage_server/static/js/app.js`
- Test: `tests/web/test_compose_projects.py`
- Test: `tests/web/test_package_resources.py`

**Interfaces:**
- Consumes: 按钮属性 `data-dialog-open="container-dialog-<container-id>"`。
- Produces: 弹框属性 `id="container-dialog-<container-id>"`；前端调用 `document.getElementById(button.dataset.dialogOpen)`。

- [ ] **Step 1: 写失败测试**

在 `tests/web/test_compose_projects.py` 更新详情断言：

```python
assert 'data-dialog-open="container-dialog-mall-web"' in response.text
assert 'id="container-dialog-mall-web"' in response.text
```

在 `tests/web/test_package_resources.py` 增加：

```python
def test_compose_dialog_script_uses_id_without_css_escape():
    script = (
        files("docker_manage_server")
        .joinpath("static/js/app.js")
        .read_text(encoding="utf-8")
    )
    assert "document.getElementById(button.dataset.dialogOpen)" in script
    assert "CSS.escape" not in script
```

- [ ] **Step 2: 运行测试并确认 RED**

运行：

```bash
.venv/bin/python -m pytest -q \
  tests/web/test_compose_projects.py::test_compose_project_detail_renders_container_dialog_and_tools \
  tests/web/test_package_resources.py::test_compose_dialog_script_uses_id_without_css_escape
```

预期：模板仍使用旧目标值，脚本仍包含 `CSS.escape()`，测试失败。

- [ ] **Step 3: 最小实现固定 ID 定位**

在 Compose 详情模板中将按钮和弹框关联改为：

```html
<button data-dialog-open="container-dialog-{{ container.item.id }}">查看详情</button>
<dialog id="container-dialog-{{ container.item.id }}" ...>
```

在 `app.js` 中改为：

```javascript
const dialog = document.getElementById(button.dataset.dialogOpen);
if (dialog) dialog.showModal();
```

- [ ] **Step 4: 运行测试并确认 GREEN**

运行：

```bash
.venv/bin/python -m pytest -q \
  tests/web/test_compose_projects.py \
  tests/web/test_package_resources.py
```

预期：全部通过。

- [ ] **Step 5: 提交**

```bash
git add src/docker_manage_server/templates/compose_projects/detail.html \
  src/docker_manage_server/static/js/app.js \
  tests/web/test_compose_projects.py \
  tests/web/test_package_resources.py
git commit -m "fix: open compose dialogs by element id"
```

### Task 3: 浏览器回归与完整验证

**Files:**
- Verify only; no planned production changes.

**Interfaces:**
- Consumes: Task 1 的内容版本 URL 与 Task 2 的固定弹框 ID。
- Produces: 可重复的浏览器与测试验证证据。

- [ ] **Step 1: 启动本地测试页面并刷新浏览器**

使用现有 `WebFakeRuntime` 启动 Compose 项目详情页，修改完成后按本地 Web 测试规范刷新页面。

- [ ] **Step 2: 验证弹框交互**

使用浏览器确认：

```text
点击“查看详情” → dialog.open == true 且可见
点击“关闭” → dialog.open == false 且不可见
再次打开并点击遮罩 → dialog.open == false 且不可见
CSS 与 JS URL 都包含 ?v=<12 位摘要>
```

- [ ] **Step 3: 运行完整校验**

运行：

```bash
git diff --check
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pytest -q
DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server \
  DOCKER_MANAGE_SERVER_PORT=6308 \
  docker compose config --quiet
git status --short
```

预期：无 diff 错误、编译成功、所有测试通过、Compose 配置有效，仅允许计划内文件变化。

- [ ] **Step 4: 若浏览器验证引出必要测试调整则提交**

如果没有新增修改则不创建空提交；如仅补充回归断言，运行对应测试后提交：

```bash
git add tests
git commit -m "test: cover compose dialog browser behavior"
```
