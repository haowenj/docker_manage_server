# Docker Manage Server 服务端渲染管理台设计

## 背景

Docker Manage Server 当前提供 FastAPI JSON API 和 WebSocket 终端接口，但访问根路径会返回 404，也没有归档上传、审核、部署和容器查看的图形界面。本设计在保留全部 `/api/*` 行为的前提下，增加一个随服务端镜像交付的服务端渲染管理台。

## 目标

- 访问 `/` 即可进入中文管理台。
- 在页面完成部署归档上传、审核、部署、丢弃和任务状态查看。
- 在页面查看容器列表、容器详情、日志和在线终端。
- 使用 Jinja2 服务端渲染首屏和普通页面交互。
- 仅使用少量浏览器脚本完成确认框、状态轮询、日志刷新和 WebSocket 终端。
- 保持现有 API、归档安全检查、Docker 执行规则和离线部署方式兼容。
- 所有页面资源随 Python 包和 Docker 镜像离线交付。

## 非目标

- 首版不增加用户、角色、登录或权限系统。
- 首版不增加容器启动、停止、重启、删除等新 Docker 操作。
- 不引入 React、Vue、Svelte、Node.js 运行时或前端构建流水线。
- 不增加数据库；任务状态继续使用数据目录中的 JSON 文件。
- 不改变部署归档 schema、manifest schema 或现有 API 响应格式。

## 已确认约束

- 页面范围为完整管理台：概览、归档上传、审核、部署/丢弃、任务状态、容器列表、详情、日志和在线终端。
- 渲染方式为 Jinja2 服务端渲染加少量原生 JavaScript。
- 首版不启用鉴权，仅允许部署在受信任内网。
- 视觉使用固定侧边栏工作台布局，但采用明亮风格：白色侧栏、浅灰内容区、蓝色主操作和低饱和状态色，不使用黑色控制台主题。
- 桌面优先；窄屏时侧边栏转换为顶部导航，宽表格允许水平滚动。

## 总体架构

### 运行组件

`create_app()` 继续构造唯一一组 `TaskStore`、`DeploymentService` 和 `DockerRuntime`。新增 Web 路由工厂接收这三个对象，并直接调用业务对象，不通过 HTTP 回调本服务的 JSON API。

应用由以下部分组成：

1. 现有 API 路由：继续提供 `/api/*` 和容器终端 WebSocket。
2. Web 页面路由：提供 HTML GET、表单 POST 和 303 重定向。
3. Jinja2 模板：负责基础布局、页面、局部组件和错误页面。
4. 本地静态资源：负责明亮主题、响应式布局、状态轮询、日志刷新和终端连接。
5. Web 视图辅助模块：负责状态文案、时间格式、端口格式和模板视图模型，避免在模板中堆积业务判断。

`jinja2>=3.1,<4` 加入 Python 运行依赖。模板和静态资源作为 `docker_manage_server` 包数据配置到 setuptools 中，因此 `pip install .` 后仍可通过 `importlib.resources` 定位。Dockerfile 无需安装 Node.js，也不在镜像构建时执行前端编译。

### 文件职责

- `src/docker_manage_server/web.py`：页面路由、表单处理、同源校验调用和错误响应。
- `src/docker_manage_server/web_views.py`：模板视图模型和纯格式化函数。
- `src/docker_manage_server/templates/`：基础布局、页面、组件和 HTTP 错误模板。
- `src/docker_manage_server/static/css/app.css`：明亮侧边栏、状态、表格、表单和响应式样式。
- `src/docker_manage_server/static/js/app.js`：确认操作、部署状态轮询和日志刷新。
- `src/docker_manage_server/static/js/terminal.js`：现有终端 WebSocket 与终端组件的连接代码。
- `src/docker_manage_server/static/vendor/xterm/`：固定使用 MIT 许可的 `@xterm/xterm 6.0.0` 与 `@xterm/addon-fit 0.11.0` 浏览器分发文件、样式和许可证；资源从本站提供，不访问 CDN，也不需要生产环境 Node.js。版本依据官方稳定 npm 包页面固定，不跟随 beta 标签自动升级。

## URL 与页面设计

### 管理台首页 `GET /`

