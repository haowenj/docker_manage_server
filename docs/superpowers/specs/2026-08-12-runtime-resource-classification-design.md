# 运行资源分类展示设计

## 背景与目标

当前 Docker Manage Server 的首页和“容器管理”页面都按单个容器展示。Compose 项目内容器与直接通过 `docker run` 启动的独立容器混在同一个列表中，页面无法表达两类资源不同的管理边界。后续如果直接在这一结构上增加启动、停止、重启和删除，系统也无法可靠选择容器级 Docker 操作还是项目级 Compose 操作。

本次先重构只读信息架构，不增加生命周期操作：

- 使用 Compose 项目作为 Compose 工作负载的顶层展示和未来操作主体；
- 直接启动的容器仍以单个容器为展示主体；
- 首页、运行管理页和详情页使用一致的分类规则；
- Compose 项目内容器允许查看详情、日志和终端，但不提供单容器生命周期操作；
- 为后续分别实现 Docker 容器操作和 Compose 项目操作建立明确边界。

## 范围

### 本次包含

- 首页三模块分类展示；
- 将“容器管理”升级为“运行管理”；
- Compose 项目列表和详情页；
- Compose 项目内容器的只读详情弹框；
- Compose 项目范围内的容器日志和终端页面；
- Compose 项目与独立容器聚合逻辑；
- Compose CLI 局部失败时的降级展示；
- 对应自动化测试和 README 更新。

### 本次不包含

- 容器或 Compose 项目的启动、停止、重启和删除；
- Compose 文件编辑；
- Compose 内容器的单独生命周期操作；
- 运行数据缓存、历史统计或最近访问记录；
- 日志持续流式跟随。

## 关键决策

### 管理主体

Compose 项目是 Compose 工作负载唯一的顶层管理主体。项目内容器是项目的只读运行单元，不出现在独立容器列表中，也不复用独立容器详情页作为主要入口。

独立容器指不带 `com.docker.compose.project` 标签的容器。独立容器继续使用现有容器详情、日志和终端能力。

### 数据来源

采用 Compose CLI 与 Docker SDK 混合方案：

- `docker compose ls --all --format json` 提供 Compose 项目的权威列表、状态和配置文件信息；
- Docker SDK 一次读取全部容器，包括停止状态容器，并提供容器完整属性、日志和终端能力；
- 容器标签用于将容器关联到 Compose 项目和服务；
- 未带 Compose 项目标签的容器归入独立容器列表。

该方案保留项目级 Compose 语义，又能复用已有容器运行时能力。后续独立容器操作可走 Docker SDK，Compose 项目操作可走 Compose CLI。

## 页面信息架构

### 侧边栏

- “运行概览”保持不变；
- “部署任务”保持不变；
- “容器管理”更名为“运行管理”，主地址为 `/runtime`。

旧的 `/containers` 列表地址使用重定向兼容已有书签。现有 `/containers/{container_id}` 及其日志和终端子页面继续服务独立容器。

### 首页 `/`

首页依次展示：

1. 最近部署任务；
2. Compose 项目；
3. 独立容器。

三个模块各显示前 5 项，并提供“查看全部”入口：

- 最近部署任务按更新时间倒序；
- Compose 项目按运行状态排序，运行中的项目优先，同组内按项目名排序；
- 独立容器按运行状态排序，运行中的容器优先，同组内按容器创建时间倒序。

首页不会显示 Compose 项目内部的单个容器。Compose 项目名链接到项目详情，独立容器名链接到独立容器详情。

首页指标调整为能表达运行资源分类的统计信息，包括 Compose 项目数、独立容器数、容器总数、运行中容器数以及失败任务数。指标基于当前实时 Docker 状态计算。

### 运行管理 `/runtime`

运行管理页包含两个完整列表：

- Compose 项目；
- 独立容器。

列表使用与首页相同的排序规则，但不限制为 5 项。Compose 项目链接到 `/compose-projects/{project_name}`，独立容器链接到 `/containers/{container_id}`。

### Compose 项目详情

项目详情路由为：

```text
/compose-projects/{project_name}
```

页面展示：

- 项目名；
- Compose 状态；
- 配置文件路径；
- 当前容器总数和运行中容器数；
- 项目下全部容器，包括停止状态容器。

