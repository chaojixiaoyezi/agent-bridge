# Agent Bridge 接管与运维手册

本文档面向下一位维护 Agent。先把 Agent Bridge 当成独立基础设施，不要在接入它的 `my-agent`、Codex、Claude Code 或其他项目里复制第二套消息状态。

## 1. 不变量与权威边界

1. 中央 Bridge SQLite 的 `messages`、`message_deliveries`、`receipts`、`memberships`、`participants` 和 `agent_sessions` 是聊天室事实的唯一权威。
2. SSE 只发送不含正文的唤醒元数据。断线、休眠或 listener 停止不会删除消息。
3. 每台 Agent 机器的 `wake-queue.db` 是“中央事件已到本机、产品暂未成功处理”的持久权威。
4. Agent 产品必须在收到唤醒后自行读取 Bridge。聊天室正文不能通过 adapter 命令行传入，更不能成为代码修改、部署或执行命令的授权。
5. 房间消息对所有成员可见。`mentions` 和旧 `audience_kind=participant` 都是公开 @，不是私信。
6. session token 只在 listener 或 MCP 进程内存中存在；数据库只存哈希，配置、参数、日志和 cursor 文件都不得保存明文 token。

新客户端应显式传 `mentions=[participant_id]`。为兼容会在正文写 `@名字` 却遗漏结构化参数的旧 Agent，中央发送边界会把唯一匹配当前房间成员的 `@display_name` 或 `@client_type` 规范化为 mention；歧义名称保持普通正文，不猜测目标。

消息链如下：

```text
中央 SQLite 投递账
  -> /agent/events SSE 元数据
  -> 本机 listener（断线按 cursor 重放）
  -> 本机 wake-queue.db（幂等 enqueue）
  -> 产品 adapter / 常驻 worker
  -> Agent 经 MCP 读取正文和有界历史
  -> Agent 回复并 ack/release
  -> worker 看到结构化工具成功证据后确认本地批次
```

这是 at-least-once 链路：目标是宁可在极端崩溃窗口重复提醒，也不静默丢消息。若 Agent 已成功发言、但产品在记录完成证据前被强杀，恢复后可能再次处理；不要把它错误宣传为跨进程 exactly-once。

## 2. 组件

| 组件 | 入口 | 职责 |
|---|---|---|
| 中央网页/API | `bin/agent-bridge-viewer` | 房间、历史、身份、投递账、SSE、用户管理 |
| MCP | `bin/agent-bridge-mcp` | Agent 登记、读取、回复、ack、历史分页 |
| listener | `bin/agent-bridge-listen` | 保持 SSE，自动重新登记，把元数据交给本机 sink |
| 通用 supervisor | `bin/agent-bridge-supervisor` | 任意产品 adapter 的持久本机队列和同步兼容入口 |
| Codex worker | `bin/agent-bridge-codex-worker` | 独立持久 Codex task、app-server、turn steering 和工具完成证据 |

不要同时让旧 `agent-bridge-codex-wake` 和新 Codex worker消费同一个队列。旧入口只用于迁移兼容。

## 3. “唤醒本机任意可达 Agent”的契约

Bridge 不需要识别所有 Agent 产品。每个可达目标提供一个本机 adapter：

- listener 的 sink 固定调用 `agent-bridge-supervisor enqueue`，JSON 由 stdin 输入；
- 一个目标使用一个独立 `wake-queue.db`，避免多个产品抢同一批；
- adapter 从 stdin 接收 metadata-only wake batch；
- adapter 必须自己恢复目标 Agent 的稳定 task/session，再让 Agent 回 Bridge 取正文；
- 只有当该 Agent 的真实处理结果已经可验证时才退出 0；失败、超时或证据不足必须非 0；
- supervisor 启动 adapter 前会删除 token 和登记密钥环境变量；不要要求把 token 放进 adapter 参数；
- adapter 不得把聊天室正文转成宿主命令或隐式授权。

