# 镜像管理模块设计

## 目标

为 Docker Manage Server 新增独立的“镜像管理”模块，用于查看本机 Docker daemon 中的全部镜像、按本地镜像信息搜索、分页浏览、查看完整 inspect 数据和引用容器，并在镜像未被任何容器引用时安全删除镜像。

本功能只管理本机镜像，不搜索远程仓库，不拉取、导入或构建镜像。所有查询与删除都通过 Docker SDK 执行，不引入后台任务、批量操作或强制删除。

## 导航与页面结构

左侧主导航新增一级入口“镜像管理”，地址为 `/images`，与“运行概览”“部署任务”“运行管理”并列，不放入运行管理页内部。

### 镜像列表 `/images`

列表使用服务端搜索和分页：

- 每页固定显示 20 条；
- 默认按创建时间倒序排列，创建时间相同则按完整镜像 ID 稳定排序；
- 同一个完整镜像 ID 只显示一条记录，其全部仓库名和 Tag 在同一条记录中展示；
- dangling 镜像必须显示，以“未标记”代替空 Tags；
- 列表字段包括 Tags、短镜像 ID、大小、创建时间和引用容器数量；
- 点击镜像记录进入镜像详情页；
- 页面显示符合当前搜索条件的总数量、当前页和总页数。

搜索框使用查询参数 `q`，匹配以下本机字段：

- 完整镜像 ID；
- 去掉 `sha256:` 前缀的完整 ID及其短 ID；
- 完整仓库名与 Tag，例如 `library/redis:7-alpine`；
- 仓库名和 Tag 的任意子串。

搜索采用 Unicode 大小写无关的模糊包含匹配。提交新搜索时回到第 1 页；翻页链接保留原搜索关键字。空白关键字等同于不搜索。

分页参数 `page` 必须是大于等于 1 的整数。超出总页数的有效正整数返回空结果页，并保留准确的总数和总页数；类型错误或小于 1 返回 `422`。没有任何匹配结果时总页数为 0，当前页仍回显请求的有效页码。

### 镜像详情 `/images/{image_id}`

详情路由允许使用 Docker daemon 可解析的完整或短镜像 ID，但服务端读取后立即固定为 daemon 返回的完整不可变镜像 ID，后续引用匹配和删除只使用该完整 ID。

详情页分为三部分：

1. 常用摘要：完整镜像 ID、全部 Tags、大小、创建时间、架构、操作系统、入口点和默认命令；
2. 引用容器：列出所有直接引用该镜像的运行中和已停止容器；
3. 完整 inspect：以缩进格式展示 Docker SDK 返回的完整镜像属性 JSON。

inspect JSON 必须作为文本转义后显示，不能作为 HTML 插入页面。未知或 Docker daemon 未返回的摘要字段统一显示“—”。

## 引用容器与导航

镜像引用关系以容器 inspect 中解析出的不可变镜像 ID为准，不按可变的镜像名称或 Tag 判断。查询包含运行中和已停止的全部容器。

每条引用记录显示：

- 容器名称；
- 容器状态；
- 完整容器 ID；
- 是否由 Compose 管理；
- Compose 项目名和服务名（适用时）。

导航规则：

- 独立容器链接到 `/containers/{完整容器ID}`；
- Compose 容器链接到 `/compose-projects/{项目名}?container={完整容器ID}`；
- Compose 项目详情页加载后，根据 `container` 参数自动打开对应容器的只读详情弹框；
- 自动打开前必须使用现有项目容器清单验证该完整容器 ID属于当前项目；不匹配、缺失或格式无效时忽略参数，不打开其他弹框，也不泄露其他项目的容器信息。

Compose 容器详情弹框继续保持只读，不增加单容器生命周期操作。

## 分层设计

### DockerRuntime

`DockerRuntime` 只负责与 Docker SDK 交互，增加窄范围方法：

- `list_images() -> list[dict[str, Any]]`：列出所有本机镜像并序列化基础字段与完整 inspect；
- `get_serialized_image(image_id: str) -> dict[str, Any]`：读取单张镜像并返回 daemon 给出的完整 ID；
- `remove_image(reference: str) -> None`：以 `force=False`、`noprune=False` 删除指定 Tag 或镜像 ID；

Docker SDK 的镜像不存在错误映射为新的 `ImageNotFoundError`，其他 daemon/API 错误统一映射为 `DockerRuntimeError`。

镜像序列化至少保留完整 ID、RepoTags、RepoDigests、创建时间、大小、架构、操作系统、Entrypoint、Cmd 和原始 inspect 属性。容器序列化必须提供容器 inspect 中的不可变 `Image` 字段，供引用匹配使用。

### ImageInventoryService

新增 `ImageInventoryService`，作为 Web 与 REST API 共用的唯一镜像查询和安全删除边界。它负责：

