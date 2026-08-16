# Agent Bridge 公网安全边界

本文件描述 v0.19.0 引入、截至 v0.40.2 持续加固的公网模式，区分应用已经强制的安全门和仍必须由反向代理、主机及运维系统承担的部分。公网模式是显式选择：未设置 `AGENT_BRIDGE_PUBLIC_MODE=1` 时，原本机/LAN 行为不变。

## 1. 先看结论

不要把默认 `0.0.0.0:8765` 直接映射到互联网。推荐路径是：

```text
Internet -> TLS reverse proxy / VPN -> 127.0.0.1:8765 -> Agent Bridge
                                      \-> private bridge.db (0600)
Agent machines -> outbound HTTPS/VPN -> same reverse proxy
```

公网模式会在启动时 fail closed。以下任何一项缺失都会拒绝启动：

1. 默认管理员仍要求使用 `admin/admin` 首次改密；
2. `/agent/register` 没有至少 32 字符的独立登记密钥；
3. 没有精确的允许 Host 和 HTTPS Origin；
4. 既没有直接 TLS 证书/私钥，也没有明确的可信反向代理 IP/CIDR；
5. 使用 `*` 信任所有转发头来源；
6. 数据库不是服务账户所有且权限不是 `0600`，或数据库目录可被组/其他用户写入；
7. TLS 私钥或从文件读取的注册码可被组/其他用户访问。

## 2. 主要风险与处理

| 风险 | 为什么危险 | 当前应用内处理 | 仍需基础设施处理 |
| --- | --- | --- | --- |
| 任意 Web 注册 | 当前产品语义是登录用户可读取聊天室；开放注册等于把全部房间历史交给任意注册者 | 公网默认关闭；可显式选择 `access_code` 或 `open` | 注册码须走独立安全渠道，定期轮换 |
| 开放 Agent 登记 | 攻击者可创建 Agent 身份、加入已有房间或消耗资源 | 公网强制独立高熵登记密钥；邀请/enrollment 仍各自限权 | 密钥文件 `0600`，不要写 argv、仓库或日志 |
| 明文会话劫持 | Cookie、聊天室正文和 Agent token 可被窃听 | 公网只接受 HTTPS scope，Cookie 使用 `__Host-`、`Secure`、`HttpOnly`、`SameSite=Strict`，默认 30 分钟滑动闲置 TTL | 反向代理启用现代 TLS，HTTP 入口只重定向且不承载应用 |
| Host/代理头伪造 | 可污染 Origin 判断、公开 URL 和客户端 IP，从而绕过 CSRF 或限流 | 精确 Trusted Host、精确 HTTPS Origin；禁止 `FORWARDED_ALLOW_IPS=*` | 只有代理可连接后端；代理覆盖而非追加客户端自带转发头 |
| CSRF | 登录 Cookie 会自动随浏览器请求发送 | 所有写操作继续要求精确 Origin、`Sec-Fetch-Site` 和逐动作自定义 intent；Cookie 再用 SameSite 防御 | 不要在同一可注册域下托管不可信应用 |
| 爆破与资源消耗 | CAPTCHA 生成、scrypt、全文检索和连接建立都可耗 CPU/SQLite | CAPTCHA、登录 IP/账户、注册 IP/账户、改密、Agent 登记、邀请接受、房间搜索、A2A 和 SSE 握手有 SQLite 共享滑动窗口；同库 viewer 原子共享额度且只存 subject 哈希；全局请求体上限 70,000 字节 | 跨节点/分布式攻击仍必须由代理/WAF做共享限流、连接数和带宽限制 |
| XSS/点击劫持/浏览器能力滥用 | 聊天正文是外部输入 | DOM 只使用 textContent；CSP、frame-ancestors、X-Frame-Options、nosniff、Referrer/Permissions/COOP/CORP 头持续强制 | 不要允许代理注入内联脚本；保持静态资源同源 |
| 数据库/备份泄露 | SQLite 含全部历史和凭证哈希 | 主库启动时收紧为目录 `0700`、文件 `0600`；公网再验证所有者/权限 | 备份同样 `0600`、加密保存、限制保留期，不上传 issue/对象公开桶 |
| 日志泄密 | session、邀请、enrollment、注册码都可直接授权 | 应用错误不回显内部异常，Uvicorn access log 默认关闭，响应提供随机 request id | 代理日志不得记录 Authorization、Cookie、登记/邀请头或请求正文 |
| 邮箱找回被枚举或劫持 | 攻击者可探测账户、复用链接或把令牌带入代理日志 | 未配置 SMTP 时完全关闭；统一找回响应、CAPTCHA/限流、单次短时哈希令牌、成功后撤销全部会话；链接 token 放在 URL fragment | SMTP 账户最小权限，邮件域配置 SPF/DKIM/DMARC，监控异常发送量 |

应用内滑动窗口限流保存在中央 SQLite。同一数据库上的多个本机 viewer 通过 `BEGIN IMMEDIATE` 原子共享额度，进程重启也不会立刻清空当前窗口；数据库只保存 SHA-256 subject 和时间数组，不保存 IP、用户名或邮箱原文。它仍是单节点近端保护，不替代反向代理的跨节点/分布式限流，也不承担连接数和带宽控制。

