# 运行资源生命周期操作设计

## 目标

为运行管理中的独立 Docker 容器和 Docker Compose 项目增加启动、停止、重启、删除操作，并保持两类资源的操作边界：独立容器使用 Docker 容器 API，Compose 项目始终通过 Docker Compose CLI 以项目为单位操作。

所有操作同步执行。页面等待操作完成后再跳转或显示错误，不引入后台任务、轮询或操作队列。

## 资源与动作语义

### 独立容器

- 启动：Docker 容器 `start`。
- 停止：Docker 容器 `stop`。
- 重启：Docker 容器 `restart`。
- 删除：Docker 容器 `remove`，不使用强制删除，不删除关联卷。

运行中的独立容器不允许删除，必须先停止。服务端必须重新读取容器状态并验证归属，不能只依赖页面隐藏按钮。

### Compose 项目

- 启动：`docker compose --project-name <项目名> start`，只启动现有且已停止的项目容器，不创建缺失容器。
- 停止：`docker compose --project-name <项目名> stop`。
- 重启：`docker compose --project-name <项目名> restart`。
- 删除：`docker compose --project-name <项目名> down`。

Compose 删除不传 `--volumes`，因此删除项目容器和网络，但保留命名卷及其数据。命令在不包含 Compose 配置文件的临时空目录中执行，仅凭已经验证的项目名定位现有项目，避免意外读取 Docker Manage 自身或其他目录中的 Compose 配置。

不提供 Compose 项目内容器的单独生命周期操作。

## 页面交互

操作按钮只出现在资源详情页顶部，运行管理列表与首页不增加操作按钮。

### 独立容器按钮

- 运行中：显示“停止”“重启”；不显示“启动”和“删除”。
- 已停止：显示“启动”“删除”；不显示“停止”和“重启”。

“启动”直接提交。“停止”“重启”“删除”使用现有 `data-confirm` 机制弹出确认。删除提示明确说明只允许删除已停止容器。

### Compose 项目按钮

只要项目中至少一个容器正在运行，就把项目视为运行中：

- 运行中：显示“停止”“重启”“删除”。
- 完全停止：显示“启动”“删除”。

“启动”直接提交。“停止”“重启”“删除”需要确认。删除确认明确说明会删除项目容器与网络，但保留命名卷和数据。

启动、停止、重启成功后使用 `303` 返回原详情页并显示最新状态。删除成功后使用 `303` 返回 `/runtime`，因为原资源详情已不存在。

## 分层与接口

### DockerRuntime

提供明确、窄范围的方法：

- `start_container(container_id: str) -> None`
- `stop_container(container_id: str) -> None`
- `restart_container(container_id: str) -> None`
- `remove_container(container_id: str) -> None`
- `start_compose_project(project_name: str) -> None`
- `stop_compose_project(project_name: str) -> None`
- `restart_compose_project(project_name: str) -> None`
- `remove_compose_project(project_name: str) -> None`

独立容器方法调用 Docker SDK。Compose 方法只允许四个固定子命令，使用参数数组调用 CLI，不经过 shell。Compose 删除固定映射到 `down`。

### RuntimeInventoryService

独立容器操作前调用 `require_standalone_container()`：

1. 读取当前容器。
2. 验证没有 `com.docker.compose.project` 标签。
3. 返回完整不可变容器 ID和实时运行状态。
4. 后续操作只使用该完整 ID，避免名称在验证与执行之间被重新占用。

Compose 操作前调用 `find_project()` 并要求项目存在。用户提供的项目名不能绕过实时资源清单直接进入命令。

### Web 路由

HTML 表单使用明确 POST 路由：

- `/containers/{container_id}/start`
- `/containers/{container_id}/stop`
- `/containers/{container_id}/restart`
- `/containers/{container_id}/delete`
- `/compose-projects/{project_name}/start`
- `/compose-projects/{project_name}/stop`
- `/compose-projects/{project_name}/restart`
- `/compose-projects/{project_name}/delete`

路由不接受任意 `action` 字符串。

### REST API

API 使用明确动作端点：

- `POST /api/containers/{container_id}/start`
- `POST /api/containers/{container_id}/stop`
- `POST /api/containers/{container_id}/restart`
- `DELETE /api/containers/{container_id}`
- `POST /api/compose-projects/{project_name}/start`
- `POST /api/compose-projects/{project_name}/stop`
- `POST /api/compose-projects/{project_name}/restart`
- `DELETE /api/compose-projects/{project_name}`

成功的非删除操作返回最新资源；删除返回被删除资源的稳定身份信息。

## 状态校验与幂等边界

为避免隐藏错误，本功能采用严格状态规则：

- 启动只允许已停止资源。
- 停止和重启只允许运行中资源。
- 独立容器删除只允许已停止容器。
- Compose 删除允许运行中或已停止项目。

状态不允许时不调用 Docker/Compose，返回冲突错误。页面按钮规则与服务端规则一致，但服务端规则是最终边界。

## 错误处理

- 资源不存在或归属不符：404，不泄露其真实归属。
- 当前状态不允许动作：409，提示当前状态和允许的操作。
- Docker daemon、Docker SDK 或 Compose CLI 执行失败：503，保留可用于排查的安全错误文本。
- Compose CLI 非零退出、超时、命令不可用统一映射为运行时错误。

Web 路由使用现有错误页展示；API 返回结构化 `detail`。失败后不进行补偿性强制操作，也不把普通删除升级为强制删除。

## 安全边界

- 继续使用现有同源 POST/DELETE 检查。
- 所有命令参数以列表传入，不使用 shell 字符串。
- Compose 项目名必须来自实时 inventory；独立容器必须通过归属检查。
- 独立容器操作使用校验后返回的完整 ID。
- 不提供 `docker rm -f`、`docker compose down --volumes` 或任意命令输入。

## 测试与验收

### 单元测试

- Docker SDK 四种独立容器动作及异常映射。
- Compose 四条固定命令、空工作目录、超时和非零退出映射。
- 独立容器归属与状态规则。
- Compose 项目存在性与状态规则。

### Web 测试

- 两类详情页按状态显示正确按钮。
- 停止、重启、删除具有正确确认提示，启动不要求确认。
- 成功操作的 `303` 跳转目标。
- 404、409、503 错误页。
- Compose 容器详情弹框不出现生命周期操作。

### API 测试

- 八个明确端点的成功返回与状态码。
- 归属不符、资源不存在、状态冲突、运行时错误。
- 名称与完整 ID 不同时，实际动作使用完整 ID。

### 集成与浏览器验证

- 真实 Docker 测试只创建唯一命名的临时独立容器，验证停止、启动、重启和停止后删除，并在测试清理阶段安全移除测试资源。
- 不自动对本机已有 Compose 项目执行变更操作。
- 浏览器验证按钮显示、确认框、成功跳转和状态刷新。
- 完整 pytest、字节码编译、JavaScript 语法检查和 Compose 配置校验通过。

## 范围外事项

- 批量操作。
- 强制删除运行中容器。
- 删除 Compose 命名卷或数据。
- Compose 项目内容器的单独启动、停止、重启或删除。
- 后台操作任务、进度轮询、自动重试。
- 通过 `up -d` 重建缺失的 Compose 容器。
