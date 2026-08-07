# Container Logs and Terminal Pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move container logs and terminal out of the detail page into new-tab server-rendered pages whose content stays inside fixed-height scrolling viewports.

**Architecture:** Add two HTML routes that reuse the existing container lookup, logs API, and terminal WebSocket. Keep the detail page informational, load xterm assets only on the terminal page, and prevent the resize feedback loop by giving both tool viewports explicit bounded heights with internal scrolling.

**Tech Stack:** Python 3.11, FastAPI, Jinja2, pytest, native JavaScript, xterm.js 6.0.0, CSS

## Global Constraints

- Both detail-page tool links open a new browser tab with `target="_blank"` and `rel="noopener"`.
- The routes are exactly `/containers/{container_id}/logs` and `/containers/{container_id}/terminal`.
- Logs continue to use `/api/containers/{container_id}/logs`; terminal continues to use `/api/containers/{container_id}/terminal` with `/bin/sh`.
- Both logs and terminal have an explicit bounded `height`; excess content scrolls inside the tool viewport and never expands the page.
- Terminal history scrolls through xterm's own buffer; the terminal viewport uses `overflow: hidden`.
- No new front-end framework, build system, online resource, Docker action, or Runtime interface is introduced.
- Existing bright visual style, Content-Security-Policy, HTML escaping, API behavior, and WebSocket protocol remain unchanged.

---

## File Structure

- Modify `tests/web/test_containers.py`: cover detail-page links, separate tool pages, resource loading, and HTML 404/503 behavior.
- Modify `src/docker_manage_server/web.py`: share container page rendering and register the two new GET routes.
- Modify `src/docker_manage_server/templates/containers/detail.html`: replace inline tools with top-right links.
- Create `src/docker_manage_server/templates/containers/logs.html`: server-render the standalone log viewer.
- Create `src/docker_manage_server/templates/containers/terminal.html`: server-render the standalone xterm shell and load xterm assets locally.
- Modify `tests/web/test_dashboard.py`: assert packaged CSS defines bounded tool viewports and internal scrolling.
- Modify `src/docker_manage_server/static/css/app.css`: add heading action layout and fixed-height log/terminal viewports, including mobile rules.
- Keep `src/docker_manage_server/static/js/app.js` and `src/docker_manage_server/static/js/terminal.js` unchanged because their API and DOM contracts are reused on the new pages.

### Task 1: Split container logs and terminal into standalone pages

**Files:**
- Modify: `tests/web/test_containers.py`
- Modify: `src/docker_manage_server/web.py`
- Modify: `src/docker_manage_server/templates/containers/detail.html`
- Create: `src/docker_manage_server/templates/containers/logs.html`
- Create: `src/docker_manage_server/templates/containers/terminal.html`

**Interfaces:**
- Consumes: `DockerRuntime.get_container(container_id)`, `DockerRuntime._serialize_container(container)`, `container_view(item)`, existing logs API, and existing terminal WebSocket.
- Produces: `_container_page(request, runtime, container_id, template_name) -> HTMLResponse`, `GET /containers/{container_id}/logs`, and `GET /containers/{container_id}/terminal`.

- [ ] **Step 1: Replace the inline-tool test with failing page-separation tests**

Create a local helper in `tests/web/test_containers.py` and replace `test_container_detail_exposes_logs_and_terminal_targets` with these assertions:

