# 部署任务目录大小与手动删除 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在部署任务列表实时展示任务包目录大小，并允许用户显式删除非活动任务的记录与包目录，同时保证稳定部署目录和 Docker 资源不受影响。

**Architecture:** `TaskStore` 负责安全统计和删除任务目录，`deployment_config` 提供统一的可删除状态谓词，`DeploymentService` 在任务锁内执行权威状态检查。服务端渲染列表只负责逐任务读取大小并格式化，Web 与 API 删除入口共用 `delete_task()`。

**Tech Stack:** Python 3.11、FastAPI、Jinja2、Pydantic 2、pytest、本地 JSON 任务存储。

## Global Constraints

- 仅删除 `packages/<任务 ID>/` 和 `tasks/<任务 ID>.json`。
- 不读取或删除 `deployments/<app_name>/`，不操作容器、Compose 项目、网络、数据卷、Docker 镜像或构建缓存。
- 允许删除 `pending_review`、`failed`、`deployed`；拒绝 `uploaded`、`extracting`、`deploying`。
- 目录大小实时统计普通文件逻辑大小，不跟随或计入符号链接，不持久化、不缓存。
- 目录不存在显示 `0 B`；无法安全读取显示“无法读取”，不能拖垮整个列表。
- 删除按钮的可见性不是授权，服务层必须在任务锁内重新读取并检查状态。
- 保留详情页既有 `discard()` 入口及语义；列表和现有 DELETE API 使用新增 `delete_task()`。
- 不新增依赖、后台任务、批量删除或自动清理。
- 不安装或调用 `gh` 及任何 Gitee CLI；Git 操作只使用本地 `git`。

---

### Task 1: 增加目录大小统计与统一删除状态谓词

**Files:**
- Modify: `src/docker_manage_server/storage.py:1-116`
- Modify: `src/docker_manage_server/deployment_config.py:105-132`
- Modify: `tests/unit/test_storage.py:1-110`
- Modify: `tests/unit/test_deployment_config.py:1-125`

**Interfaces:**
- Produces: `TaskStore.package_size_bytes(task_id: str) -> int`；不可读取时抛出 `OSError`。
- Produces: `can_delete_task(task: DeploymentTask) -> bool`。

- [ ] **Step 1: 写入目录大小失败测试**

在 `tests/unit/test_storage.py` 增加：

```python
def test_package_size_sums_regular_files_and_ignores_symlinks(tmp_path: Path):
    store = TaskStore(tmp_path)
    task = store.create("sized", "demo.tar.gz")
    (task.package_dir / "archive.tar.gz").write_bytes(b"1234")
    nested = task.package_dir / "extracted"
    nested.mkdir()
    (nested / "compose.yaml").write_bytes(b"123456")
    external = tmp_path / "external"
    external.mkdir()
    (external / "large.bin").write_bytes(b"x" * 100)
    (task.package_dir / "file-link").symlink_to(external / "large.bin")
    (task.package_dir / "dir-link").symlink_to(external, target_is_directory=True)

    assert store.package_size_bytes("sized") == 10


def test_package_size_returns_zero_for_missing_directory(tmp_path: Path):
    store = TaskStore(tmp_path)
    assert store.package_size_bytes("missing") == 0


def test_package_size_skips_file_that_disappears(tmp_path: Path, monkeypatch):
    store = TaskStore(tmp_path)
    task = store.create("changing", "demo.tar.gz")
    target = task.package_dir / "vanishing.bin"
    target.write_bytes(b"123")
    real_lstat = Path.lstat

    def vanish(path):
        if path == target:
            raise FileNotFoundError(path)
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", vanish)
    assert store.package_size_bytes("changing") == 0


def test_package_size_propagates_read_error(tmp_path: Path, monkeypatch):
    store = TaskStore(tmp_path)
    task = store.create("blocked", "demo.tar.gz")
    target = task.package_dir / "blocked.bin"
    target.write_bytes(b"123")
    real_lstat = Path.lstat

    def blocked(path):
        if path == target:
            raise PermissionError("blocked")
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", blocked)
    with pytest.raises(PermissionError, match="blocked"):
        store.package_size_bytes("blocked")


def test_delete_can_retry_after_state_file_unlink_failure(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    store.create("partial", "demo.tar.gz")
    state_path = tmp_path / "tasks/partial.json"
    real_unlink = Path.unlink

    def fail_state_once(path, *args, **kwargs):
        if path == state_path:
            raise PermissionError("blocked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_state_once)
    with pytest.raises(PermissionError, match="blocked"):
        store.delete("partial")
    assert not store.package_dir("partial").exists()
    assert state_path.is_file()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    store.delete("partial")
    assert not state_path.exists()
```

