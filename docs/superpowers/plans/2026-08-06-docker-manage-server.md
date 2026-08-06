# Docker Manage Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个运行在 Docker 中、可审核并部署 Docker Manage tar.gz 归档，同时提供宿主机容器信息、日志和交互终端的 FastAPI 服务端。

**Architecture:** FastAPI 负责 HTTP/WebSocket API；Docker SDK for Python 负责 Docker Engine 查询、日志和 Exec；Docker CLI + Compose Plugin 通过无 shell 子进程执行 `docker load` 和 `docker compose up -d`。上传归档先进入任务 staging 目录，审核通过后以 `manifest.app_name` 为键合并到稳定部署目录。

**Tech Stack:** Python 3.12、FastAPI、Uvicorn、Pydantic、Docker SDK for Python、Docker CLI/Compose Plugin、pytest、httpx。

## Global Constraints

- 服务端通过 `/var/run/docker.sock` 管理宿主机 Docker Engine。
- 服务端数据目录固定为容器内 `/app/data`，宿主机通过 `./data:/app/data` 挂载。
- `data/` 必须加入 Git 忽略，运行时内容不得提交。
- 第一版不做 API 鉴权，不要求业务容器提供健康检查接口。
- 健康状态只依据 Docker `State.Running`；健康检查接口只检查服务端和 Docker daemon 连接。
- 待审核阶段不得修改 `data/deployments/`，不得执行 `docker load` 或 Compose。
- 实际部署目录固定为 `data/deployments/<manifest.app_name>/`，不能使用 task_id 作为 Compose 工作目录。
- 合并部署包时覆盖归档中存在的同名文件，但不删除归档中不存在的文件。
- 用户丢弃待审核任务时物理删除该任务目录；部署失败不自动回滚。
- `.env` 和 `compose.yaml` review 接口返回完整原文，不做脱敏或编辑。
- 容器终端只允许连接 `Running` 容器，使用 Docker Exec + PTY + WebSocket。

---

### Task 1: 初始化 Python 服务端工程

**Files:**
- Create: `pyproject.toml`
- Create: `Dockerfile`
- Create: `compose.yaml`
- Create: `.gitignore`
- Create: `src/docker_manage_server/__init__.py`
- Create: `src/docker_manage_server/config.py`
- Create: `tests/conftest.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces `docker_manage_server.config.Settings`, `get_settings()` and an importable `docker_manage_server.main` target for later tasks.

- [ ] **Step 1: Write the failing configuration test**

```python
from pathlib import Path

from docker_manage_server.config import Settings


def test_settings_defaults_to_app_data(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DATA_DIR", raising=False)
    settings = Settings()
    assert settings.data_dir == Path("/app/data")


def test_settings_reads_data_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "runtime"))
    assert Settings().data_dir == tmp_path / "runtime"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_config.py -q`

Expected: FAIL because `docker_manage_server.config` does not exist.

- [ ] **Step 3: Add the minimal project and configuration implementation**

`pyproject.toml` must declare runtime dependencies `fastapi`, `uvicorn[standard]`, `docker`, `python-multipart` and test dependency `pytest`, `httpx`.

`config.py` must contain:

```python
from dataclasses import dataclass, field
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "/app/data")))
    docker_host: str | None = field(default_factory=lambda: os.getenv("DOCKER_HOST"))
    compose_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("COMPOSE_TIMEOUT_SECONDS", "1800"))
    )


def get_settings() -> Settings:
    return Settings()
```

`.gitignore` must ignore `/data/`, `.venv/`, `__pycache__/`, `.pytest_cache/` and build artifacts. `compose.yaml` must define the server service, build the local `Dockerfile`, publish port `8000`, mount `./data:/app/data` and `/var/run/docker.sock:/var/run/docker.sock`.

`Dockerfile` must install Python, the `docker` Python package, Docker CLI and Compose Plugin, copy the package source, and run:

```text
uvicorn docker_manage_server.main:app --host 0.0.0.0 --port 8000
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/unit/test_config.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the bootstrap**

```bash
git add pyproject.toml Dockerfile compose.yaml .gitignore src tests
git commit -m "chore: bootstrap docker manage server"
```

### Task 2: 建立任务模型和持久化存储

**Files:**
- Create: `src/docker_manage_server/models.py`
- Create: `src/docker_manage_server/storage.py`
- Test: `tests/unit/test_storage.py`

