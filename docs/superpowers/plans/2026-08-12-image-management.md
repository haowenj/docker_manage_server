# 镜像管理模块 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增本机 Docker 镜像的服务端分页、搜索、详情、引用容器查询和安全删除模块。

**Architecture:** `DockerRuntime` 提供窄范围 Docker SDK 镜像适配器；新增 `ImageInventoryService` 负责聚合、搜索、分页、不可变 ID 引用匹配和删除边界。REST API 与服务端渲染 Web 页面共享该服务，Compose 项目详情只接受服务端验证后的容器深链参数。

**Tech Stack:** Python 3.11、Docker SDK for Python、FastAPI、Jinja2、pytest、原生 JavaScript

## Global Constraints

- 只搜索本机镜像，不访问远程仓库。
- 每页固定 20 条，服务端搜索、排序和分页。
- 同一完整镜像 ID 聚合为一条记录，展示全部 Tags，并保留 dangling 镜像。
- 删除前重新读取镜像和全部容器；运行中或已停止容器引用都会阻止删除。
- 所有删除都使用 `force=False`，不删除容器、卷或构建缓存。
- 多 Tag 删除中途失败立即返回 503，不重试、不回滚、不升级为强制删除。
- 所有容器引用匹配与删除动作都使用 Docker daemon 返回的完整不可变 ID。
- 所有变更直接提交到用户指定的 `main` 分支，不创建分支或 worktree，不推送远端。

---

## File Map

- `src/docker_manage_server/docker_runtime.py`：Docker SDK 镜像列表、详情、删除和异常映射。
- `src/docker_manage_server/image_inventory.py`：镜像数据结构、搜索、分页、引用查询与安全删除边界。
- `src/docker_manage_server/api.py`：装配镜像服务并暴露 REST API。
- `src/docker_manage_server/web.py`：镜像页面、删除表单和 Compose 容器深链。
- `src/docker_manage_server/web_views.py`：镜像大小、时间和引用链接的展示格式化。
- `src/docker_manage_server/templates/images/list.html`：搜索、分页和镜像列表。
- `src/docker_manage_server/templates/images/detail.html`：摘要、引用容器、inspect JSON 和删除操作。
- `src/docker_manage_server/templates/base.html`：一级“镜像管理”导航。
- `src/docker_manage_server/templates/compose_projects/detail.html`：经服务端验证的自动打开弹框标记。
- `src/docker_manage_server/static/js/app.js`：按固定 DOM ID 打开已验证弹框。
- `tests/unit/test_docker_runtime.py`：Docker SDK 镜像适配器测试。
- `tests/unit/test_image_inventory.py`：镜像服务业务边界测试。
- `tests/api/test_image_api.py`：REST API 成功与错误映射测试。
- `tests/web/test_images.py`：镜像页面、搜索、分页、详情和删除测试。
- `tests/web/test_compose_projects.py`：Compose 深链验证。
- `tests/web/test_security.py`：镜像删除同源边界测试。
- `tests/integration/test_real_docker.py`：唯一临时镜像和容器的真实 Docker 验证。
- `README.md`：入口、行为与安全删除说明。

---

### Task 1: Docker SDK 镜像适配器

**Files:**
- Modify: `src/docker_manage_server/docker_runtime.py`
- Modify: `tests/unit/test_docker_runtime.py`

**Interfaces:**
- Produces: `ImageNotFoundError(DockerRuntimeError)`
- Produces: `DockerRuntime.list_images() -> list[dict[str, Any]]`
- Produces: `DockerRuntime.get_serialized_image(image_id: str) -> dict[str, Any]`
- Produces: `DockerRuntime.remove_image(reference: str) -> None`
- Produces: `DockerRuntime._serialize_image(image: Any) -> dict[str, Any]`

- [ ] **Step 1: Write failing adapter tests**

Build a fake image with `Id`, `RepoTags`, `RepoDigests`, `Created`, `Size`, `Architecture`, `Os`, `Config.Entrypoint`, `Config.Cmd`. Assert list/get return identical serialized data, the ID is `sha256:immutable-image-id`, Tags and commands are preserved, and `raw_attrs` is the inspect object.

Add SDK `ImageNotFound` and `DockerException` fakes asserting lookup maps to `ImageNotFoundError`, list maps to `DockerRuntimeError`, and deletion calls exactly:

```python
client.images.remove("demo/app:1", force=False, noprune=False)
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/unit/test_docker_runtime.py -k image`

Expected: import or attribute failures because the image adapter is absent.

- [ ] **Step 3: Implement the adapter**

Alias Docker SDK `ImageNotFound`, add the application exception, and implement:

```python
    def list_images(self) -> list[dict[str, Any]]:
        try:
            images = self.client.images.list(all=True)
        except DockerException as exc:
            raise DockerRuntimeError(str(exc)) from exc
        return [self._serialize_image(image) for image in images]

    def get_serialized_image(self, image_id: str) -> dict[str, Any]:
        try:
            image = self.client.images.get(image_id)
        except SDKImageNotFound as exc:
            raise ImageNotFoundError(image_id) from exc
        except DockerException as exc:
            raise DockerRuntimeError(str(exc)) from exc
        return self._serialize_image(image)

    def remove_image(self, reference: str) -> None:
        try:
            self.client.images.remove(reference, force=False, noprune=False)
        except SDKImageNotFound as exc:
            raise ImageNotFoundError(reference) from exc
        except DockerException as exc:
            raise DockerRuntimeError(str(exc)) from exc
```

`_serialize_image()` returns `id`, `short_id`, `tags`, `digests`, `created`, `size`, `architecture`, `os`, `entrypoint`, `command`, `raw_attrs` with safe defaults.

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv/bin/python -m pytest -q tests/unit/test_docker_runtime.py`

Then commit `src/docker_manage_server/docker_runtime.py` and its unit test as `feat: expose docker image operations`.

---

### Task 2: 镜像查询、引用与安全删除服务

**Files:**
- Create: `src/docker_manage_server/image_inventory.py`
- Create: `tests/unit/test_image_inventory.py`

**Interfaces:**
- Consumes: Task 1 镜像方法和现有 `list_containers()`。
- Produces: `PAGE_SIZE = 20`。
- Produces: `ImageSummary`, `ImagePage`, `ImageDetail`, `ImageContainerReference`。
- Produces: `ImageInUseError`, `InvalidImagePageError`。
- Produces: `ImageInventoryService.list(query: str = "", page: int | str = 1) -> ImagePage`。
- Produces: `ImageInventoryService.get(image_id: str) -> ImageDetail`。
- Produces: `ImageInventoryService.remove(image_id: str) -> dict[str, Any]`。

- [ ] **Step 1: Write failing list/search/page tests**

Create a mutable fake runtime. Add duplicate full IDs with different Tags, dangling images, Unicode names, and 25 unique IDs. Assert fixed 20-item pages, accurate totals, case-insensitive Tag search, Unicode search, multi-Tag aggregation, and dangling retention. Parametrize invalid pages `0`, `-1`, `"x"`, `"1.5"`; a valid page beyond the end returns empty items with unchanged totals.

- [ ] **Step 2: Write failing reference and deletion tests**

Use containers with exact immutable `image_id`. Both running and stopped references must raise `ImageInUseError` without removal; unrelated image names/Tags must not count. For an unused multi-Tag image assert:

```python
deleted = service.remove("demo/app:1")
assert runtime.remove_calls == ["demo/app:1", "demo/app:2", "sha256:image"]
assert deleted == {"id": "sha256:image", "tags": ["demo/app:1", "demo/app:2"]}
```

Add a second-Tag failure test and a last-Tag-already-deleted-image test.

- [ ] **Step 3: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/unit/test_image_inventory.py`

Expected: module import fails.

- [ ] **Step 4: Implement data structures and query behavior**

Create frozen `ImageSummary`, `ImagePage`, `ImageContainerReference`, `ImageDetail` dataclasses with the exact fields from the approved spec. `list()` trims query, validates page, aggregates by full ID, unions and casefold-sorts Tags/Digests, sorts by `(created or "", id)` descending, matches full/stripped/short ID and Tags with Unicode `casefold()`, and slices 20 items.

- [ ] **Step 5: Implement references and safe removal**

`get()` fixes the daemon full ID and compares it to every container `image_id`. Reference objects include Compose project/service labels and sort by name/ID. `remove()` reuses this fresh detail, rejects any reference, deletes saved sorted Tags then full ID, and accepts final-ID `ImageNotFoundError` only after saved Tags were processed.

- [ ] **Step 6: Verify GREEN and commit**

Run: `.venv/bin/python -m pytest -q tests/unit/test_image_inventory.py tests/unit/test_docker_runtime.py`

Then commit the service and tests as `feat: query and safely remove images`.

---

### Task 3: 镜像 REST API

**Files:**
- Modify: `src/docker_manage_server/api.py`
- Create: `tests/api/test_image_api.py`

**Interfaces:**
- Consumes: Task 2 服务。
- Produces: `GET /api/images`, `GET /api/images/{image_id}`, `DELETE /api/images/{image_id}`。
- Error mapping: 404 `ImageNotFoundError`; 409 `ImageInUseError`; 422 `InvalidImagePageError`; 503 `DockerRuntimeError`。

- [ ] **Step 1: Write failing API tests**

Create a fake runtime and app fixture. Cover list query/page payload, detail inspect/references, used-image conflict, safe delete stable identity, missing 404, invalid page 422, and list/detail/delete 503. Assert delete returns:

```python
{
    "deleted": True,
    "id": "sha256:image",
    "tags": ["demo:1"],
}
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/api/test_image_api.py`

Expected: routes return 404.

- [ ] **Step 3: Assemble service and routes**

Construct `images = ImageInventoryService(runtime)`, expose `app.state.images`, and pass the same instance to `create_web_router`. Use `dataclasses.asdict` for list/detail payloads. Add the three explicit routes and exact exception mappings.

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv/bin/python -m pytest -q tests/api/test_image_api.py tests/web/test_security.py`

Then commit API and tests as `feat: expose image management api`.

---

### Task 4: 镜像列表、详情和删除页面

**Files:**
- Modify: `src/docker_manage_server/web.py`
- Modify: `src/docker_manage_server/web_views.py`
- Modify: `src/docker_manage_server/templates/base.html`
- Create: `src/docker_manage_server/templates/images/list.html`
- Create: `src/docker_manage_server/templates/images/detail.html`
- Modify: `src/docker_manage_server/static/css/app.css`
- Modify: `tests/web/conftest.py`
- Create: `tests/web/test_images.py`
- Modify: `tests/web/test_package_resources.py`

**Interfaces:**
- Produces: `GET /images`, `GET /images/{image_id}`, `POST /images/{image_id}/delete`。
- Produces: `image_summary_view()` and `image_reference_view()`。

- [ ] **Step 1: Extend WebFakeRuntime and write failing page tests**

Add mutable images, `list_images`, `get_serialized_image`, `remove_image`, and `image_remove_calls` to `WebFakeRuntime`. Create `test_images.py` covering navigation active state, 25 records across 20+5 pages, query retention, dangling/multi-Tag display, escaped inspect JSON, independent/Compose reference links, used-image message and absent delete form, unused-image confirmation and 303, plus 404/409/422/503.

Key assertions:

```python
assert "共 25 个镜像" in response.text
assert "第 2 / 2 页" in response.text
assert "q=demo&amp;page=1" in response.text
assert response.text.count('class="image-row"') == 5
assert 'href="/containers/direct-full-id"' in detail.text
assert 'href="/compose-projects/mall?container=compose-full-id"' in detail.text
assert "&lt;script&gt;" in detail.text
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/web/test_images.py`

Expected: routes return 404.

- [ ] **Step 3: Implement presentation helpers**

`image_summary_view()` renders Tags or `未标记`, local formatted created time, IEC size, entrypoint and command. `image_reference_view()` URL-encodes exact IDs and emits independent `/containers/{id}` or Compose `/compose-projects/{project}?container={id}` links.

- [ ] **Step 4: Add routes and exact error mapping**

List uses `images.list(q, page)` and maps invalid page to 422. Detail calls `images.get()` and serializes inspect using `json.dumps(detail.inspect, ensure_ascii=False, indent=2, sort_keys=True)`. Delete maps 404/409/503 and redirects `/images` with 303 on success.

- [ ] **Step 5: Build templates, navigation and CSS**

Add sidebar `镜像管理`. The GET search form omits `page` so a new search resets to page 1. List shows total/page summary and previous/next URLs preserving encoded `q`. Detail shows summary, reference table and escaped inspect JSON. Only unused images receive a delete form with `data-confirm="确认删除此镜像及其全部 Tags？该操作不可恢复。"`.

Add minimal classes for search, Tags, pagination and inspect layout. Package tests assert both image templates exist.

- [ ] **Step 6: Verify GREEN and commit**

Run: `.venv/bin/python -m pytest -q tests/web/test_images.py tests/web/test_package_resources.py tests/api/test_image_api.py`

Then commit production and Web test files as `feat: add image management pages`.

---

### Task 5: Compose 引用容器深链

**Files:**
- Modify: `src/docker_manage_server/web.py`
- Modify: `src/docker_manage_server/templates/compose_projects/detail.html`
- Modify: `src/docker_manage_server/static/js/app.js`
- Modify: `tests/web/test_compose_projects.py`
- Modify: `tests/web/test_package_resources.py`

**Interfaces:**
- Consumes: `GET /compose-projects/{project_name}?container={full_id}`。
- Produces: server-validated `auto_open_dialog_id: str | None`。

- [ ] **Step 1: Write failing deep-link tests**

Create a project with one full container ID. Assert the matching parameter produces `data-auto-open-dialog="container-dialog-{full_id}"`; unknown, short/name and cross-project IDs produce an empty marker. Cross-project IDs must not appear in response text. Package JS test finds `document.getElementById(autoOpenDialogId)` and still rejects `CSS.escape`.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/web/test_compose_projects.py -k auto_open tests/web/test_package_resources.py`

Expected: marker and JS assertions fail.

- [ ] **Step 3: Validate exact full ID on the server**