给测试文件增加 `import pytest`。

- [ ] **Step 2: 写入状态谓词失败测试**

在 `tests/unit/test_deployment_config.py` 的导入中加入 `can_delete_task`，增加：

```python
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
    tmp_path, status, expected
):
    assert can_delete_task(make_task(tmp_path, status=status)) is expected
```

- [ ] **Step 3: 运行测试确认接口不存在**

```bash
.venv/bin/python -m pytest \
  tests/unit/test_storage.py::test_package_size_sums_regular_files_and_ignores_symlinks \
  tests/unit/test_storage.py::test_package_size_returns_zero_for_missing_directory \
  tests/unit/test_storage.py::test_package_size_skips_file_that_disappears \
  tests/unit/test_storage.py::test_package_size_propagates_read_error \
  tests/unit/test_storage.py::test_delete_can_retry_after_state_file_unlink_failure \
  tests/unit/test_deployment_config.py::test_delete_permission_depends_only_on_non_active_status -q
```

Expected: collection FAIL，两个新接口尚不存在。

- [ ] **Step 4: 实现安全目录遍历**

在 `storage.py` 增加 `import os` 和 `import stat`，在 `package_dir()` 前加入：

```python
    def package_size_bytes(self, task_id: str) -> int:
        root = self.package_dir(task_id)
        try:
            root_mode = root.lstat().st_mode
        except FileNotFoundError:
            return 0
        if not stat.S_ISDIR(root_mode):
            raise OSError("task package path is not a directory")

        total = 0

        def handle_walk_error(exc: OSError) -> None:
            if isinstance(exc, FileNotFoundError):
                return
            raise exc

        for directory, dirnames, filenames in os.walk(
            root,
            topdown=True,
            onerror=handle_walk_error,
            followlinks=False,
        ):
            directory_path = Path(directory)
            safe_directories = []
            for name in dirnames:
                path = directory_path / name
                try:
                    mode = path.lstat().st_mode
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(mode):
                    safe_directories.append(name)
            dirnames[:] = safe_directories
            for name in filenames:
                path = directory_path / name
                try:
                    file_stat = path.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(file_stat.st_mode):
                    total += file_stat.st_size
        return total
```

`lstat()` 保证文件和目录符号链接均不被跟随；除并发消失外的读取错误继续抛出。

- [ ] **Step 5: 实现统一状态谓词**

在 `deployment_config.py` 增加：

```python
def can_delete_task(task: DeploymentTask) -> bool:
    return task.status in {
        TaskStatus.PENDING_REVIEW,
        TaskStatus.FAILED,
        TaskStatus.DEPLOYED,
    }
```

- [ ] **Step 6: 运行存储与配置测试**

```bash
.venv/bin/python -m pytest tests/unit/test_storage.py tests/unit/test_deployment_config.py -q
```

Expected: 全部 PASS。

- [ ] **Step 7: 提交**

```bash
git add src/docker_manage_server/storage.py src/docker_manage_server/deployment_config.py tests/unit/test_storage.py tests/unit/test_deployment_config.py
git commit -m "feat: measure deletable deployment tasks"
```

---

### Task 2: 新增任务锁内的通用删除服务

**Files:**
- Modify: `src/docker_manage_server/deployment.py:12-32,215-245`
- Modify: `tests/unit/test_deployment.py:70-100,240-345`

**Interfaces:**
- Consumes: `can_delete_task(task) -> bool`。
- Produces: `DeploymentService.delete_task(task_id: str) -> DeploymentTask`。
- Preserves: `discard(task_id)` 既有行为不变。

- [ ] **Step 1: 写入允许状态、活动状态与部署目录保护测试**

在 `tests/unit/test_deployment.py` 增加：