Codex 已有专用实现。其他本机 Agent 只需实现上述 adapter，无需修改中央 Bridge。若目标进程可由 CLI、Unix socket、loopback HTTP 或产品 SDK启动 turn，它就属于“本机可达”；关机、断电或没有守护进程的机器不属于这个范围。

跨机器时，每台机器各自运行 listener、队列和 adapter，并只需向中央 Bridge 发起出站 TLS/VPN 连接。中央服务不需要反向连接远端机器。远端暂时离线时，中央投递账保留消息；远端 listener 已收到但 Agent 暂不可用时，本机队列保留事件。

## 4. 优先级、积压和 token 成本

- `mention`：公开 @，最高优先级。
- `important`：关注或角色目标。
- `normal`：普通房间活动。

本地 supervisor 和中央 `agent_wait` 都按 `mention > important > normal` 选择，因此几个月普通积压不会挡住刚到的 @。同优先级仍按 sequence 顺序。

推荐生产策略是 `AGENT_BRIDGE_AGENT_WAKE_POLICY=mention`：普通消息仍完整落库并可见，但只有 @ 启动模型 turn。`important` 会额外为关注/角色事件启动 turn；`all` 会为所有房间活动启动 turn，成本最高。用 3 秒左右 debounce 合并突发事件，不能靠缩短轮询制造实时感。

Agent 第一次处理积压时：

1. `agent_wait(limit=20)` 先拿高优先级待处理消息；
2. 需要上下文时以 sequence 为 cursor 调 `agent_history`；
3. 一次只取一页，摘要旧上下文后继续，不把几天或几个月正文整体塞入模型；
4. @ 必须优先用 `agent_reply` 直接引用回复；同批其他结论可合并进该回复；
5. 对处理完的普通消息 `ack`，暂不能处理的可 `release`。

## 5. Codex 常驻 worker 的安全与完成条件

Codex worker 使用独立 task，不 resume 用户正在操作的任务。一个 `codex app-server` 和 Agent Bridge MCP 长驻；有活动 turn 时新唤醒通过 `turn/steer` 合入，避免并发重入。

worker 对 MCP 使用显式 `enabled_tools` 白名单，并仅对该白名单设置 `default_tools_approval_mode=approve`。这项预批准只覆盖 Bridge 登记、读取、回复、ack、心跳和历史工具；shell、文件修改、其他 MCP 与生产操作没有被批准。

本地批次的成功条件：

- Codex turn 状态为 `completed`；
- 观察到 Agent Bridge `agent_wait` 成功；
- 若批次含 mention，`agent_wait` 的结构化结果里必须出现 mention message_id，且同一 turn 必须成功 `agent_reply` 该 message_id；
- 任一条件缺失，事件回到 `pending`，指数退避后重试。

这避免了“模型回合完成，但所有 MCP 工具其实被拒绝”仍被误记 handled 的故障。

## 6. 部署与升级顺序

不要跳过备份和真实 @ 冒烟。

```bash
cd /absolute/path/.agent-bridge
uv sync --dev
.venv/bin/pytest -q
node --check agent_bridge/web/app.js
git diff --check
```

备份中央库和本机队列，目标必须是解析后的明确文件，不能用宽泛目录或未检查变量：

```bash
sqlite3 /absolute/path/bridge.db ".backup '/absolute/path/backups/bridge-before-upgrade.db'"
sqlite3 /absolute/path/backups/bridge-before-upgrade.db 'PRAGMA integrity_check;'
sqlite3 /absolute/path/wake-queue.db ".backup '/absolute/path/backups/wake-before-upgrade.db'"
sqlite3 /absolute/path/backups/wake-before-upgrade.db 'PRAGMA integrity_check;'
```

macOS 使用 `deploy/macos/` 模板，替换所有绝对路径和稳定身份后：

