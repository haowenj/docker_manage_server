# Image Batch Deletion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在镜像列表当前页提供安全的批量选择、占用预检和确认删除，只删除未被任何运行中或已停止容器引用的完整镜像，并清理未占用的无 Tag 悬空镜像。

**Architecture:** `ImageInventoryService` 新增按不可变镜像 ID 工作的批量预检与整镜像删除接口，API 用两个同源 POST 端点暴露预检和执行结果。Jinja 列表模板只渲染当前页候选项，独立 ES module 管理选择、弹框、安全 DOM 更新和刷新导航；现有详情页逐 Tag 删除路径保持不变。

**Tech Stack:** Python 3.11、FastAPI、Pydantic v2、Jinja2、Docker SDK、原生 HTML `<dialog>`、原生 ES modules、Node.js 内置测试运行器、pytest。

## Global Constraints

- 批量选择只作用于当前页，翻页、搜索、清除搜索或取消批量模式时清空选择。
- 运行中和已停止的现存容器都算占用；被任一容器引用的镜像整张跳过，不删除任何 Tag。
- 未占用的有 Tag 镜像和无 Tag 悬空镜像都按完整不可变镜像 ID 删除整张镜像。
- 用户确认时必须重新读取镜像和容器状态；预检结果不是删除授权。
- 不使用 Docker `force`，不删除容器，不改变现有详情页单张镜像名称删除功能。
- 每次请求只能包含 1 至 20 个不同的 `sha256:<64 个十六进制字符>` 镜像 ID。
- 单项 Docker 删除失败必须记录后继续处理其他镜像；初始 Docker 可用性预检失败时不得开始删除。
- 批量删除依赖 JavaScript，不新增无 JavaScript 批量降级表单。
- 不引入新的前端包或大型测试依赖；ES module 使用 Node.js 内置 `node:test` 验证纯函数。
- 所有不可信文本通过 Jinja 自动转义或 DOM `textContent` 渲染，不使用 `innerHTML`。

## File Structure

- `src/docker_manage_server/image_inventory.py`：批量预检/删除的数据类、容器引用快照转换、逐镜像锁和建议页码计算。
- `src/docker_manage_server/api.py`：严格的 Pydantic 请求模型、两个批量 POST 路由和 422/503 映射。
- `src/docker_manage_server/templates/images/list.html`：当前页选择列、操作栏和批量预检/结果弹框契约。
- `src/docker_manage_server/static/js/image_batch_delete.mjs`：选择状态、预检、确认、结果渲染和安全刷新；纯函数同时供 Node 测试导入。
- `src/docker_manage_server/static/css/app.css`：批量工具栏、选择列、结果列表和响应式布局。
- `pyproject.toml`：把 `static/js/*.mjs` 纳入 wheel package data。
- `tests/unit/test_image_inventory.py`：服务层预检、占用、悬空、竞态、部分失败和页码测试。
- `tests/api/test_image_api.py`：请求校验、预检/执行响应和初始 503 测试。
- `tests/web/test_images.py`：列表模板和当前查询/页码 DOM 契约。
- `tests/web/test_security.py`：两个新增 POST 端点的跨源拒绝。
- `tests/web/test_package_resources.py`：模块安全 DOM 契约和打包检查。
- `tests/js/image_batch_delete.test.mjs`：选择状态、显示名称和列表刷新 URL 的 Node 原生单元测试。

---

### Task 1: 服务层批量预检与整镜像删除

**Files:**
- Modify: `src/docker_manage_server/image_inventory.py`
- Test: `tests/unit/test_image_inventory.py`

**Interfaces:**
- Consumes: `DockerRuntime.list_images()`, `DockerRuntime.list_containers()`, `DockerRuntime.get_serialized_image(image_id)`, `DockerRuntime.remove_image(image_id)`。
- Produces: `ImageBatchItem`, `ImageBatchPreviewResult`, `ImageBatchDeleteResult`, `ImageInventoryService.preview_batch_removal(image_ids)`, `ImageInventoryService.remove_unused_images(image_ids, query, page)`。

- [ ] **Step 1: 写批量预检的失败测试**

在 `tests/unit/test_image_inventory.py` 的 import 中加入 `ImageBatchPreviewResult`，并追加以下测试。测试使用完整 ID，避免服务测试与 API 格式约束脱节：

```python
USED_ID = "sha256:" + "1" * 64
FREE_ID = "sha256:" + "2" * 64
DANGLING_ID = "sha256:" + "3" * 64
MISSING_ID = "sha256:" + "4" * 64


def test_batch_preview_groups_free_dangling_used_and_missing_images():
    runtime = FakeRuntime(
        [
            image(USED_ID, ("demo/used:1",)),
            image(FREE_ID, ("demo/free:1",)),
            image(DANGLING_ID),
        ],
        [
            container("running", USED_ID, name="z-running", running=True),
            container("stopped", USED_ID, name="a-stopped", running=False),
        ],
    )

    result = ImageInventoryService(runtime).preview_batch_removal(
        (USED_ID, FREE_ID, DANGLING_ID, MISSING_ID)
    )

    assert isinstance(result, ImageBatchPreviewResult)
    assert [item.id for item in result.deletable] == [FREE_ID, DANGLING_ID]
    assert [item.id for item in result.in_use] == [USED_ID]
    assert [item.name for item in result.in_use[0].containers] == [
        "a-stopped",
        "z-running",
    ]
    assert [item.running for item in result.in_use[0].containers] == [False, True]
    assert result.missing == (MISSING_ID,)
    assert result.deletable[1].tags == ()
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
.venv/bin/pytest tests/unit/test_image_inventory.py::test_batch_preview_groups_free_dangling_used_and_missing_images -v
```

Expected: collection fails because `ImageBatchPreviewResult` and `preview_batch_removal` do not exist yet.

- [ ] **Step 3: 实现预检数据类和单次容器快照分类**

在 `ImageContainerReference` 之后增加：