```python
def _stub_container():
    return type(
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


def test_container_detail_links_to_standalone_tool_pages(web_context):
    client, _store, runtime = web_context
    runtime.get_container = lambda _container_id: _stub_container()

    response = client.get("/containers/abc123")

    assert response.status_code == 200
    assert 'href="/containers/abc123/logs"' in response.text
    assert 'href="/containers/abc123/terminal"' in response.text
    assert response.text.count('target="_blank"') == 2
    assert response.text.count('rel="noopener"') == 2
    assert "data-log-viewer" not in response.text
    assert "data-terminal-url" not in response.text
    assert "/static/js/terminal.js" not in response.text
    assert "/static/vendor/xterm/xterm.css" not in response.text


def test_container_log_page_reuses_existing_log_viewer(web_context):
    client, _store, runtime = web_context
    runtime.get_container = lambda _container_id: _stub_container()

    response = client.get("/containers/abc123/logs")

    assert response.status_code == 200
    assert 'data-log-url="/api/containers/abc123/logs"' in response.text
    assert "data-log-tail" in response.text
    assert "data-log-timestamps" in response.text
    assert "data-log-refresh" in response.text
    assert "log-output-viewport" in response.text
    assert 'href="/containers/abc123"' in response.text
    assert "/static/js/terminal.js" not in response.text


def test_container_terminal_page_loads_local_xterm(web_context):
    client, _store, runtime = web_context
    runtime.get_container = lambda _container_id: _stub_container()

    response = client.get("/containers/abc123/terminal")

    assert response.status_code == 200
    assert 'data-terminal-url="/api/containers/abc123/terminal"' in response.text
    assert 'data-terminal-command="/bin/sh"' in response.text
    assert "terminal-viewport" in response.text
    assert "/static/vendor/xterm/xterm.css" in response.text
    assert 'type="module"' in response.text
    assert "/static/js/terminal.js" in response.text
    assert 'href="/containers/abc123"' in response.text
```

Replace `test_container_pages_render_html_errors` so all three detail routes share the same 404/503 behavior:

```python
def test_container_pages_render_html_errors(web_context):
    client, _store, runtime = web_context
    runtime.get_container = lambda _value: (_ for _ in ()).throw(
        ContainerNotFoundError("x")
    )

    for path in (
        "/containers/missing",
        "/containers/missing/logs",
        "/containers/missing/terminal",
    ):
        missing = client.get(path)
        assert missing.status_code == 404
        assert "找不到容器" in missing.text

    runtime.list_containers = lambda: (_ for _ in ()).throw(
        DockerRuntimeError("offline")
    )
    unavailable = client.get("/containers")
    assert unavailable.status_code == 503
    assert "offline" in unavailable.text

    runtime.get_container = lambda _value: (_ for _ in ()).throw(
        DockerRuntimeError("offline")
    )
    for path in (
        "/containers/abc123/logs",
        "/containers/abc123/terminal",
    ):
        unavailable = client.get(path)
        assert unavailable.status_code == 503
        assert "offline" in unavailable.text
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
pytest tests/web/test_containers.py -q
```

Expected: FAIL because the detail page still embeds the tools and the two standalone routes/templates do not exist.

- [ ] **Step 3: Add shared container page rendering and standalone routes**

In `src/docker_manage_server/web.py`, add this module-level helper after `_read_optional`:

```python
def _container_page(
    request: Request,
    runtime: DockerRuntime,
    container_id: str,
    template_name: str,
) -> HTMLResponse:
    try:
        item = DockerRuntime._serialize_container(runtime.get_container(container_id))
    except ContainerNotFoundError:
        return _web_error(request, 404, "找不到容器", container_id)
    except DockerRuntimeError as exc:
        return _web_error(request, 503, "Docker daemon 不可用", str(exc))
    container = container_view(item)
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "page_title": container["name"] or container["item"].get("short_id"),
            "active_nav": "containers",
            "container": container,
        },
    )
```

Replace the existing detail route body and register the new routes before `return router`:

```python
    @router.get("/containers/{container_id}", response_class=HTMLResponse)
    def container_detail(request: Request, container_id: str):
        return _container_page(
            request, runtime, container_id, "containers/detail.html"
        )

    @router.get("/containers/{container_id}/logs", response_class=HTMLResponse)
    def container_logs(request: Request, container_id: str):
        return _container_page(
            request, runtime, container_id, "containers/logs.html"
        )

    @router.get("/containers/{container_id}/terminal", response_class=HTMLResponse)
    def container_terminal(request: Request, container_id: str):
        return _container_page(
            request, runtime, container_id, "containers/terminal.html"
        )
```

- [ ] **Step 4: Replace inline tools with top-right links**

Remove the xterm `head` block, inline log panel, inline terminal panel, and terminal `scripts` block from `src/docker_manage_server/templates/containers/detail.html`. Change its first panel heading to:

