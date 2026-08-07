# 表单上传 Origin 策略修复设计

## 问题

管理台通过浏览器直连服务端端口上传离线包时，`POST /deployments` 返回 403。实际请求中 `Host` 正确，但页面响应的 `Referrer-Policy: no-referrer` 使浏览器表单提交携带 `Origin: null`。现有安全边界要求 Origin 的主机与 Host 完全一致，因此在上传逻辑和文件系统写入之前拒绝请求。

## 方案

把管理台响应的 `Referrer-Policy` 从 `no-referrer` 改为 `same-origin`。同源表单提交会携带可校验的 Origin 和 Referer，跨源请求仍不会泄露 Referer。现有 Origin/Host 校验保持不变，不放行 `Origin: null`。

不采用以下方案：

- 允许 `Origin: null`：无法可靠区分受信任表单与不透明来源，会削弱跨站请求防护。
- 新增 CSRF Token：可以提供更强保护，但超出本次单点回归的必要范围，并会改变全部写请求和页面表单。

## 测试

先新增安全响应头回归断言，确认旧实现因仍返回 `no-referrer` 而失败；然后做最小实现修改并运行 Web、安全、完整测试。最后通过真实浏览器直连上传一个无敏感内容的无效归档，确认请求进入上传校验并返回 422，而不是在安全边界返回 403。

## 交付

完整回归通过后，使用项目的 Docker Manage 打包 CLI 按现有环境变量、端口和服务器路径配置生成并验证新的 `linux/amd64` 离线归档。