首页服务端获取任务列表和容器列表，渲染：

- Docker daemon 连接状态；
- 全部容器、运行中容器、待审核、已部署和失败任务数量；
- 最近更新的部署任务；
- 容器状态摘要；
- “上传部署归档”主操作。

Docker 不可用时首页仍返回 HTTP 200，任务区域照常显示，容器区域显示服务降级警告，不把整个管理台变成错误页。

### 部署任务列表 `GET /deployments`

页面顶部是 `.tar.gz` 文件上传表单，下方按 `updated_at` 倒序显示所有未被丢弃的任务。每行包含应用名、原文件名、状态、创建时间、更新时间和详情入口。空列表显示明确的首次上传引导。

### 上传归档 `POST /deployments`

路由生成任务 ID 并直接调用 `DeploymentService.upload()`：

- 成功：返回 303，跳转到 `/deployments/{task_id}`。
- 归档校验失败：保留服务层写入的失败任务，返回 422 的部署列表页面，显示具体错误并提供失败任务详情入口。

不复制或重写现有归档校验逻辑。

### 部署任务详情 `GET /deployments/{task_id}`

页面显示任务状态、应用名、归档文件名、时间、文件树、完整 `.env`、完整 `compose.yaml`、命令输出和错误。`.env`、Compose 和命令输出全部以自动转义的 `<pre>` 文本显示。

仅 `pending_review` 状态显示以下表单：

- `POST /deployments/{task_id}/deploy`：确认后加入 FastAPI `BackgroundTasks`，返回 303 到详情页。
- `POST /deployments/{task_id}/discard`：确认后丢弃任务，返回 303 到任务列表。

详情页初始内容由服务端渲染。任务为 `deploying` 时，浏览器每两秒读取现有 `GET /api/deployment-tasks/{task_id}`，只更新状态、错误和命令输出；进入 `deployed` 或 `failed` 后停止轮询。

### 容器列表 `GET /containers`

页面显示名称、镜像、运行状态、端口和短 ID。状态颜色区分运行中、已停止和异常。DockerRuntime 失败时返回 HTTP 503 的同风格页面，并显示错误详情。

### 容器详情 `GET /containers/{container_id}`

服务端渲染容器基本信息、状态、命令、端口、挂载、网络和标签。页面包含两个交互面板：

- 日志：默认读取最近 100 行；允许选择 tail 数量和时间戳；通过现有日志 API 刷新，所有文本按纯文本插入。
- 终端：使用随服务端离线交付的 xterm.js 渲染 ANSI/TTY 输出，连接现有 `/api/containers/{container_id}/terminal`，默认命令为 `/bin/sh`；窗口变化时继续发送现有 resize 消息。

首版不提供容器生命周期修改按钮。

### 静态资源 `GET /static/*`

FastAPI 挂载包内静态目录。资源 URL 使用固定文件名，不依赖 CDN。模板不包含内联脚本，交互元素通过 `data-*` 属性由本地脚本绑定。

## 任务列表与时间字段

`DeploymentTask` 增加可选的 UTC `created_at` 和 `updated_at` 字段。`TaskStore.create()` 为新任务设置两个时间，`TaskStore.save()` 更新 `updated_at`。

`TaskStore.list()` 扫描 `tasks/*.json`，使用现有 Pydantic 模型读取并按更新时间倒序返回。旧任务 JSON 缺少时间字段时，使用状态文件的修改时间补齐创建和更新时间，保证升级后可以继续读取。

被丢弃的任务继续按现有行为删除状态和包目录，因此不会出现在列表中。

## 请求与数据流

普通页面遵循 Post/Redirect/Get：

1. 浏览器提交表单。
2. Web 路由验证请求来源和输入。
3. Web 路由直接调用现有 store、deployment 或 runtime。
4. 成功时返回 303 到对应 GET 页面。
5. GET 页面重新从业务对象读取真实状态并渲染。

现有 `/api/*` 不调用 Web 路由，Web 路由也不通过 HTTP 调用 `/api/*`。浏览器脚本只在部署轮询、日志刷新和终端场景复用现有 API。

## 错误处理

