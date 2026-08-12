# 镜像名称链接与详情页删除弹框实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将镜像 Tag 统一展示为可点击的“镜像名称”，并在镜像详情页内完成删除预览、确认、异步执行和结果展示，不再跳转到独立删除页面。

**Architecture:** 保留现有 `ImageInventoryService` 和 Tag 级安全删除 API，不修改删除判定。服务端在详情响应中同时渲染删除预览和无 JavaScript 表单；浏览器脚本拦截该表单并调用现有同源 DELETE API，使用 DOM API 原地渲染结果，再通过详情 API 同步权威名称。Web POST 作为降级路径，只重定向回详情或列表。

**Tech Stack:** Python 3.11+、FastAPI、Jinja2、Docker SDK、原生 JavaScript、HTML `<dialog>`、pytest

## Global Constraints

- 所有镜像删除继续调用 `ImageInventoryService.remove_available_tags()`，不得新增强制删除或完整 ID 删除。
- 删除前展示的预览不是授权清单；提交时必须继续在不可变镜像 ID 锁内重新计算并逐 Tag 校验。
- 跨源 `DELETE` 和 `POST` 必须继续由现有安全中间件拒绝为 403。
- 不引入前端框架或新运行时依赖；不使用 `innerHTML` 渲染 Docker/API 数据。
- 当前页面的异步删除不得改变浏览器 URL；无 JavaScript 降级使用 `303` 返回详情或列表。
- 镜像列表继续使用完整不可变镜像 ID 作为详情链接目标；无 Tag 镜像使用短 ID 作为可见名称。
- 保留现有分页、搜索、inspect、容器引用和 Tag 删除并发语义。

---

### Task 1: 镜像名称列表与详情视图模型

**Files:**
- Modify: `src/docker_manage_server/web_views.py:61-69`
- Modify: `src/docker_manage_server/templates/images/list.html`
- Test: `tests/web/test_images.py`

**Interfaces:**
- Consumes: `ImageSummary.id`, `ImageSummary.short_id`, `ImageSummary.tags`。
- Produces: `image_summary_view(item)` 新增 `display_name: str`；模板继续使用 `image.item.id` 作为详情 URL，并直接使用 `image.item.tags` 判断是否为未标记镜像。

- [ ] **Step 1: 写列表名称与链接的失败测试**

在 `tests/web/test_images.py` 中扩展列表测试，并新增无 Tag 测试：

```python
def test_image_navigation_list_search_and_pagination(web_context):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(index) for index in range(25)]

    response = client.get("/images?q=demo&page=2")

    assert response.status_code == 200
    assert "镜像名称" in response.text
    assert "<th>Tags</th>" not in response.text
    assert 'href="/images/sha256:image-4"' in response.text
    assert ">demo/item:4</a>" in response.text


def test_untagged_image_uses_short_id_as_detail_link(web_context):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(1, tags=())]

    response = client.get("/images")

    assert response.status_code == 200
    assert 'href="/images/sha256:image-1"' in response.text
    assert ">image-1</a>" in response.text
    assert "未标记" in response.text
```

- [ ] **Step 2: 运行测试确认因旧表头和不可点击名称失败**

Run: `.venv/bin/python -m pytest -q tests/web/test_images.py::test_image_navigation_list_search_and_pagination tests/web/test_images.py::test_untagged_image_uses_short_id_as_detail_link`

Expected: FAIL，响应仍包含 `<th>Tags</th>`，且 Tag/无 Tag 短 ID 尚未按新规则链接。

- [ ] **Step 3: 实现视图模型与模板**

在 `image_summary_view()` 返回值中加入：

```python
"display_name": item.tags[0] if item.tags else item.short_id,
```

把 `templates/images/list.html` 首列表头改为 `镜像名称`。有 Tag 时为每个 Tag 渲染：

```jinja2
<a class="tag" href="/images/{{ image.item.id }}">{{ tag }}</a>
```

无 Tag 时渲染：

```jinja2
<a class="tag" href="/images/{{ image.item.id }}">{{ image.item.short_id }}</a>
<span class="muted">未标记</span>
```

镜像 ID 列的现有链接保持不变。

- [ ] **Step 4: 运行列表测试确认通过**

Run: `.venv/bin/python -m pytest -q tests/web/test_images.py::test_image_navigation_list_search_and_pagination tests/web/test_images.py::test_untagged_image_uses_short_id_as_detail_link`

Expected: `2 passed`。