```html
<div class="panel-heading container-heading">
  <h2>{{ container.name }}</h2>
  <div class="container-heading-actions">
    <span class="status {{ 'status-running' if container.running else 'status-stopped' }}">{{ container.status_label }}</span>
    <a class="button button-secondary" href="/containers/{{ container.item.id }}/logs" target="_blank" rel="noopener" aria-label="查看 {{ container.name }} 日志（新标签页）">查看日志</a>
    <a class="button button-primary" href="/containers/{{ container.item.id }}/terminal" target="_blank" rel="noopener" aria-label="连接 {{ container.name }} 终端（新标签页）">连接终端</a>
  </div>
</div>
```

- [ ] **Step 5: Create the standalone log template**

Create `src/docker_manage_server/templates/containers/logs.html`:

```html
{% extends "base.html" %}
{% block content %}
<section class="panel tool-page-panel log-panel" data-log-viewer data-log-url="/api/containers/{{ container.item.id }}/logs">
  <div class="panel-heading">
    <div>
      <h2>{{ container.name }} · 容器日志</h2>
      <span class="status {{ 'status-running' if container.running else 'status-stopped' }}">{{ container.status_label }}</span>
    </div>
    <a class="button button-secondary" href="/containers/{{ container.item.id }}">返回容器详情</a>
  </div>
  <div class="toolbar">
    <label>行数 <select data-log-tail><option>100</option><option>500</option><option value="all">全部</option></select></label>
    <label><input type="checkbox" data-log-timestamps> 显示时间戳</label>
    <button class="button button-secondary" type="button" data-log-refresh>刷新日志</button>
  </div>
  <pre class="log-output-viewport" data-log-output>点击“刷新日志”读取日志。</pre>
</section>
{% endblock %}
```

- [ ] **Step 6: Create the standalone terminal template**

Create `src/docker_manage_server/templates/containers/terminal.html`:

```html
{% extends "base.html" %}
{% block head %}<link rel="stylesheet" href="/static/vendor/xterm/xterm.css">{% endblock %}
{% block content %}
<section class="panel tool-page-panel terminal-panel" data-terminal-url="/api/containers/{{ container.item.id }}/terminal" data-terminal-command="/bin/sh">
  <div class="panel-heading">
    <div>
      <h2>{{ container.name }} · 在线终端</h2>
      <span class="status {{ 'status-running' if container.running else 'status-stopped' }}">{{ container.status_label }}</span>
    </div>
    <a class="button button-secondary" href="/containers/{{ container.item.id }}">返回容器详情</a>
  </div>
  <div class="toolbar">
    <span data-terminal-status>尚未连接</span>
    <button class="button button-primary" type="button" data-terminal-connect>连接终端</button>
  </div>
  <div class="terminal-viewport" data-terminal-viewport></div>
</section>
{% endblock %}
{% block scripts %}<script type="module" src="/static/js/terminal.js"></script>{% endblock %}
```

- [ ] **Step 7: Run the focused tests and verify they pass**

Run:

```bash
pytest tests/web/test_containers.py -q
```

Expected: all container web tests PASS.

- [ ] **Step 8: Commit the route and template split**

```bash
git add tests/web/test_containers.py src/docker_manage_server/web.py src/docker_manage_server/templates/containers/detail.html src/docker_manage_server/templates/containers/logs.html src/docker_manage_server/templates/containers/terminal.html
git commit -m "feat: split container logs and terminal pages"
```

### Task 2: Enforce fixed-height internal scrolling

**Files:**
- Modify: `tests/web/test_dashboard.py`
- Modify: `src/docker_manage_server/static/css/app.css`

**Interfaces:**
- Consumes: `.container-heading-actions`, `.log-output-viewport`, and `.terminal-viewport` classes emitted by Task 1 templates.
- Produces: bounded desktop/mobile tool viewport dimensions and stable xterm parent geometry.

- [ ] **Step 1: Add a failing packaged-CSS contract test**

Append this test to `tests/web/test_dashboard.py`:

```python
def test_tool_viewports_have_bounded_height_and_internal_scrolling():
    css = (
        files("docker_manage_server")
        .joinpath("static/css/app.css")
        .read_text(encoding="utf-8")
    )

    assert ".log-output-viewport, .terminal-viewport" in css
    assert "height: clamp(320px, 58vh, 720px)" in css
    assert ".log-output-viewport { overflow: auto;" in css
    assert ".terminal-viewport { overflow: hidden;" in css
    assert ".terminal-viewport { min-height: 360px;" not in css
```

