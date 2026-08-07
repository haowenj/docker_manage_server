# Form Upload Origin Policy Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让浏览器直连管理台上传离线包时通过同源安全校验，同时继续拒绝跨源和不透明来源请求。

**Architecture:** 保留现有 `origin_matches_host()` 和全局安全中间件，只把响应的 Referrer Policy 调整为 `same-origin`，使同源表单 POST 携带可校验 Origin。通过响应头单元回归、现有跨源测试、完整测试和真实浏览器表单上传验证行为。

**Tech Stack:** Python 3.12、FastAPI、Starlette TestClient、pytest、Docker Compose、Docker Manage 打包 CLI

## Global Constraints

- 不允许把 `Origin: null` 加入可信来源。
- 继续拒绝 Origin 主机与 Host 不一致的 POST、PUT、PATCH、DELETE 和 WebSocket。
- 离线包目标平台保持 `linux/amd64`，宿主机端口保持 `6308`，数据路径保持 `/data/docker-manage-server`。
- 不修改现有用户未跟踪文件 `.docker-manage/` 和 `uv.lock` 的内容，打包 CLI 自己维护的快照除外。

---

### Task 1: Referrer Policy 回归修复

**Files:**
- Modify: `tests/web/test_security.py`
- Modify: `src/docker_manage_server/api.py:70`

**Interfaces:**
- Consumes: `create_app()` 返回的 FastAPI 应用及其安全中间件。
- Produces: 所有 HTTP 响应包含 `Referrer-Policy: same-origin`；现有 Origin/Host 校验接口不变。

- [ ] **Step 1: Write the failing test**

把 `test_responses_include_security_headers` 的断言改为：

```python
assert response.headers["referrer-policy"] == "same-origin"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/web/test_security.py::test_responses_include_security_headers`

Expected: FAIL，实际值为 `no-referrer`，期望值为 `same-origin`。

- [ ] **Step 3: Write minimal implementation**

把安全中间件响应头改为：

```python
response.headers["Referrer-Policy"] = "same-origin"
```

- [ ] **Step 4: Run focused tests**

Run: `.venv/bin/python -m pytest -q tests/web/test_security.py tests/web/test_deployments.py`

Expected: 7 tests pass，跨源 POST 仍返回 403，同源上传仍进入路由。

### Task 2: 完整验证和离线包

**Files:**
- Verify: `src/docker_manage_server/api.py`
- Verify: `tests/web/test_security.py`
- Generate: `.docker-manage/dist/` 下由 CLI 以实际版本命名的 `.tar.gz` 归档

**Interfaces:**
- Consumes: 修复后的服务端源码、现有 `.docker-manage/.env` 和 `.docker-manage/ports.json`。
- Produces: 经校验的 `linux/amd64` Docker Manage 离线归档。

- [ ] **Step 1: Run the complete test suite**

Run: `.venv/bin/python -m pytest -q`

Expected: 所有测试通过，无失败和错误。

- [ ] **Step 2: Verify Compose configuration**

Run: `DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server DOCKER_MANAGE_SERVER_PORT=6308 docker compose config`

Expected: 发布端口为 `6308:8000`，数据目录 source/target 均为 `/data/docker-manage-server`。

- [ ] **Step 3: Run real-browser regression**

在临时 `DATA_DIR` 下启动服务，用真实浏览器直连 `/deployments` 并上传无敏感内容的无效归档。

Expected: POST 到达归档校验并返回 422，不再由安全中间件返回 403；请求中的 Origin 与 Host 一致。

- [ ] **Step 4: Inspect and plan the package with the bundled CLI**

Run:

```bash
SKILL_DIR=/Users/wenjuhao/code/python/docker_manage/skills/package-docker-app
PROJECT=/Users/wenjuhao/code/python/docker_manage_server
uv run --project "$SKILL_DIR" docker-package-app inspect "$PROJECT" --json
```

保存实际 `run_id`，按 CLI 返回的问题生成权限 `0600` 的完整 answers JSON，再使用同一 `run_id` 执行 `plan --non-interactive --json`。

- [ ] **Step 5: Package after exact plan confirmation**

使用 CLI 返回的实际 `plan_hash` 执行：

```bash
uv run --project "$SKILL_DIR" docker-package-app package "$PROJECT" \
  --run-id "$RUN_ID" --answers "$ANSWERS" \
  --confirm-plan-hash "$PLAN_HASH" --non-interactive --json
```

Expected: CLI 返回归档绝对路径、大小、SHA-256、打包镜像和服务器路径，退出码为 0。

- [ ] **Step 6: Review final diff**

Run: `git diff --check && git status --short`

Expected: 只有计划内源码、测试和由 CLI 管理的打包产物状态发生变化，且没有空白错误。