- [ ] **Step 5: 提交列表改动**

```bash
git add src/docker_manage_server/web_views.py src/docker_manage_server/templates/images/list.html tests/web/test_images.py
git commit -m "feat: link image names to details"
```

---

### Task 2: 详情页删除弹框与 Web 降级路径

**Files:**
- Modify: `src/docker_manage_server/web.py:1-10, 207-214, 415-510`
- Modify: `src/docker_manage_server/templates/images/detail.html`
- Modify: `src/docker_manage_server/static/css/app.css:80-84`
- Test: `tests/web/test_images.py`

**Interfaces:**
- Consumes: `ImageTagRemovalPreview(id, deletable_tags, retained_tags)` 与 `ImageTagRemovalResult.image_exists`。
- Produces: 详情模板上下文 `removal_preview`；弹框根节点 `data-image-delete-dialog`、`data-delete-url`、`data-detail-url`；表单 `data-image-delete-form`；结果和名称更新所需的稳定 `data-*` 节点。
- Produces: `POST /images/{image_id}/delete` 成功后在镜像仍存在时 303 到 `/images/{result.id}`，否则 303 到 `/images`；`GET /images/{image_id}/delete` 307 到 `/images/{image_id}`。

- [ ] **Step 1: 写详情弹框与降级路由失败测试**

用下面行为替换 `test_unused_image_delete_previews_and_redirects_to_result`，并补使用中名称断言：

```python
def test_image_detail_embeds_delete_preview_dialog(web_context):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(1, tags=("demo/app:1", "demo/app:2"))]
    runtime.containers = [direct_reference()]

    response = client.get("/images/sha256:image-1")

    assert response.status_code == 200
    assert "镜像名称" in response.text
    assert 'data-dialog-open="image-delete-dialog"' in response.text
    assert 'id="image-delete-dialog"' in response.text
    assert 'data-image-delete-dialog' in response.text
    assert 'data-delete-url="/api/images/sha256:image-1/tags"' in response.text
    assert 'data-detail-url="/api/images/sha256:image-1"' in response.text
    assert 'action="/images/sha256:image-1/delete"' in response.text
    assert "demo/app:2" in response.text
    assert "demo/app:1" in response.text
    assert "极短的外部并发窗口" in response.text
    assert 'href="/images/sha256:image-1/delete"' not in response.text


def test_image_delete_web_fallback_redirects_without_result_page(web_context):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(1, tags=("demo/app:1", "demo/app:2"))]
    runtime.containers = [direct_reference()]

    deleted = client.post(
        "/images/sha256:image-1/delete",
        follow_redirects=False,
    )

    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/images/sha256:image-1"
    assert client.get(
        "/images/sha256:image-1/delete",
        follow_redirects=False,
    ).headers["location"] == "/images/sha256:image-1"


def test_image_delete_web_fallback_returns_to_list_when_image_is_gone(web_context):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(1, tags=("demo/app:1",))]
    runtime.containers = []

    response = client.post(
        "/images/sha256:image-1/delete",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/images"
```

- [ ] **Step 2: 运行测试确认旧的链接和结果页跳转导致失败**

Run: `.venv/bin/python -m pytest -q tests/web/test_images.py::test_image_detail_embeds_delete_preview_dialog tests/web/test_images.py::test_image_delete_web_fallback_redirects_without_result_page tests/web/test_images.py::test_image_delete_web_fallback_returns_to_list_when_image_is_gone`

Expected: FAIL，详情仍链接到独立预览页，POST 仍重定向结果页。

- [ ] **Step 3: 把完整预览传给详情模板并简化 Web 路由**

在详情上下文中用：

```python
"removal_preview": removal_preview,
```

替代 `has_deletable_tags`。将 GET 删除预览路由改为：

```python
@router.get("/images/{image_id}/delete")
def preview_image_tag_removal(image_id: str):
    return RedirectResponse(f"/images/{image_id}", status_code=307)
```

将 POST 成功返回改为：

```python
target = f"/images/{result.id}" if result.image_exists else "/images"
return RedirectResponse(target, status_code=303)
```

删除 `tag_removal_results`、其锁、结果记录写入和结果页读取路由；保留部署任务使用的 `uuid4`，删除不再使用的 `Lock` 导入。

- [ ] **Step 4: 在详情模板嵌入弹框**

标题和定义项使用：

