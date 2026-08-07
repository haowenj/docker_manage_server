# Server Bind Directory Permissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Docker Manage Server 在部署旧、新离线包时安全创建缺失的项目内 bind 目录，并让非 root 应用能够写入，同时保持已有服务器目录权限不变。

**Architecture:** 复用 manifest 现有 `server_paths` 字段，把合法路径从归档审核传递到部署任务。部署时只创建稳定目录内缺失的相对 `files/...` 路径并显式设置 `0777`；overlay 只对本次新建目录复制归档模式。

**Tech Stack:** Python 3.12、FastAPI、Pydantic、pytest、Docker Compose、Docker Manage 打包 CLI

## Global Constraints

- 兼容已有 schema version 1 离线包，不修改 manifest schema。
- 已有服务器目录、权限、所有者和内容不得修改。
- 绝对服务器路径不得由部署服务自动创建或 chmod。
- 自动创建仅限稳定部署目录下的相对 `files/...` 路径。
- 不执行递归 chmod；新建可写目录模式固定为 `0777`。
- 最终服务端离线包平台保持 `linux/amd64`、端口保持 `6308:8000`、数据目录保持 `/data/docker-manage-server`。

---

### Task 1: Manifest server_paths 校验与持久化

**Files:**
- Modify: `tests/conftest.py`
- Modify: `tests/unit/test_artifacts.py`
- Modify: `tests/unit/test_deployment.py`
- Modify: `src/docker_manage_server/artifacts.py`
- Modify: `src/docker_manage_server/models.py`
- Modify: `src/docker_manage_server/deployment.py`

**Interfaces:**
- Produces: `ArchiveReview.server_paths: tuple[str, ...]`
- Produces: `DeploymentTask.server_paths: tuple[str, ...] = ()`

- [ ] **Step 1: Write failing archive and upload tests**

让测试归档默认包含 `server_paths=["./files/sqlite"]`，断言审核结果和上传任务均保存规范化后的 `("files/sqlite",)`；新增 `../outside` 被拒绝的归档测试。

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/unit/test_artifacts.py tests/unit/test_deployment.py`

Expected: FAIL，因为 `ArchiveReview` 和 `DeploymentTask` 尚无 `server_paths`。

- [ ] **Step 3: Implement minimal parsing and persistence**

读取 manifest 列表；绝对路径保留，安全相对路径规范化为 `files/...`；空值、反斜杠、NUL、`..` 及非 `files/` 相对路径抛出 `ValueError`。上传时把结果保存到任务。

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/python -m pytest -q tests/unit/test_artifacts.py tests/unit/test_deployment.py`

Expected: 相关测试通过。

### Task 2: 部署前创建缺失目录

**Files:**
- Modify: `tests/unit/test_artifacts.py`
- Modify: `tests/unit/test_deployment.py`
- Modify: `src/docker_manage_server/artifacts.py`
- Modify: `src/docker_manage_server/deployment.py`

**Interfaces:**
- Produces: `prepare_server_directories(deployment_dir: Path, server_paths: tuple[str, ...]) -> None`

- [ ] **Step 1: Write failing permission tests**

覆盖四种行为：缺失 `files/sqlite` 创建为 `0777`；Compose 执行前路径已存在；已有目录 `0700` 保持不变；绝对路径不创建。增加既有符号链接解析到部署目录外时失败的测试。

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/unit/test_artifacts.py tests/unit/test_deployment.py`

Expected: FAIL，因为准备函数不存在且部署流程未调用。

- [ ] **Step 3: Implement minimal directory preparation**

只处理规范化相对 `files/...` 路径；先验证解析结果仍在部署目录内，缺失时 `mkdir(parents=True)` 后显式 `chmod(0o777)`，已有目标保持不变。部署流程在 overlay 后、Docker 命令前调用。

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/python -m pytest -q tests/unit/test_artifacts.py tests/unit/test_deployment.py`

Expected: 所有新增目录准备测试通过。

### Task 3: Overlay 新目录模式保留与完整交付

**Files:**
- Modify: `tests/unit/test_artifacts.py`
- Modify: `src/docker_manage_server/artifacts.py`
- Generate: `.docker-manage/dist/` 下由 CLI 以实际提交版本命名的服务端归档

**Interfaces:**
- Consumes: `overlay_directory(source: Path, target: Path) -> None`
- Produces: 新目录采用源目录权限，已有目录权限不变。

- [ ] **Step 1: Write failing overlay mode tests**

源目录设为 `0777`，断言新目标目录模式为 `0777`；预先创建目标目录并设 `0700`，断言 overlay 后仍为 `0700`。

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/unit/test_artifacts.py`

Expected: 新目标目录实际为受 umask 影响的 `0755`，测试失败。

- [ ] **Step 3: Implement minimal mode preservation**

目录不存在时创建并使用 `stat.S_IMODE(source_mode)` 显式 chmod；目录已存在时只确认其类型，不改变模式。

- [ ] **Step 4: Run full verification**

Run: `.venv/bin/python -m pytest -q && git diff --check`

Expected: 全部测试通过且无空白错误。

- [ ] **Step 5: Commit and package**

提交计划内源码、测试和文档；然后严格使用 `package-docker-app` CLI 重新 inspect、plan，展示精确 `plan_hash` 并等待用户确认后生成 `linux/amd64` 离线包。