- 按完整镜像 ID聚合多 Tags；
- 对 Tags 去重并稳定排序；
- 保留 dangling 镜像；
- 创建时间倒序排序；
- 本地模糊搜索；
- 固定 20 条服务端分页；
- 使用全部容器的不可变镜像 ID计算引用列表；
- 读取镜像详情并返回摘要、inspect 和引用容器；
- 删除前重新读取镜像和全部容器，执行最终引用校验；
- 固定使用校验后获得的完整镜像 ID和 Tags 执行非强制删除。

服务层使用以下数据结构：

- `ImageSummary`：列表所需的稳定镜像摘要；
- `ImagePage`：`items`、`query`、`page`、`page_size`、`total_items` 和 `total_pages`；
- `ImageDetail`：摘要、完整 inspect 与引用容器；
- `ImageInUseError`：携带引用容器的名称和完整 ID；
- `InvalidImagePageError`：表示业务层接收到无效页码。

## 删除语义

删除只允许在镜像未被任何运行中或已停止容器引用时执行。页面是否显示按钮不是安全边界；Web 与 API 都必须调用 `ImageInventoryService`，由服务端在执行前重新读取实时镜像和容器状态。

删除流程：

1. 读取目标镜像，固定 daemon 返回的完整镜像 ID、当前 Tags 和 inspect；
2. 读取全部容器，以不可变镜像 ID检查引用；
3. 存在任一引用时抛出 `ImageInUseError`，不调用任何删除方法；
4. 对每个当前 Tag 调用 Docker SDK 删除，始终使用 `force=False`；
5. Tags 移除完成后，以完整镜像 ID调用删除，仍使用 `force=False`；
6. 返回删除前保存的完整镜像 ID与 Tags。

这一多 Tag 删除流程不是原子操作。如果 Docker 在处理中途失败，部分 Tag 可能已经移除。系统必须立即停止后续操作并返回 `503`，不得重试、回滚、升级为强制删除或隐瞒部分成功；用户刷新详情后看到 daemon 中的最新剩余状态。

如果 Docker SDK 在移除最后一个 Tag 时已经同时删除镜像，使最终按 ID删除返回镜像不存在，则将该结果视为删除成功；其他镜像不存在情况仍返回 `404`。实现必须通过操作前固定的完整 ID与 Tags 区分这两种情况。

## Web 路由与交互

新增明确路由：

- `GET /images?q=&page=1`：镜像列表；
- `GET /images/{image_id}`：镜像详情；
- `POST /images/{image_id}/delete`：安全删除镜像。

详情页在镜像未被引用时显示“删除镜像”按钮，提交前使用现有 `data-confirm` 弹出确认，提示会删除该镜像的全部 Tags且不可恢复。镜像被引用时不显示删除按钮，改为显示“该镜像正被 N 个容器使用”，并保留引用容器列表帮助用户定位。

服务端仍会在提交后进行最终引用校验。删除成功使用 `303` 跳转 `/images`；失败使用现有错误页展示错误状态和安全错误信息。

Compose 项目详情页增加对 `container` 查询参数的只读处理。现有对话框 ID继续由完整容器 ID生成，前端使用 `getElementById` 精确打开，不拼接 CSS 选择器。

## REST API

新增接口：

- `GET /api/images?q=&page=1`；
- `GET /api/images/{image_id}`；
- `DELETE /api/images/{image_id}`。

列表响应包含：

```json
{
  "items": [],
  "query": "redis",
  "page": 1,
  "page_size": 20,
  "total_items": 0,
  "total_pages": 0
}
```

详情响应包含 `item`、`inspect` 和 `containers`。删除成功返回：

```json
{
  "deleted": true,
  "id": "sha256:...",
  "tags": ["example/app:1.0"]
}
```

所有 API 和 Web 写操作继续受现有同源边界保护。

## 错误处理

- 镜像不存在：`404`；
- 镜像被任意容器引用：`409`，错误内容列出引用容器名称；
- `page` 不是整数或小于 1：`422`；
- Docker daemon、SDK、inspect 或删除失败：`503`；
- 跨源 DELETE/POST：`403`，且不得调用 Docker 删除。

错误响应不得泄露其他 Compose 项目中不属于当前导航上下文的内部信息，但镜像详情的引用列表本身被授权展示本机全部直接引用容器，这是镜像管理模块的明确功能范围。

## 安全边界

- 删除不使用 `force=True`；
- 不提供任意 Docker 命令输入；
- 删除前必须重新读取镜像和全部容器；
- 所有实际删除只使用校验后固定的完整镜像 ID与 daemon 返回的当前 Tags；
- 搜索文本只用于内存字符串匹配和页面转义，不进入 shell、文件路径或 Docker 命令；
- inspect JSON 和 Tags 必须经过模板自动转义；
- Compose 自动打开弹框只接受当前项目内已验证的完整容器 ID；
- 不删除容器、卷、构建缓存或其他镜像。

## 测试与验收

### 单元测试