```python
@pytest.mark.parametrize(
    "status",
    (TaskStatus.PENDING_REVIEW, TaskStatus.FAILED, TaskStatus.DEPLOYED),
)
def test_delete_task_removes_allowed_task_but_keeps_deployment(
    tmp_path, status
):
    service = make_service(tmp_path)
    task = service.store.create("task-1", "demo.tar.gz")
    task.status = status
    task.app_name = "demo"
    task.deployment_dir = service.store.deployment_dir("demo")
    task.deployment_dir.mkdir(parents=True)
    stable_file = task.deployment_dir / "compose.yaml"
    stable_file.write_text("services: {}\n", encoding="utf-8")
    service.store.save(task)

    deleted = service.delete_task("task-1")

    assert deleted.task_id == "task-1"
    assert not service.store.package_dir("task-1").exists()
    with pytest.raises(KeyError):
        service.store.get("task-1")
    assert stable_file.read_text(encoding="utf-8") == "services: {}\n"


@pytest.mark.parametrize(
    "status",
    (TaskStatus.UPLOADED, TaskStatus.EXTRACTING, TaskStatus.DEPLOYING),
)
def test_delete_task_rejects_active_status(tmp_path, status):
    service = make_service(tmp_path)
    task = service.store.create("task-1", "demo.tar.gz")
    task.status = status
    service.store.save(task)

    with pytest.raises(DeploymentStateError):
        service.delete_task("task-1")

    assert service.store.package_dir("task-1").is_dir()
    assert service.store.get("task-1").status is status


def test_delete_task_propagates_missing_task(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(KeyError):
        service.delete_task("missing")


def test_delete_task_propagates_storage_failure(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    task = service.store.create("task-1", "demo.tar.gz")
    task.status = TaskStatus.FAILED
    service.store.save(task)

    def fail_delete(task_id):
        raise PermissionError("blocked")

    monkeypatch.setattr(service.store, "delete", fail_delete)
    with pytest.raises(PermissionError, match="blocked"):
        service.delete_task("task-1")
    assert service.store.get("task-1").status is TaskStatus.FAILED
```

- [ ] **Step 2: 运行测试确认方法不存在**

```bash
.venv/bin/python -m pytest \
  tests/unit/test_deployment.py::test_delete_task_removes_allowed_task_but_keeps_deployment \
  tests/unit/test_deployment.py::test_delete_task_rejects_active_status \
  tests/unit/test_deployment.py::test_delete_task_propagates_missing_task \
  tests/unit/test_deployment.py::test_delete_task_propagates_storage_failure -q
```

Expected: FAIL，`DeploymentService` 尚无 `delete_task()`。

- [ ] **Step 3: 实现任务锁内删除**

在 `deployment.py` 的配置导入中加入 `can_delete_task`，并在 `discard()` 前增加：

```python
    def delete_task(self, task_id: str) -> DeploymentTask:
        with self._task_lock(task_id):
            task = self.store.get(task_id)
            if not can_delete_task(task):
                raise DeploymentStateError(
                    f"task {task_id} cannot be deleted from status "
                    f"{task.status.value}"
                )
            self.store.delete(task_id)
            return task
```

不要调用 `discard()`，不要修改任务状态，不读取 `deployment_dir`。

- [ ] **Step 4: 运行部署服务测试**

Run: `.venv/bin/python -m pytest tests/unit/test_deployment.py -q`

Expected: 全部 PASS，包括现有 `discard()` 测试。

- [ ] **Step 5: 提交**

```bash
git add src/docker_manage_server/deployment.py tests/unit/test_deployment.py
git commit -m "feat: delete inactive deployment tasks"
```

---

### Task 3: 在任务列表展示大小与删除操作

**Files:**
- Modify: `src/docker_manage_server/web_views.py:7-36`
- Modify: `src/docker_manage_server/web.py:12-72,237-360`
- Modify: `src/docker_manage_server/templates/deployments/list.html:1-32`
- Modify: `tests/web/test_deployments.py:1-86`

**Interfaces:**
- Consumes: `TaskStore.package_size_bytes()`、`can_delete_task()`、`DeploymentService.delete_task()`。
- Produces: `task_list_view(task, package_size_bytes: int | None) -> dict[str, Any]`。
- Produces: `POST /deployments/{task_id}/delete`。

- [ ] **Step 1: 写入列表大小与按钮可见性测试**

在 `tests/web/test_deployments.py` 增加：