**Interfaces:**
- `TaskStatus`: `uploaded`, `extracting`, `pending_review`, `deploying`, `deployed`, `discarded`, `failed`。
- `DeploymentTask`: `task_id`, `status`, `original_filename`, `app_name`, `package_dir`, `extracted_dir`, `deployment_dir`, `error`, `command_output`。
- `TaskStore.create(task_id, original_filename) -> DeploymentTask`
- `TaskStore.save(task) -> DeploymentTask`
- `TaskStore.get(task_id) -> DeploymentTask`
- `TaskStore.delete(task_id) -> None`
- `TaskStore.package_dir(task_id) -> Path`
- `TaskStore.extracted_dir(task_id) -> Path`

- [ ] **Step 1: Write failing persistence tests**

```python
from pathlib import Path

from docker_manage_server.models import TaskStatus
from docker_manage_server.storage import TaskStore


def test_create_task_persists_json(tmp_path: Path):
    store = TaskStore(tmp_path)
    task = store.create("task-1", "demo.tar.gz")
    assert task.status is TaskStatus.UPLOADED
    assert store.get("task-1").original_filename == "demo.tar.gz"
    assert store.package_dir("task-1").is_dir()


def test_delete_task_removes_only_task_directory(tmp_path: Path):
    store = TaskStore(tmp_path)
    store.create("task-1", "demo.tar.gz")
    store.create("task-2", "other.tar.gz")
    store.delete("task-1")
    assert not (tmp_path / "packages/task-1").exists()
    assert (tmp_path / "packages/task-2").exists()


def test_save_and_reload_status(tmp_path: Path):
    store = TaskStore(tmp_path)
    task = store.create("task-1", "demo.tar.gz")
    task.status = TaskStatus.PENDING_REVIEW
    store.save(task)
    assert store.get("task-1").status is TaskStatus.PENDING_REVIEW
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_storage.py -q`

Expected: FAIL because the models and store do not exist.

- [ ] **Step 3: Implement models and JSON-backed storage**

Use a mutable Pydantic model for task state so status transitions can be saved explicitly. `TaskStore` must create `packages/`, `tasks/` and `deployments/`, write task JSON atomically through a sibling temporary file, and raise `KeyError` for an unknown task. `delete()` must resolve the exact task package directory and task JSON path before deleting them; it must never delete the complete `data` directory.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_storage.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the task store**

```bash
git add src/docker_manage_server/models.py src/docker_manage_server/storage.py tests/unit/test_storage.py
git commit -m "feat: add persistent deployment task store"
```

### Task 3: 实现安全归档解压、校验和与 review 数据

**Files:**
- Create: `src/docker_manage_server/artifacts.py`
- Test: `tests/unit/test_artifacts.py`
- Test: `tests/fixtures/archive/compose.yaml`
- Test: `tests/fixtures/archive/.env`
- Test: `tests/fixtures/archive/manifest.json`
- Test: `tests/fixtures/archive/checksums.sha256`

**Interfaces:**
- `ArchiveReview`: `app_name`, `files`, `env_text`, `compose_text`。
- `extract_and_review(archive_path: Path, extracted_dir: Path) -> ArchiveReview`
- `list_files(extracted_dir: Path) -> tuple[FileEntry, ...]`
- `overlay_directory(source: Path, target: Path) -> None`

- [ ] **Step 1: Write failing archive tests**

```python
import tarfile
from pathlib import Path

import pytest

from docker_manage_server.artifacts import extract_and_review, overlay_directory


def test_extract_review_reads_manifest_env_and_compose(valid_archive: Path, tmp_path: Path):
    review = extract_and_review(valid_archive, tmp_path / "extracted")
    assert review.app_name == "demo"
    assert "SECRET=value" in review.env_text
    assert "services:" in review.compose_text


def test_path_traversal_archive_is_rejected(tmp_path: Path):
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("../../outside.txt")
        info.size = 1
        bundle.addfile(info, __import__("io").BytesIO(b"x"))
    with pytest.raises(ValueError, match="unsafe archive member"):
        extract_and_review(archive, tmp_path / "extracted")


def test_overlay_does_not_delete_files_missing_from_new_package(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "compose.yaml").write_text("new", encoding="utf-8")
    (target / "compose.yaml").write_text("old", encoding="utf-8")
    (target / "files/data.db").parent.mkdir()
    (target / "files/data.db").write_text("keep", encoding="utf-8")
    overlay_directory(source, target)
    assert (target / "compose.yaml").read_text(encoding="utf-8") == "new"
    assert (target / "files/data.db").read_text(encoding="utf-8") == "keep"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_artifacts.py -q`

Expected: FAIL because the artifact module does not exist.

- [ ] **Step 3: Implement safe extraction and overlay**