```jinja2
<h2 data-image-title>{{ image.display_name }}</h2>
<dt>镜像名称</dt>
<dd><div class="tag-list" data-image-name-list>...</div></dd>
```

删除按钮仅在 `removal_preview.deletable_tags` 非空时出现：

```jinja2
<button class="button button-danger" type="button" data-dialog-open="image-delete-dialog">删除可用镜像名称</button>
```

在页面末尾加入：

```jinja2
<dialog id="image-delete-dialog" class="container-dialog" data-image-delete-dialog
  data-delete-url="/api/images/{{ image.item.id }}/tags"
  data-detail-url="/api/images/{{ image.item.id }}">
  <div class="dialog-heading">
    <h2>确认删除可用镜像名称</h2>
    <button class="button button-secondary" type="button" data-dialog-close>关闭</button>
  </div>
  <div data-image-delete-preview>
    <div class="alert alert-warning">Docker 不支持条件删除 Tag。系统会在提交时重新检查，但检查与删除之间仍存在极短的外部并发窗口。</div>
    <section class="dialog-section"><h3>将删除的镜像名称</h3><ul>{% for tag in removal_preview.deletable_tags %}<li><code>{{ tag }}</code></li>{% endfor %}</ul></section>
    <section class="dialog-section"><h3>因容器正在使用而保留的镜像名称</h3><ul>{% for tag in removal_preview.retained_tags %}<li><code>{{ tag }}</code></li>{% else %}<li>无</li>{% endfor %}</ul></section>
  </div>
  <div class="alert alert-danger" data-image-delete-error hidden></div>
  <div data-image-delete-result hidden>
    <section class="dialog-section"><h3>实际删除</h3><ul data-deleted-tags></ul></section>
    <section class="dialog-section"><h3>保留</h3><ul data-retained-tags></ul></section>
    <section class="dialog-section"><h3>跳过</h3><ul data-skipped-tags></ul></section>
    <p data-image-exists-status></p>
    <a class="button button-secondary" href="/images" data-image-list-link hidden>返回镜像列表</a>
  </div>
  <form method="post" action="/images/{{ image.item.id }}/delete" data-image-delete-form>
    <button class="button button-danger" type="submit" data-image-delete-submit>确认删除</button>
  </form>
</dialog>
```

无 Tag 的详情名称区域显示短 ID 和“未标记”。给 `.container-dialog` 增加 `overflow: auto`，并让弹框表单在结果显示后可隐藏。

- [ ] **Step 5: 运行 Web 测试确认通过**

Run: `.venv/bin/python -m pytest -q tests/web/test_images.py`

Expected: 所有镜像 Web 测试通过，既有 404/409/503 映射不变。

- [ ] **Step 6: 提交详情和降级路径**

```bash
git add src/docker_manage_server/web.py src/docker_manage_server/templates/images/detail.html src/docker_manage_server/static/css/app.css tests/web/test_images.py
git commit -m "feat: embed image deletion in detail dialog"
```

---

### Task 3: 异步删除、结果渲染与详情同步

**Files:**
- Modify: `src/docker_manage_server/static/js/app.js:7-18`
- Modify: `tests/web/test_package_resources.py`
- Test: `tests/web/test_images.py`

**Interfaces:**
- Consumes: `DELETE /api/images/{id}/tags` 返回 `{id, deleted_tags, retained_tags, skipped_tags, image_exists}`。
- Consumes: `GET /api/images/{id}` 返回 `{item: {id, short_id, tags, ...}, inspect, containers}`。
- Produces: 弹框内 `data-image-delete-result` 内容、详情页 `data-image-title` 和 `data-image-name-list` 的权威当前值；失败文本写入 `data-image-delete-error`。

- [ ] **Step 1: 写前端契约失败测试**

在 `tests/web/test_package_resources.py` 新增：

```python
def test_image_delete_dialog_uses_same_origin_api_and_safe_dom_updates():
    script = (
        files("docker_manage_server")
        .joinpath("static/js/app.js")
        .read_text(encoding="utf-8")
    )
    assert 'document.querySelectorAll("[data-image-delete-dialog]")' in script
    assert "method: \"DELETE\"" in script
    assert "dialog.dataset.deleteUrl" in script
    assert "dialog.dataset.detailUrl" in script
    assert "response.ok" in script
    assert "replaceChildren" in script
    assert "textContent" in script
    assert "innerHTML" not in script
```

- [ ] **Step 2: 运行契约测试确认脚本尚未实现**