每个容器提供：

- “查看详情”：在当前页面打开只读弹框；
- “查看日志”：在新标签页打开 Compose 专属日志页；
- “进入终端”：在新标签页打开 Compose 专属终端页。

详情弹框展示容器名称、Compose 服务名、镜像、状态、端口、挂载、网络和标签。弹框不提供生命周期操作。

### Compose 专属日志与终端

路由为：

```text
/compose-projects/{project_name}/containers/{container_id}/logs
/compose-projects/{project_name}/containers/{container_id}/terminal
```

页面顶部明确显示项目名、服务名和容器名，并提供“返回 Compose 项目”入口。日志和终端底层继续复用现有 Docker SDK 与 WebSocket 能力，但页面和导航不进入独立容器详情层级。

服务端必须验证容器的 `com.docker.compose.project` 标签与路径中的项目名完全一致。容器不存在或归属不匹配时都返回 404。停止状态容器可以查看详情和已有日志；终端页面显示不可连接状态，WebSocket 仍拒绝为停止容器创建 exec。

## 运行数据模型

新增只读视图模型：

### `ComposeProject`

- `name`：Compose 项目名；
- `status`：Compose CLI 返回的项目状态，或本系统补充的缺失状态；
- `config_files`：Compose 配置文件路径列表；
- `running_containers`：运行中容器数；
- `container_count`：项目容器总数；
- `containers`：属于该项目的序列化容器列表。

### `RuntimeOverview`

- `compose_projects`：排序后的 Compose 项目；
- `standalone_containers`：排序后的独立容器；
- `compose_error`：Compose CLI 局部错误；
- `docker_error`：Docker SDK 整体错误，仅用于允许局部渲染的首页。

这些模型只反映当前运行状态，不写入任务数据目录，也不作为历史记录持久化。

## 聚合数据流

首页与运行管理共用一个聚合方法，保证分类规则一致：

1. 执行 `docker compose ls --all --format json`；
2. 解析项目名、状态和配置文件路径；
3. 通过 Docker SDK 一次读取全部容器，包括停止状态容器；
4. 从容器标签读取：
   - `com.docker.compose.project`；
   - `com.docker.compose.service`；
   - `com.docker.compose.project.config_files`；
5. 带 Compose 项目标签的容器按项目名归组；
6. 不带 Compose 项目标签的容器归入独立容器列表；
7. 合并 Compose CLI 项目和标签发现的项目；
8. 计算项目容器数量、运行数量并应用排序规则。

Compose CLI 列出的项目即使当前没有容器也必须保留。容器标签存在、但 Compose CLI 没有列出对应项目时，系统仍生成项目行，状态显示为“未被 Compose CLI 发现”，并可从标签补充配置文件路径。这样的容器绝不能归入独立容器。

配置文件字段可能由 Compose CLI 或标签以逗号分隔形式返回。解析层将其标准化为去重后的路径列表，只用于展示，不用于读取文件或拼接命令。

项目详情每次请求都实时执行聚合，不增加缓存。首版优先保持数据来源清晰和状态新鲜度。

## Docker 运行时扩展

`DockerRuntime` 增加 Compose 项目枚举能力，执行参数固定为：

```text
docker compose ls --all --format json
```

命令必须：

- 使用参数数组；
- 使用 `shell=False`；
- 继承现有命令超时限制；
- 捕获标准输出和标准错误；
- 将非零退出码、超时、无法执行和非法 JSON 转换为明确的 Compose 列表错误。

容器日志、容器详情和终端继续使用 Docker SDK。项目名永远不会拼接进 shell 字符串，也不会被当作本地文件路径使用。

## 错误处理与降级

### Docker daemon 不可用

- 首页仍展示部署任务；
- Compose 项目和独立容器区域显示 Docker 不可用提示；
- `/runtime` 返回带明确说明的 503 页面；
- 现有健康检查仍以 Docker daemon 是否可用为准。

### Docker SDK 正常、Compose CLI 失败

- 首页和 `/runtime` 继续展示独立容器；
- Compose 区域显示局部错误提示；
- 带 Compose 项目标签的容器仍根据标签组成项目；
- Compose 容器不会因 CLI 失败而进入独立容器列表；
- Compose CLI 失败不使健康检查返回 503。