Validate every tar member before writing. Reject absolute paths, `..` traversal, duplicate members, device files, FIFO and links whose normalized targets leave the extraction root. Require `manifest.json`, `checksums.sha256`, `compose.yaml` and `.env`; validate the checksum set exactly against extracted regular files, following the client artifact contract. `app_name` must match `[A-Za-z0-9][A-Za-z0-9._-]*` and must not contain `/` or `\\`.

`overlay_directory()` must use explicit recursive copying, preserve existing target-only files, create target directories as needed, and never resolve a source path outside the validated extraction root.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_artifacts.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the artifact module**

```bash
git add src/docker_manage_server/artifacts.py tests/unit/test_artifacts.py tests/fixtures/archive
git commit -m "feat: validate and inspect deployment archives"
```

### Task 4: 封装 Docker Engine 与 Compose 命令

**Files:**
- Create: `src/docker_manage_server/docker_runtime.py`
- Test: `tests/unit/test_docker_runtime.py`

**Interfaces:**
- `DockerRuntime(client: Any | None = None, command_runner: Callable[..., CompletedProcess] = subprocess.run)`
- `DockerRuntime.ping() -> bool`
- `DockerRuntime.list_containers() -> list[dict[str, Any]]`
- `DockerRuntime.get_container(container_id: str) -> Any`
- `DockerRuntime.logs(container_id: str, tail: str, timestamps: bool) -> bytes`
- `DockerRuntime.load_image(image_tar: Path, cwd: Path) -> CommandResult`
- `DockerRuntime.compose_up(cwd: Path) -> CommandResult`
- `DockerRuntime.create_terminal(container_id: str, command: list[str]) -> TerminalSession`

- [ ] **Step 1: Write failing runtime tests**

```python
from types import SimpleNamespace

from docker_manage_server.docker_runtime import DockerRuntime


def test_list_containers_returns_ps_fields_and_raw_attrs():
    raw = {"Id": "abc", "Name": "/demo", "State": {"Running": True}}
    fake = SimpleNamespace(
        id="abc",
        short_id="abc",
        name="demo",
        image=SimpleNamespace(tags=["demo:latest"]),
        attrs=raw,
        status="Up 2 minutes",
        ports={"80/tcp": [{"HostPort": "8080"}]},
        labels={"app": "demo"},
    )
    client = SimpleNamespace(containers=SimpleNamespace(list=lambda all=True: [fake]))
    result = DockerRuntime(client=client).list_containers()
    assert result[0]["id"] == "abc"
    assert result[0]["running"] is True
    assert result[0]["raw_attrs"] == raw


def test_compose_up_uses_fixed_directory_and_no_shell(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")

    result = DockerRuntime(client=SimpleNamespace(), command_runner=runner).compose_up(tmp_path)
    assert result.returncode == 0
    assert calls[0][0] == ["docker", "compose", "--project-directory", str(tmp_path), "up", "-d"]
    assert calls[0][1]["cwd"] == str(tmp_path)
    assert calls[0][1]["shell"] is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_docker_runtime.py -q`

Expected: FAIL because the runtime module does not exist.

- [ ] **Step 3: Implement SDK and subprocess adapters**

Create the default SDK client with `docker.from_env()`. `list_containers()` must call `containers.list(all=True)` and serialize normalized fields plus the complete `container.attrs`. `logs()` delegates to `container.logs(tail=tail, timestamps=timestamps)`. `load_image()` invokes `["docker", "load", "-i", str(image_tar)]`; `compose_up()` invokes `["docker", "compose", "--project-directory", str(cwd), "up", "-d"]`; both use `cwd=str(cwd)`, `shell=False`, captured stdout/stderr and a configured timeout.

Terminal creation must call Docker low-level Exec with `stdin=True`, `stdout=True`, `stderr=True`, `tty=True`, then expose the returned socket and exec ID for the WebSocket adapter. `get_container()` and terminal creation must translate Docker not-found and non-running errors into service exceptions.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_docker_runtime.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the Docker adapter**

```bash
git add src/docker_manage_server/docker_runtime.py tests/unit/test_docker_runtime.py
git commit -m "feat: add docker engine runtime adapter"
```

### Task 5: 实现部署服务和任务状态转换

**Files:**
- Create: `src/docker_manage_server/deployment.py`
- Test: `tests/unit/test_deployment.py`

**Interfaces:**
- `DeploymentService(store: TaskStore, runtime: DockerRuntime)`
- `DeploymentService.upload(task_id: str, archive: BinaryIO, filename: str) -> DeploymentTask`
- `DeploymentService.deploy(task_id: str) -> DeploymentTask`
- `DeploymentService.discard(task_id: str) -> None`

- [ ] **Step 1: Write failing deployment tests**

