# Docker Manage Server 第一版设计

## 1. 范围与目标

服务端运行在 Docker 容器中，通过挂载宿主机的
`/var/run/docker.sock` 管理宿主机 Docker Engine。第一版提供以下能力：

- 上传客户端生成的 Docker Manage `.tar.gz` 部署归档；
- 安全解压、校验并展示归档内容；
- 在部署前查看完整的 `.env` 与 Compose 文件内容；
- 用户确认后加载镜像并执行 Compose 部署；
- 查看宿主机所有 Docker 容器的信息与日志；
- 通过 WebSocket + Docker Exec 进入运行中的容器终端；
- 通过 Docker daemon 连接状态提供服务端健康检查。

第一版不包含：

- 登录或 API 鉴权；
- 要求业务容器实现 HTTP 健康检查；
- 在线编辑 `.env` 或 Compose 文件；
- 部署回滚、版本切换和自动清理策略；
- 容器编排以外的主机运维能力。

由于 Docker Socket 等价于宿主机 Docker 控制权限，服务端只面向受信任的内网环境部署。

## 2. 总体架构

采用 Python + FastAPI。Docker SDK for Python 负责 Docker Engine API 交互；Docker Compose 部署仍通过服务端镜像内的 Docker CLI 和 Compose Plugin 执行。

```text
浏览器/客户端
       │ HTTP + WebSocket
       ▼
FastAPI 服务端容器
       ├── 部署包模块
       ├── 任务状态模块
       ├── Docker SDK
       └── docker compose 子进程
              │
              ▼
宿主机 Docker Engine
              │
              ▼
宿主机上的业务容器
```

服务端容器至少需要：

- 挂载 `/var/run/docker.sock:/var/run/docker.sock`；
- 将宿主机绝对路径 `${DOCKER_MANAGE_DATA_DIR}` 挂载到服务端容器内完全相同的 `${DOCKER_MANAGE_DATA_DIR}`；
- 安装 Python `docker` 包；
- 安装 Docker CLI 与 Compose Plugin。

服务端启动时通过 `docker.from_env()` 创建 Docker 客户端，并通过 `client.ping()` 检查 Docker daemon 是否可访问。

## 3. 数据目录与稳定部署路径

服务端项目根目录新增 `data/`，加入 `.gitignore`。目录内容不提交到 Git。运行服务端时通过 `DOCKER_MANAGE_DATA_DIR` 指定宿主机数据目录；该路径必须是绝对路径，并且同时作为服务端容器内的 `DATA_DIR`。

```text
data/
├── packages/
│   └── <task_id>/
│       ├── archive.tar.gz
│       └── extracted/
├── tasks/
│   └── <task_id>.json
└── deployments/
    └── <app_name>/
        ├── compose.yaml
        ├── .env
        ├── manifest.json
        ├── images.tar
        └── files/
```

### 3.1 待审核目录

上传归档先保存到 `data/packages/<task_id>/archive.tar.gz`，再解压到同一任务下的 `extracted/`。待审核期间不修改 `data/deployments/`，也不执行 Docker 加载或 Compose 命令。

归档必须是客户端现有的 tar.gz 格式，并通过以下检查：

- 成员路径不得越出解压根目录；
- 不接受设备文件、FIFO 和不安全的符号链接；
- `manifest.json`、`checksums.sha256` 存在且可解析；
- 校验和清单与实际文件完全匹配；
- `manifest.json` 中的 `app_name` 必须是安全的单层目录名；
- 必须存在 `compose.yaml` 和 `.env`。

### 3.2 固定部署目录

实际部署目录固定为：

```text
data/deployments/<manifest.app_name>/
```

不能使用 `task_id` 作为 Compose 工作目录。这样 Compose 中的相对 bind mount 始终相对于同一个目录，能够保持客户端已有的稳定部署路径语义。

部署确认后，将 `extracted/` 的内容覆盖式合并到固定部署目录：

- 归档中存在的同名文件可以覆盖旧文件；
- 归档中不存在的文件不删除；
- 归档不包含 `files/...` 时，服务器已有的 bind mount 数据保持不变；
- 归档再次选择 `copy` 并包含 `files/...` 时，允许按归档内容覆盖对应文件；
- 绝对服务器路径不由服务端自动清空或改写。

因此，第一次选择 `copy` 后，后续选择跳过复制不会覆盖已有容器数据。由于 Compose 从服务端容器内启动，宿主机和服务端容器必须使用相同的绝对数据目录，否则 Docker daemon 无法解析服务端容器内部的相对挂载路径。

## 4. 部署任务状态机

任务状态持久化到 `data/tasks/<task_id>.json`，服务端重启后可以继续查询历史任务状态。

```text
uploaded → extracting → pending_review → deploying → deployed
    │           │              │              │
    └──────→ failed        discarded       failed
```