非法 JSON 与命令失败采用相同的局部降级策略。局部错误信息经过模板转义后展示。

### 项目和容器错误

- 项目不存在返回 404；
- 项目存在但没有容器时正常展示空项目详情；
- Compose 内容器不存在返回 404；
- 容器标签不属于路径中的项目返回 404，不泄露它实际属于哪个项目；
- 停止容器的终端连接继续返回现有“容器未运行”错误。

## 安全边界

- 保留现有同源检查、CSP、`X-Content-Type-Options` 和 Referrer Policy；
- Compose 项目名只与 Docker 实时返回的数据比较；
- 项目名、服务名、标签、配置路径和 Docker 错误均由 Jinja2 转义；
- Compose CLI 不接受任何来自 URL 的动态命令参数；
- Compose 专属容器路由必须验证项目归属；
- 项目内容器不显示启动、停止、重启或删除入口。

## API 与内部接口

新增 `RuntimeInventoryService` 作为唯一的运行资源聚合边界。它依赖 `DockerRuntime`，向首页、运行管理页和 Compose 详情页提供 `RuntimeOverview`，并提供按项目名查找项目及验证内容器归属的方法。页面路由不得各自重复 Compose 分类逻辑。

现有 `/api/containers` 和独立容器终端 WebSocket 保持向后兼容。本次不新增公开的运行资源 JSON 列表 API：Compose 容器弹框数据随项目详情由服务端渲染，并由页面内经过模板转义的结构化数据驱动。

Compose 终端新增带项目上下文的 WebSocket 路由：

```text
/api/compose-projects/{project_name}/containers/{container_id}/terminal
```

该路由在接受 WebSocket 后、创建 Docker exec 前验证容器归属。Compose 专属终端页面只连接此路由，不连接无项目上下文的独立容器终端路由。

## 前端交互

- 首页 Compose 项目与独立容器在宽屏下并排，窄屏下纵向排列；
- Compose 项目详情的容器弹框使用原生可访问的对话框语义；
- 弹框支持明确关闭按钮和键盘关闭；
- 日志、终端链接使用新标签页并带 `rel="noopener"`；
- 不为本次功能引入前端框架或外部 CDN；
- 继续复用已有本地 CSS、JavaScript 与 xterm 静态资源。

## 测试策略

### 单元测试

- Compose CLI JSON 正确解析；
- 多配置文件路径正确标准化；
- 命令包含 `--all --format json`，使用 `shell=False` 并继承超时；
- 非零退出码、超时和非法 JSON 转换为 Compose 局部错误；
- Compose 容器按项目标签正确分组；
- Compose 容器不会进入独立容器列表；
- CLI 未发现但标签存在的项目仍被保留；
- 无容器的停止项目仍被保留；
- 项目和独立容器排序规则正确；
- Compose 内容器归属验证正确。

### Web 测试

- 首页三个模块各最多渲染 5 项；
- 首页部署任务、Compose 项目和独立容器分别链接到正确详情；
- `/runtime` 展示两个完整列表；
- `/containers` 列表重定向到 `/runtime`；
- 独立容器现有详情、日志和终端页面保持可用；
- Compose 项目详情渲染项目信息和全部容器；
- 容器只读弹框正确展示并转义 Docker 数据；
- Compose 专属日志和终端页面显示项目、服务和容器上下文；
- 跨项目容器 URL 返回 404；
- Docker 整体故障与 Compose CLI 局部故障采用不同降级行为；
- 窄屏样式将两个运行资源模块改为纵向排列。

### 回归验证

- 运行完整 pytest 测试集；
- 运行 `docker compose config`；
- 在 Docker daemon 可用时覆盖真实容器读取；
- 手工验证一个 Compose 项目、一个独立容器和一个停止 Compose 项目的页面路径。

## 后续扩展

分类完成并稳定后，再单独设计生命周期操作：

- 独立容器通过 Docker SDK 执行启动、停止、重启和删除；
- Compose 项目通过项目级 Compose 命令执行启动、停止、重启和删除；
- Compose 项目内容器继续不提供单独生命周期操作。

本次聚合模型和路由边界应保证后续操作层无需重新判断资源归属。