```python
@dataclass(frozen=True)
class ImageBatchItem:
    id: str
    short_id: str
    tags: tuple[str, ...]
    containers: tuple[ImageContainerReference, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class ImageBatchPreviewResult:
    deletable: tuple[ImageBatchItem, ...]
    in_use: tuple[ImageBatchItem, ...]
    missing: tuple[str, ...]


@dataclass(frozen=True)
class ImageBatchDeleteResult:
    deleted: tuple[ImageBatchItem, ...]
    in_use: tuple[ImageBatchItem, ...]
    missing: tuple[str, ...]
    failed: tuple[ImageBatchItem, ...]
    suggested_page: int
```

在 `ImageInventoryService` 中增加：

```python
    def preview_batch_removal(
        self,
        image_ids: tuple[str, ...],
    ) -> ImageBatchPreviewResult:
        summaries = {
            item.id: item for item in _summaries(self.runtime.list_images())
        }
        containers = self.runtime.list_containers()
        deletable = []
        in_use = []
        missing = []
        for image_id in image_ids:
            summary = summaries.get(image_id)
            if summary is None:
                missing.append(image_id)
                continue
            references = _references_from(containers, image_id)
            item = _batch_item(summary, references)
            (in_use if references else deletable).append(item)
        return ImageBatchPreviewResult(
            deletable=tuple(deletable),
            in_use=tuple(in_use),
            missing=tuple(missing),
        )
```

把 `_references()` 改为委托共享 helper，并在模块级追加以下实现，确保预检只读一次容器列表且与详情页使用相同匹配语义：

```python
    def _references(
        self,
        immutable_image_id: str,
    ) -> tuple[ImageContainerReference, ...]:
        return _references_from(
            self.runtime.list_containers(),
            immutable_image_id,
        )


def _batch_item(
    summary: ImageSummary,
    containers: tuple[ImageContainerReference, ...] = (),
    error: str | None = None,
) -> ImageBatchItem:
    return ImageBatchItem(
        id=summary.id,
        short_id=summary.short_id,
        tags=summary.tags,
        containers=containers,
        error=error,
    )


def _references_from(
    containers: list[dict[str, Any]],
    immutable_image_id: str,
) -> tuple[ImageContainerReference, ...]:
    references = []
    for item in containers:
        if item.get("image_id") != immutable_image_id:
            continue
        labels = _labels(item)
        references.append(
            ImageContainerReference(
                id=str(item["id"]),
                name=str(item.get("name") or item["id"]),
                status=str(item.get("status") or "unknown"),
                running=bool(item.get("running")),
                compose_project=_optional_text(labels.get(COMPOSE_PROJECT_LABEL)),
                compose_service=_optional_text(labels.get(COMPOSE_SERVICE_LABEL)),
            )
        )
    return tuple(
        sorted(references, key=lambda item: (item.name.casefold(), item.id))
    )
```

- [ ] **Step 4: 运行预检测试并确认 GREEN**

Run:

```bash
.venv/bin/pytest tests/unit/test_image_inventory.py::test_batch_preview_groups_free_dangling_used_and_missing_images -v
```

Expected: PASS.

- [ ] **Step 5: 写执行阶段的失败测试**

保留 `FakeRuntime` 已有的 `fail_reference` 删除失败能力，并追加：

```python
def test_batch_remove_deletes_whole_images_and_continues_after_failure():
    failing_id = "sha256:" + "5" * 64
    runtime = FakeRuntime(
        [
            image(FREE_ID, ("demo/free:1", "demo/free:2")),
            image(DANGLING_ID),
            image(failing_id, ("demo/fail:1",)),
            image(USED_ID, ("demo/used:1",)),
        ],
        [container("stopped", USED_ID, name="consumer", running=False)],
    )
    runtime.fail_reference = failing_id

    result = ImageInventoryService(runtime).remove_unused_images(
        (FREE_ID, failing_id, DANGLING_ID, USED_ID, MISSING_ID),
        query="demo",
        page=2,
    )

    assert runtime.remove_calls == [FREE_ID, failing_id, DANGLING_ID]
    assert [item.id for item in result.deleted] == [FREE_ID, DANGLING_ID]
    assert [item.id for item in result.in_use] == [USED_ID]
    assert result.in_use[0].containers[0].name == "consumer"
    assert result.missing == (MISSING_ID,)
    assert [item.id for item in result.failed] == [failing_id]
    assert result.failed[0].error == "remove failed"
    assert result.suggested_page == 1


def test_batch_remove_rechecks_containers_after_preview():
    runtime = FakeRuntime([image(FREE_ID, ("demo/free:1",))])
    service = ImageInventoryService(runtime)
    assert [item.id for item in service.preview_batch_removal((FREE_ID,)).deletable] == [
        FREE_ID
    ]
    runtime.containers.append(
        container("new-container", FREE_ID, name="new-consumer", running=True)
    )

    result = service.remove_unused_images((FREE_ID,), query="", page=1)

    assert runtime.remove_calls == []
    assert [item.id for item in result.in_use] == [FREE_ID]
    assert result.in_use[0].containers[0].name == "new-consumer"


def test_batch_remove_requires_successful_initial_docker_snapshot():
    runtime = FakeRuntime([image(FREE_ID, ("demo/free:1",))])

    def fail_containers():
        raise DockerRuntimeError("daemon offline")

    runtime.list_containers = fail_containers

    with pytest.raises(DockerRuntimeError, match="daemon offline"):
        ImageInventoryService(runtime).remove_unused_images(
            (FREE_ID,), query="", page=1
        )

    assert runtime.remove_calls == []
```

- [ ] **Step 6: 运行执行测试并确认 RED**

Run:

```bash
.venv/bin/pytest \
  tests/unit/test_image_inventory.py::test_batch_remove_deletes_whole_images_and_continues_after_failure \
  tests/unit/test_image_inventory.py::test_batch_remove_rechecks_containers_after_preview \
  tests/unit/test_image_inventory.py::test_batch_remove_requires_successful_initial_docker_snapshot -v
```

Expected: FAIL because `remove_unused_images` does not exist.

- [ ] **Step 7: 实现逐镜像重新检查、整镜像删除和页码建议**

在 `ImageInventoryService` 中增加：