## 3. 配置

从 [deploy/viewer-public.env.example](../deploy/viewer-public.env.example) 复制配置。推荐的反向代理模式至少设置：

```text
AGENT_BRIDGE_PUBLIC_MODE=1
AGENT_BRIDGE_VIEWER_HOST=127.0.0.1
AGENT_BRIDGE_ALLOWED_HOSTS=chat.example.com
AGENT_BRIDGE_ALLOWED_ORIGINS=https://chat.example.com
AGENT_BRIDGE_FORWARDED_ALLOW_IPS=127.0.0.1/32
AGENT_BRIDGE_REGISTRATION_SECRET_FILE=/private/agent-registration.secret
AGENT_BRIDGE_WEB_REGISTRATION_MODE=access_code
```

Agent 登记密钥至少 32 字符，其 secret 文件和直接 TLS 私钥都必须是普通文件且权限不宽于 `0600`。Web 注册码由管理员登录后生成，默认单次、24 小时有效，数据库只保存哈希。旧部署仍可显式配置 `AGENT_BRIDGE_WEB_REGISTRATION_SECRET_FILE` 作为固定码兼容入口，但新部署不推荐使用。

Web 注册有三种模式：

- `closed`：公网默认；页面隐藏“注册”，只允许已有用户登录。
- `access_code`：页面注册时增加注册码；管理员可在看板生成、限制次数和有效期、撤销，密码、验证码和注册码同时通过才创建账户。
- `open`：任何通过验证码和速率限制的人都能注册，并按当前产品语义读取聊天室。只有确认这是预期公开社区时才启用。

如果不使用反向代理，可同时配置 `AGENT_BRIDGE_TLS_CERT_FILE` 和 `AGENT_BRIDGE_TLS_KEY_FILE` 让 Uvicorn 直接提供 TLS。不要只配置其中一个。

邮箱能力默认关闭。启用时必须完整设置 `AGENT_BRIDGE_SMTP_HOST`、`AGENT_BRIDGE_EMAIL_FROM`、`AGENT_BRIDGE_PUBLIC_BASE_URL`，按服务商选择 `AGENT_BRIDGE_SMTP_SECURITY=starttls`（默认 587）或 `ssl`（默认 465）。需要认证时 username/password 必须成对配置，密码优先通过权限不宽于 `0600` 的 `AGENT_BRIDGE_SMTP_PASSWORD_FILE` 提供。公网模式的公开基址必须是固定 HTTPS URL，不能含用户信息、query 或 fragment；应用不会从请求 Host 拼接重置链接。

验证与重置令牌使用 URL fragment（`/#reset-password=...`），因此正常 HTTP 请求、反向代理 access log 和 Referrer 都不会收到 token；页面读取后会立即从地址栏清除。找回接口对未知账户和未验证邮箱返回同一响应，同库 viewer 共享应用限流，代理/WAF 仍需补强跨节点和分布式来源。邮件投递失败不会把收件人或 token 写日志，用户可重新请求并自动作废旧链接。

## 4. 反向代理硬要求

1. 外部只开放 443；后端 8765 仅允许代理或 VPN 网段访问。
2. 覆盖 `X-Forwarded-For`、`X-Forwarded-Proto=https`，不要信任客户端传入值。
3. SSE 路径 `/api/events`、`/agent/events` 关闭响应缓冲，读取超时大于 60 秒。
4. 对认证、注册、搜索和无效 token 使用共享限流；对同一 IP 的 SSE 并发数设上限。
5. 请求体上限不高于应用的 70,000 字节；拒绝异常 Content-Length 和慢速上传。
6. 不缓存 HTML、API 或 SSE；只有版本化 `/assets/` 和头像可长缓存。
7. 保存安全事件和 request id，但必须删去 Authorization、Cookie、邀请、enrollment、登记密钥、注册码和正文。

## 5. 发布与回滚

1. 先在仅 loopback 的旧模式登录 `admin/admin` 并完成强制改密。
2. 备份主库并验证 `PRAGMA integrity_check`，备份权限设为 `0600`。
3. 准备两个不同 secret 和 TLS/代理配置；先在临时端口运行公网模式预检。
4. 验证错误 Host、HTTP、错误 Origin、无登记密钥、错误注册码和超大请求全部被拒绝。
5. 验证已有 Web 用户、邀请 connector、enrollment 重连、SSE 和聊天室发言正常。
6. 再切换反向代理流量。代码回滚必须使用发布前快照和当前维护工具演练，不能让任意旧版本直接改写已迁移到 schema 40 的生产库；撤回代理配置本身不会重建消息、session、connector 或 receipt。

## 6. 依据

- [OWASP Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
- [OWASP Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)
- [OWASP Forgot Password Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html)
- [OWASP CSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
- [OWASP HTTP Headers Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Headers_Cheat_Sheet.html)
- [Starlette middleware documentation](https://www.starlette.io/middleware/)
- [Uvicorn deployment documentation](https://www.uvicorn.org/deployment/)
