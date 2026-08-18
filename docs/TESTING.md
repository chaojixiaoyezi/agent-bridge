# Agent Bridge 测试分层

这套测试把“代码逻辑正确”“浏览器可用”“真实 Agent 能收到并回复”和“线上可平滑升级”分开证明。任何测试失败都应保留原始队列、数据库或日志证据，不能用模型文字替代状态核对。

## 1. 默认离线测试

```bash
uv sync --locked --dev
uv run pytest
uv run ruff check .
uv run python -m compileall -q agent_bridge tests
node --check agent_bridge/web/app.js
for entrypoint in bin/*; do sh -n "$entrypoint"; done
git diff --check
```

默认 `pytest` 使用临时 SQLite，覆盖 schema 迁移、身份与房间隔离、消息和投递账、SSE 重放、MCP stdio、listener/supervisor 重试、Codex/Claude/native TUI 契约、Web 权限、安全边界、快照恢复和 viewer-only 发布。`browser` 与 `live_codex` 测试默认只标记跳过，不会连接用户浏览器或调用真实模型。

## 2. 真实浏览器回归

```bash
AGENT_BRIDGE_RUN_BROWSER_TESTS=1 \
AGENT_BRIDGE_BROWSER_CHANNEL=chrome \
uv run pytest tests/test_browser_e2e.py -s
```

它启动随机 loopback 端口和临时数据库，验证登录、首次改密、聊天优先布局、房间切换、滚动锚点、侧栏、主题、窄屏和资源/耗时门禁。测试不读取生产 Cookie、房间或 `bridge.db`。

## 3. 真实 Codex 隔离冒烟

前置条件：`/opt/homebrew/bin/codex` 可用，并且 `codex login status` 显示已登录。

```bash
AGENT_BRIDGE_RUN_LIVE_CODEX_TESTS=1 \
uv run pytest tests/test_live_codex_e2e.py -s
```

测试会真实启动两轮 `agent-bridge-listen`、本机 supervisor 队列、`agent-bridge-codex-worker --once`、Codex app-server 和 Agent Bridge MCP。所有 Bridge 状态都在 pytest 临时目录中，服务仅监听随机 loopback 端口；不会打开生产数据库、加入生产群或复用生产 connector。

验收条件同时包括：

- 第一次正文末尾的精确 `@` 被服务端解析为唯一结构化 participant；
- listener 停止并以同一稳定身份重连后，第二次正文中间的 `@` 仍能唤醒；
- 两轮都观察到真实 `agent_wait` 和对原 message id 的唯一 `agent_reply`；
- 原投递进入 `acked`，本机事件进入 `handled`，没有 `pending`、`inflight` 或 `deferred`；
- 重连前后 participant 只有一个，Codex thread id 不变；
- 控制聊天室没有成员、消息或投递泄漏；
- 测试创建的 Codex task 被归档，临时登记密钥无论成功失败都会删除。

真实模型和上游网络耗时不稳定，因此这项测试不放进普通 CI，也不设置苛刻延迟门禁。超时或协议失败会终止整个测试进程组并保留可定位的 stdout/stderr 尾部；不能把一次快速回复当作稳定性证明。

## 4. 发布合同

CI 的 `Migration, security, and release contracts` 单独核对旧库就地迁移、消息行数、公开模式 fail-closed、私有房间 ACL、凭证轮换、运行主租约和快照恢复。发布前还应运行：

```bash
bin/agent-bridge-maintain --database "$PWD/bridge.db" \
  verify --manifest /path/to/snapshot/manifest.json
bin/agent-bridge-maintain --database "$PWD/bridge.db" \
  rehearse-restore --manifest /path/to/snapshot/manifest.json
```

正式滚动发布使用 `release-viewer`：先做 SQLite online backup 和临时副本恢复演练，再只重启 viewer，并比较全部 Agent launchd PID。中央数据库仍有写入者时禁止把备份直接覆盖回线上路径。

## 5. 删除旧代码的证据标准

不能仅凭静态工具未发现调用就删除运行入口。至少同时核对仓库引用、当前 launchd/systemd 配置、活跃进程、connector 私有配置和升级文档；数据库 migration、旧 schema 读取和仍被部署使用的产品兼容入口不算死代码。静态检查可用 `vulture --min-confidence 80` 辅助，但它不是部署证据。
