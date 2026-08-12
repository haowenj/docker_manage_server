# Docker Manage Server

Docker Manage Server 在内网 Docker 宿主机上运行，负责接收客户端生成的部署归档、审核归档内容、部署 Compose 应用，并查看宿主机容器状态、日志和终端。

## 启动

服务端容器需要访问宿主机 Docker Socket，并把运行数据目录以“宿主机和服务端容器内相同的绝对路径”挂载。这样服务端容器里执行的 Compose 命令解析出的 bind mount 路径，宿主机 Docker daemon 也能直接访问。

```bash
export DOCKER_MANAGE_DATA_DIR="$PWD/data"
export DOCKER_MANAGE_SERVER_PORT=6308
mkdir -p "$DOCKER_MANAGE_DATA_DIR"
docker compose up --build -d
curl --fail --retry 10 --retry-all-errors --retry-delay 1 \
  "http://localhost:${DOCKER_MANAGE_SERVER_PORT}/api/health"
```

Linux 服务器上建议使用固定的绝对路径，例如：

```bash
export DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server
export DOCKER_MANAGE_SERVER_PORT=6308
mkdir -p "$DOCKER_MANAGE_DATA_DIR"
docker compose up --build -d
curl --fail "http://localhost:${DOCKER_MANAGE_SERVER_PORT}/api/health"
```

如果不显式设置 `DOCKER_MANAGE_DATA_DIR`，Compose 默认使用当前项目目录下的 `data/` 绝对路径。
`DOCKER_MANAGE_SERVER_PORT` 只控制宿主机发布端口，容器内端口始终为 `8000`；未设置时宿主机端口也默认为 `8000`。

启动后访问：

- 运行概览：`http://服务器IP:${DOCKER_MANAGE_SERVER_PORT}/`
- 部署任务：`http://服务器IP:${DOCKER_MANAGE_SERVER_PORT}/deployments`
- 运行管理：`http://服务器IP:${DOCKER_MANAGE_SERVER_PORT}/runtime`
- 镜像管理：`http://服务器IP:${DOCKER_MANAGE_SERVER_PORT}/images`
- API 文档：`http://服务器IP:${DOCKER_MANAGE_SERVER_PORT}/docs`

运行概览依次展示最近 5 个部署任务、前 5 个 Compose 项目和前 5 个独立容器。运行管理展示完整列表。Compose 项目通过 `docker compose ls --all` 读取，因此已停止但仍存在的项目也会保留。旧的 `/containers` 列表地址会跳转到 `/runtime`。

Compose 项目详情是项目内容器的统一入口。容器详情为只读弹框；日志和终端使用带项目上下文的独立页面。Compose 内容器不提供单容器启动、停止、重启或删除。没有 Compose 项目标签的独立容器继续使用独立容器详情、日志和终端页面。

镜像管理列出本机 Docker daemon 中的全部镜像，每页固定 20 条，可按仓库名、Tag 或镜像 ID 模糊搜索。镜像详情展示常用摘要、完整 inspect JSON，以及所有引用它的运行中或已停止容器；Compose 容器引用会进入项目详情并打开对应的只读容器弹框。只有完全未被容器引用的镜像才能删除，删除不使用强制模式，也不会删除容器、卷或构建缓存。

独立容器详情页提供启动、停止、重启和删除操作。运行中的独立容器必须先停止才能删除，不会执行强制删除。

Compose 项目详情页以项目为单位执行 `docker compose start`、`stop`、`restart` 和 `down`。删除项目会删除项目容器与网络，但不使用 `--volumes`，因此保留命名卷和数据。Compose 项目内容器不提供单独生命周期操作。

服务端镜像必须同时提供 Docker CLI、Docker Compose 插件和 Docker Socket 访问。Docker daemon 不可用时运行管理不可用；Compose 列表命令单独失败时，页面仍显示独立容器，并根据容器标签隔离 Compose 容器。

管理台首版不含鉴权，可上传并部署归档、读取 `.env`、查看日志并打开容器终端，只能暴露在受信任内网。

`compose.yaml` 已包含以下两个挂载：

```yaml
volumes:
  - ${DOCKER_MANAGE_DATA_DIR}:${DOCKER_MANAGE_DATA_DIR}
  - /var/run/docker.sock:/var/run/docker.sock
```

第一版没有 API 鉴权。Docker Socket 赋予服务端宿主机级 Docker 控制权限，因此只应在受信任的内网环境使用。

## 部署流程

1. 上传客户端生成的 `.tar.gz` 归档。
2. 服务端将归档保存并解压到任务 staging 目录。
3. 页面审核文件树、完整 `.env` 和 `compose.yaml`；需要时进入“编辑配置”。
4. 编辑页可以修改 `.env`、`compose.yaml`，并添加部署根目录内的“相对目录 + `0000`–`0777` 权限”规则。保存时先运行 Compose 配置校验，不会提前修改正式部署目录。
5. 用户确认部署后，服务端把内容合并到稳定目录：

   ```text
   ${DOCKER_MANAGE_DATA_DIR}/deployments/<app_name>/
   ```

6. 服务端创建规则中缺失的目录，并对明确配置的目录本身执行 `chmod`；不会递归修改内部文件或子目录。
7. 如果存在 `images.tar`，先执行 `docker load`，再从稳定目录执行 `docker compose up -d`。
8. Compose 部署失败的任务可以编辑后重新部署，也可以原样重试；上传或解压失败的任务不能重试。

归档中不存在的文件不会从稳定部署目录删除。因此，后续归档选择跳过 bind mount 内容时，已有的 `files/...` 数据仍会保留。

## API 示例

```bash
SERVER_URL="http://localhost:${DOCKER_MANAGE_SERVER_PORT:-8000}"
curl -F 'file=@my-app.tar.gz' "$SERVER_URL/api/deployment-tasks"
curl "$SERVER_URL/api/deployment-tasks/<task_id>/review"
curl -X POST "$SERVER_URL/api/deployment-tasks/<task_id>/deploy"
curl "$SERVER_URL/api/containers"
curl "$SERVER_URL/api/containers/<container_id>/logs?tail=100&timestamps=true"
```

容器终端使用：

```text
WebSocket /api/containers/<container_id>/terminal
```

默认执行容器内的 `/bin/sh`，也可以通过 `command` 查询参数指定其他命令。

## 开发测试

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e '.[test]'
.venv/bin/python -m pytest -q
docker compose config
```

真实 Docker 集成测试在 Docker daemon 不可用时会自动跳过。