```python
def test_task_list_shows_package_size_and_delete_only_for_inactive_tasks(
    web_context
):
    client, store, _runtime = web_context
    pending = store.create("pending", "pending.tar.gz")
    pending.status = TaskStatus.PENDING_REVIEW
    store.save(pending)
    (pending.package_dir / "payload.bin").write_bytes(b"x" * 1536)
    active = store.create("active", "active.tar.gz")
    active.status = TaskStatus.DEPLOYING
    store.save(active)

    response = client.get("/deployments")

    assert response.status_code == 200
    assert "1.5 KiB" in response.text
    assert 'action="/deployments/pending/delete"' in response.text
    assert 'action="/deployments/active/delete"' not in response.text
    assert "不会影响已部署服务、稳定部署目录或 Docker 数据" in response.text


def test_task_list_marks_unreadable_package_size(web_context, monkeypatch):
    client, store, _runtime = web_context
    task = store.create("blocked", "blocked.tar.gz")
    task.status = TaskStatus.FAILED
    store.save(task)
    real_size = store.package_size_bytes

    def blocked(task_id):
        if task_id == "blocked":
            raise PermissionError("blocked")
        return real_size(task_id)

    monkeypatch.setattr(store, "package_size_bytes", blocked)
    response = client.get("/deployments")
    assert response.status_code == 200
    assert "无法读取" in response.text
```

- [ ] **Step 2: 写入 Web 删除路由测试**

增加：

```python
def test_web_delete_task_redirects_and_preserves_deployment(web_context):
    client, store, _runtime = web_context
    task = store.create("deployed", "demo.tar.gz")
    task.status = TaskStatus.DEPLOYED
    task.app_name = "demo"
    task.deployment_dir = store.deployment_dir("demo")
    task.deployment_dir.mkdir(parents=True)
    stable = task.deployment_dir / "compose.yaml"
    stable.write_text("services: {}\n", encoding="utf-8")
    store.save(task)

    response = client.post("/deployments/deployed/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/deployments"
    assert stable.is_file()
    assert client.get("/deployments/deployed").status_code == 404


def test_web_delete_task_maps_missing_and_active_status(web_context):
    client, store, _runtime = web_context
    active = store.create("active", "demo.tar.gz")
    active.status = TaskStatus.DEPLOYING
    store.save(active)
    assert client.post("/deployments/missing/delete").status_code == 404
    assert client.post("/deployments/active/delete").status_code == 409
    assert store.package_dir("active").is_dir()


def test_web_delete_task_maps_storage_failure(web_context, monkeypatch):
    client, store, _runtime = web_context
    task = store.create("blocked", "demo.tar.gz")
    task.status = TaskStatus.FAILED
    store.save(task)

    def fail_delete(task_id):
        raise PermissionError("blocked")

    monkeypatch.setattr(store, "delete", fail_delete)
    response = client.post("/deployments/blocked/delete")
    assert response.status_code == 500
    assert "删除部署任务失败" in response.text
    assert store.get("blocked").status is TaskStatus.FAILED
```

- [ ] **Step 3: 运行 Web 测试确认功能缺失**

```bash
.venv/bin/python -m pytest \
  tests/web/test_deployments.py::test_task_list_shows_package_size_and_delete_only_for_inactive_tasks \
  tests/web/test_deployments.py::test_task_list_marks_unreadable_package_size \
  tests/web/test_deployments.py::test_web_delete_task_redirects_and_preserves_deployment \
  tests/web/test_deployments.py::test_web_delete_task_maps_missing_and_active_status \
  tests/web/test_deployments.py::test_web_delete_task_maps_storage_failure -q
```

Expected: FAIL，列表和删除路由尚未接入。

- [ ] **Step 4: 增加列表专用视图模型**

在 `web_views.py` 导入 `can_delete_task`，并在 `task_view()` 后加入：

```python
def task_list_view(
    task: DeploymentTask,
    package_size_bytes: int | None,
) -> dict[str, Any]:
    view = task_view(task)
    view["package_size"] = (
        _format_bytes(package_size_bytes)
        if package_size_bytes is not None
        else "无法读取"
    )
    view["deletable"] = can_delete_task(task)
    return view
```

- [ ] **Step 5: 实时构建列表上下文**

在 `web.py` 导入 `task_list_view`，把 `_deployment_list_context()` 改为：

```python
def _deployment_list_context(store: TaskStore) -> dict[str, Any]:
    entries = []
    for task in store.list():
        try:
            size = store.package_size_bytes(task.task_id)
        except OSError:
            size = None
        entries.append(task_list_view(task, size))
    return {
        "page_title": "部署任务",
        "active_nav": "deployments",
        "tasks": entries,
        "upload_error": None,
    }
```