Accept `container: str | None = None` in the Compose detail route. After project load, set `auto_open_dialog_id = f"container-dialog-{container}"` only when an item in `project.containers` has an exactly equal full ID. Pass it to the template. Exact equality rejects names, short IDs and cross-project containers.

- [ ] **Step 4: Open only the validated dialog on load**

Emit `data-auto-open-dialog="{{ auto_open_dialog_id or '' }}"` on the detail root. JS reads that dataset value, calls `document.getElementById(autoOpenDialogId)`, verifies `HTMLDialogElement`, and calls `showModal()` only if it is not already open. Do not add `CSS.escape`.

- [ ] **Step 5: Verify GREEN and commit**

Run `.venv/bin/python -m pytest -q tests/web/test_compose_projects.py tests/web/test_package_resources.py`, then `node --check src/docker_manage_server/static/js/app.js`.

Commit the five files as `feat: deep link image container references`.

---

### Task 6: 安全边界、真实 Docker、文档和验收

**Files:**
- Modify: `tests/web/test_security.py`
- Modify: `tests/integration/test_real_docker.py`
- Modify: `README.md`

**Interfaces:**
- Verifies Tasks 1-5; no new production interface.

- [ ] **Step 1: Add cross-origin deletion tests**

For Web POST and API DELETE, send `Origin: https://evil.example`, assert 403, and assert image removal calls remain empty.

- [ ] **Step 2: Add isolated real Docker verification**

Use a UUID name/tag and `docker-manage.test=image-management` label. Require already-local `alpine:3.21`; skip without pulling if absent. Build a tiny unique image, find it through search, create a unique stopped container and verify conflict, start it and verify conflict again, remove it, safely delete the image, and verify it is gone. `finally` may force-clean only exact uniquely labelled test resources.

Run: `.venv/bin/python -m pytest -q tests/integration/test_real_docker.py::test_real_image_inventory_and_safe_deletion`

Expected: pass with Docker/local base, otherwise clean skip.

- [ ] **Step 3: Update README**

Document `/images`, local-only search, fixed 20-item pages, inspect JSON, reference navigation, and no-force deletion blocked by both running and stopped containers.

- [ ] **Step 4: Browser validation with fake data**

Use a local fake app, never the real daemon. Verify nav/search/page retention, multi-Tag/dangling rows, escaped inspect, used-image no-delete, unused-image confirm/303, exact Compose dialog deep link, and invalid/cross-project deep links opening none.

- [ ] **Step 5: Full verification**

Run these commands independently and require exit 0:

```text
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests
node --check src/docker_manage_server/static/js/app.js
docker compose config --quiet with DOCKER_MANAGE_IMAGE, DOCKER_MANAGE_HOST_PORT and DOCKER_MANAGE_SERVER_PATH set to test values
git diff --check
```

Real Docker tests may skip only for unavailable daemon or absent local base image.

- [ ] **Step 6: Commit and independent review**

Commit README/integration/security as `docs: verify image management workflows`. Review `a2750e5..HEAD` against the approved spec. Fix every Critical/Important finding with regression tests and repeat full verification. Keep local `main`; do not push.

---

### Task 7: 按容器原始引用预览并删除可用 Tags

**Files:**
- Modify: `src/docker_manage_server/docker_runtime.py`
- Modify: `src/docker_manage_server/image_inventory.py`
- Modify: `src/docker_manage_server/api.py`
- Modify: `src/docker_manage_server/web.py`
- Modify: `src/docker_manage_server/templates/images/detail.html`
- Create: `src/docker_manage_server/templates/images/delete.html`
- Create: `src/docker_manage_server/templates/images/delete_result.html`
- Modify: image unit/API/Web/integration tests and `README.md`

**Interfaces:**
- Container serialization produces `image_reference` from inspect `Config.Image`.
- `preview_tag_removal(image_id) -> ImageTagRemovalPreview` returns immutable ID, deletable and retained Tags.
- `remove_available_tags(image_id) -> ImageTagRemovalResult` returns deleted, retained, skipped Tags and image existence.
- Web preview/POST/result routes and API preview/DELETE-tags routes follow the design revision.

- [ ] Write failing tests for original Tag references, running/stopped retention, dangling/all-used 409, submit-time recomputation, disappeared/repointed skip, per-image lock, 503 deletion/serialization errors, preview/result HTML and API payloads.
- [ ] Run focused tests and confirm failures reflect the old whole-image behavior.
- [ ] Implement container `image_reference`, frozen preview/result dataclasses, per-image locks, Tag classification and non-force removal without explicit full-ID deletion.
- [ ] Implement API preview and Tag deletion routes; remove the old whole-image DELETE route.
- [ ] Implement preview confirmation and result pages with short-lived in-memory result IDs; change detail action to open preview.
- [ ] Update real Docker coverage and README to Tag-level semantics.
- [ ] Run focused and complete verification, commit, and request review.