```python
    def remove_unused_images(
        self,
        image_ids: tuple[str, ...],
        query: str = "",
        page: int = 1,
    ) -> ImageBatchDeleteResult:
        self.preview_batch_removal(image_ids)
        deleted = []
        in_use = []
        missing = []
        failed = []
        for image_id in image_ids:
            with self._lock_for(image_id):
                try:
                    summary = _summary(
                        self.runtime.get_serialized_image(image_id)
                    )
                except ImageNotFoundError:
                    missing.append(image_id)
                    continue
                except DockerRuntimeError as exc:
                    failed.append(_unknown_batch_item(image_id, str(exc)))
                    continue
                try:
                    references = _references_from(
                        self.runtime.list_containers(), image_id
                    )
                except DockerRuntimeError as exc:
                    failed.append(_batch_item(summary, error=str(exc)))
                    continue
                if references:
                    in_use.append(_batch_item(summary, references))
                    continue
                try:
                    self.runtime.remove_image(image_id)
                except ImageNotFoundError:
                    missing.append(image_id)
                except DockerRuntimeError as exc:
                    failed.append(_batch_item(summary, error=str(exc)))
                else:
                    deleted.append(_batch_item(summary))
        return ImageBatchDeleteResult(
            deleted=tuple(deleted),
            in_use=tuple(in_use),
            missing=tuple(missing),
            failed=tuple(failed),
            suggested_page=self._suggested_page(query, page),
        )

    def _suggested_page(self, query: str, requested_page: int) -> int:
        try:
            current = self.list(query=query, page=1)
        except DockerRuntimeError:
            return requested_page
        return min(requested_page, max(current.total_pages, 1))
```

模块级增加缺失 inspect 时仍可表达失败项的 helper：

```python
def _unknown_batch_item(image_id: str, error: str) -> ImageBatchItem:
    return ImageBatchItem(
        id=image_id,
        short_id=image_id.removeprefix("sha256:")[:12],
        tags=(),
        error=error,
    )
```

`_suggested_page()` 的 Docker 刷新失败不会抹掉已完成的删除事实；此时前端仍刷新请求页，普通列表路由负责显示最新状态或 503。

- [ ] **Step 8: 运行镜像服务层测试并确认 GREEN**

Run:

```bash
.venv/bin/pytest tests/unit/test_image_inventory.py -v
```

Expected: all image inventory tests PASS.

- [ ] **Step 9: 提交服务层实现**

```bash
git add src/docker_manage_server/image_inventory.py tests/unit/test_image_inventory.py
git commit -m "feat: add safe image batch deletion service"
```

---

### Task 2: 严格批量 API 契约

**Files:**
- Modify: `src/docker_manage_server/api.py`
- Test: `tests/api/test_image_api.py`

**Interfaces:**
- Consumes: Task 1 的 `preview_batch_removal()` 和 `remove_unused_images()`，结果通过 `dataclasses.asdict()` 序列化。
- Produces: `POST /api/images/batch-delete-preview` 和 `POST /api/images/batch-delete`。

- [ ] **Step 1: 扩展 API fake runtime 和镜像 fixture**

把 `tests/api/test_image_api.py` 的 fixture 改为使用真实格式常量，并让 fake runtime 支持按 ID 单项失败：

```python
IMAGE_ID = "sha256:" + "a" * 64
FREE_ID = "sha256:" + "b" * 64
DANGLING_ID = "sha256:" + "c" * 64
MISSING_ID = "sha256:" + "d" * 64


def image_fixture(image_id=IMAGE_ID, tags=("demo:1",)):
    return {
        "id": image_id,
        "short_id": image_id.removeprefix("sha256:")[:12],
        "tags": list(tags),
        "digests": [],
        "created": "2026-08-12T00:00:00Z",
        "size": 100,
        "architecture": "amd64",
        "os": "linux",
        "entrypoint": None,
        "command": ["serve"],
        "raw_attrs": {"Id": image_id},
    }
```

同步当前测试中的 `sha256:image` 为 `IMAGE_ID`。在 `ImageApiRuntime.__init__` 增加 `self.fail_reference = None`，并在 `remove_image()` 开头加入：

```python
        if reference == self.fail_reference:
            raise DockerRuntimeError("remove failed")
```

- [ ] **Step 2: 写预检与执行 API 的失败测试**

追加：

```python
def test_image_batch_api_previews_then_deletes_only_unused_images(client):
    runtime = client.app.state.test_runtime
    runtime.images.extend(
        [
            image_fixture(FREE_ID, ("demo/free:1", "demo/free:2")),
            image_fixture(DANGLING_ID, ()),
        ]
    )

    preview = client.post(
        "/api/images/batch-delete-preview",
        json={"image_ids": [IMAGE_ID, FREE_ID, DANGLING_ID, MISSING_ID]},
    )

    assert preview.status_code == 200
    assert [item["id"] for item in preview.json()["deletable"]] == [
        FREE_ID,
        DANGLING_ID,
    ]
    assert preview.json()["in_use"][0]["id"] == IMAGE_ID
    assert preview.json()["in_use"][0]["containers"][0]["name"] == "consumer"
    assert preview.json()["missing"] == [MISSING_ID]

    deleted = client.post(
        "/api/images/batch-delete",
        json={
            "image_ids": [IMAGE_ID, FREE_ID, DANGLING_ID, MISSING_ID],
            "query": "demo",
            "page": 2,
        },
    )

    assert deleted.status_code == 200
    payload = deleted.json()
    assert [item["id"] for item in payload["deleted"]] == [FREE_ID, DANGLING_ID]
    assert [item["id"] for item in payload["in_use"]] == [IMAGE_ID]
    assert payload["missing"] == [MISSING_ID]
    assert payload["failed"] == []
    assert payload["suggested_page"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"image_ids": []},
        {"image_ids": [IMAGE_ID, IMAGE_ID]},
        {"image_ids": ["sha256:not-hex"]},
        {"image_ids": ["a" * 64]},
        {"image_ids": [f"sha256:{index:064x}" for index in range(21)]},
        {"image_ids": [IMAGE_ID], "page": 0},
        {"image_ids": [IMAGE_ID], "unexpected": True},
    ],
)
def test_image_batch_api_rejects_invalid_payloads(client, payload):
    path = (
        "/api/images/batch-delete"
        if "page" in payload or "unexpected" in payload
        else "/api/images/batch-delete-preview"
    )
    assert client.post(path, json=payload).status_code == 422


def test_image_batch_execute_maps_initial_snapshot_failure_to_503(client):
    runtime = client.app.state.test_runtime
    runtime.fail = "list"

    response = client.post(
        "/api/images/batch-delete",
        json={"image_ids": [IMAGE_ID], "query": "", "page": 1},
    )

    assert response.status_code == 503
    assert runtime.images == [image_fixture()]


def test_image_batch_api_returns_item_failure_and_continues(client):
    runtime = client.app.state.test_runtime
    runtime.containers = []
    runtime.images.extend(
        [
            image_fixture(FREE_ID, ("demo/free:1",)),
            image_fixture(DANGLING_ID, ()),
        ]
    )
    runtime.fail_reference = FREE_ID

    response = client.post(
        "/api/images/batch-delete",
        json={
            "image_ids": [FREE_ID, DANGLING_ID],
            "query": "",
            "page": 1,
        },
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["failed"]] == [FREE_ID]
    assert response.json()["failed"][0]["error"] == "remove failed"
    assert [item["id"] for item in response.json()["deleted"]] == [DANGLING_ID]
```