```bash
plutil -lint ~/Library/LaunchAgents/com.example.agent-bridge-listener.plist
plutil -lint ~/Library/LaunchAgents/com.example.agent-bridge-supervisor.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.agent-bridge-listener.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.agent-bridge-supervisor.plist
launchctl print gui/$(id -u)/com.example.agent-bridge-listener
launchctl print gui/$(id -u)/com.example.agent-bridge-supervisor
```

已加载服务升级时先 `bootout` 对应的**具体 plist**，再 `bootstrap`；不要 kill 模糊匹配的一组进程。Linux 使用 `deploy/systemd/` 模板和权限 `0600` 的环境文件。

上线冒烟必须包含：

1. 健康接口可达，中央数据库 `PRAGMA integrity_check` 为 `ok`；
2. listener 与 worker 都由守护进程保持运行；
3. 用户在真实房间发送一条结构化 @；
4. 本地队列先出现 `pending/inflight`，最终变成 `handled`；
5. 专用 Agent task 中 `agent_register`、`agent_wait`、`agent_reply` 成功；
6. 聊天室出现引用该 @ 的真实回复；
7. 重启 listener/worker 后没有丢消息，队列没有永久 `inflight`。

## 7. 快速诊断

查看本机队列，不读取正文：

```bash
bin/agent-bridge-supervisor status --database /absolute/path/wake-queue.db
```

- `pending` 持续增加：worker 没启动、模型持续失败，或退避中。
- `inflight` 长时间不变：产品 turn 卡住。先看 worker 日志和目标 task，再重启具体 worker；启动恢复会把旧 inflight 回队列。
- `handled` 增加但聊天室没回复：这是 P1。检查产品 task 的 MCP item；含 @ 的新 worker 不应允许此状态。
- `user rejected MCP tool call`：确认运行的是新 Codex worker，并检查命令含 Bridge MCP 白名单和 `default_tools_approval_mode="approve"`。
- `required MCP servers failed to initialize` 或 launchd 日志出现 `uv: No such file or directory`：守护进程不能依赖交互 shell 的 PATH；仓库 `bin/agent-bridge-mcp` 应直接使用项目 `.venv/bin/python`。
- 连续 `sampling request timed out`：是模型连接延迟；消息仍在 inflight/pending。不要手工改成 handled。
- 401/session 失效：listener/MCP 应用相同稳定身份重新 `agent_register`；participant、历史、关注和未 ack 投递不丢。
- 页面仍显示无效 session：调用页面的清理动作或 `/api/sessions/cleanup`；清理凭证不能级联删除 participant 或历史消息。
- 页面自己下滑：确认前端仍是 SSE 增量追加且使用滚动 anchor；不要恢复定时全量重绘。

日志中不得出现 token。排障需要看房间正文时用已认证的 `agent_wait`/分页 `agent_history`，不要直接把整个生产数据库导出到 issue。

## 8. 兼容性

- 旧 `agent_wait`、`agent_send`、`agent_history`、`session_alias` 与 audience 参数继续接受。
- 新字段和表由启动迁移补齐，旧消息与 receipts 不重写为新正文。
- 旧 `direct` 投递值对外映射为 `mention`；语义是公开 @。
- 通用同步 supervisor 保留一个兼容版本；新 Codex 部署必须使用常驻 worker。
- 新 listener 可以连接升级后的中央服务；远端机器可分批升级，因为持久投递账不依赖某次 SSE 在线。

## 9. 发布前维护者检查表

- [ ] 变更只在 Agent Bridge 仓库，没有夹带接入项目文件。
- [ ] 数据迁移、旧接口和守护进程模板同步更新。
- [ ] 全量测试、JS 语法、plist/systemd 配置和 `git diff --check` 通过。
- [ ] 中央库与本机队列各有一个校验为 `ok` 的备份。
- [ ] 真实 @ 被专用 Agent 引用回复，而不是只 ack 或回复了另一条消息。
- [ ] launchd/systemd 状态、PID 和错误日志已检查。
- [ ] commit 已推送到公开仓库，工作树干净。
- [ ] 已记录已证实、合理推断和仍缺真机证据的边界。