- [ ] **Step 6: 增加删除路由**

在现有 `discard_archive()` 路由之后加入：

```python
    @router.post("/deployments/{task_id}/delete")
    def delete_deployment_task(request: Request, task_id: str):
        try:
            deployment.delete_task(task_id)
        except KeyError:
            return _web_error(request, 404, "找不到部署任务", task_id)
        except DeploymentStateError as exc:
            return _web_error(request, 409, "任务当前状态不允许删除", str(exc))
        except (OSError, ValueError) as exc:
            return _web_error(request, 500, "删除部署任务失败", str(exc))
        return RedirectResponse("/deployments", status_code=303)
```

- [ ] **Step 7: 修改列表模板**

将表头改为七列，并把操作单元格改为：

```jinja2
<thead><tr><th>应用</th><th>归档</th><th>状态</th><th>任务目录大小</th><th>创建时间</th><th>更新时间</th><th>操作</th></tr></thead>
```

```jinja2
<td>{{ entry.package_size }}</td>
```

```jinja2
<td>
  <div class="button-row">
    <a href="/deployments/{{ entry.task.task_id }}">详情</a>
    {% if entry.deletable %}
    <form method="post" action="/deployments/{{ entry.task.task_id }}/delete" data-confirm="确认永久删除此任务记录、原始上传包和解压文件？不会影响已部署服务、稳定部署目录或 Docker 数据。">
      <button class="button button-danger" type="submit">删除</button>
    </form>
    {% endif %}
  </div>
</td>
```

空列表行的 `colspan` 从 6 改为 7。

- [ ] **Step 8: 运行 Web 回归测试**

Run: `.venv/bin/python -m pytest tests/web/test_deployments.py -q`

Expected: 全部 PASS。

- [ ] **Step 9: 提交**

```bash
git add src/docker_manage_server/web_views.py src/docker_manage_server/web.py src/docker_manage_server/templates/deployments/list.html tests/web/test_deployments.py
git commit -m "feat: show and delete deployment task data"
```

---

### Task 4: 将 API 删除入口切换到通用删除服务

**Files:**
- Modify: `src/docker_manage_server/api.py:228-246`
- Modify: `tests/api/test_deployment_api.py:1-150`

**Interfaces:**
- Consumes: `DeploymentService.delete_task()`。
- Preserves: `DELETE /api/deployment-tasks/{task_id}` URL 和成功响应结构。

- [ ] **Step 1: 写入 API 状态和边界测试**

在 `tests/api/test_deployment_api.py` 增加：

```python
def test_api_deletes_deployed_task_but_keeps_stable_directory(
    client, valid_archive
):
    task_id = upload(client, valid_archive)["task_id"]
    store = client.app.state.store
    task = store.get(task_id)
    task.status = TaskStatus.DEPLOYED
    assert task.deployment_dir is not None
    task.deployment_dir.mkdir(parents=True)
    stable = task.deployment_dir / "compose.yaml"
    stable.write_text("services: {}\n", encoding="utf-8")
    store.save(task)

    response = client.delete(f"/api/deployment-tasks/{task_id}")

    assert response.status_code == 200
    assert response.json()["task_id"] == task_id
    assert stable.is_file()
    assert client.get(f"/api/deployment-tasks/{task_id}").status_code == 404


def test_api_delete_rejects_active_task_and_maps_missing(client, valid_archive):
    task_id = upload(client, valid_archive)["task_id"]
    store = client.app.state.store
    task = store.get(task_id)
    task.status = TaskStatus.DEPLOYING
    store.save(task)
    assert client.delete(f"/api/deployment-tasks/{task_id}").status_code == 409
    assert client.delete("/api/deployment-tasks/missing").status_code == 404
    assert store.package_dir(task_id).is_dir()


def test_api_delete_maps_storage_failure(client, valid_archive, monkeypatch):
    task_id = upload(client, valid_archive)["task_id"]
    store = client.app.state.store

    def fail_delete(candidate):
        raise PermissionError("blocked")

    monkeypatch.setattr(store, "delete", fail_delete)
    response = client.delete(f"/api/deployment-tasks/{task_id}")
    assert response.status_code == 500
    assert "blocked" in response.json()["detail"]
    assert store.get(task_id).status is TaskStatus.PENDING_REVIEW
```