```python
def test_upload_ends_in_pending_review(tmp_path, valid_archive):
    service = make_service(tmp_path)
    task = service.upload("task-1", valid_archive.open("rb"), "demo.tar.gz")
    assert task.status.value == "pending_review"
    assert task.app_name == "demo"


def test_discard_physically_removes_pending_task(tmp_path, valid_archive):
    service = make_service(tmp_path)
    service.upload("task-1", valid_archive.open("rb"), "demo.tar.gz")
    service.discard("task-1")
    assert not (tmp_path / "packages/task-1").exists()


def test_deploy_loads_image_then_runs_compose_without_deleting_old_bind_data(
    tmp_path, valid_archive_with_files, fake_runtime
):
    service = make_service(tmp_path, fake_runtime)
    service.upload("task-1", valid_archive_with_files.open("rb"), "demo.tar.gz")
    (tmp_path / "deployments/demo/files/old.db").parent.mkdir(parents=True)
    (tmp_path / "deployments/demo/files/old.db").write_text("keep")
    task = service.deploy("task-1")
    assert task.status.value == "deployed"
    assert (tmp_path / "deployments/demo/files/old.db").exists()
    assert fake_runtime.calls == ["load", "compose"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_deployment.py -q`

Expected: FAIL because the deployment service does not exist.

- [ ] **Step 3: Implement deployment orchestration**

Upload must stream the file into `packages/<task_id>/archive.tar.gz`, transition through `uploaded` and `extracting`, call `extract_and_review()`, save `app_name`, `extracted_dir` and stable `deployment_dir`, then transition to `pending_review`.

Deploy must reject every status except `pending_review`, lock by `app_name`, transition to `deploying`, call `overlay_directory()`, load `images.tar` when present, call Compose from the stable directory, save captured command output, and transition to `deployed` only when both commands succeed. Any exception transitions to `failed` and preserves all task files. Discard must reject `deploying` and `deployed`, return a discarded result to the API, and then delete only the exact task package directory and task JSON; no discarded state file remains after the intentional physical deletion.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_deployment.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the deployment service**

```bash
git add src/docker_manage_server/deployment.py tests/unit/test_deployment.py
git commit -m "feat: add staged deployment workflow"
```

### Task 6: 暴露部署和健康检查 API

**Files:**
- Create: `src/docker_manage_server/api.py`
- Create: `src/docker_manage_server/main.py`
- Modify: `tests/conftest.py`
- Test: `tests/api/test_deployment_api.py`

**Interfaces:**
- `create_app(settings: Settings | None = None, store: TaskStore | None = None, runtime: DockerRuntime | None = None) -> FastAPI`
- `app = create_app()` in `main.py`。

- [ ] **Step 1: Write failing API tests**

```python
def test_upload_returns_pending_review(client, valid_archive):
    response = client.post(
        "/api/deployment-tasks",
        files={"file": ("demo.tar.gz", valid_archive.read_bytes(), "application/gzip")},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "pending_review"


def test_review_returns_full_env_and_compose(client, valid_archive):
    task_id = upload(client, valid_archive)["task_id"]
    response = client.get(f"/api/deployment-tasks/{task_id}/review")
    assert response.status_code == 200
    assert "SECRET=value" in response.json()["env"]
    assert "services:" in response.json()["compose"]


def test_health_does_not_require_running_application_containers(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["docker_connected"] is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/api/test_deployment_api.py -q`

Expected: FAIL because the FastAPI application does not exist.

- [ ] **Step 3: Implement routes and dependency injection**

Implement the exact deployment routes from the design. Upload uses `UploadFile` and returns `201`; review returns `files`, `env`, `compose`; deploy returns `202` after starting the deployment task and exposes status through the task endpoint; invalid transitions return `409`; unknown task IDs return `404`; malformed archives return `422` with the persisted task error. `GET /api/health` returns HTTP 200 when the API is alive and Docker responds to `ping()`, otherwise HTTP 503.

Keep application construction injectable so tests can supply temporary storage and fake Docker runtime. Add `client`, `valid_archive` and `upload` fixtures to `tests/conftest.py`. The default `create_app()` uses `Settings`, `TaskStore(settings.data_dir)` and `DockerRuntime()`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/api/test_deployment_api.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the deployment API**

```bash
git add src/docker_manage_server/api.py src/docker_manage_server/main.py tests/api/test_deployment_api.py
git commit -m "feat: expose deployment task api"
```

### Task 7: 暴露容器信息、日志和 WebSocket 终端

**Files:**
- Modify: `src/docker_manage_server/api.py`
- Modify: `src/docker_manage_server/docker_runtime.py`
- Test: `tests/api/test_container_api.py`