- Docker SDK 镜像列表、详情和非强制删除调用；
- 镜像不存在和 daemon/API 异常映射；
- 按完整 ID聚合多 Tags、Tags 去重与稳定排序；
- dangling 镜像保留；
- 创建时间倒序和相同时间的稳定排序；
- Unicode 大小写无关搜索，覆盖仓库名、Tag、完整 ID和短 ID；
- 20 条分页、空结果、页码越界和非法页码；
- 使用不可变镜像 ID匹配运行中及已停止容器；
- 有引用时不执行删除；
- 多 Tag 非强制删除顺序；
- 中途失败停止后续删除并传播运行时错误；
- 最后一个 Tag 已连带删除镜像时按成功处理。

### Web 测试

- 左侧“镜像管理”导航和 active 状态；
- 列表固定 20 条、总数和页数；
- 搜索后从第 1 页开始且翻页保留关键字；
- dangling 与多 Tag 展示；
- 详情摘要、转义后的 inspect JSON和引用容器；
- 独立容器和 Compose 容器生成正确链接；
- Compose 项目链接只打开当前项目内正确的容器弹框；
- 未引用镜像显示删除按钮和确认提示；
- 被引用镜像隐藏删除按钮并显示阻止原因；
- 删除成功 303 跳转；
- 404、409、422、503 错误页。

### API 与安全测试

- 列表、搜索、分页、详情和删除成功响应；
- 404、409、422、503；
- 多 Tag 删除返回原始稳定身份；
- 跨源 API DELETE 和 Web POST 返回 403，且运行时没有删除调用。

### 集成与浏览器验证

- 真实 Docker 测试只构建或创建带唯一测试标签和唯一名称的临时镜像；
- 创建唯一临时容器验证运行中和已停止容器都能阻止删除；
- 删除临时容器后验证镜像可以非强制删除；
- 清理阶段只处理本测试创建并可由唯一标签验证的资源，不操作机器上已有镜像或容器；
- 浏览器验证搜索、翻页、详情、删除确认与跳转；
- 浏览器验证从镜像引用列表进入 Compose 项目并自动打开正确容器弹框；
- 完整 pytest、Python 字节码编译、JavaScript 语法检查、Compose 配置校验和代码审查通过。

## 范围外事项

- Docker Hub 或其他远程仓库搜索；
- 镜像拉取、导入、构建、打 Tag或推送；
- 批量删除；
- 强制删除；
- 自动清理 dangling 镜像；
- 删除构建缓存、卷或容器；
- 后台任务、进度轮询、重试或回滚；
- 自定义每页数量或客户端分页。

## 设计修订：按使用状态删除 Tags

本节替代前文“删除整张镜像”的语义。删除改为 Tag 粒度，不再主动按完整镜像 ID 删除底层镜像。

容器序列化增加 inspect `Config.Image` 作为 `image_reference`。服务端以该原始引用与完整 Tag 精确匹配，判断 Tag 是否被运行中或已停止容器使用。使用镜像 ID或 digest 创建的容器仍出现在引用列表中，但不把某个 Tag标记为正在使用。

详情页只要存在至少一个未使用 Tag，就显示“删除可用 Tags”。操作先进入预览页，列出当前可删除和因容器引用而保留的 Tags；没有 Tag 的 dangling 镜像或全部 Tags 都在使用时不能提交。用户一次确认后删除全部可用 Tags，不提供复选框。

提交时实时重算，不能相信预览结果：

1. 重新读取目标镜像并固定 daemon 返回的完整 ID；
2. 重新读取全部容器原始镜像引用，计算 `retained_tags` 和候选 Tags；
3. 每个候选 Tag 删除前重新解析；已消失或已指向其他镜像时放入 `skipped_tags`，不调用删除；
4. 仍指向固定完整 ID的 Tag使用 `force=False`、`noprune=False` 删除并放入 `deleted_tags`；
5. 不主动按完整镜像 ID删除；返回镜像 ID、三个 Tag列表和 `image_exists`。

Docker Engine 没有“仅当 Tag仍指向指定镜像 ID时删除”的原子条件接口。重新解析和删除之间仍有极短的外部并发窗口；预览页必须明确说明。进程内按完整镜像 ID互斥，避免本应用自身并发操作，但不能锁住其他 Docker 客户端。

Tag 已消失或重指属于预期并发结果，进入 `skipped_tags`。Docker SDK/daemon 删除失败立即停止，返回 `503`，不重试、不回滚、不强制删除。inspect 或序列化阶段的 Docker SDK错误也必须映射为 `503`。

Web 路由调整为：

- `GET /images/{image_id}/delete`：Tag删除预览；
- `POST /images/{image_id}/delete`：实时重算并删除可用 Tags；
- `GET /images/tag-removal-results/{result_id}`：一次性结果页。

POST 成功以 `303` 跳转结果页。结果页列出实际删除、保留、跳过的 Tags和镜像是否仍存在；镜像仍存在时提供返回详情，始终提供返回列表。

REST API 调整为：

- `GET /api/images/{image_id}/tag-removal-preview`；
- `DELETE /api/images/{image_id}/tags`。

API 删除响应包含 `id`、`deleted_tags`、`retained_tags`、`skipped_tags` 和 `image_exists`。没有任何可删除 Tag返回 `409`。