- [ ] **Step 3: 运行 API 测试并确认 RED**

Run:

```bash
.venv/bin/pytest \
  tests/api/test_image_api.py::test_image_batch_api_previews_then_deletes_only_unused_images \
  tests/api/test_image_api.py::test_image_batch_api_rejects_invalid_payloads \
  tests/api/test_image_api.py::test_image_batch_execute_maps_initial_snapshot_failure_to_503 \
  tests/api/test_image_api.py::test_image_batch_api_returns_item_failure_and_continues -v
```

Expected: FAIL with 404 because both POST routes are absent.

- [ ] **Step 4: 增加 Pydantic 请求模型和校验器**

在 `api.py` 中把 Pydantic import 改为：

```python
from pydantic import BaseModel, ConfigDict, Field, field_validator
```

在 `DeploymentConfigurationPayload` 后增加：

```python
IMAGE_ID_PATTERN = r"^sha256:[0-9a-fA-F]{64}$"


class ImageBatchPreviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("image_ids")
    @classmethod
    def validate_image_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("image_ids must not contain duplicates")
        import re

        if any(re.fullmatch(IMAGE_ID_PATTERN, item) is None for item in value):
            raise ValueError("image_ids must contain full sha256 image IDs")
        return value


class ImageBatchDeletePayload(ImageBatchPreviewPayload):
    query: str = ""
    page: int = Field(default=1, ge=1)
```

把 `import re` 移到模块顶层，与其他标准库 import 放在一起，并删除 validator 内的局部 import。

- [ ] **Step 5: 在动态 image ID 路由之前增加两个 POST 路由**

紧跟 `list_images()` 路由后加入：

```python
    @app.post("/api/images/batch-delete-preview")
    def preview_image_batch_delete(
        payload: ImageBatchPreviewPayload,
    ) -> dict[str, Any]:
        try:
            return asdict(
                images.preview_batch_removal(tuple(payload.image_ids))
            )
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/images/batch-delete")
    def delete_image_batch(
        payload: ImageBatchDeletePayload,
    ) -> dict[str, Any]:
        try:
            return asdict(
                images.remove_unused_images(
                    tuple(payload.image_ids),
                    query=payload.query,
                    page=payload.page,
                )
            )
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
```

- [ ] **Step 6: 运行镜像 API 测试并确认 GREEN**

Run:

```bash
.venv/bin/pytest tests/api/test_image_api.py -v
```

Expected: all image API tests PASS.

- [ ] **Step 7: 提交 API 实现**

```bash
git add src/docker_manage_server/api.py tests/api/test_image_api.py
git commit -m "feat: expose image batch deletion API"
```

---

### Task 3: 列表批量模式的服务端 DOM 与样式

**Files:**
- Modify: `src/docker_manage_server/templates/images/list.html`
- Modify: `src/docker_manage_server/static/css/app.css`
- Test: `tests/web/test_images.py`

**Interfaces:**
- Consumes: `images_page()` 已提供的 `image_page` 和 `images`；每行 `image.item.id` 是完整不可变 ID。
- Produces: `data-image-batch` 根节点、操作按钮、选择列、API URL、当前查询/页码和弹框插槽，供 Task 4 ES module 绑定。

- [ ] **Step 1: 写模板契约失败测试**

在 `tests/web/test_images.py` 追加：

```python
def test_image_list_exposes_current_page_batch_delete_controls(web_context):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(index) for index in range(25)]

    response = client.get("/images?q=demo&page=2")

    assert response.status_code == 200
    assert 'data-image-batch' in response.text
    assert 'data-preview-url="/api/images/batch-delete-preview"' in response.text
    assert 'data-delete-url="/api/images/batch-delete"' in response.text
    assert 'data-query="demo"' in response.text
    assert 'data-page="2"' in response.text
    assert 'data-image-batch-enter' in response.text
    assert 'data-image-batch-cancel' in response.text
    assert 'data-image-batch-submit' in response.text
    assert 'data-image-select-all' in response.text
    assert response.text.count('data-image-select-item') == 5
    assert 'value="sha256:image-4"' in response.text
    assert 'id="image-batch-delete-dialog"' in response.text
    assert 'data-image-batch-confirm' in response.text
    assert 'type="module"' in response.text
    assert '/static/js/image_batch_delete.mjs' in response.text


def test_empty_image_list_disables_batch_entry(web_context):
    client, _store, runtime = web_context
    runtime.images = []

    response = client.get("/images")

    assert response.status_code == 200
    assert 'data-image-batch-enter disabled' in response.text
    assert 'data-image-select-item' not in response.text
```

- [ ] **Step 2: 运行模板测试并确认 RED**

Run:

```bash
.venv/bin/pytest \
  tests/web/test_images.py::test_image_list_exposes_current_page_batch_delete_controls \
  tests/web/test_images.py::test_empty_image_list_disables_batch_entry -v
```

Expected: FAIL because the list has no batch DOM contract.