状态含义：

- `uploaded`：归档已落盘；
- `extracting`：正在解压和校验；
- `pending_review`：解压成功，等待用户查看；
- `deploying`：正在合并固定部署目录、加载镜像和执行 Compose；
- `deployed`：Compose 命令成功完成；
- `discarded`：用户放弃部署，任务目录已物理删除；
- `failed`：解压、校验或部署失败，保留任务目录和错误信息以便排查。

同一个 `app_name` 同时只允许一个部署任务执行合并和 Compose 操作。第一版使用按应用加锁的方式避免并发覆盖。

部署步骤固定为：

1. 获取该应用的部署锁；
2. 将归档内容合并到 `data/deployments/<app_name>/`；
3. 如果存在 `images.tar`，执行 `docker load`；
4. 以固定部署目录为工作目录执行 `docker compose up -d`；
5. 保存命令输出、退出码和最终状态；
6. 释放部署锁。

第一版不做回滚。Compose 失败时保留已合并的部署目录和任务错误信息。

## 5. HTTP API

API 前缀为 `/api`。

### 5.1 健康检查

```http
GET /api/health
```

只检查服务端进程和 Docker daemon 连接，不检查业务容器内部接口。返回服务端状态、Docker 连接状态和当前容器数量。

### 5.2 部署包接口

```http
POST   /api/deployment-tasks
GET    /api/deployment-tasks/{task_id}
GET    /api/deployment-tasks/{task_id}/files
GET    /api/deployment-tasks/{task_id}/review
POST   /api/deployment-tasks/{task_id}/deploy
DELETE /api/deployment-tasks/{task_id}
```

行为约定：

- 上传接口接收 multipart 文件并立即创建任务；
- 解压和校验完成后，任务才进入 `pending_review`；
- review 接口返回文件树、完整 `.env` 原文和 Compose 原文；
- deploy 只允许 `pending_review` 任务调用；
- DELETE 只用于未部署任务，执行后物理删除该任务的 `packages/<task_id>/` 和状态文件；
- 已进入 `deployed` 或 `deploying` 的任务不能通过此接口删除。

### 5.3 容器与日志接口

```http
GET /api/containers
GET /api/containers/{container_id}
GET /api/containers/{container_id}/logs
```

容器列表使用 `all=True`，返回 Docker API 原始信息，并提供便于页面展示的标准字段，包括：

- ID、短 ID、名称；
- 镜像和镜像 ID；
- 命令、创建时间、状态；
- 端口映射；
- Labels；
- State、Mounts、Networks 等 inspect 信息。

运行状态统一取 Docker 状态中的 `Running` 字段。第一版不把 Compose healthcheck 或业务 HTTP 检查纳入健康状态。

日志接口支持读取最近日志，至少包含 `tail`、`timestamps` 参数；容器日志由 Docker Engine 返回，服务端不自行持久化。

### 5.4 容器终端

```text
WebSocket /api/containers/{container_id}/terminal
```

连接后：

- 默认在运行中的容器内执行 `/bin/sh`；
- 允许客户端指定其他命令，例如 `/bin/bash`；
- 服务端创建 `stdin=True`、`tty=True` 的 Docker Exec；
- 客户端发送的输入写入 Exec Socket；
- Exec 输出转发回 WebSocket；
- 支持终端窗口 resize；
- 容器停止、Exec 结束或 WebSocket 断开时关闭底层 Socket。

只有 `Running` 容器允许建立终端会话。

## 6. 错误处理

- Docker daemon 不可连接时，健康检查返回失败，容器相关接口返回明确的 Docker 连接错误；
- 归档格式、路径或校验和错误时，任务进入 `failed`，保留目录供排查；
- 找不到任务或容器时返回 404；
- 状态不允许的操作返回 409；
- Compose 或 Docker CLI 非零退出时，保存 stdout、stderr 和退出码；
- 非法的 `app_name`、路径遍历和越界读取统一拒绝；
- 部署过程中的文件合并只允许写入 `data/deployments/<app_name>/`，不得根据用户输入写入任意宿主机路径。

## 7. 测试范围

第一版至少覆盖：

- Docker Socket 不可用和可用时的健康检查；
- tar.gz 安全解压和校验和验证；
- 含 `.env`、Compose、`images.tar`、`files/` 的真实客户端归档；
- review 阶段不触发 Docker 命令；
- discard 只删除待审核任务目录；
- `copy` 归档覆盖固定部署目录中的文件；
- 不含 `files/` 的后续归档不会删除既有 bind mount 数据；
- Compose 执行失败时任务状态、日志和目录保留；
- 容器列表返回完整原始 Docker 信息；
- 日志读取和 WebSocket 终端的数据双向转发；
- 非运行容器拒绝建立终端；
- 同一 `app_name` 的并发部署会被串行化。
