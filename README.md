# Docker Manage Server

Docker Manage Server 在内网 Docker 宿主机上运行，负责接收客户端生成的部署归档、审核归档内容、部署 Compose 应用，并查看宿主机容器状态、日志和终端。

## 启动

服务端容器需要访问宿主机 Docker Socket，并把运行数据目录以“宿主机和服务端容器内相同的绝对路径”挂载。这样服务端容器里执行的 Compose 命令解析出的 bind mount 路径，宿主机 Docker daemon 也能直接访问。

```bash
export DOCKER_MANAGE_DATA_DIR="$PWD/data"
mkdir -p "$DOCKER_MANAGE_DATA_DIR"
docker compose up --build -d
curl --fail --retry 10 --retry-all-errors --retry-delay 1 http://localhost:8000/api/health
```

Linux 服务器上建议使用固定的绝对路径，例如：

```bash
export DOCKER_MANAGE_DATA_DIR=/opt/docker-manage-server/data
mkdir -p "$DOCKER_MANAGE_DATA_DIR"
docker compose up --build -d
```

如果不显式设置 `DOCKER_MANAGE_DATA_DIR`，Compose 默认使用当前项目目录下的 `data/` 绝对路径。

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
3. 页面查看文件树、完整 `.env` 和 `compose.yaml`。
4. 用户选择部署后，服务端将内容合并到稳定目录：

   ```text
   ${DOCKER_MANAGE_DATA_DIR}/deployments/<app_name>/
   ```

5. 如果存在 `images.tar`，先执行 `docker load`，再从稳定目录执行 `docker compose up -d`。

归档中不存在的文件不会从稳定部署目录删除。因此，后续归档选择跳过 bind mount 内容时，已有的 `files/...` 数据仍会保留。

## API 示例

```bash
curl -F 'file=@my-app.tar.gz' http://localhost:8000/api/deployment-tasks
curl http://localhost:8000/api/deployment-tasks/<task_id>/review
curl -X POST http://localhost:8000/api/deployment-tasks/<task_id>/deploy
curl http://localhost:8000/api/containers
curl 'http://localhost:8000/api/containers/<container_id>/logs?tail=100&timestamps=true'
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