- [ ] **Step 3: 用明确 DOM 契约改写镜像列表模板**

保留现有搜索、列内容和分页逻辑，把 `templates/images/list.html` 改为以下结构；各结果列表由 JavaScript 用 `replaceChildren()` 安全填充：

```html
{% extends "base.html" %}
{% block content %}
<section class="panel" data-image-batch data-preview-url="/api/images/batch-delete-preview" data-delete-url="/api/images/batch-delete" data-query="{{ image_page.query }}" data-page="{{ image_page.page }}">
  <div class="panel-heading">
    <div><h2>本机镜像</h2><span class="muted">共 {{ image_page.total_items }} 个镜像</span></div>
    <div class="image-batch-actions">
      <button class="button button-danger" type="button" data-image-batch-enter{% if not images %} disabled{% endif %}>批量删除</button>
      <div class="image-batch-mode-actions" data-image-batch-actions hidden>
        <button class="button button-danger" type="button" data-image-batch-submit disabled>删除已选（<span data-image-selected-count>0</span>）</button>
        <button class="button button-secondary" type="button" data-image-batch-cancel>取消</button>
      </div>
    </div>
  </div>
  <form class="search-form" method="get" action="/images">
    <label for="image-search">搜索镜像</label>
    <input id="image-search" type="search" name="q" value="{{ image_page.query }}" placeholder="仓库名、Tag 或镜像 ID">
    <button class="button button-primary" type="submit">搜索</button>
    {% if image_page.query %}<a class="button button-secondary" href="/images">清除</a>{% endif %}
  </form>
  <div class="table-scroll"><table>
    <thead><tr><th class="image-select-cell" data-image-select-cell hidden><input type="checkbox" aria-label="全选当前页镜像" data-image-select-all></th><th>镜像名称</th><th>镜像 ID</th><th>大小</th><th>创建时间</th><th>引用容器</th></tr></thead>
    <tbody>{% for image in images %}<tr class="image-row"><td class="image-select-cell" data-image-select-cell hidden><input type="checkbox" value="{{ image.item.id }}" aria-label="选择镜像 {{ image.display_name }}" data-image-select-item></td><td><div class="tag-list">{% if image.item.tags %}{% for tag in image.item.tags %}<a class="tag" href="/images/{{ image.item.id }}">{{ tag }}</a>{% endfor %}{% else %}<a class="tag" href="/images/{{ image.item.id }}">{{ image.item.short_id }}</a><span class="muted">未标记</span>{% endif %}</div></td><td><a href="/images/{{ image.item.id }}"><code>{{ image.item.short_id }}</code></a></td><td>{{ image.size }}</td><td>{{ image.created }}</td><td>{{ image.item.container_count }}</td></tr>{% else %}<tr><td colspan="6" class="empty-cell">没有符合条件的本机镜像。</td></tr>{% endfor %}</tbody>
  </table></div>
  <nav class="pagination" aria-label="镜像分页"><span>第 {{ image_page.page }} / {{ image_page.total_pages }} 页</span><div>{% if image_page.page > 1 %}<a class="button button-secondary" href="/images?q={{ image_page.query|urlencode }}&amp;page={{ image_page.page - 1 }}">上一页</a>{% endif %}{% if image_page.page < image_page.total_pages %}<a class="button button-secondary" href="/images?q={{ image_page.query|urlencode }}&amp;page={{ image_page.page + 1 }}">下一页</a>{% endif %}</div></nav>
  <dialog id="image-batch-delete-dialog" class="container-dialog" data-image-batch-dialog>
    <div class="dialog-heading"><h2 data-image-batch-dialog-title>批量删除镜像</h2><button class="button button-secondary" type="button" data-image-batch-close>关闭</button></div>
    <div class="alert alert-danger" data-image-batch-error hidden></div>
    <div data-image-batch-preview>
      <section class="dialog-section" data-batch-deletable-section><h3>将删除</h3><ul class="batch-image-list" data-batch-deletable></ul></section>
      <section class="dialog-section" data-batch-in-use-section><h3>不会删除：被容器占用</h3><ul class="batch-image-list" data-batch-in-use></ul></section>
      <section class="dialog-section" data-batch-missing-section hidden><h3>已不存在</h3><ul class="batch-image-list" data-batch-missing></ul></section>
      <div class="alert alert-warning">确认时会重新检查运行中和已停止的容器；新被占用的镜像不会删除。</div>
      <button class="button button-danger" type="button" data-image-batch-confirm>确定删除</button>
    </div>
    <div data-image-batch-result hidden>
      <p data-image-batch-summary></p>
      <section class="dialog-section" data-batch-deleted-section><h3>已删除</h3><ul class="batch-image-list" data-batch-deleted></ul></section>
      <section class="dialog-section" data-batch-result-in-use-section><h3>因容器占用跳过</h3><ul class="batch-image-list" data-batch-result-in-use></ul></section>
      <section class="dialog-section" data-batch-result-missing-section><h3>已不存在</h3><ul class="batch-image-list" data-batch-result-missing></ul></section>
      <section class="dialog-section" data-batch-failed-section><h3>删除失败</h3><ul class="batch-image-list" data-batch-failed></ul></section>
      <button class="button button-secondary" type="button" data-image-batch-return>返回镜像列表</button>
    </div>
  </dialog>
</section>
{% endblock %}
{% block scripts %}<script type="module" src="{{ url_for('static', path='/js/image_batch_delete.mjs') }}?v={{ static_asset_version('js/image_batch_delete.mjs') }}"></script>{% endblock %}
```

- [ ] **Step 4: 增加批量布局样式**

在 `app.css` 的镜像/弹框规则附近追加：

```css
.image-batch-actions, .image-batch-mode-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.image-select-cell { width: 44px; text-align: center; }
.image-select-cell input { width: 16px; height: 16px; }
.batch-image-list { display: grid; gap: 10px; margin: 10px 0 18px; padding-left: 22px; }
.batch-image-list > li { overflow-wrap: anywhere; }
.batch-container-list { margin: 7px 0 0; color: var(--muted); }
.batch-item-error { display: block; margin-top: 5px; color: var(--danger); }
```

