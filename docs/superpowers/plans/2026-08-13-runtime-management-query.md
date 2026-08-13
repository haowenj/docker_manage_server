# 运行管理页面查询 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 为运行管理页面的 Compose 项目和独立容器增加不包含状态筛选的服务端查询，并保留 URL 查询条件。

**Architecture:** 在运行清单层新增纯过滤函数，接收 RuntimeOverview 和两个查询字符串，返回不修改原对象的新清单。/runtime 路由读取 compose_q、container_q，过滤后把查询值传给服务端渲染模板；两个 GET 表单通过隐藏字段保留另一侧查询条件。

**Tech Stack:** Python 3.11、FastAPI、Jinja2、pytest、现有服务端渲染 CSS。

## Global Constraints

- 不查询状态。
- Compose 查询匹配项目名、项目内容器名和 Compose 服务名。
- 独立容器查询匹配容器名、完整/短 ID 和镜像名。
- 两个查询区域互相独立，但提交任一查询时保留另一查询参数。
- 空白查询显示该区域全部资源；任意非空文本均按不区分大小写的普通关键词处理。
- 不新增 API、不引入 JavaScript 或前端框架。
- 直接在当前 main 分支和工作区修改，不创建分支或 worktree。

---

### Task 1: Add runtime inventory filtering

Files:
- Modify: src/docker_manage_server/runtime_inventory.py
- Test: tests/unit/test_runtime_inventory.py

Interface:
- Add filter_runtime_overview(overview: RuntimeOverview, compose_query: str = "", container_query: str = "") -> RuntimeOverview.
- Preserve compose_error and docker_error, do not mutate overview.

- [ ] Write failing unit tests for:
  - case-insensitive Compose project matching that retains every container in the matched project;
  - Compose container name and service matching that retains only matching containers;
  - standalone name, full ID, short ID, and image matching;
  - whitespace-only queries returning all resources and preserving errors.
- [ ] Run uv run pytest tests/unit/test_runtime_inventory.py -q and confirm failure because the function is absent.
- [ ] Implement filter_runtime_overview plus private _filter_compose_project and _matches_any helpers. Project-name matches return the complete project; container/service matches return a copied ComposeProject with matching containers only; standalone matching checks name, id, short_id, and image with strip().casefold() semantics.
- [ ] Run the focused unit tests and confirm they pass.
- [ ] Commit with git add src/docker_manage_server/runtime_inventory.py tests/unit/test_runtime_inventory.py && git commit -m "feat: filter runtime inventory results".

### Task 2: Add runtime page query parameters and forms

Files:
- Modify: src/docker_manage_server/web.py
- Modify: src/docker_manage_server/templates/runtime/list.html
- Test: tests/web/test_runtime.py

Interface:
- Change /runtime to accept compose_q: str = "" and container_q: str = "".
- Call filter_runtime_overview after inventory.load(), use the filtered overview for both tables, and pass compose_q/container_q to the template.

- [ ] Write failing web tests for independent Compose and standalone filtering, query value echo, preservation of the other query parameter, and separate empty-match messages.
- [ ] Run uv run pytest tests/web/test_runtime.py -q and confirm the new tests fail because the route/template does not filter yet.
- [ ] Add two GET search forms to runtime/list.html. Compose form uses compose_q and matches project/container/service; standalone form uses container_q and matches name/ID/image. Each form preserves the other parameter with a hidden input and offers a clear link that preserves the other query. Do not add status controls.
- [ ] Render "暂无匹配的 Compose 项目。" and "暂无匹配的独立容器。" when the corresponding query is non-empty, while retaining existing no-resource messages for empty queries.
- [ ] Run the focused web tests and confirm they pass.
- [ ] Commit with git add src/docker_manage_server/web.py src/docker_manage_server/templates/runtime/list.html tests/web/test_runtime.py && git commit -m "feat: add runtime management queries".

### Task 3: Full verification and review

Files:
- Review: docs/superpowers/specs/2026-08-13-runtime-management-query-design.md
- Review: src/docker_manage_server/runtime_inventory.py
- Review: src/docker_manage_server/web.py
- Review: src/docker_manage_server/templates/runtime/list.html

- [ ] Run uv run pytest tests/unit/test_runtime_inventory.py tests/web/test_runtime.py -q; expect zero failures.
- [ ] Run uv run pytest; expect exit code 0 and zero failures.
- [ ] Run git diff --check HEAD~2..HEAD, git status -sb, and git log --oneline -3; confirm no whitespace errors and no unintended files.