- [ ] **Step 2: Run the CSS contract test and verify it fails**

Run:

```bash
pytest tests/web/test_dashboard.py::test_tool_viewports_have_bounded_height_and_internal_scrolling -q
```

Expected: FAIL because `.terminal-viewport` still has only `min-height: 360px` and the log viewport class does not exist.

- [ ] **Step 3: Add heading layout and fixed tool viewports**

Replace the existing `.terminal-viewport` rule in `src/docker_manage_server/static/css/app.css` with:

```css
.container-heading-actions { display: flex; align-items: center; justify-content: flex-end; gap: 10px; flex-wrap: wrap; }
.tool-page-panel { display: flex; min-height: 0; flex-direction: column; }
.log-output-viewport, .terminal-viewport { width: 100%; height: clamp(320px, 58vh, 720px); min-height: 0; }
.log-output-viewport { overflow: auto; margin-bottom: 0; }
.terminal-viewport { overflow: hidden; padding: 8px; background: #fff; border: 1px solid var(--border); border-radius: 8px; }
.terminal-viewport .xterm { height: 100%; }
```

Inside the existing `@media (max-width: 760px)` block, append:

```css
  .container-heading-actions { width: 100%; justify-content: flex-start; }
  .log-output-viewport, .terminal-viewport { height: clamp(280px, 52vh, 520px); }
```

The explicit height keeps `ResizeObserver` tied to the page viewport instead of xterm child content. `overflow: hidden` leaves terminal history scrolling to xterm; logs use native `overflow: auto`.

- [ ] **Step 4: Run page and CSS tests and verify they pass**

Run:

```bash
pytest tests/web/test_containers.py tests/web/test_dashboard.py -q
```

Expected: all selected web tests PASS.

- [ ] **Step 5: Commit the fixed-height viewport behavior**

```bash
git add tests/web/test_dashboard.py src/docker_manage_server/static/css/app.css
git commit -m "fix: bound container tool viewport heights"
```

### Task 3: Full regression and browser stability verification

**Files:**
- Verify only; no source changes expected.

**Interfaces:**
- Consumes: all routes, templates, styles, logs API, and terminal WebSocket changed or reused by Tasks 1–2.
- Produces: evidence that server-rendered pages, APIs, package resources, and live resize behavior remain stable.

- [ ] **Step 1: Run formatting and diff checks**

Run:

```bash
git diff --check
```

Expected: exit code 0 and no output.

- [ ] **Step 2: Run the complete automated suite**

Run:

```bash
pytest -q
```

Expected: all tests PASS with no new warnings or errors.

- [ ] **Step 3: Launch the app and verify page navigation in a browser**

Start the app against the local Docker daemon:

```bash
DATA_DIR="$(mktemp -d)" .venv/bin/uvicorn docker_manage_server.main:app --host 127.0.0.1 --port 6308
```

Open `http://127.0.0.1:6308/containers`, select a real container, and verify:

1. “查看日志” and “连接终端” appear beside the container name.
2. Each action opens a new browser tab at the expected standalone URL.
3. The detail page contains no inline log or terminal viewport.
4. Each standalone page has a working “返回容器详情” link.

Expected: all navigation stays on server-rendered pages and no external asset request occurs.

- [ ] **Step 4: Verify fixed-height behavior with long output and resize**

On the log page, load more than one viewport of logs and confirm the log block scrolls internally. On the terminal page, connect and run a command that produces more than one viewport of output; record `document.querySelector('[data-terminal-viewport]').getBoundingClientRect().height` and `document.documentElement.scrollHeight`, wait through multiple ResizeObserver callbacks, and record both again. Resize the browser once and repeat.

Expected: terminal history scrolls inside xterm; the terminal viewport height remains within the CSS clamp and does not continually increase; the document scroll height remains stable after layout settles.

- [ ] **Step 5: Confirm repository scope**

Run:

```bash
git status --short
git log --oneline -4
```

Expected: only known pre-existing untracked artifacts such as `.docker-manage/` and `uv.lock` remain; the design, implementation plan, route/template split, and viewport fix commits are present.