在现有移动端 media query 中追加：

```css
  .image-batch-actions, .image-batch-mode-actions { width: 100%; }
```

- [ ] **Step 5: 先写资源存在性失败测试**

在 `tests/web/test_package_resources.py` 追加：

```python
def test_image_batch_delete_module_is_packaged():
    module = (
        files("docker_manage_server")
        .joinpath("static/js/image_batch_delete.mjs")
    )
    assert module.is_file()
```

Run:

```bash
.venv/bin/pytest tests/web/test_package_resources.py::test_image_batch_delete_module_is_packaged -v
```

Expected: FAIL because the ES module does not exist.

- [ ] **Step 6: 创建最小模块文件以使页面资源可加载**

创建 `src/docker_manage_server/static/js/image_batch_delete.mjs`，先只放 Task 3 页面加载和 Task 4 测试导入所需的最小行为；Task 4 会在测试先行后扩展为完整交互：

```javascript
export const imageLabel = (item) => item.tags?.[0] || item.short_id || item.id;
```

同时在 `pyproject.toml` 的 package data 增加：

```toml
    "static/js/*.mjs",
```

- [ ] **Step 7: 运行 Web 模板与资源测试并确认 GREEN**

Run:

```bash
.venv/bin/pytest tests/web/test_images.py tests/web/test_package_resources.py -v
```

Expected: all image Web tests PASS; existing list row count and pagination assertions remain unchanged.

- [ ] **Step 8: 提交列表 DOM 和样式**

```bash
git add \
  pyproject.toml \
  src/docker_manage_server/templates/images/list.html \
  src/docker_manage_server/static/css/app.css \
  src/docker_manage_server/static/js/image_batch_delete.mjs \
  tests/web/test_images.py \
  tests/web/test_package_resources.py
git commit -m "feat: add image batch selection interface"
```

---

### Task 4: 前端选择、预检、确认和结果刷新

**Files:**
- Modify: `src/docker_manage_server/static/js/image_batch_delete.mjs`
- Create: `tests/js/image_batch_delete.test.mjs`
- Modify: `tests/web/test_package_resources.py`

**Interfaces:**
- Consumes: Task 2 JSON 字段 `deletable`, `in_use`, `missing`, `deleted`, `failed`, `suggested_page`；Task 3 的全部 `data-image-batch-*` DOM hooks。
- Produces: `selectionState(items)`, `imageLabel(item)`, `imagesListUrl(query, page)` 纯函数和页面自动初始化副作用。

- [ ] **Step 1: 写纯函数 Node 失败测试**

创建 `tests/js/image_batch_delete.test.mjs`：

```javascript
import test from "node:test";
import assert from "node:assert/strict";
import {
  imageLabel,
  imagesListUrl,
  selectionState,
} from "../../src/docker_manage_server/static/js/image_batch_delete.mjs";

test("selectionState counts selection and derives all/partial states", () => {
  assert.deepEqual(selectionState([false, false]), {
    count: 0,
    canDelete: false,
    all: false,
    indeterminate: false,
  });
  assert.deepEqual(selectionState([true, false]), {
    count: 1,
    canDelete: true,
    all: false,
    indeterminate: true,
  });
  assert.deepEqual(selectionState([true, true]), {
    count: 2,
    canDelete: true,
    all: true,
    indeterminate: false,
  });
});

test("imageLabel prefers a tag and identifies dangling images", () => {
  assert.equal(imageLabel({ id: "sha256:a", short_id: "a", tags: ["demo:1"] }), "demo:1");
  assert.equal(imageLabel({ id: "sha256:b", short_id: "b", tags: [] }), "b（未标记）");
});

test("imagesListUrl preserves unicode query and suggested page", () => {
  assert.equal(imagesListUrl("演示 app", 2), "/images?q=%E6%BC%94%E7%A4%BA+app&page=2");
  assert.equal(imagesListUrl("", 1), "/images?page=1");
});
```

- [ ] **Step 2: 运行 Node 测试并确认 RED**

Run:

```bash
node --test tests/js/image_batch_delete.test.mjs
```

Expected: FAIL because `selectionState` and `imagesListUrl` are not exported and dangling labeling is incomplete.

- [ ] **Step 3: 实现纯函数、选择状态和安全 DOM renderer**

把模块替换为以下实现的第一部分：

```javascript
export const selectionState = (checkedItems) => {
  const count = checkedItems.filter(Boolean).length;
  return {
    count,
    canDelete: count > 0,
    all: checkedItems.length > 0 && count === checkedItems.length,
    indeterminate: count > 0 && count < checkedItems.length,
  };
};

export const imageLabel = (item) => (
  item.tags?.[0] || `${item.short_id || item.id}（未标记）`
);

export const imagesListUrl = (query, page) => {
  const params = new URLSearchParams();
  if (query) params.set("q", query);
  params.set("page", String(page));
  return `/images?${params.toString()}`;
};

const appendImage = (list, item, { containers = false, error = false } = {}) => {
  const row = document.createElement("li");
  const code = document.createElement("code");
  code.textContent = imageLabel(item);
  row.appendChild(code);
  if (containers && item.containers?.length) {
    const nested = document.createElement("ul");
    nested.className = "batch-container-list";
    item.containers.forEach((container) => {
      const containerRow = document.createElement("li");
      containerRow.textContent = `${container.name}（${container.status}）`;
      nested.appendChild(containerRow);
    });
    row.appendChild(nested);
  }
  if (error && item.error) {
    const message = document.createElement("span");
    message.className = "batch-item-error";
    message.textContent = item.error;
    row.appendChild(message);
  }
  list.appendChild(row);
};

const renderImages = (list, items, options) => {
  list.replaceChildren();
  items.forEach((item) => appendImage(list, item, options));
  list.closest("section").hidden = items.length === 0;
};

const renderMissing = (list, ids) => {
  list.replaceChildren();
  ids.forEach((id) => {
    const row = document.createElement("li");
    const code = document.createElement("code");
    code.textContent = id;
    row.appendChild(code);
    list.appendChild(row);
  });
  list.closest("section").hidden = ids.length === 0;
};

const responseError = (payload, status) => {
  if (typeof payload.detail === "string") return payload.detail;
  return `HTTP ${status}`;
};
```