- [ ] **Step 2: 运行测试确认现有 API 不支持该语义**

```bash
.venv/bin/python -m pytest \
  tests/api/test_deployment_api.py::test_api_deletes_deployed_task_but_keeps_stable_directory \
  tests/api/test_deployment_api.py::test_api_delete_rejects_active_task_and_maps_missing \
  tests/api/test_deployment_api.py::test_api_delete_maps_storage_failure -q
```

Expected: FAIL；现有接口调用 `discard()`，已部署任务返回 409，缺失任务未映射为 404。

- [ ] **Step 3: 切换 API 服务调用和错误映射**

将现有 `discard_task()` 改为：

```python
    @app.delete("/api/deployment-tasks/{task_id}")
    def delete_task(task_id: str) -> dict[str, Any]:
        try:
            task = deployment.delete_task(task_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="deployment task not found",
            ) from exc
        except DeploymentStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return _task_payload(task)
```

- [ ] **Step 4: 运行 API 回归测试**

Run: `.venv/bin/python -m pytest tests/api/test_deployment_api.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/docker_manage_server/api.py tests/api/test_deployment_api.py
git commit -m "feat: expose deployment task deletion API"
```

---

### Task 5: 文档与完整交付验证

**Files:**
- Modify: `README.md:62-81`
- Verify: `src/docker_manage_server/storage.py`
- Verify: `src/docker_manage_server/deployment_config.py`
- Verify: `src/docker_manage_server/deployment.py`
- Verify: `src/docker_manage_server/web_views.py`
- Verify: `src/docker_manage_server/web.py`
- Verify: `src/docker_manage_server/api.py`
- Verify: `src/docker_manage_server/templates/deployments/list.html`
- Verify: `tests/unit/test_storage.py`
- Verify: `tests/unit/test_deployment_config.py`
- Verify: `tests/unit/test_deployment.py`
- Verify: `tests/web/test_deployments.py`
- Verify: `tests/api/test_deployment_api.py`

**Interfaces:**
- Documents: 列表大小的范围、可删除状态以及删除不影响运行资源的边界。

- [ ] **Step 1: 更新 README**

在部署流程后加入：

```markdown
部署任务列表实时显示 `packages/<任务 ID>/` 中普通文件的逻辑大小。待审核、失败和已部署任务可以由用户确认后删除；删除只移除任务记录、原始上传包和解压目录，不影响稳定部署目录、Compose 服务、Docker 镜像、网络、数据卷或业务数据。正在上传、解压或部署的任务不能删除。
```

- [ ] **Step 2: 运行针对性测试**

```bash
.venv/bin/python -m pytest tests/unit/test_storage.py tests/unit/test_deployment_config.py tests/unit/test_deployment.py tests/web/test_deployments.py tests/api/test_deployment_api.py -q
```

Expected: 全部 PASS。

- [ ] **Step 3: 运行完整测试套件**

Run: `.venv/bin/python -m pytest -q`

Expected: 全部 PASS；真实 Docker 测试按既有条件 SKIP，不得新增失败。

- [ ] **Step 4: 执行源码与 Compose 检查**

```bash
git diff --check
.venv/bin/python -m compileall -q src/docker_manage_server
docker compose config --quiet
```

Expected: 三条命令退出码均为 0。

- [ ] **Step 5: 构建并检查 wheel**

```bash
rm -rf dist
.venv/bin/python -m pip wheel --no-deps --wheel-dir dist .
.venv/bin/python -c 'import glob, zipfile; p=glob.glob("dist/docker_manage_server-*.whl")[0]; n=zipfile.ZipFile(p).namelist(); assert "docker_manage_server/templates/deployments/list.html" in n; assert "docker_manage_server/storage.py" in n; print(p)'
```

Expected: wheel 构建成功，检查脚本打印 wheel 路径。不要提交 `dist/`。

- [ ] **Step 6: 提交文档**

```bash
git add README.md
git commit -m "docs: explain deployment task cleanup"
```

- [ ] **Step 7: 检查最终范围**

```bash
git status --short --branch
git log -8 --oneline
```

Expected: 源码、测试和 README 均已提交；最多只剩未跟踪的 `dist/` 交付产物。最终报告测试通过数、跳过数、wheel 和 Compose 检查结果，并明确删除任务不会删除稳定部署目录或 Docker 资源。
