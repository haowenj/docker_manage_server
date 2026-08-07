# 服务端 Bind 目录权限修复设计

## 问题

离线包对项目内 bind mount 选择 `keep_server_path` 后不会携带本地目录。服务端首次部署且稳定目录不存在时，Docker Compose 自动把 bind source 创建为 `root:root 0755`。目标应用容器若使用非 root 用户，SQLite 等进程无法在该目录创建文件。

服务端目前还会丢失已复制目录的归档权限：`overlay_directory()` 对普通文件使用 `copy2()`，但对目录只执行受 umask 影响的 `mkdir()`。因此选择 `copy` 的全新 bind 目录也可能从归档中的 `0777` 变为 `0755`。

## 方案

归档已有的 `manifest.server_paths` 继续作为兼容接口，不修改归档 schema。上传审核时读取并保存该字段；部署时只处理安全的相对 `files/...` 路径：目标缺失时在执行 Compose 前创建目录并显式设为 `0777`，目标已经存在时不修改权限、所有者或内容。绝对服务器路径不自动创建或 chmod。

同时调整 `overlay_directory()`：目录目标确实由本次 overlay 新建时，显式复制源目录的权限位；目标目录原先已经存在时保持原权限。这样 `copy` 与 `keep_server_path` 两条路径都不会递归修改服务器已有数据。

## 安全边界

- 拒绝 `server_paths` 中包含 `..`、空值或越出稳定部署目录的相对路径。
- 仅允许自动创建 `files/` 下的相对路径；绝对路径只保留给 Compose 使用。
- 如果 `files/` 下的既有符号链接把目标解析到部署目录之外，部署失败，不跟随到外部路径。
- 不执行递归 `chmod`；只设置本次新建目录本身的权限。
- 已有文件或目录永不自动 chown、chmod 或删除。

## 数据流

1. `extract_and_review()` 校验并返回 `manifest.server_paths`。
2. `DeploymentService.upload()` 把路径保存进 `DeploymentTask`；旧任务缺少字段时默认空元组。
3. `DeploymentService.deploy()` overlay 归档后、执行 Docker 命令前调用目录准备函数。
4. 目录准备函数创建缺失的安全相对路径并设置 `0777`，然后正常执行镜像加载和 Compose。

## 测试

- 归档审核读取合法 `server_paths`，拒绝不安全相对路径。
- 缺失的 `files/...` 路径在 Compose 调用前创建为 `0777`。
- 已有目录模式保持不变。
- 绝对服务器路径不由服务端创建。
- overlay 对新目录保留模式，对已有目录不改模式。
- 完整 pytest、Compose 配置和新服务端离线包校验全部通过。