Run: `.venv/bin/python -m pytest -q tests/web/test_package_resources.py::test_image_delete_dialog_uses_same_origin_api_and_safe_dom_updates`

Expected: FAIL，脚本中尚无镜像删除弹框处理器。

- [ ] **Step 3: 实现通用弹框关闭与安全列表渲染**

把现有容器弹框关闭选择器扩展为：

```javascript
document.querySelectorAll("[data-container-dialog], [data-image-delete-dialog]").forEach((dialog) => {
  dialog.querySelector("[data-dialog-close]")?.addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
});
```

新增只使用 DOM API 的工具：

```javascript
const renderTextList = (list, values, emptyText = "无") => {
  const items = values.length ? values : [emptyText];
  list.replaceChildren(...items.map((value) => {
    const row = document.createElement("li");
    const code = document.createElement("code");
    code.textContent = value;
    row.appendChild(code);
    return row;
  }));
};
```

- [ ] **Step 4: 实现异步提交与结果状态机**

对每个 `[data-image-delete-dialog]`：

1. 监听 `[data-image-delete-form]` 的 `submit` 并 `preventDefault()`；
2. 禁用 `[data-image-delete-submit]`，清空并隐藏旧错误；
3. `fetch(dialog.dataset.deleteUrl, {method: "DELETE", headers: {Accept: "application/json"}})`；
4. 对非 2xx 响应读取 JSON `detail`，没有 JSON 时使用 `HTTP <status>`；
5. 成功后用 `renderTextList()` 更新 deleted/retained/skipped，隐藏预览和表单，显示结果；
6. `image_exists=false` 时把状态写为“镜像已不存在”，隐藏页面删除入口并显示返回列表链接；
7. `image_exists=true` 时 GET `dialog.dataset.detailUrl`，用返回的 `item.tags` 更新 `[data-image-name-list]` 和 `[data-image-title]`；无 Tag 时用 `item.short_id` 并附加“未标记”；
8. 详情同步失败时保留删除结果，在错误区域显示“删除已完成，但详情同步失败，请手动刷新”；
9. DELETE 失败时显示错误并重新启用确认按钮。

所有 API 文本必须通过 `textContent` 写入，不得使用 `innerHTML`。

- [ ] **Step 5: 运行前端契约、语法和 Web 测试**

Run: `.venv/bin/python -m pytest -q tests/web/test_package_resources.py tests/web/test_images.py && node --check src/docker_manage_server/static/js/app.js`

Expected: pytest 全部通过，`node --check` exit 0。

- [ ] **Step 6: 提交异步交互**

```bash
git add src/docker_manage_server/static/js/app.js tests/web/test_package_resources.py
git commit -m "feat: delete image names without navigation"
```

---

### Task 4: 浏览器验收与完整验证

**Files:**
- Modify only if verification exposes a scoped defect.

**Interfaces:**
- Consumes: Tasks 1-3 完成的列表、详情弹框、API 和降级路径。
- Produces: 可复现的测试与浏览器验收证据；不新增功能接口。

- [ ] **Step 1: 运行完整自动化验证**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests
node --check src/docker_manage_server/static/js/app.js
DOCKER_MANAGE_DATA_DIR=/tmp/docker-manage-data DOCKER_MANAGE_HOST_PATH=/tmp/docker-manage-host docker compose config --quiet
git diff --check
```

Expected: pytest 零失败，其余命令均 exit 0。

- [ ] **Step 2: 在本地服务中验收列表和弹框**

启动测试服务后通过浏览器验证：

1. `/images` 首列显示“镜像名称”，点击名称进入正确详情；
2. 详情点击“删除可用镜像名称”只打开弹框，地址栏不变；
3. 弹框同时显示可删除、保留名称和并发提示；
4. 确认删除时页面不跳转，按钮在请求期间禁用；
5. 成功结果原地显示 deleted/retained/skipped；
6. 镜像保留时详情名称同步，镜像消失时出现返回列表入口；
7. 关闭按钮和点击 backdrop 均可关闭弹框。

- [ ] **Step 3: 检查最终仓库状态**

Run: `git status --short && git log -5 --oneline --decorate`

Expected: 工作树干净，当前分支为 `main`，最新提交为本计划的交互实现提交。

- [ ] **Step 4: 如验收产生修正则提交，否则记录无需额外提交**

如有修正：

```bash
git add <仅本次验收修正文件>
git commit -m "fix: polish image delete dialog workflow"
```

如无修正，不创建空提交。