- 未知任务和容器返回定制的 404 HTML 页面。
- 非法任务状态操作返回 409 HTML 页面。
- 上传归档错误返回 422，并显示服务端具体校验原因。
- Docker daemon 或 Docker SDK 错误在容器页面返回 503；首页降级为警告区域。
- 部署失败保留 `failed` 状态、错误和完整命令输出，不自动重试。
- 模板渲染错误和未知异常仍由 FastAPI 记录，不在页面暴露 Python traceback。
- API 路由继续返回现有 JSON 或纯文本错误，不被 HTML 异常处理器改写。

## 安全边界

首版不启用鉴权。基础布局固定显示“未启用鉴权，仅限受信任内网”的警告，审核页额外提示 `.env` 可能包含敏感信息。

安全规则如下：

- Jinja2 对 HTML 模板变量默认自动转义。
- `.env`、Compose、日志、标签、挂载和命令输出均作为文本处理。
- 页面使用 Content-Security-Policy，只允许本站脚本、样式、图片和连接；不允许第三方 CDN。
- 对带 `Origin` 的 POST、DELETE、PUT、PATCH 和 WebSocket 请求校验 Origin 的主机与 `Host` 一致；缺少 Origin 的现有 CLI/API 客户端保持兼容。
- 部署和丢弃按钮包含浏览器确认步骤，但确认框不替代服务端状态校验。
- 不放宽现有归档成员、校验和、应用名、任务 ID 和部署路径安全检查。
- 不新增任意 Docker 命令参数输入；终端命令仍使用现有 `shlex.split()` 和无 shell Docker Exec。

## 视觉与响应式规则

- 左侧为白色固定导航，包含产品标识、概览、部署任务、容器管理和 Docker 状态。
- 主内容区使用浅灰背景，卡片为白色，主按钮为蓝色。
- 运行、待审核和失败状态分别使用低饱和绿、黄、红，并始终配合文字，不能只依赖颜色。
- 字体使用系统字体，不访问在线字体服务。
- 日志和 Compose 使用等宽系统字体；终端由 xterm.js 独立渲染，但页面外壳仍保持明亮风格。
- 在窄屏上，侧栏转为顶部导航；统计卡片自动换行；表格和长代码块允许水平滚动。

## 测试策略

### 自动化测试

- `TaskStore`：任务列表、倒序排序、时间更新和旧 JSON 兼容。
- 首页：Docker 正常、Docker 不可用、统计数字、最近任务和空状态。
- 部署页面：上传成功 303、上传失败 422、审核内容自动转义、部署/丢弃状态限制和轮询标记。
- 容器页面：列表、详情、未知容器 404、Docker 错误 503、日志参数和终端连接数据。
- 安全响应：CSP、静态资源、同源 POST、跨源拒绝、缺少 Origin 的 API 兼容和 WebSocket Origin 校验。
- 包资源：安装后的包可以定位模板、CSS、脚本、xterm.js 和许可证。
- 回归：运行全部现有 API、归档、部署、Docker Runtime、Compose mount 和真实 Docker 条件测试。

### 视觉与运行验证

- 使用真实 Jinja2 模板检查首页、任务列表、审核详情、容器列表和容器详情。
- 验证空列表、长文件树、长 `.env`、长 Compose、长日志、部署失败和 Docker 离线状态。
- 在桌面和窄屏视口检查明亮侧边栏布局和滚动行为。
- 构建 Docker 镜像，确认模板和静态资源存在且 `/` 可以访问。
- 重新生成并验证 `linux/amd64` 离线归档；宿主机端口保持 `6308`，容器端口保持 `8000`，数据路径保持 `/data/docker-manage-server`，Docker Socket 保持 `/var/run/docker.sock`。

## 完成标准

- 用户访问 `/` 可以进入明亮侧边栏管理台。
- 用户可以在页面上传、审核、部署或丢弃归档，并查看最终状态和命令输出。
- 用户可以查看容器列表、详情、日志并打开可用的在线终端。
- Docker 不可用、归档非法和部署失败均有明确页面反馈。
- `/api/*` 和自动文档继续可用且现有测试通过。
- 页面不依赖公网资源或前端构建环境。
- Docker 镜像与离线部署归档包含全部模板和静态资源。