- [ ] **Step 4: 实现页面初始化、预检和确认执行**

继续在同一模块追加完整初始化逻辑：

```javascript
const root = document.querySelector("[data-image-batch]");

if (root) {
  const enter = root.querySelector("[data-image-batch-enter]");
  const actions = root.querySelector("[data-image-batch-actions]");
  const cancel = root.querySelector("[data-image-batch-cancel]");
  const submit = root.querySelector("[data-image-batch-submit]");
  const count = root.querySelector("[data-image-selected-count]");
  const cells = Array.from(root.querySelectorAll("[data-image-select-cell]"));
  const items = Array.from(root.querySelectorAll("[data-image-select-item]"));
  const selectAll = root.querySelector("[data-image-select-all]");
  const dialog = root.querySelector("[data-image-batch-dialog]");
  const close = dialog.querySelector("[data-image-batch-close]");
  const confirm = dialog.querySelector("[data-image-batch-confirm]");
  const returnButton = dialog.querySelector("[data-image-batch-return]");
  const previewPanel = dialog.querySelector("[data-image-batch-preview]");
  const resultPanel = dialog.querySelector("[data-image-batch-result]");
  const errorPanel = dialog.querySelector("[data-image-batch-error]");
  let completed = false;

  const selectedIds = () => items.filter((item) => item.checked).map((item) => item.value);

  const syncSelection = () => {
    const state = selectionState(items.map((item) => item.checked));
    count.textContent = String(state.count);
    submit.disabled = !state.canDelete;
    selectAll.checked = state.all;
    selectAll.indeterminate = state.indeterminate;
  };

  const setBatchMode = (enabled) => {
    enter.hidden = enabled;
    actions.hidden = !enabled;
    cells.forEach((cell) => { cell.hidden = !enabled; });
    if (!enabled) items.forEach((item) => { item.checked = false; });
    syncSelection();
  };

  const showError = (message) => {
    errorPanel.textContent = message;
    errorPanel.hidden = false;
  };

  const clearError = () => {
    errorPanel.textContent = "";
    errorPanel.hidden = true;
  };

  const returnToList = () => {
    const page = Number.parseInt(dialog.dataset.suggestedPage || root.dataset.page, 10);
    window.location.assign(imagesListUrl(root.dataset.query, page));
  };

  enter.addEventListener("click", () => setBatchMode(true));
  cancel.addEventListener("click", () => setBatchMode(false));
  items.forEach((item) => item.addEventListener("change", syncSelection));
  selectAll.addEventListener("change", () => {
    items.forEach((item) => { item.checked = selectAll.checked; });
    syncSelection();
  });

  submit.addEventListener("click", async () => {
    const imageIds = selectedIds();
    if (!imageIds.length) return;
    submit.disabled = true;
    confirm.disabled = true;
    clearError();
    try {
      const response = await fetch(root.dataset.previewUrl, {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({ image_ids: imageIds }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(responseError(payload, response.status));
      renderImages(dialog.querySelector("[data-batch-deletable]"), payload.deletable);
      renderImages(dialog.querySelector("[data-batch-in-use]"), payload.in_use, { containers: true });
      renderMissing(dialog.querySelector("[data-batch-missing]"), payload.missing);
      confirm.disabled = payload.deletable.length === 0;
      previewPanel.hidden = false;
      resultPanel.hidden = true;
      completed = false;
      dialog.showModal();
    } catch (error) {
      showError(`预检失败：${error.message}`);
      if (!dialog.open) dialog.showModal();
    } finally {
      syncSelection();
    }
  });

  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    clearError();
    try {
      const response = await fetch(root.dataset.deleteUrl, {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({
          image_ids: selectedIds(),
          query: root.dataset.query,
          page: Number.parseInt(root.dataset.page, 10),
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(responseError(payload, response.status));
      renderImages(dialog.querySelector("[data-batch-deleted]"), payload.deleted);
      renderImages(dialog.querySelector("[data-batch-result-in-use]"), payload.in_use, { containers: true });
      renderMissing(dialog.querySelector("[data-batch-result-missing]"), payload.missing);
      renderImages(dialog.querySelector("[data-batch-failed]"), payload.failed, { error: true });
      dialog.querySelector("[data-image-batch-summary]").textContent = `已删除 ${payload.deleted.length} 个，跳过 ${payload.in_use.length + payload.missing.length} 个，失败 ${payload.failed.length} 个。`;
      dialog.dataset.suggestedPage = String(payload.suggested_page);
      previewPanel.hidden = true;
      resultPanel.hidden = false;
      completed = true;
    } catch (error) {
      showError(`删除失败：${error.message}`);
      confirm.disabled = false;
    }
  });

  close.addEventListener("click", () => {
    if (completed) returnToList();
    else dialog.close();
  });
  returnButton.addEventListener("click", returnToList);
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog && !completed) dialog.close();
  });
  dialog.addEventListener("cancel", (event) => {
    if (completed) {
      event.preventDefault();
      returnToList();
    }
  });
  syncSelection();
}
```

- [ ] **Step 5: 运行 Node 测试和语法检查并确认 GREEN**

Run:

```bash
node --test tests/js/image_batch_delete.test.mjs
node --check src/docker_manage_server/static/js/image_batch_delete.mjs
```

Expected: 3 Node tests PASS and syntax check exits 0.

- [ ] **Step 6: 写资源打包与安全 DOM 契约失败测试**

在 `tests/web/test_package_resources.py` 追加：

```python
def test_image_batch_delete_module_uses_safe_dom_updates():
    module = (
        files("docker_manage_server")
        .joinpath("static/js/image_batch_delete.mjs")
    )
    assert module.is_file()
    script = module.read_text(encoding="utf-8")
    assert "data-image-batch" in script
    assert 'method: "POST"' in script
    assert '"Content-Type": "application/json"' in script
    assert "replaceChildren" in script
    assert "textContent" in script
    assert "innerHTML" not in script
    assert "window.location.assign" in script
```

- [ ] **Step 7: 运行资源测试并确认 GREEN**

Run:

```bash
.venv/bin/pytest tests/web/test_package_resources.py -v
```

Expected: all package resource tests PASS.

- [ ] **Step 8: 提交前端交互**

```bash
git add \
  src/docker_manage_server/static/js/image_batch_delete.mjs \
  tests/js/image_batch_delete.test.mjs \
  tests/web/test_package_resources.py
git commit -m "feat: implement image batch deletion workflow"
```

---

### Task 5: 安全回归、集成验证和交付检查

**Files:**
- Modify: `tests/web/test_security.py`
- Verify: `src/docker_manage_server/image_inventory.py`
- Verify: `src/docker_manage_server/api.py`
- Verify: `src/docker_manage_server/templates/images/list.html`
- Verify: `src/docker_manage_server/static/js/image_batch_delete.mjs`

**Interfaces:**
- Consumes: Tasks 1–4 的完整服务、API 和页面流程。
- Produces: 跨源写请求回归保护和可打包、可部署的最终验证证据。

- [ ] **Step 1: 扩展跨源镜像删除失败测试**

在 `test_cross_origin_image_deletes_are_rejected()` 的 `(method, path)` 参数中加入两个端点，并为 POST 提供 JSON；为避免改变现有两个请求的调用形式，把测试改为：

```python
def test_cross_origin_image_deletes_are_rejected(web_context):
    client, _store, runtime = web_context
    image_id = "sha256:" + "a" * 64
    runtime.images = [
        {
            "id": image_id,
            "short_id": "a" * 12,
            "tags": ["demo/app:1"],
            "raw_attrs": {"Id": image_id},
        }
    ]
    requests = (
        ("post", f"/images/{image_id}/delete", None),
        ("delete", f"/api/images/{image_id}/tags", None),
        (
            "post",
            "/api/images/batch-delete-preview",
            {"image_ids": [image_id]},
        ),
        (
            "post",
            "/api/images/batch-delete",
            {"image_ids": [image_id], "query": "", "page": 1},
        ),
    )
    for method, path, payload in requests:
        response = getattr(client, method)(
            path,
            headers={"Origin": "https://evil.example"},
            json=payload,
        )
        assert response.status_code == 403
    assert runtime.image_remove_calls == []
```

- [ ] **Step 2: 运行安全测试并确认 GREEN**

Run:

```bash
.venv/bin/pytest tests/web/test_security.py -v
```

Expected: all security tests PASS and no image removal call is recorded.

- [ ] **Step 3: 运行聚焦功能测试**

Run:

```bash
.venv/bin/pytest \
  tests/unit/test_image_inventory.py \
  tests/api/test_image_api.py \
  tests/web/test_images.py \
  tests/web/test_package_resources.py \
  tests/web/test_security.py -v
node --test tests/js/image_batch_delete.test.mjs
node --check src/docker_manage_server/static/js/app.js
node --check src/docker_manage_server/static/js/image_batch_delete.mjs
```

Expected: all focused pytest and Node tests PASS; both JavaScript syntax checks exit 0.

- [ ] **Step 4: 运行全量回归、Python 编译和差异检查**

Run:

```bash
.venv/bin/pytest
.venv/bin/python -m compileall -q src tests
git diff --check
```

Expected: full pytest suite PASS; compileall and diff check exit 0 with no output.

- [ ] **Step 5: 构建 wheel 并验证新增资源进入安装包**

Run:

```bash
uv build --wheel -o dist
.venv/bin/python -c "import zipfile; from pathlib import Path; wheel=max(Path('dist').glob('docker_manage_server-*.whl'), key=lambda p: p.stat().st_mtime); names=zipfile.ZipFile(wheel).namelist(); assert any(n.endswith('templates/images/list.html') for n in names); assert any(n.endswith('static/js/image_batch_delete.mjs') for n in names); assert any(n.endswith('static/css/app.css') for n in names)"
```

Expected: wheel build succeeds and the resource assertion exits 0.

- [ ] **Step 6: 验证 Compose 配置**

Run:

```bash
DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server DOCKER_MANAGE_SERVER_PORT=6308 docker compose config --quiet
```

Expected: exits 0 with no configuration errors.

- [ ] **Step 7: 在本机页面做一次交互烟测**

Run the server on an unused loopback port:

```bash
DATA_DIR=/tmp/docker-manage-server-batch-delete-smoke .venv/bin/uvicorn docker_manage_server.api:create_app --factory --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765/images` in the in-app browser and verify:

- “批量删除”进入/取消时选择列正确显隐；
- 全选、部分选择和“删除已选（N）”计数一致；
- 窄屏下工具栏不溢出；
- 无镜像时入口禁用；
- Docker 不可用时预检错误显示在弹框且保留选择。

Stop the server after inspection. Expected: no browser console errors and no unsafe deletion is attempted during the daemon-unavailable smoke test.

- [ ] **Step 8: 提交安全测试和最终必要修正**

```bash
git add \
  tests/web/test_security.py \
  src/docker_manage_server \
  tests \
  pyproject.toml
git commit -m "test: verify image batch deletion delivery"
```

如果 Step 3–7 没有产生 Task 5 之外的修正，则这个提交只包含 `tests/web/test_security.py`；不得提交 `dist/`、`.pytest_cache/`、`__pycache__/` 或烟测数据目录。

---

## Final Verification Checklist

- [ ] 当前页单选、全选、部分选择、取消和零选择禁用均有自动化或烟测证据。
- [ ] 预检列出运行中与已停止容器，并整张标记占用镜像为不会删除。
- [ ] 执行阶段重新检查容器；预检后新增占用会跳过。
- [ ] 未占用的有 Tag 镜像和无 Tag 悬空镜像均按完整 ID 删除。
- [ ] 单项失败、镜像消失和占用跳过不会中断其他镜像。
- [ ] 结果分组、查询词和建议页码正确，当前页清空时回退上一有效页。
- [ ] 两个批量 POST 端点拒绝跨源请求和非法/重复/超量 ID。
- [ ] 所有动态文本使用 `textContent`，模块不存在 `innerHTML`。
- [ ] 镜像聚焦测试、全量 pytest、Node 测试、语法、compileall、wheel 和 Compose 验证全部通过。