**Interfaces:**
- `GET /api/containers` returns `items: list[dict[str, Any]]`。
- `GET /api/containers/{container_id}` returns one normalized container plus `raw_attrs`。
- `GET /api/containers/{container_id}/logs?tail=all&timestamps=false` returns text。
- `WebSocket /api/containers/{container_id}/terminal` supports binary input, binary output and JSON resize messages。

- [ ] **Step 1: Write failing container API tests**

```python
def test_containers_returns_all_docker_ps_fields(client, fake_runtime):
    response = client.get("/api/containers")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["id"] == "abc"
    assert item["image"] == "demo:latest"
    assert item["running"] is True
    assert item["raw_attrs"]["State"]["Running"] is True


def test_logs_returns_docker_output(client):
    response = client.get("/api/containers/abc/logs?tail=100&timestamps=true")
    assert response.status_code == 200
    assert response.text == "hello\\n"


def test_terminal_rejects_stopped_container(client, stopped_runtime):
    with client.websocket_connect("/api/containers/stopped/terminal") as websocket:
        message = websocket.receive_json()
    assert message["error"] == "container_not_running"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/api/test_container_api.py -q`

Expected: FAIL because the routes and terminal bridge do not exist.

- [ ] **Step 3: Implement container routes and terminal bridge**

Use the runtime adapter for all Docker operations. Serialize the complete SDK `attrs` dictionary under `raw_attrs` while keeping normalized `docker ps` fields at the response top level. Logs must pass through `tail` and `timestamps` without persisting output.

For WebSocket terminal sessions, create a PTY Exec, run two concurrent relay loops, and close both resources in a `finally` block. Binary WebSocket frames are stdin. Text JSON frames with `{"type":"resize","width":120,"height":40}` call `exec_resize`; malformed resize messages receive an error JSON and do not terminate the session. Reject a stopped container before creating Exec.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/api/test_container_api.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the container API**

```bash
git add src/docker_manage_server/api.py src/docker_manage_server/docker_runtime.py tests/api/test_container_api.py
git commit -m "feat: add container logs and terminal api"
```

### Task 8: 完成端到端验证与运行文档

**Files:**
- Create: `tests/integration/test_real_docker.py`
- Create: `README.md`

**Interfaces:**
- The documented local command starts the server with the Socket and data mounts.
- Integration tests skip cleanly when Docker or Compose is unavailable.

- [ ] **Step 1: Write the integration tests**

```python
import shutil
import subprocess

import pytest


docker_available = shutil.which("docker") and subprocess.run(
    ["docker", "info"], capture_output=True, check=False
).returncode == 0


@pytest.mark.skipif(not docker_available, reason="Docker daemon unavailable")
def test_real_health_and_container_listing():
    from fastapi.testclient import TestClient
    from docker_manage_server.main import app

    client = TestClient(app)
    assert client.get("/api/health").status_code == 200
    assert isinstance(client.get("/api/containers").json()["items"], list)
```

- [ ] **Step 2: Run the complete test suite**

Run: `python -m pytest -q`

Expected: all unit/API tests pass; the real Docker test is either PASS or SKIPPED with its stated reason.

- [ ] **Step 3: Add usage documentation**

`README.md` must document:

```bash
docker compose up --build -d
curl http://localhost:8000/api/health
```

It must show the Socket and `./data` mounts, explain that `data/deployments/<app_name>/` is stable, and state that the server has host-level Docker control and intentionally has no authentication in version one.

- [ ] **Step 4: Run the documented startup check**

Run: `docker compose config`.

Expected: Compose renders a valid service with both required mounts and port `8000`.

- [ ] **Step 5: Commit the verified delivery**

```bash
git add README.md tests/integration/test_real_docker.py docs/superpowers/specs/2026-08-06-docker-manage-server-design.md
git commit -m "docs: document server deployment and verification"
```

## Self-Review Checklist

- Spec coverage: Tasks 1–2 cover runtime/config/state storage; Task 3 covers archive validation, review and stable overlay; Task 4 covers Docker SDK, CLI and Compose; Task 5 covers task transitions and deployment; Tasks 6–7 cover every API; Task 8 covers integration and operations documentation.
- Placeholder scan: Every task contains concrete files, interfaces, commands and expected outcomes.
- Type consistency: `TaskStore`, `DockerRuntime`, `DeploymentService` and `create_app` signatures are defined before later tasks consume them.
- Safety consistency: review never touches the stable deployment directory; discard targets only one task; overlay never deletes target-only files; Docker commands use argument arrays with `shell=False`.
