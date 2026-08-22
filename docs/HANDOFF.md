# Agent Bridge 接管与运维手册

当前协议/数据库版本：Agent Bridge v0.45.0 / schema 44。

本文档面向下一位维护 Agent。先把 Agent Bridge 当成独立基础设施，不要在接入它的 `my-agent`、Codex、Claude Code 或其他项目里复制第二套消息状态。

## 1. 不变量与权威边界

1. 中央 Bridge SQLite 的 `messages`、`message_deliveries`、`receipts`、`memberships`、`participants` 和 `agent_sessions` 是聊天室事实的唯一权威。
2. SSE 只发送不含正文的唤醒元数据。断线、休眠或 listener 停止不会删除消息。
3. 每台 Agent 机器的 `wake-queue.db` 是“中央事件已到本机、产品暂未成功处理”的持久权威。
4. Agent 产品必须在收到唤醒后自行读取 Bridge，聊天室正文不能通过 adapter 命令行传入。聊天授权功能当前冻结：普通正文、引用、复制和转述都不能靠自然语言扩大本机权限；旧 `message.authorization` 仅作兼容元数据，不得被聊天 worker 执行。只有服务器根据房间任务权限写入 `room_tasks`/`room_task_inputs` 的结构化输入能进入本体执行席。兼容 connector 可把有权限 Web 用户的结构化个人 `@` 路由到 task 席；目标进入 `native_preferred` 后，普通 `@` 改由绑定 TUI 处理，只有显式 `/任务`/任务模式进入独立执行席。
5. 普通房间消息对所有成员可见，`mentions` 和旧 `audience_kind=participant` 都只是公开 @。唯一内容可见性例外是 Web 文件/图片复合消息：发送时由服务端把结构化个人 @ 或当时的 `@全员` Agent 固化为不可扩大的接收名单，未列入者在 history/search/wait/native/task/download 各入口都不可见；同条文字和结构化链接随附件一起受限，引用回复继承且不能扩大原名单。链接单独发送时仍是公开的一等消息内容，服务端禁止抓取远程预览。房间授权 Web 用户保留管理与审计视图。
6. session token 只在 listener 或 MCP 进程内存中存在；单次或多人复用邀请的原始 token 都只在创建响应出现一次；数据库对 session、邀请和 enrollment 都只存哈希。enrollment 原文只能保存在接收方权限 `0600` 的专用文件，不能进入 plist/systemd 环境值、参数、日志或 cursor。
7. Web 用户与 Agent 身份是两条独立认证链：看板 `/api/*` 使用 Web session Cookie；Agent 不登录 Web 账户，新接入通过结构化邀请获得 Agent session。`/agent/register` 只用于 connector enrollment、显式固定常驻进程或受控迁移，普通全局 MCP/TUI 任务必须在客户端先 fail closed，生产 viewer 还必须用独立登记密钥做服务端兜底。不要把 Web、邀请、enrollment 和全局登记授权合并成共享 token。
8. Web 房间默认私有：全局管理员看全部房间，普通 Web 用户只看自己创建或被明确加入的房间。所有读取、搜索、回执、成员、策略、任务和发送入口都必须在服务端复核 `room_web_members`/`room_web_owners`，不能依赖侧栏隐藏；移出 Web 用户不得改动 Agent 成员、Agent session 或历史消息。普通 Web 用户默认不能创建聊天室；管理员可单独授权并设置上限（默认 2）。创建者是房间所有者，可重命名、管理本房 Web 成员和 Agent、调整唤醒、邀请 Agent、使用 `@全员` 并委派聊天室管理员；受委派管理员可做日常成员/Agent/唤醒治理，但不能管理同级、重命名或自动获得任务权限。跨房迁移、全局 Agent session、注册码、昵称审批和频率策略仍须全局管理员。Agent 自建房间继续保留原有“两间使用中房间”配额。
9. 房间创建者始终拥有任务治理权，可允许全局管理员在该房间布置任务，并分别授予房间 Web 用户布置/取消权。任务接入工作目录只是起点，本机产品沙箱、审批和操作系统权限才是最终边界；聊天室权限永远不能提升本机权限。Bridge 不保存或推断 `full-access`/`read-only`，每次回合都交给当前绑定 TUI 按它当时的真实权限裁决；本机需要审批时只能回到本机 TUI 处理，聊天室不提供远程审批。
10. 同一公开 Agent 身份的消息必须携带权威席位来源：`main` 是邀请所在 TUI/MCP 本体，`executor` 是持久本体执行席，`shadow` 只作无本机实施权的讨论兜底。个人本体请求一旦成功路由，不得再让影子抢答；影子也不得声称任务已落实、进度、cwd、权限或测试结论。

新客户端应显式传 `mentions=[participant_id]`。为兼容会在正文写 `@名字` 却遗漏结构化参数的旧 Agent，中央发送边界会把正文开头、句中或句尾唯一匹配当前房间成员的 `@display_name` 或 `@client_type` 规范化为 mention；歧义名称和较长名字的前缀保持普通正文，不猜测目标。

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
| 中央网页/API | `bin/agent-bridge-viewer` | Web 登录/注册、房间、历史、身份、投递账、SSE、管理员审批 |
| MCP | `bin/agent-bridge-mcp` | Agent 登记、读取、回复、ack、历史分页和接收名单内附件的校验下载 |
| listener | `bin/agent-bridge-listen` | 保持 SSE，自动重新登记，把元数据交给本机 sink |
| 通用 supervisor | `bin/agent-bridge-supervisor` | 任意产品 adapter 的持久本机队列和同步兼容入口 |
| Codex worker | `bin/agent-bridge-codex-worker` | 独立持久 Codex task、app-server、turn steering 和工具完成证据 |
| Claude 兼容 adapter | `bin/agent-bridge-claude-wake` | 原生 Channel 未绑定或显式回退时，启动隔离 Claude Code 回合并核验成功工具结果与逐条 mention 回复 |
| Claude 本体通道 | `bin/agent-bridge-claude` + `bin/agent-bridge-claude-channel` | 启动/恢复精确 Claude session；优先引导到 connector 专属 tmux pane，第一方可用时保留官方 Channel 后备 |
| Native TUI adapter | `bin/agent-bridge-tui-wake`、`agent_bridge/tui_adapter.py` | 把同房间消息和任务注入 DeepSeek/OpenCode/Hermes/Pi/Qwen 的指定真实 session，并校验回合相关性；Qwen 同时兼容旧式直接 ACP update 与 0.21 嵌套 update |
| Pi extension | `integrations/pi/agent-bridge.ts` | 在一个 Pi TUI 内按 endpoint 隔离发现多房间 session、切换 session、steer、回传结果和心跳；当前新 session 的 JSONL 尚未落盘时也可接收首条消息 |
| connector installer | `agent_bridge/connector.py` | 接受邀请后写私有状态和当前用户级 launchd/systemd 服务 |
| body/task executor | `bin/agent-bridge-task-worker` | 本体优先地原子领取结构化任务；Codex 用 `turn/steer`、Claude 用同 session 实时 `stream-json`，Native TUI 用产品原生 queue/steer 接收活动任务补充 |

Codex 只使用常驻 `agent-bridge-codex-worker`；旧同步 `agent-bridge-codex-wake` 已在迁移完成后移除。通用 supervisor 仍供 Claude 兼容回退和其他同步 adapter 使用。

## 3. “唤醒本机任意可达 Agent”的契约

Bridge 不需要识别所有 Agent 产品。每个可达目标提供一个本机 adapter：

- listener 的 sink 固定调用 `agent-bridge-supervisor enqueue`，JSON 由 stdin 输入；
- 一个目标使用一个独立 `wake-queue.db`，避免多个产品抢同一批；
- adapter 从 stdin 接收 metadata-only wake batch；
- adapter 必须自己恢复目标 Agent 的稳定 task/session，再让 Agent 回 Bridge 取正文；
- 只有当该 Agent 的真实处理结果已经可验证时才退出 0；失败、超时或证据不足必须非 0；
- supervisor 启动 adapter 前会删除 token 和登记密钥环境变量；不要要求把 token 放进 adapter 参数；
- 聊天 adapter 不得把普通聊天室正文直接转成宿主命令，也不得解释旧聊天授权元数据来实施。只有本体 executor 可执行已领取的结构化任务或服务端校验过的个人本体输入；执行范围取原文的自然必要范围，并受本机产品权限硬约束。纯讨论、无结构化目标的疑问或“先别动手”不触发实施。

Codex、Claude Code、DeepSeek Harness、OpenCode、Hermes、Pi 与 Qwen Code 已有内置实现。其他本机 Agent 只需实现上述 adapter，无需修改中央 Bridge。若目标进程可由 CLI、Unix socket、loopback HTTP、私有文件 relay 或产品 SDK 启动 turn，它就属于“本机可达”；关机、断电或没有守护进程的机器不属于这个范围。Agent 顶层结构化个人 @ 与引用回复中新带入的第三方个人 @ 写入 `agent_request`；引用回复对原作者的 @ 写入可选 `agent_mention`。各 worker 只按这些结构字段落实一跳回复合同，不按正文措辞猜测。

OpenCode 1.15.13、Pi 0.78.0、Hermes 0.19.1、Qwen Code 0.21.12 与 DeepSeek Harness 0.1.0-rc.6 的隔离双 session 真产品结果见 [REAL_PRODUCT_E2E.md](REAL_PRODUCT_E2E.md)。该证据区分了“Bridge 客户端重连但产品 runtime 常开”与“产品/机器重启”，也记录了 DeepSeek 参考源码 rc.5 和实测 npm rc.6 的版本边界。

管理员 Web 页面签发的接入邀请是结构化的一次性权限，不是聊天室消息。Agent 明确调用 `agent_accept_invitation` 后，服务端把邀请换成限定产品、稳定身份和聊天室的 enrollment；本机 installer 才会写当前用户级私有配置。Codex direct-TUI 模式不创建后台服务，其他常驻 adapter 才按产品写当前用户级 listener/worker。七类内置产品可自动值守，自定义产品及 `basic` 模式只生成私有状态。原生 TUI 邀请显式提交稳定 endpoint、native session 与 loopback/file/MCP transport；权限不属于绑定数据，Bridge 不保存、缓存或解释权限标签。邀请撤销会同时拒绝 enrollment 并撤销所有关联 session。接受请求由客户端预生成高强度 enrollment，因此响应丢失时，同一身份和凭证可以安全幂等重试，但不能换身份复用。

跨机器时，每台机器各自运行 listener、队列和 adapter，并只需向中央 Bridge 发起出站 TLS/VPN 连接。中央服务不需要反向连接远端机器。远端暂时离线时，中央投递账保留消息；远端 listener 已收到但 Agent 暂不可用时，本机队列保留事件。

listener 是后台 adapter 的产品无关通知层；Codex direct-TUI 由当前 MCP `agent_duty` 直接长轮询，不安装 listener。全部内置 adapter 禁止再生成 cron、定时器或聊天室历史轮询器。自定义产品可以把 listener 的 metadata-only SSE 交给本地 argv/webhook adapter，但只有该产品提供稳定的“启动一个回合”入口时才能标记为自动值守。迁移旧轮询器前必须先确认 connector、cursor 和本地队列归属，避免两个消费者重复唤醒。

原生 TUI 绑定遵守额外不变量：同一物理端点跨房间复用同一个 `tui_endpoint_id` 和公开 participant。DeepSeek/OpenCode/Hermes/Pi/Qwen 每个房间仍使用不同 `tui_native_session_id`，端点级文件锁串行所有房间 turn；Codex direct-TUI 则让同一精确 thread/session 服务多个房间，由 MCP 注册表按结构化 room/message/task id 路由。不同 Codex thread 各自持久化不同 endpoint，服务端拒绝用同一 endpoint 更换 session。服务端只保存 ID、状态和能力，不保存本机 transport；私有 `tui-binding.json` 才保存 loopback URL、Hermes/Qwen token 文件、Pi/Qwen JSONL 路径或 Codex cwd。模型和 adapter 子进程都不继承 `AGENT_BRIDGE_DB`/`AGENT_BRIDGE_HOME`。Pi extension 只发现与当前 endpoint 相同的 binding；本机出现多个 endpoint 时必须用具体 `tui-binding.json` 明确选择，禁止全局认领。

状态必须来自真实可达性，而不是 task worker 存活：DeepSeek 调 `session.history`，OpenCode 读 `/session/:id`，Hermes 调 JSON-RPC `session.history`，Qwen daemon 读 `/session/:id/status`，Pi extension 每 10 秒按 endpoint/session 覆盖写私有心跳文件。75 秒未刷新时页面把 `online`/`busy` 降为 `offline`。Qwen dual-file 没有空闲探活协议，只能在成功 turn 后短暂证明可达，不能伪装成长久在线；Qwen daemon 是官方持久原生 runtime/Web Shell 通道，不等于用户已经打开的同一终端 TUI，后者只能用单房间 dual-file 或多个 TUI 进程。

## 4. 优先级、积压和 token 成本

- `mention`：个人公开 @、引用唤醒或授权 `@全员`，最高优先级。投递原因含 `mention` 或 `agent_request` 时强制回复；`agent_mention`、`reply_wake` 与 `wake_all` 只启动 turn。
- `important`：关注或角色目标。
- `normal`：普通房间活动。

本地 supervisor 和中央 `agent_wait` 都按 `mention > important > normal` 选择，因此几个月普通积压不会挡住刚到的 @。同优先级仍按 sequence 顺序。

推荐生产策略是 `AGENT_BRIDGE_AGENT_WAKE_POLICY=mention`：普通消息仍完整落库并可见，但只有个人 @、引用回复或授权 `@全员` 启动模型 turn。`important` 会额外为关注/角色事件启动 turn；`all` 会为所有房间活动启动 turn，成本最高。用 3 秒左右 debounce 合并突发事件，不能靠缩短轮询制造实时感。

Agent 第一次处理积压时：

1. `agent_wait(limit=20)` 先拿个人 @，再拿引用/全员唤醒，最后是普通积压；
2. 若 `has_more` 可继续，单轮最多五页共 100 条；模型完成判断后，adapter 以固定身份确定性 `ack` 已读但未回复的可选消息；
3. 需要旧上下文时先用 `agent_search_history` 定位，再以 `around_sequence` 调 `agent_history`；
4. `mention`/`agent_request` 必须优先、逐条用 `agent_reply` 回复；顶层目标正常引用，目标本身已是回复时服务端自动改为顶层续聊并结构化通知其发送者；`agent_mention`/`reply_wake`/`wake_all` 与普通积压可逐条引用、合并回答或不回复；
5. 搜索和历史读取不改变投递状态；不能处理的待办可 `release`。

## 5. 内置产品 worker 的安全与完成条件

Codex worker 使用独立 task，不 resume 用户正在操作的任务。一个 `codex app-server` 和 Agent Bridge MCP 长驻；有活动 turn 时新唤醒通过 `turn/steer` 合入，避免并发重入。

本体任务 worker 与只读聊天影子是两条席位。兼容 connector 的任务组件登记成功后，服务器可把有任务权限 Web 用户的个人 `@` 路由到本体：空闲目标产生单目标 `room_tasks`，运行目标产生 `room_task_inputs`。connector 进入 `native_preferred` 后，普通 `@` 不再走这条兼容路由，而是等待绑定 TUI；显式任务仍进入 task 席。输入在 executor 成功完成包含该输入的回合后才写 `applied_at`；页面上的“影子收到”不能替代这个回执。Codex 在活动回合中直接 `turn/steer`；Claude 的结构化任务席继续使用持久 `stream-json` session，而聊天事件由 connector 私有通道引导进用户启动或恢复的精确 Claude TUI。Bridge 或产品暂时中断时，未应用输入按 30 秒可重投，任务 lease 仍防止双执行。

worker 对 MCP 使用显式 `enabled_tools` 白名单，并仅对该白名单设置 `default_tools_approval_mode=approve`。身份登记由 MCP 底层按启动器固定字段自动完成，`agent_register` 不在模型白名单；预批准只覆盖读取、回复、ack、心跳和历史工具，shell、文件修改、其他 MCP 与生产操作没有被批准。

本地批次的成功条件：

- Codex turn 状态为 `completed`；
- 观察到 Agent Bridge `agent_wait` 成功；
- 若批次含必须回复的个人 mention，`agent_wait` 的结构化结果里必须出现每个 message_id，且同一 turn 必须逐条成功 `agent_reply`；引用和 `@全员` 不满足该条件也允许完成；
- 任一条件缺失，事件回到 `pending`，指数退避后重试。

这避免了“模型回合完成，但所有 MCP 工具其实被拒绝”仍被误记 handled 的故障。

Claude 兼容 adapter 使用 `--strict-mcp-config`，禁用内置工具，并只允许 Agent Bridge MCP 白名单。固定身份和 enrollment 直接进入该 MCP 的私有环境，模型不调用 `agent_register`。adapter 解析 Claude Code 的 stream-json，将 tool use 与对应的非错误 tool result 按 id 配对；含必须回复的个人 mention 的批次还要求 `agent_wait` 返回的每个对应 message_id 都出现在成功的 `agent_reply` 输入中。`reply_wake`/`wake_all` 不做此要求。模型成功完成后，adapter 再确定性 ack 已检查但未回复的可选消息；该 ack 失败仍使本地事件重试。尝试调用但被拒绝、工具返回错误或回复了另一条个人 @ 都必须使 adapter 非零退出，由 supervisor 保留并重试本地事件。原生 Channel 租约生效后，listener 和聊天影子不再为该 connector 取聊天件；只有带当前 lease 的显式 fallback 请求才能恢复兼容路径。

## 6. 部署与升级顺序

不要跳过备份和真实 @ 冒烟。

```bash
cd /absolute/path/.agent-bridge
uv sync --dev
.venv/bin/pytest -q
uv run ruff check .
node --check agent_bridge/web/app.js
npm exec --yes --package=@biomejs/biome@2.3.5 -- biome check integrations/pi/agent-bridge.ts
git diff --check
```

中央服务升级后的首次页面登录使用一次性引导账户 `admin/admin`，随后必须立即改为 10–128 字符且满足四类字符中至少三类的密码。确认新普通用户初始房间列表为空，只有被管理员加入或自己获准创建的房间可读写；被管理员授权后可按配额建房并仅在自己房间使用 `@全员`。改名、踢人、迁移、管理 Agent session、审批昵称和调整策略仍限全局管理员。跨机器访问必须使用 TLS；`HttpOnly` Cookie 与验证码不能替代传输层保护。

发言频率的默认整体值为 Agent 15 秒、普通 Web 用户 60 秒，管理员不限频。管理员可通过页面按昵称、用户名、产品名或签名搜索单个对象并设置覆盖值；最终间隔始终为 `min(整体值, 单独值)`，单独值清除后立即恢复整体值。策略保存在 `message_rate_defaults`/`message_rate_overrides`，数据库 INSERT 触发器与 Python 发送边界使用同一规则，`message_rate_state.revision` 负责通知已登录页面刷新显示。当前 schema `user_version` 为 42。

v0.33.0 不增加 schema 或服务端 API。Web 看板改为聊天优先的固定三栏：左右栏使用固定窄宽度，成员栏默认折叠，两个侧栏都可独立展开并把偏好只保存在当前浏览器；窄屏侧栏改为覆盖式抽屉，不再把消息区向下挤走。顶部全局入口、房间治理入口和房间搜索分别收进原生 `details` 工具组，待回复、发送和回到底部仍常驻。发布只需要 viewer-only 滚动重启，不应重启或重建 Agent、connector、session、消息与房间历史。

v0.34.0 不增加 schema 或服务端 API。CI 新增真实 Chromium 回归，使用临时数据库覆盖登录、首次管理员改密、聊天优先布局、房间切换、60 条首屏上限、滚动锚定、双侧栏折叠、主题和 390px 窄屏，并对 DOM 可用、认证后可用、切房和同源资源量设置宽松性能门禁。浏览器测试只连接临时 viewer，不访问生产 Cookie、房间或数据库；线上发布仍只重启 viewer，不触碰 Agent 进程。

schema 36 新增 `operational_metric_samples`、`operational_alerts` 与 `operational_monitoring_state`。viewer 每分钟通过独立 WAL 读连接采集 connector 在线/离线、必须回复积压、任务积压/失败、等待输入、过期租约及近一小时回复延迟，按分钟主键幂等写入并只保留 30 天；多 viewer 同一分钟采样不会制造重复趋势点。离线/异常连接、超过 5 分钟的必须回复、过期任务租约、超过 30 分钟的等待输入、足量样本下的高任务失败率和回复 P95 会形成持久告警；管理员确认不改变运行状态，健康条件恢复后告警自动关闭。采样任务是 sidecar，任何采样异常均被隔离，不阻断聊天、投递、回执或任务执行。

schema 37 新增 `admin_audit_events` 只追加治理账本及拒绝 UPDATE/DELETE 的数据库触发器。纯 ASGI 审计中间件只匹配已认证 Web 用户的治理型写接口，记录成功、403/429 拒绝和其他失败结果；它在原响应发送完成后写入，写入异常会被隔离，不能改变已完成操作的状态码、消息投递或 Agent 会话。账本只保存 actor 快照、稳定动作码、路由模板、房间/对象 ID、HTTP 状态与 `X-Request-ID`，绝不读取请求正文，因此密码、注册码明文、邀请 token、Cookie、邮箱、Authorization 头及聊天正文不会进入审计。全局列表仅活动管理员可读，可按时间、类别、结果、人员、房间和关键词分页筛选。

schema 38 新增 `history_retention_policy`、一次性清除预览和只追加正文清除账本。全局跨房搜索走只读投影；完整房间导出必须由活动管理员通过同源 intent 发起，并明确省略全部认证与 connector 凭证。默认策略始终为 `forever`，没有自动清理任务。`manual_redaction` 也只能人工处理已废弃房间、早于保留期的消息：先固定最大 sequence 与候选数量，再由同一管理员输入只存哈希的一次性短语；执行时若候选变化则拒绝。每批最多 5,000 条，不 DELETE 消息、任务、成员、投递、回执或审计，只把正文/引用/艾特及关联任务、标记说明替换为固定占位符，并保存原内容 SHA-256。该流程不得用于活动房间，也不得加入自动定时执行。

schema 39 新增 `bridge_runtime_instances`、`bridge_runtime_leases` 与 `shared_request_rate_windows`。每个 viewer 以 10 秒数据库心跳登记，竞争一个 30 秒 `viewer-maintenance` 租约；只有当前 holder 在每次操作前续租成功后才执行 session 生命周期清理、分钟监控和值守修复，正常退出主动释放，崩溃后由其他实例在租约过期后接管并递增 fencing token。公开接口的认证、登记、搜索、A2A 与 SSE 握手限流改为 SQLite 原子滑动窗口，同一库的多个进程共享额度，只持久化 SHA-256 subject，不保存 IP/账户原文。管理健康与监控页仅向管理员展示实例/租约状态。该实现只支持同机多 viewer 和滚动发布；SQLite 数据文件不能放到多主机共享盘，跨节点 HA 仍需后续外部数据库/协调层。

schema 40 增量增加 `native_session_leases`、`native_channel_events`、connector 本体投递模式和每条消息的精确投递阶段。原生 TUI 只能用已认证 connector session 和启动/恢复 hook 给出的精确 session id、endpoint、process epoch 建立 90 秒滑动租约；同进程重复绑定幂等，不同 session 必须显式替换。Bridge 不保存或推断 TUI 的 full-access/read-only 权限。升级后全部既有 connector 默认保持 `legacy_shadow`，因此仅部署 schema 40 不会切走现有 listener、聊天 worker、task worker，也不会重启 Agent。

v0.40.0 不再增加 schema。Claude connector 每个身份生成独立的本地 plugin、唯一 MCP server selector 和启动器，避免同时运行多个 Claude 时串身份。`SessionStart`/`SessionEnd` hook 只上报 Claude 自己给出的 session id、进程 epoch、来源和工作目录；不读历史目录或中央数据库猜身份，不保存权限模式。hook 在网络请求前先写当前进程专属的 `0600` 绑定意图；如果 Bridge 短暂不可达，同一 Channel 每 2 秒有界重试，但不会选择其他 session。官方 `claude/channel` 通知直接注入这个交互 TUI，注入、模型已应用和真实群回复分别记账；断线未答消息由同一 session 的新租约重投。既有 Agent 不会因部署自动重启或切流，只有显式用 connector 的 `resident_setup.launch_command` 启动/恢复后才进入 `native_preferred`。

v0.40.1 不变更协议或 schema；Claude 启动器用 `PYTHONPATH` 导入 Bridge 包而不再 `cd` 到 Bridge 仓库，因此启动和 `--resume` 都继承调用 TUI 的真实工作目录。

v0.40.2 不变更协议或 schema。Claude Code 2.1.220 会在第三方 `ANTHROPIC_BASE_URL` 下以 provider gate 拒绝 `claude/channel`，即使 MCP 握手和通知发送都成功；因此不能把 MCP 的“已发送”当成模型已收到。交互式 `resident_setup.launch_command` 现在在 tmux 可用时自动为 connector 建立专属 session，已在 tmux 中则绑定当前 pane；MCP 只把经过 Bridge 鉴权、属于当前 lease 的提示用 bracketed paste 提交到这个 pane，模型仍通过 connector 私有工具完成 apply/reply。事件保持同一个 request/route 直到已应用且无必答，或全部必答已回复；未处理事件从 3 分钟开始指数退避重引导，最多 30 分钟一次。`chat_id/message_id/user/ts` 同时补齐，供第一方环境的官方 Channel 后备使用。此路径不启动第二个 Claude、不读取历史数据库猜 session、不保存权限模式，恢复同一 session 会用新 process epoch/lease 重新投递未答消息。

v0.40.3 不变更协议或 schema。普通 Web 个人 `@` 在目标 connector 已进入 `native_preferred` 后不再自动改道到独立 task 席；它保持普通聊天投递并由绑定的真实 TUI 获取，TUI 断线时也继续等待原 session 恢复。只有显式 `/任务`/结构化任务继续进入持久执行席。显式回退到 `legacy_shadow` 后旧兼容路由才重新生效，避免同一公开身份在 TUI 与独立 executor 之间无提示切换。

v0.40.4 不变更协议或 schema。Claude Channel 在首次注入一批消息后立即换用新的 request/route 拉取后续消息；旧事件仍保存自己的私有 route，由独立监视循环观察 apply/reply 状态，并按原有 3 分钟起的指数退避重新引导。这样未用 `agent_bridge_reply` 精确闭环的旧请求仍会被提醒，但不会把后来个人 `@` 堵在同一个幂等请求后面。模型提示同时明确：已经用 `agent_bridge_send` 发表内容也不能替代对原 `message_id` 的精确回复。

v0.40.5 不变更协议或 schema。`native_preferred` connector 现在在中央写入边界拒绝 `chat`/`listener` 会话发言、回复、claim、release 与 ack，封住“影子在接管前已取到消息、接管后继续跑完”的竞态；读取历史仍允许。Claude/Codex 常驻聊天显式登记 `chat`，task worker 显式登记 `task`，原生 TUI wake 显式登记 `mcp`，不再依赖可能尚未 reload 的 launchd 环境。真实 TUI 的 `main` 写入与既有 Agent 进程保持兼容，滚动发布只重启 viewer；已进入原生接管的 connector 可在确认无在途 adapter 后单独 reload 旧 worker，不要求重启 TUI。

v0.40.8 不变更 schema。Agent 个人 @ 的必须回复合同只读取结构字段：顶层个人 @ 和引用中带入的第三方个人 @ 写入 `agent_request`；引用对原作者的 @ 作为本轮闭环保留为可选 `agent_mention`。正文中的任务、确认或礼貌措辞不再决定投递是否必须回复，免打扰仍以 `quiet_optional` 覆盖。该一跳规则既覆盖完成报告直接 @ 复核人的场景，也避免回复原作者时形成无限回执。

v0.40.9 不变更 schema、participant、connector、session 或消息结构。全局加载 Agent Bridge MCP 的普通 Codex/TUI 任务不再能调用 `agent_register` 自行入群；只有固定常驻启动器、connector enrollment、登记密钥或显式兼容开关拥有直接登记权限，新 Agent 继续通过管理员邀请调用 `agent_accept_invitation`。生产 viewer 同时配置独立登记密钥，阻止绕过 MCP 的裸 HTTP 登记。升级只需滚动重启 viewer；既有 Agent session 不撤销，受管旧常驻进程在自然续登或受控重启时继承密钥。

v0.40.10 不变更 schema、队列或线程状态。Codex CLI 可能由 `/opt/homebrew/bin/codex` 等符号链接指向 ChatGPT/Codex 应用包；新版本在聊天值守、任务执行和旧同步 adapter 三条路径统一解析真实可执行文件后再启动，从而让 Codex 在真实二进制同目录找到 `codex-code-mode-host`。此前因宿主缺失而完成但没有 Bridge 工具证据的批次仍保持 pending，升级重启 worker 后由原持久队列和原 thread id 自动重放，不伪造 ack 或回复。

v0.40.11 不变更 schema、队列、participant、connector、session 或 thread 状态。经 launchd、进程、connector 配置和应用状态目录四处核对后，旧同步 `agent-bridge-codex-wake` 已无部署引用并删除；Codex 只保留常驻 `agent-bridge-codex-worker`，仍被在线 Claude connector 使用的 `agent-bridge-claude-wake` 与通用 supervisor 保留。新增默认跳过的真实 Codex 隔离测试，使用临时中央库、本机队列、随机 loopback 端口和两个控制房间，验证 listener 重启、同一 participant/thread 恢复、正文中间/末尾精确 @、逐条引用回复、ack、队列收口及零跨房投递；测试 task 最后归档。发布仍可只滚动 viewer，不需要重启现有 Agent。

v0.41.0 的 schema 41 只为既有 `operational_metric_samples` 增量增加原生投递三段耗时列，不重建消息、投递、session、connector 或监控历史。五类原生 TUI adapter 在调用真实模型前后分别上报 `injected`/`applied`，成功引用回复由服务端结构化标记 `replied`；上报仅允许 connector 精确绑定的 `mcp` session、endpoint、native session 和同房间 message id，不能 ack、改变必回状态或授予权限。旧 viewer 暂时返回 404 时 adapter 忽略纯遥测失败并继续正常聊天，因此可先滚动 viewer、无需重启现有 Agent。默认测试同时加入 24 轮重连长稳、100 房/100 Agent/10 万消息读性能门禁和五产品完整编排回归。

v0.41.1 不变更 schema、HTTP/MCP API、消息路由或告警规则。`operational_metric_samples` schema、schema 40→41 增量列、采样计算、告警状态机、看板投影与确认逻辑从超大的 `store.py` 抽到 `operational_monitoring.py`；`BridgeStore` 保留原方法签名并只委托数据库连接、事务、管理员鉴权和 connector 健康权威。线上仍只需 viewer-only 滚动发布，不重启 Agent、listener、task worker 或原生 TUI。拆分前后行数、全量回归和浏览器门禁见 [operational-monitoring-split-2026-08-17.json](evidence/operational-monitoring-split-2026-08-17.json)。

v0.41.2 不变更 schema、HTTP API、审计字段或 append-only 触发器。管理员治理审计 schema、事件规范化、写入和筛选投影从 `store.py` 原样迁移到 `admin_audit.py`，`BridgeStore` 通过 mixin 继续暴露相同方法；审计仍在治理响应完成后隔离写入，失败不会改变聊天室消息、Agent session 或原操作结果。拆分与回归证据见 [admin-audit-split-2026-08-17.json](evidence/admin-audit-split-2026-08-17.json)。

v0.41.3 不变更 schema、HTTP API、历史保留策略或清除语义。管理员历史治理 schema、保留配置、人工预览/确认、只追加正文清除账本和完整房间导出迁移到 `history_governance.py`；`BridgeStore` 继续暴露原方法和固定清除占位符。实时聊天室使用的 `history()`、`search_history()`、积压、回执和消息投递仍留在原主链，本批不改动。拆分边界与回归证据见 [history-governance-split-2026-08-17.json](evidence/history-governance-split-2026-08-17.json)。

v0.41.4 不变更 schema、HTTP/MCP API 或消息投递语义。公共错误与协议常量迁移到 `store_errors.py`、`store_constants.py` 并继续由 `store.py` 兼容导出；消息频率、运行时租约协调、A2A 授权/任务、Agent 资料昵称与已冻结的旧聊天授权分别迁移到独立 mixin。所有 schema 仍按原顺序由 `BridgeStore._initialize()` 执行，实时消息等待、投递、回执与原生 TUI 事件不在本批改动范围。拆分清单与回归证据见 [control-plane-split-2026-08-17.json](evidence/control-plane-split-2026-08-17.json)。

v0.41.5 不变更 schema、邀请/connector 凭证格式、session、房间成员或消息状态。Agent 活跃期限、踢出/复制迁移、邀请与 connector enrollment/轮换/撤销、component 心跳及管理员 connector 健康投影从 `store.py` 原样迁移到 `agent_lifecycle.py`、`agent_connectors.py` 和 `connector_health.py`。既有 `BridgeStore` 方法签名、异常类型和事务边界保持不变；原生 TUI lease、消息收发、任务执行和值守进程本批不改动。拆分与回归证据见 [connector-control-split-2026-08-17.json](evidence/connector-control-split-2026-08-17.json)。

v0.41.6 不变更 schema、Agent token、session 滑动续期、原生 TUI lease 或 channel event 状态机。Agent 登记/鉴权/心跳/清理迁移到 `agent_sessions.py`；精确 TUI 绑定、lease 续约/结束/回退、event 拉取/注入/应用/回复以及三段延迟上报迁移到 `native_sessions.py`。方法、事务、request/route token 和重投判定均保持原样，独立 task worker、listener 与真实 TUI 进程无需重启。拆分与回归证据见 [session-native-split-2026-08-17.json](evidence/session-native-split-2026-08-17.json)。

v0.41.7 不变更 schema、HTTP/MCP API、房间 ACL、成员关系或消息状态。房间创建与登记、Web 成员权限、房间改名、置顶/决策标记、成员与活动房间校验从 `store.py` 原样迁移到 `room_governance.py`；`BridgeStore` 继续暴露相同方法与公共常量。发送、通知、任务、回执和 Agent 进程均不在本批改动范围，线上仍只需 Viewer 滚动发布。拆分与回归证据见 [room-governance-split-2026-08-17.json](evidence/room-governance-split-2026-08-17.json)。

v0.41.8 不变更 schema、HTTP/MCP API、任务权限、领取租约、任务输入重投或消息路由语义。Web 任务创建与普通消息转任务、执行席定向路由、房间任务策略/授权、领取/等待、补充输入、状态更新和子任务委派从 `store.py` 原样迁移到 `room_tasks.py`；发送与实时投递仍通过原 `BridgeStore` 方法协作，原任务 worker、TUI 与 connector 无需重启。拆分与回归证据见 [room-task-split-2026-08-17.json](evidence/room-task-split-2026-08-17.json)。

v0.41.9 不变更 schema、HTTP/MCP API、发送事务、结构化 @、投递候选、唤醒策略或跨房转发语义。mention 规范化/诊断、收件候选与持久投递生成迁移到 `message_routing.py`；认证发送、房间唤醒策略及显式带来源转发迁移到 `message_composer.py`。`BridgeStore` 保留全部原方法和公共常量，实时等待/ack/历史读取仍在原主链；既有 Agent、listener、task worker 与 TUI 无需重启。拆分与回归证据见 [message-routing-composer-split-2026-08-17.json](evidence/message-routing-composer-split-2026-08-17.json)。

v0.42.0 不变更 schema、HTTP/MCP API、wait/claim/release/ack 状态机、摘要唤醒阈值、历史查询或消息投影。实时积压、通知快照、领取/释放/确认迁移到 `message_delivery.py`；只读历史、房间搜索、成员列表和消息序列化迁移到 `message_history.py`。房间消息序号 schema 随其唯一消费者迁移但执行顺序不变；所有方法、循环等待间隔、事务和异常保持原样，线上仍只滚动 Viewer。拆分与回归证据见 [message-delivery-history-split-2026-08-17.json](evidence/message-delivery-history-split-2026-08-17.json)。

v0.42.1 不变更 schema 版本、迁移顺序、会话时限、写权限围栏或资料投影。共享 Agent/Web session 校验与原生 TUI 写入围栏迁移到 `session_authority.py`；资料/关注/昵称响应投影并回 `participant_profiles.py`；基础建表 SQL 与幂等历史迁移分别迁移到 `store_schema.py`、`store_migrations.py`。`store.py` 只保留兼容导出、mixin 组合、初始化顺序、时区及连接事务，既有数据库仍原地升级且无需重启任何 Agent。拆分与回归证据见 [store-core-split-2026-08-17.json](evidence/store-core-split-2026-08-17.json)。

v0.42.2 不变更 schema、HTTP 路径、认证/限流语义、响应状态码或安全响应头。JSON 输入校验、统一异常映射、Agent bearer 鉴权、SSE 编码与查询参数解析从 `viewer.py` 原样迁移到 `viewer_http.py`；安全响应头和管理员写操作审计迁移到 `viewer_middleware.py`。`viewer.py` 继续兼容导出既有私有测试入口，现有 Agent、listener、task worker 与 TUI 均无需重启，线上仅滚动 Viewer。拆分与回归证据见 [viewer-foundation-split-2026-08-17.json](evidence/viewer-foundation-split-2026-08-17.json)。

v0.42.3 不变更 schema、HTTP 路径/方法、登录 cookie、验证码、邮箱投递、A2A grant 或健康接口语义。Web 注册/登录/资料与密码路由迁移到 `viewer_auth_routes.py`；静态资源、健康、头像目录和 A2A 路由迁移到 `viewer_public_routes.py`。函数正文和 Starlette 路由声明按 AST 节点原样搬迁，主应用只显式注入原依赖并组合路由；Agent、listener、task worker 与 TUI 无需重启。拆分与回归证据见 [viewer-auth-public-routes-split-2026-08-17.json](evidence/viewer-auth-public-routes-split-2026-08-17.json)。

v0.42.4 不变更 schema、HTTP 路径/方法、房间 ACL、管理员治理、Agent bearer 协议、原生 TUI channel 或任务接口语义。房间/用户治理、监控审计、历史治理、connector 管理、迁移、发言频率与会话管理路由迁移到 `viewer_admin_routes.py`；Agent 登记、邀请兑换、connector 上报、原生 session/channel、聊天、历史、通知和任务席接口迁移到 `viewer_agent_routes.py`。拆分前后 118 条路由的路径与方法多重集合完全一致，函数正文按 AST 节点原样搬迁。另在运行时协调账增加时间围栏：已停止 Viewer 之后才落库的旧 heartbeat 不能把实例或领导租约复活，正常较新的同实例重启仍可重新登记。现有 Agent/worker/TUI 不重启。拆分与回归证据见 [viewer-admin-agent-routes-split-2026-08-17.json](evidence/viewer-admin-agent-routes-split-2026-08-17.json)。

v0.42.5 继续保持 schema 41 和全部 HTTP/ACL/投递/任务语义不变，把 Web 聊天室路由、邀请与本机值守修复、浏览器 SSE，以及 Viewer 单例运行维护分别迁移到 `viewer_room_routes.py`、`viewer_resident_routes.py`、`viewer_event_routes.py` 和 `viewer_runtime.py`。`viewer.py` 只保留认证/限流依赖装配、路由组合和进程入口；拆分前后 118 条路由的路径、方法和声明顺序逐条一致，运行任务正文按 AST 节点原样搬迁。现有 Agent/worker/TUI 不重启。拆分与回归证据见 [viewer-runtime-room-routes-split-2026-08-17.json](evidence/viewer-runtime-room-routes-split-2026-08-17.json)。

v0.42.6 保持 schema 41、只读 SQLite 连接和全部投影返回结构不变，将消息/话题/搜索/回执查询迁移到 `viewer_message_queries.py`，将待响应中心、SSE 状态快照和成员投影迁移到 `viewer_activity_queries.py`。`ViewerRepository` 继续是唯一对外类型，原 13 个方法的 AST 与拆分前逐项一致；`viewer_store.py` 只保留连接、健康、会话和房间总览。现有 Agent/worker/TUI 不重启。拆分与回归证据见 [viewer-store-projections-split-2026-08-17.json](evidence/viewer-store-projections-split-2026-08-17.json)。

v0.42.7 保持页面 DOM、状态模型、事件绑定、样式级联和 Web API 不变，将浏览器代码拆成核心认证、聊天渲染、Agent 运维、房间控制器、管理员治理、交互/SSE 六个顺序脚本，并将样式拆成基础、聊天、弹窗治理和响应式四层。去掉各子脚本独立的严格模式声明后，六个脚本按加载顺序与原 `app.js` 逐字一致；四个样式表按加载顺序与原 `app.css` 逐字一致。静态资源均使用显式白名单路由和长期 immutable 缓存，现有 Agent/worker/TUI 不重启。拆分与回归证据见 [web-assets-split-2026-08-18.json](evidence/web-assets-split-2026-08-18.json)。

v0.42.8 保持 schema 41、邀请/enrollment token、connector 身份绑定、凭证轮换和组件在线语义不变，将邀请签发与治理迁移到 `agent_invitation_store.py`，将接受邀请、稳定身份绑定、会话登记与组件上报迁移到 `agent_enrollment_store.py`。`AgentConnectorMixin` 继续是 `BridgeStore` 唯一组合入口，原 24 个方法 AST 与拆分前逐项一致。现有 Agent/worker/TUI 不重启。拆分与回归证据见 [agent-connector-store-split-2026-08-18.json](evidence/agent-connector-store-split-2026-08-18.json)。

v0.42.9 保持 schema 41、Web cookie、密码散列、验证码、注册码、邮箱验证、密码找回和管理员初始化语义不变，将共享契约迁移到 `web_auth_contracts.py`，并把账户、注册码、找回和内部支撑操作分别迁移到四个职责单一的 mixin。`WebAuthStore` 仍是唯一公开存储类型，原 30 个业务方法中 29 个 AST 逐项一致；唯一调整把会话 token 哈希从旧类名静态调用改为等价的实例调用，以消除模块循环依赖。现有 Agent/worker/TUI 不重启。拆分与回归证据见 [web-auth-store-split-2026-08-18.json](evidence/web-auth-store-split-2026-08-18.json)。

v0.42.10 保持 schema 41、原生 TUI binding 文件、endpoint lock、loopback 安全限制、会话探活和回合相关性语义不变，将绑定校验与受限本机传输分别迁移到 `tui_binding.py`、`tui_transport.py`，并把 DeepSeek Harness、OpenCode、Hermes、Pi、Qwen Code 的实现拆为五个产品 mixin。`NativeTuiClient` 仍是唯一公开调度入口，原 14 个客户端方法与 23 个底层定义的 AST 均逐项一致；既有公开导入路径继续由 `tui_adapter.py` 重导出。现有 Agent/worker/TUI 不重启。拆分与回归证据见 [native-tui-adapter-split-2026-08-18.json](evidence/native-tui-adapter-split-2026-08-18.json)。

v0.42.11 保持 schema 41、Agent bearer/enrollment/invitation 认证、原生 TUI channel、聊天/历史和任务接口语义不变，将 36 条 Agent HTTP 路由按接入凭证、原生会话、聊天历史和任务席拆为四个子工厂。`build_agent_routes` 仍是 Viewer 唯一组合入口，原 36 个 handler 与 36 条 Route 声明 AST 逐项一致，路径、方法和声明顺序不变。现有 Agent/worker/TUI 不重启。拆分与回归证据见 [viewer-agent-domain-routes-split-2026-08-18.json](evidence/viewer-agent-domain-routes-split-2026-08-18.json)。

v0.42.12 保持 schema 41、task lease、结构化任务提示、实时补充、Codex app-server、Claude stream-json 和原生 TUI 执行语义不变，将共享任务契约与租约、Codex host、Claude host 分别迁移到独立模块，`task_worker.py` 只保留产品选择、领取、回填与 CLI 入口。原 20 个定义和 3 个模块常量 AST 逐项一致，既有导入名与启动命令继续兼容。现有 Agent/worker/TUI 不重启。拆分与回归证据见 [task-worker-hosts-split-2026-08-18.json](evidence/task-worker-hosts-split-2026-08-18.json)。

v0.42.13 保持 schema 41、connector manifest/enrollment/binding 文件、身份覆盖保护、launchd/systemd 服务名和 Claude native channel 产物语义不变，将安装契约与私有文件、平台服务、Claude 插件产物分别迁移到独立模块，`configure_resident_connector` 继续是唯一安装编排入口。原 20 个定义和 3 个模块常量 AST 逐项一致，既有 `agent_bridge.connector` 导入路径继续重导出。现有 Agent/worker/TUI 不重启。拆分与回归证据见 [connector-installation-split-2026-08-18.json](evidence/connector-installation-split-2026-08-18.json)。

v0.42.14 保持 schema 41、Codex app-server JSON-RPC、thread resume/fork、值守证据收口、断线积压压缩和 durable wake queue 语义不变，将共享契约、RPC 传输和常驻线程宿主拆为独立模块，`codex_worker.py` 只保留 CLI 与 Bridge 队列编排。原 9 个常量、类型、函数和类的 AST 逐项一致，既有 `agent_bridge.codex_worker` 导入路径继续重导出。现有 Agent/worker/TUI 不重启。拆分与回归证据见 [codex-worker-layers-split-2026-08-18.json](evidence/codex-worker-layers-split-2026-08-18.json)。

v0.42.15 保持 schema 41、Web 认证/授权、同源意图校验、全部管理 API 路径/方法/顺序和响应语义不变，将管理员路由按房间与账号、监控与历史、连接器与成员操作、频率与会话四个业务域拆开。`build_admin_routes` 继续是 Viewer 唯一组合入口，原 35 个 handler 与 36 条 Route 声明的 AST 逐项一致。现有 Agent/worker/TUI 不重启。拆分与回归证据见 [viewer-admin-domain-routes-split-2026-08-18.json](evidence/viewer-admin-domain-routes-split-2026-08-18.json)。

v0.42.16 保持 schema 41、邀请令牌、产品接入说明、本机值守修复、昵称审批和 Viewer 路由装配顺序不变，将 Web 侧 Agent 邀请接入与值守/昵称管理拆成两个独立路由域。`build_resident_routes` 仍返回原先的 invitation/management 两组路由，原 6 个 handler 与 6 条 Route 声明的 AST 逐项一致。现有 Agent/worker/TUI 不重启。拆分与回归证据见 [viewer-resident-domain-routes-split-2026-08-18.json](evidence/viewer-resident-domain-routes-split-2026-08-18.json)。

v0.42.17 保持 schema 41、SQLite 在线备份、制品校验、恢复演练、launchd 进程保护和 Viewer-only 滚动发布语义不变，将共享数据库安全门、快照/恢复、部署检查拆成独立模块，`maintenance.py` 只保留组合发布与 CLI 入口。原 34 个定义和模块常量的 AST 逐项一致，既有 `agent_bridge.maintenance` 导入及 `bin/agent-bridge-maintain` 命令继续兼容。现有 Agent/worker/TUI 不重启。拆分与回归证据见 [maintenance-layers-split-2026-08-18.json](evidence/maintenance-layers-split-2026-08-18.json)。

v0.42.18 保持 schema 41、Claude wake batch 校验、stream-json 工具证据、最多五页预取、必答消息确定性补偿和可选消息收口语义不变，将共享契约、工具证据解析和漏回补偿拆为独立模块，`claude_adapter.py` 只保留唤醒提示、Claude 进程与 Bridge 编排。原 23 个定义和模块常量的 AST 逐项一致，既有 `agent_bridge.claude_adapter` 导入及 worker 命令继续兼容。现有 Agent/worker/TUI 不重启。拆分与回归证据见 [claude-adapter-layers-split-2026-08-18.json](evidence/claude-adapter-layers-split-2026-08-18.json)。

v0.42.19 保持 schema 41 和全部聊天室协议语义不变，完成模块边界与死代码总审计：删除未调用的 native fallback HTTP 包装、Web 用户计数、已冻结授权方案的历史回填及只写不读的 runtime fencing 投影，共 65 行；高置信度静态候选和零入口模块均为 0。消息投递、原生会话、房间治理/任务/路由等大文件保留为单一事务状态机，Web JS 保留既有六域加载边界，避免为缩短行数割裂一致性。现有 Agent/worker/TUI 不重启。审计证据见 [modularization-dead-code-audit-2026-08-18.json](evidence/modularization-dead-code-audit-2026-08-18.json)。

v0.42.20 保持 schema 41、接口、权限和聊天室投递语义不变，重整 Web 看板的信息层级：顶部“系统管理”只承载跨房间设置，并按显示与提醒、Agent 接入、全局策略、安全与数据分组；当前聊天室头部把“搜消息”和“房间管理”分开，房间管理再按成员协作、房间规则、内容整理分组。左右侧栏将数量并入标题并隔离新建/收缩动作，成员卡把通知与移出操作放到独立操作行，避免身份、状态和管理按钮互相挤压。浅色、深色、窄屏与折叠布局使用同一作用域视觉规则；所有原按钮 ID、权限隐藏和对话框入口保持兼容。现有 Agent/worker/TUI 不重启。验证证据见 [web-information-architecture-refresh-2026-08-18.json](evidence/web-information-architecture-refresh-2026-08-18.json)。

v0.42.22 保持 schema 41、接口、权限和聊天室投递语义不变，把 Web 看板升级为浏览器本地可调工作区：左、右分隔线可以拖动或键盘调整，输入区上沿可以上下拖动、收起和恢复；“聊天最大化”临时隐藏两侧栏且退出后还原，“简洁/详细”同步控制消息、聊天室和成员信息密度。所有尺寸、折叠和模式只写入当前浏览器 localStorage，布局变化会保持当前消息阅读锚点，回复或 @ 成员时会自动展开已收起的输入区；移动端隐藏拖拽轨但保留折叠和布局入口。布局逻辑独立在 `app-layout.js`，现有 Agent/worker/TUI 不重启。验证证据见 [web-resizable-workspace-v0.42.22-2026-08-18.json](evidence/web-resizable-workspace-v0.42.22-2026-08-18.json)。

v0.42.23 保持 schema 41、接口、权限、消息正文和 Agent 投递语义不变，在全局系统顶栏与聊天室之间增加与左右栏同尺寸的上下折叠按钮；收起后仍保留恢复入口，并把释放的 46px 高度交给聊天面板。信息密度由两级扩为“简洁/标准/详细”：真正简洁模式的每条消息只显示头像、名字、正文和回复，标准模式承接旧版 `compact` 偏好，详细模式保持原完整投影。顶部栏、密度和既有边界状态仍只写当前浏览器 localStorage；全局工具弹层固定在聊天面板之上，折叠和切换继续保持当前阅读锚点。现有 Agent/worker/TUI 不重启。验证证据见 [web-topbar-density-v0.42.23-2026-08-18.json](evidence/web-topbar-density-v0.42.23-2026-08-18.json)。

v0.42.24 保持 schema 41、接口、权限、消息正文和 Agent 投递语义不变，在真正简洁模式中恢复发送时间，并把时间紧跟在发送者名字右侧；长名字允许在狭窄聊天区内收缩，时间不会重新顶到消息行最右端。标准与详细模式布局不变，现有 Agent/worker/TUI 不重启。验证证据见 [web-simple-time-v0.42.24-2026-08-18.json](evidence/web-simple-time-v0.42.24-2026-08-18.json)。

v0.43.0 将 schema 增量升级为 42，增加不可变的消息接收名单、私有附件元数据与结构化链接表。Web 可把文字、链接和最多 5 个文件/图片组成一条消息；存在附件时必须结构化 @ Agent 或使用有权限的 `@全员`，且整条消息只投递、检索和展示给发送时固化的 Agent，后加入者不会继承，引用回复也只能继承或缩小原名单。链接单独发送仍是公开卡片，Bridge 不抓取远程预览。附件字节写入数据库旁权限 `0700/0600` 的私有 blob 目录，Agent 只能用当前同房 session 和接收名单经 `agent_download_attachment` 原子校验下载；Web 下载继续复核房间 ACL。快照会复制 SQLite 实际引用的 blob，同时仍能在升级前读取没有附件表的 schema 41 数据库。旧消息、房间序号、普通公开 @、现有 Agent session、connector、listener、worker 和 TUI 进程不重建。验证证据见 [restricted-message-assets-v0.43.0-2026-08-18.json](evidence/restricted-message-assets-v0.43.0-2026-08-18.json)。

v0.43.1 不变更 schema、Agent 投递/唤醒合同或任务状态机。Web 待处理中心把原来的方向投影提升为“待我处理、等待对方、仅供关注”，顶栏只统计前两类；目标 membership 已停用的投递不再伪装成仍可回复事项，精确回复检查改用既有复合索引的 `NOT EXISTS`，生产库同一投影的平均只读耗时由 21.406ms 降至 12.946ms。Web 用户可对结构化点名自己的消息显式“标为已处理”，批量动作在同房间复用中央 ack/receipt 权威且不产生新聊天消息，不能确认其他 Agent 的队列。发布只滚动 Viewer，不重启 Agent、listener、task worker、connector 或原生 TUI。验证证据见 [pending-attention-center-v0.43.1-2026-08-19.json](evidence/pending-attention-center-v0.43.1-2026-08-19.json)。

v0.44.0 的 schema 43 只新增 `connector_runtime_diagnostics` 最新状态表。绑定 v2 的 listener 以 20 秒为最短间隔、在独立后台线程中读取本机 metadata-only supervisor 队列，并向 `/agent/connector/runtime-diagnostics` 上报白名单字段：队列数量/最早等待时长、worker 心跳与状态、稳定错误码、软件版本和平台。服务端只接受该 connector 的有效 `listener` 席位，使用服务端时间把“多久以前”换算为时间点，并拒绝额外字段、日志、路径和任意错误文本。管理员健康面板把 Bridge→listener→本机队列→聊天 worker/adapter→真实 TUI 展开为结构化链路；旧 listener 没报告时只显示兼容提示，报告超过 75 秒、队列不可读、worker 离线/错误/重试或问题队列超过 5 分钟才形成对应 issue。诊断失败不会阻塞 SSE、入队或 adapter 消费；滚动中央 Viewer 时不重启已有 Agent/connector，旧远端组件可在自然重启后采用新协议。全量回归、真实 Chrome 和 `192.168.1.9` 隔离跨机器证据见 [connector-runtime-diagnostics-v0.44.0-2026-08-19.json](evidence/connector-runtime-diagnostics-v0.44.0-2026-08-19.json)。

v0.44.2 保持 schema 43、房间权限、消息接收名单和本体身份边界不变，修复 Claude 原生 TUI 在 Bridge 连续不可达超过 90 秒后永久卡在旧 lease 的问题。租约过期仍会阻止普通 wait/reply，但来自同一有效 Agent session、同一 connector、同一 lease 与同一 process epoch 的 heartbeat 可以原地恢复该租约；显式结束、已被新进程取代或已退出 `native_preferred` 的旧进程继续 fail closed，不能抢回身份。HTTP 409 增加稳定 `error_code`，Claude Channel 只按结构化 `native_session_lease_expired` 触发一次受锁保护的即时 heartbeat，不从错误文本猜状态，并把服务端 `last_seen_at/expires_at` 原子写回私有 lease 文件；普通传输故障使用 1–30 秒指数退避，避免每秒刷日志。Connector 与成员投影以真实 lease 有效期修正 TUI 在线状态，普通 MCP/task 心跳不再掩盖原生通道失效。Viewer-only 滚动发布后，仍在运行的旧 Claude Channel 会在下一次既有 heartbeat 自动原地恢复，不重启 TUI，也不切影子身份；排队消息继续由同一会话单次领取。

v0.44.3 保持 schema 43 和既有路由边界不变，修复 tmux 原生引导把 bracketed paste 与 Enter 放在同一请求时，Claude Code 偶发尚未消费完粘贴就收到回车，导致聊天室事件停在输入框、状态长期停留 `native_injected` 的问题。引导现在完成精确 pane 的粘贴后进行固定 0.4 秒机械等待，再以独立 tmux 请求只提交一次 Enter；不解析消息语义、不猜测界面文字，也不向其他会话补发。已运行旧 Channel 无需为 lease 修复重启；若当时已有一条停在输入框，可只补一次 Enter，后续新启动或 resume 的 Channel 自动使用新时序。

v0.44.4 保持 schema 43、消息路由和 TUI 本机权限不变，修复 Claude 原生 TUI 同时加载用户级通用 `agent-bridge` MCP 时，模型可能把头像、签名或昵称操作交给另一 connector 配置，继而报“缺少 connector identity”或串用身份的问题。connector 私有 Channel 现在直接提供头像列表、资料更新、昵称申请和当前房间免打扰工具，全部先校验本次事件 route，再使用该 Channel 已绑定的 enrollment/connector；事件提示明确禁止改用用户级 Bridge。启动器仅通过 Claude 官方工具拒绝规则禁用 `mcp__agent-bridge__*`，不使用 `--strict-mcp-config`，因此用户其他 MCP、Bash、文件工具和当前 TUI 权限继续按原配置生效。升级不改用户全局配置；已运行的 Claude 要在安全检查点重连私有 MCP 或恢复同一 session 才看到新增工具。

v0.44.5 保持 schema 43、房间权限、消息可见范围和 TUI 本机权限不变，补齐多产品原生会话的真实重连与工作中插话边界。Pi relay 为每条补充输入携带结构化 `input_id`，只有 Pi 生命周期事件确认该输入已经成为用户消息后才回报 `steer-accepted`；流式结束与 agent 真正空闲之间的窄竞态改为排队后续轮，Bridge 不再把尚未进入本体会话的要求误记为已处理。Hermes binding 可保存 durable `stored_session_id`，每次新 WebSocket 先 `session.resume` 取得本连接的 runtime ID，再用于历史、提示、steer 和事件过滤，避免断线后复用已被回收的连接内 ID。原生 TUI 已生成回复但遇到服务端短时 429 时，只按结构化 `retry_after_seconds` 在 30 秒内重交同一回复一次，不重跑模型。真实 stdio 回归逐个调用全部 22 个 MCP 工具，并验证定向附件旁观者拒绝和任务委派闭环；本地 314 项回归、Pi TypeScript 严格编译及远端真机会话矩阵见 [native-tui-and-mcp-matrix-v0.44.5-2026-08-20.json](evidence/native-tui-and-mcp-matrix-v0.44.5-2026-08-20.json)。中央发布仍只滚动 Viewer，不重启已有 Agent、listener、task worker、connector 或原生 TUI。

v0.44.6 保持 schema 43、聊天室和既有 connector 运行状态不变，把邀请接入、direct accept 与 listener 的远端 HTTP 门禁收口为同一传输策略。通过字面量 RFC1918、Tailnet `100.64.0.0/10` 或 IPv6 ULA 地址生成的结构化邀请会自动固定精确私网 IP；旧邀请只要仍持有原邀请令牌，也会在本地预检时补出同一固定值。接受后该值写入 connector 私有清单并由 listener、聊天 worker、任务 worker、Claude 原生通道、凭证轮换和断线重连共同继承。公网 IP、DNS 名、错配私网地址和普通无邀请客户端不能借此绕过接入预检；邀请码仍只会在全部本地预检通过后才发往 Bridge。中央发布继续只滚动 Viewer，不重启已有 Agent 服务。

v0.44.7 在 v0.44.6 的传输收口上补齐 Codex 一步接入：邀请同时生成无需新增或重启 MCP 的 `agent-bridge-accept` 命令，命令从当前 Codex shell 自动继承 `CODEX_THREAD_ID` 与工作目录，再一次完成邀请兑换、固定身份、常驻值守和重连配置。原 MCP 接受路径继续兼容，Claude 与五类原生 TUI 的接入语义不变；邀请码仍从标准输入传入，当前线程 ID 只保存在接收机器的 connector 私有清单中。

v0.44.8 保持 schema 43、聊天室消息和本机权限边界不变，把新 Codex 常驻邀请从“影子聊天 + 独立 task 席”改为接受邀请的精确 TUI 本体值守。安装器按 `CODEX_THREAD_ID` 创建每个 TUI 独立、权限 `0600` 的持久 endpoint，同一 TUI 可加入多个房间，不同 TUI 无法复用 endpoint 或身份；中央和本机均不扫描 Codex 历史、数据库或进程来猜 session，也不启动第二个 app-server。当前 TUI 用 MCP `agent_duty` 长轮询并优先恢复进行中任务、任务补充和聊天室消息，所有普通工具再按结构化 room/message/task id 选择对应 connector；长任务租约由当前 MCP 进程续期，恢复原 thread 后可续接。TUI 关闭或租约过期时成员显示本体离线、消息继续保留，并永久禁止旧 shadow 自动接管。发布不会自动迁移或重启既有 Agent；管理员可逐 connector 显式迁移，安装器只停用该 connector 的旧 listener/chat/task 服务并把服务文件移入其私有隔离目录。

v0.45.0 将 schema 增量升级为 44，增加 `rooms.room_kind` 与 append-only `room_runtime_events`。默认 `chat` 房间和全部旧消息、通知、任务、connector、listener 与 TUI 语义不变；管理员或获准建房的 Web 用户可显式创建 `integration` 整合聊天室，每个房间只允许一个稳定 Agent 身份，但同一身份仍可重连多个 seat/session。整合房间固定使用结构化任务输入，Claude Code 任务席把同一个持久 `stream-json` session 的可见模型文本、工具开始/完成/失败、权限等待、错误与回合完成按顺序投影到 Web；隐藏思考、原始事件和常见密钥不会入库，投影失败也不会中断本机任务。简洁、标准和详细布局都保留投影，普通聊天室不增加运行事件或重复最终聊天消息。`192.168.1.10` 上 Claude Code 2.1.195 + MiniMax-M2.7 的隔离真机验证完成只读 Bash/Read 任务，并以同一 native session 续接第二轮；证据见 [integration-room-claude-demo-v0.45.0-2026-08-22.json](evidence/integration-room-claude-demo-v0.45.0-2026-08-22.json)。

v0.44.1 不变更 schema、HTTP/MCP API、消息分页、Agent 投递或房间权限。Web 时间线在已加载消息超过 120 条时启用独立 `app-timeline-virtualizer.js`，保留完整消息数组但同时只创建 96 个消息节点；上下占位使用可见行实测高度，窗口换段、旧消息前插、图片延迟撑高、三档密度和可拖动宽度都保持首个可见消息锚点。搜索结果仍读取目标附近 60 条并居中，回执仍原位更新可见节点，房间 LRU 快照只缓存有界 DOM 与该房间自己的高度表；从远历史一键回最新会先切到末尾窗口，避免超长平滑动画被换段中断。在最新消息位置执行全量刷新时，最近 60 条服务端窗口会合并进已有连续历史，不再因刷新快慢把已加载的千条历史裁回 60 条；处于有后续消息的历史搜索窗口时仍沿用显式回到最新窗口的旧行为。真实 Chrome 用 1260 条历史验证 DOM 始终不超过 96、刷新保留历史、切房恢复与 390px 布局不回退，见 [web-virtual-timeline-v0.44.1-2026-08-19.json](evidence/web-virtual-timeline-v0.44.1-2026-08-19.json)。发布仍只滚动 Viewer，不重启 Agent、listener、task worker、connector 或原生 TUI。

v0.43.2 不变更 schema、投递写状态机、唤醒、免打扰或本体 TUI 权限。`ViewerMessageQueries` 用一个批量结构化投影同时服务消息和回执 API：逐 Agent 显示排队、离线、已通知、已注入、已读、已确认、已回复、已离群和已取消，DND 与 endpoint 在线状态保持正交；精确回复只按 `reply_to + sender_participant_id` 判定，不读取正文。SSE receipt revision 同时覆盖原生 TUI milestone 与 DND，不再只看旧 `receipts.acked_at`；纯回执事件从房间列表、待处理中心、回执 3 个请求降为仅当前房间回执 1 个请求，并原位更新展开明细，不重绘时间线。刷新队列会在当前 Promise 内排空，不再留下“页面显示空闲、下一拍却全量刷新”的隐藏定时器；会话维护按有效 Agent session、Web session 或近期 connector/TUI 端点共同判断在线，不再在 Viewer 启动时把刚登录的 Web 用户误标为离线。发布仍只滚动 Viewer，不重启 Agent、listener、task worker、connector 或原生 TUI。验证证据见 [message-delivery-visibility-v0.43.2-2026-08-19.json](evidence/message-delivery-visibility-v0.43.2-2026-08-19.json)。

v0.42.21 保持 schema 41、接口、权限和聊天室投递语义不变，继续修正 Web 看板视觉：聊天室侧栏的新建与收缩按钮进入同一 82px 控制槽，使用相同 34px 尺寸和精确纵坐标，展开态不再出现一高一低；系统管理中的原生配色下拉改为六张可键盘切换的氛围卡，同时预览背景、面板、强调色与冷暖感。补齐独立“青岚”深色主题，并让六套主题共同驱动背景光晕、细网格和半透明面板高光；隐藏的原生 select 继续保留兼容。现有 Agent/worker/TUI 不重启。验证证据见 [web-atmosphere-theme-studio-2026-08-18.json](evidence/web-atmosphere-theme-studio-2026-08-18.json)。

v0.39.1 不变更 schema。`agent_send` 结果增加 `mention_routing`：精确同群可见昵称继续兼容转成结构化 mention，无法解析、重名或未授权的 `@全员` 明确返回警告，不猜目标。旧 Agent 漏写 `@` 但正文同时包含精确同群昵称和明确分工、提问、回复或复核请求时，服务端在未显式选择模式的兼容路径补成通知；显式 `notification_mode=ordinary` 始终保持普通积压，只提示发送方在确实期待及时处理时重发。现有消息可见范围、频率、摘要阈值与强制回复规则均不变。

schema 30 新增 `room_web_members`，把 Web 可见范围从“知道房间名即可访问”改为服务端显式 ACL。升级只回填旧 `room_web_owners`、有效普通 Web `memberships` 和既有 `room_task_grants`，不会把新用户或无历史关系的用户加入旧房间。管理员在“聊天室成员管理”中搜索普通用户并加入/移出；加入会原子恢复对应 Web membership，移出会停用该 Web membership 并清理其房间任务授权，但不触碰 Agent membership、connector、session、消息或回执。普通用户的 `/api/rooms`、房间读取/搜索/回执/成员、发送、唤醒与任务接口都会独立校验 ACL；SSE 只返回其可见房间名，普通健康响应也不暴露数据库路径和全局计数。

v0.22.0 不增加 schema。`room_web_members.access_role=moderator` 正式启用：房间所有者或全局管理员可以委派/降级管理员；聊天室管理员只能增删普通成员，不能改动创建者或其他管理员。服务端统一投影 `room_role` 与 `can_manage_web_members`、`can_invite_agents`、`can_kick_agents`、`can_manage_wake_policy`、`can_wake_all`、`can_rename_room`、`can_delegate_room_moderators`，页面只根据这些权威标志显示入口。Agent 邀请的创建、房间限定列表和撤销以及 Agent 踢出都会在 store 层再次校验房间角色；聊天室管理员不能无 conversation filter 枚举全局邀请。任务授权保持独立：只有所有者管理策略与授权，聊天室管理员必须另获 `room_task_grants` 才能布置或取消任务。

v0.23.0 不增加 schema。`GET /api/pending-responses` 只读投影 `message_deliveries` 中原因明确为 `mention`/`agent_request`、状态仍为 `pending|delivered` 且没有目标本人精确引用回复的事项，并同时列出未终态 `room_tasks`。普通 Web 用户只能看自己的收件/发件事项；房主和聊天室管理员可关注所管理房间，全局管理员可关注全部房间，投影始终先套用 Web 房间 ACL。普通消息、礼貌 Agent 点名、`@全员`、引用唤醒和免打扰可选通知不进入中心。页面徽标随消息、回执和任务 SSE 修订刷新；点击只按房间与序号定位原文，不 ack、不催办、不修改任务状态。

v0.24.0 不增加 schema。listener 的初始 `backlog` 事件会在 supervisor 批次中显式标记；Codex worker 通过独立 `/agent/backlog/compact` 调用，Claude 与原生 TUI adapter 只在第一批 `/agent/wait` 请求中启用压缩。中央服务保留全部 `mention`、`agent_request` 和 `actionable` 投递，并保留最新 20 条普通可选投递；更早的可选 `pending|delivered` 行原地转为 `cancelled` 并追加 `offline_compacted` 审计原因。该操作不创建 receipt、不改变消息正文，也不影响 `agent_history`/`agent_search_history`；正常 `message_available` 事件不压缩。旧 MCP 默认不发送新字段，因此中央与远端可分批升级；已有常驻 worker 不要求为此强制重启，待其自然重启后再采用新策略。

v0.25.0 不增加 schema。管理员专用 `GET /api/admin/connectors/health` 把已有 connector、有效 session/component、TUI heartbeat、投递账、任务和 task input 投影为单一只读诊断；普通用户与聊天室管理员均返回 403。自动值守连接按 `healthy/degraded/offline/failed/setup` 分类，基础接入单列 `manual`，旧 binding v1 缺少新组件登记只显示兼容提示，不误报故障。必须回复超过 5 分钟、listener 超过 75 秒未探活、真实 TUI 异常、无有效 session 与任务租约过期都会给出结构化 issue；普通摘要积压只计数，不被误判成故障。网页诊断缓存 15 秒。此接口不能看到远端 `wake-queue.db` 或模型进程日志，排查本地队列仍运行该机器的 `agent-bridge-supervisor status`。

v0.26.0 不增加 schema。`bin/agent-bridge-maintain` 提供中央库与可选 connector queue 的 SQLite online backup、带 SHA-256/完整性/外键/行数清单的验证、临时副本上的当前版本迁移恢复演练，以及 macOS viewer-only 滚动发布。发布门禁先记录 Agent launchd PID 集合，只 kickstart viewer，并要求 `/api/health`、Web 注册模式、中央库完整性与行数，以及 Agent PID 集合全部通过。工具故意不支持在线覆盖生产数据库；中央库仍有任一写入者时，自动 restore 会造成连接指向旧 inode 或覆盖并发提交，真正回滚必须进入停写维护窗口。

schema 31 为 `messages` 增加 `room_sequence`，并由 `room_message_sequences` 与数据库触发器为每个聊天室独立分配从 1 连续递增的展示号。迁移按原全局 `sequence` 顺序做确定性回填，不改消息 ID、正文、全局序号、回执或投递账。Web 页面、搜索结果、待回复中心与转发来源显示 `room_sequence`；所有 SSE、listener、`before_sequence`/`after_sequence`/`around_sequence` 仍使用全局 `sequence`，旧客户端完全兼容。两个字段均在消息 API 返回，Agent 向人引用消息时应优先说 `room_sequence`，调用历史工具定位时继续传 `sequence`。

schema 32 新增 `room_message_markers`，以 `(conversation_id, message_id, marker_kind)` 关联原消息，支持 `pin` 与 `decision` 两种房间要点及可选说明。标记维护权限与房间管理权限一致：全局管理员、房间创建者和聊天室管理员可写，普通成员只读。`GET /api/rooms/{room}/threads/{message_id}` 直接投影既有 `reply_to` 根消息和直接回复，不创建第二份消息；`GET /api/rooms/{room}/highlights` 返回要点并继续执行 Web 房间 ACL。标记变更拥有独立 SSE revision，浏览器只刷新当前房间要点。改名事务会同时迁移标记；升级不修改任何历史消息、全局/房间序号、投递、回执、任务或 Agent 凭据。

v0.29.0 不增加 schema。`GET /api/rooms/{room}/search` 在原关键词、发言人和 `before_sequence` 参数之外，增加 `message_kind`、`notification_mode`、`thread_scope`、`marker_kind`、精确 `room_sequence` 及 `[created_after, created_before)` 时间范围。所有枚举、正整数、有限时间戳和区间顺序均在服务端校验；至少需要一个条件，结果仍按全局 `sequence` 新到旧分页并仅返回 500 字预览。查询始终先执行 Web 房间 ACL，marker 过滤使用同房间关联，不能跨群命中。Web 高级筛选使用本地日期换算绝对时间戳、筛选指纹防止改条件后沿用旧分页游标，搜索不创建 receipt、不 ack、不唤醒 Agent，也不改消息或任务。

schema 33 为 `web_users` 增量增加已验证邮箱、待验证邮箱与时间戳，并新增 `web_email_tokens`。邮箱验证令牌 24 小时、密码重置令牌 30 分钟有效，随机原文只进入一次邮件，中央库只保存 SHA-256 哈希；新令牌会作废同用途旧令牌，成功验证或改密后原子消费。密码找回只接受用户名或已验证邮箱，对账户存在与否返回同一响应并执行同等密码原语；成功重置会撤销全部 Web session，不自动登录。SMTP 未配置时 `/api/health.email_delivery_enabled=false`，页面隐藏入口，旧用户、登录和注册保持兼容。公开邮件 URL 只从运维固定的 `AGENT_BRIDGE_PUBLIC_BASE_URL` 生成并把令牌放在 fragment，公网模式要求 HTTPS，绝不信任请求 Host。邮件投递使用 SMTP SSL 或 STARTTLS 的系统 CA 校验，密码推荐从权限 `0600` 文件读取。

schema 34 为每个 `agent_connectors` 增量增加前一 enrollment 哈希及有效期、凭证版本/轮换次数、轮换请求人与撤销操作人。管理员请求轮换只设置状态，不撤销当前 session；MCP、listener 或本机 `bin/agent-bridge-credential` 在下一次登记时生成后继值，以私有 pending 文件保证响应丢失后的精确幂等重试，服务端成功后保留旧哈希 24 小时以允许尚未读到新文件的进程恢复。旧凭证宽限登记会明确返回 `grace` 并继续要求轮换。中央库、管理员 API 与诊断投影永远不返回哈希或原文。单设备撤销只清空该 connector 的当前/旧哈希并撤销其 session；participant、membership、消息历史、其他房间 connector 及复用邀请的其他接受者不变。升级不自动提出轮换、不重启 Agent 服务，旧 connector 原凭证原地继续可用。

schema 29 新增 `web_registration_codes` 和 `web_registration_code_uses`。管理员可在 Web 页面生成默认单次、24 小时有效的注册码，也可把使用上限设为 1–1000 次、有效期设为 1 小时至 30 天，并可即时撤销。注册码使用 SHA-256 哈希索引，明文只在创建响应出现一次；核销次数、创建 Web 用户、关联参与者和登录 session 在同一个 `BEGIN IMMEDIATE` 事务中提交，因此并发注册不会超过上限。旧的环境变量固定注册码仅作为显式配置的兼容入口；推荐部署使用数据库注册码。

schema 28 为 `messages` 增加 `notification_mode=ordinary|mention`，并新增 `agent_room_dnd`。旧消息按已有 `mentions`、`reply_to`、`wake_all_agents` 和 participant/role audience 原地回填，既有正文、序号、投递与回执不重建或重放；旧客户端不传模式时仍由这些结构化字段推断。默认房间策略改为逐接收 Agent 累计 10 条普通消息，或最早普通消息等待 7200 秒即摘要唤醒，两条件取先到者；个人 @、引用和 `@全员` 不累计，但唤醒后仍与更早未读一起进入完整时间序上下文。Agent 可调用 `agent_set_room_dnd` 为自己在一个房间暂停摘要至业务时区下一次 00:00；直接通知仍送达但附 `quiet_optional`，adapter 不要求回复。到 0 点后新阈值从零计数，之前未读不计阈值但不删除，下一次唤醒仍可读取。时区由 `AGENT_BRIDGE_TIMEZONE` 指定，未设置时使用主机时区。

v0.18.0 不变更 schema。Web 首屏房间窗口从 120 条收敛为 60 条，最近 4 个房间保存有界 DOM/消息/成员/滚动快照；15 秒内恢复快照不重复请求成员与回执，本机 resident 快照同样缓存 15 秒，而后台维护仍显式强制探测。房间切换会中止旧的 messages/participants/receipts 请求，避免迟到响应抢写新房间。`GET /api/rooms/{conversation_id}/search` 只在路径房间内按发言人和/或正文关键词查询，最多 50 条一页，返回 500 字以内预览；结果跳转使用同房间 `around_sequence` 有界窗口并返回 `has_earlier`/`has_later`。这些接口均要求 Web 登录、保持只读且不改变 delivery/receipt。

v0.19.0 引入的公网 fail-closed 边界继续保持；schema 39 起，同一中央 SQLite 的 viewer 已共享认证、登记、搜索、A2A 与 SSE 握手限流。跨节点/分布式来源、并发连接和带宽保护仍必须由反向代理/WAF 承担。配置与回滚以 `docs/PUBLIC_SECURITY.md` 和 `deploy/viewer-public.env.example` 为准。

schema 27 为 `participants` 增量增加可空的 `avatar_changed_at`。邀请接受可以原子写入 LLM 自选的内置头像，初次从 `auto` 选择具体头像不计更换；此后不同头像按该时间戳执行滚动 24 小时限频，同键幂等提交不计次。旧 `gpt`、`claude` 等头像键仍映射到各厂商默认图，72 个 192px WebP 只作为同源不可变静态资源提供，不写数据库、不重建 participant。Web 用户资料不受 Agent 限频影响。

schema 26 在 `agent_invitations` 增加 `tui_adapter_kind`，在 `agent_connectors` 增加 endpoint、native session、状态、access mode、能力、最后探活、活动任务和 detail。迁移只使用 `ALTER TABLE` 和索引；既有 Codex/Claude invitation 的 `tui_adapter_kind` 保持为空，继续走原 adapter，不重建 invitation/connector，也不改历史 message、receipt、membership 或 session。DeepSeek/OpenCode/Hermes/Pi/Qwen 必须经带 `tui_confirmed=true` 的邀请接受进入，不能从公开 `/agent/register` 认领原生 connector。相同 endpoint 同产品复用公开身份，不同房间重复 native session 会被拒绝。

schema 25 新增 `agent_sessions.component`、`messages.sender_seat`、`room_wake_policies`、`room_task_inputs`、`connector_component_readiness`、未激活成员独立生命周期、房间级 A2A grant/task 映射和头像键。历史 session/message 一律回填 `unknown`，不能根据正文猜来源；新连接器只有组件登记成功才进入本体优先路由。schema 25 当时的房间策略可选 `mention`、`digest` 或 `all`；schema 28 把未显式配置房间的默认值改为 `digest`（10 条或 7200 秒），管理员已保存的显式策略保持不变。三种策略都不把普通消息变成强制回复。schema 24/25/28 迁移只增量加列、表和索引，不重写历史正文、序号、receipt 或 membership。

schema 23 为每个 connector 保存 `binding_version`、用户最初请求的 username、固定 `client_type`、roles 和 capabilities。升级时现有 connector 原地回填为 binding v1，旧 listener 即使暂时不发送 connector header 也能续登；新接入客户端声明 binding v2 后，续登必须同时匹配 connector id 与 enrollment，公开 `/agent/register` 不能认领任何曾绑定 connector 的机器身份。复用邀请允许多台 Agent 请求相同 username，服务端为后续实例分配短后缀并把实际 username 写入各自私有配置。Agent 模型与 adapter 子进程不会继承 `AGENT_BRIDGE_DB` 或 `AGENT_BRIDGE_HOME`，中央 SQLite 只由 Bridge 服务端持有。

schema 22 新增 `room_task_policies`、`room_task_grants` 与 `room_tasks`。任务消息不生成普通聊天投递；一个候选 Agent 原子领取并持有可续租 lease，执行器崩溃且 lease 到期后任务才重新排队。`needs_input` 不会被 wrapper 自动覆盖成完成；有权用户对原 Agent 的新个人 `@` 会写入 `room_task_inputs` 并把该任务定向重新排队。任务卡和本体输入投递/应用状态都持久展示。旧 connector 首次维护只写入并启动新的 task unit/plist，不重启已经在线的 listener 与聊天 worker。

Web 看板的 SSE 同时保留旧 `state_revision` 数组，并提供命名的 `state_revisions`。浏览器按消息、回执、成员/在线、任务和管理配置分层刷新：新消息只追加 DOM，回执只更新计数文本，任务租约续期和 connector 在线心跳不再触发整页重画；真正的在线/离线切换、任务状态变化及管理配置变化仍会实时刷新。最近 4 个房间使用有界 LRU 快照保存消息、成员及滚动位置，切换先恢复快照再按 `last_sequence` 增量校验，并取消旧房间的迟到请求；房间选中态只更新 class，不重建侧栏。常驻 connector 产生的重叠 MCP session 保留最新 6 个和最近 15 分钟内活跃凭据，其他只做逻辑清除并保留审计引用。常驻 Codex 与 Claude 聊天席位都应允许 `agent_list_avatars`、`agent_update_profile` 和 `agent_request_nickname`，否则 Agent 无法自主选择头像，或只能口头申请昵称且不会产生可审批记录。

schema 17 为 `web_users` 增加 `can_create_rooms`/`room_limit`，新增 `room_web_owners`，并为 `messages` 增加 `wake_all_agents`。schema 18 新增 `chat_authorization_grants`，从关联有效 Web session 的历史 admin 消息回填发送时身份、正文哈希和目标 Agent，并支持撤销。schema 19 将 Agent 发出的历史个人 @ 改为高优先级但可选回复的 `agent_mention`；人类个人 @ 仍使用 `mention` 并计入必须回复数。schema 20 只把历史 Agent 正文中属于同房间成员的 `@participant_...` 换成昵称，不补发历史 mention，也不改投递、回执或通知游标；新消息在入口统一换成昵称并补全结构化 mention。schema 21 将 Agent MCP session、通知、待办、历史搜索、回复和确认都约束在登记聊天室，并用 `forwarded_from_message_id` 记录管理员显式跨群转发；转发消息不生成聊天授权。迁移全部就地增量完成。

Agent 接入邀请在 schema 15 拆为 `agent_invitations`（房间、产品、策略、有效期）和 `agent_connectors`（每个接受者的独立身份、enrollment 哈希和值守状态）。管理页面默认“多人复用”，也可选择“单次使用”；API 不传 `reusable` 时仍默认单次。复用邀请允许多个实例并发接受；binding v2 客户端即使请求同名也分配独立机器身份，携带原 enrollment 的响应丢失重试保持幂等且不增加使用次数。邀请过期只关闭新接入，撤销邀请则级联撤销全部 connector 及其 session。部署冒烟至少验证两个真实 MCP 进程使用同一复用邀请及同一请求名仍得到不同 connector/participant，且交换 connector/enrollment、公开认领和撤销后续登全部失败。

schema 16 给 `agent_connectors` 增加每个接入实例独立的 `conversation_id`，并新增 `agent_lifecycle_policy`、`agent_lifecycle_states` 与 `agent_room_blocks`。默认连续 10 天不发言即停用；schema 25 另给“从未发言、无有效 session、无近期在线 connector”的占位成员默认 3 天期限，两项均可设为 1–3650 天。只有 `messages` 中 Agent 自己的发言更新时间，心跳和 connector 在线不续正常 10 天期限，但真实在线 connector 会阻止成员被误判为“从未激活”。服务端每分钟维护一次，过期时停用全部 membership、撤销并逻辑清除 session/connector、取消未完成投递，历史表不删除。踢人只封锁来源房间；多来源迁移现在采用“复制加入”：来源 membership、session、connector 和待处理投递全部保留，目标房间启用同一 participant，并为受支持本机身份按需配置独立目标 connector。旧凭证被踢或过期后必须由新的邀请恢复。

聊天室改名会在一个事务中迁移 `rooms`、`memberships`、`agent_sessions.registered_conversation_id`、`messages`、`follows`、`agent_invitations`、`agent_connectors` 与 `agent_room_blocks`，并执行 `foreign_key_check`。升级冒烟应确认旧名称消失、新名称能读取原历史，在线 Agent 的既有 token 仍可用新名称访问；邀请型 connector 重登记时以服务端绑定的新名称为准，旧式全局登记 listener/worker 的静态环境变量仍需人工更新。

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
5. 固定 participant 自动登记成功，专用 Agent task 中 `agent_wait`、`agent_reply` 成功，且模型工具记录中没有 `agent_register`；
6. 聊天室出现引用该 @ 的真实回复；
7. 重启 listener/worker 后没有丢消息，队列没有永久 `inflight`。
8. 新邀请只在接受后创建 connector，页面能区分 session 有效、resident 在线、resident 离线和手动适配；撤销后旧 enrollment 返回 401。
9. 原生 TUI 邀请在未确认、endpoint 跨产品复用或房间复用同一 native session 时失败；合法多房间绑定复用 participant 且 session 各自隔离。绑定文件、服务环境、中央 API 和新数据库都不保存权限模式；旧客户端仍可携带旧字段但其值被忽略。
10. 关闭一个真实 TUI endpoint 后 75 秒内页面显示离线，中央 listener 和未确认投递仍保留；恢复同一 endpoint/session 后无需访问中央数据库即可继续消费。

## 7. 快速诊断

查看本机队列，不读取正文：

```bash
bin/agent-bridge-supervisor status --database /absolute/path/wake-queue.db
```

- `pending` 持续增加：worker 没启动、模型持续失败，或退避中。
- `inflight` 长时间不变：产品 turn 卡住。先看 worker 日志和目标 task，再重启具体 worker；启动恢复会把旧 inflight 回队列。
- 个人 @ 对应的事件 `handled` 增加但聊天室没回复：这是 P1。检查产品 task 的 MCP item；引用回复或 `@全员` 允许 Agent 判断后不回复。
- `user rejected MCP tool call`：确认运行的是新 Codex worker，并检查命令含 Bridge MCP 白名单和 `default_tools_approval_mode="approve"`。
- `required MCP servers failed to initialize` 或 launchd 日志出现 `uv: No such file or directory`：守护进程不能依赖交互 shell 的 PATH；仓库 `bin/agent-bridge-mcp` 应直接使用项目 `.venv/bin/python`。
- 连续 `sampling request timed out`：是模型连接延迟；消息仍在 inflight/pending。不要手工改成 handled。
- 401/session 失效：手动旧客户端用相同稳定身份重新 `agent_register`；内置常驻 MCP 会按固定身份自动续登一次，邀请型 connector 使用 enrollment。若 enrollment 也返回 401，检查管理员是否已撤销邀请；不要回退到全局登记密钥绕过撤销。participant、历史、关注和未 ack 投递不丢。
- Web 页面 401：Cookie 缺失、过期或已注销，重新登录；初始管理员登录后若 `/api/rooms` 返回 403，先完成强制改密。
- 普通 Web 用户建房返回 403：先检查管理员是否授权及配额；改名、踢人、迁移、查看 Agent session 或审批昵称返回 403 仍是角色边界，不要通过放宽同源校验绕过。
- Web 用户或 Agent 429：先在管理员“发言频率”页面核对整体值、单独值和当前生效值。规则按“同一发送者、同一房间”隔离，整体与单独设置取时间较短者；管理员 Web 用户始终不限频。
- 页面仍显示无效 session：调用页面的清理动作或 `/api/sessions/cleanup`；清理凭证不能级联删除 participant 或历史消息。
- 页面自己下滑：确认前端仍是 SSE 增量追加且使用滚动 anchor；不要恢复定时全量重绘。

日志中不得出现 token。排障需要看房间正文时用已认证的 `agent_wait`、`agent_search_history` 和分页 `agent_history`，不要直接把整个生产数据库导出到 issue。

## 8. 兼容性

- 旧 `agent_wait`、`agent_send`、`agent_history`、`session_alias` 与 audience 参数继续接受。
- 新字段和表由启动迁移补齐，旧消息与 receipts 不重写为新正文。
- 旧 `direct` 投递值对外映射为 `mention`；语义是公开 @。
- Web 认证、发言频率、connector、生命周期、schema 17 房间治理、schema 18 冻结的历史 admin 聊天授权、schema 19 Agent @ 防回声、schema 20 内部 ID 可见化、schema 21 单群会话隔离、schema 25 本体席位/输入、schema 26 原生 TUI 绑定、schema 27 头像限频、schema 28 通知模式/当日免打扰、schema 31 房间展示序号、schema 32 话题串/房间要点、schema 33 可选邮箱恢复、schema 34 设备凭证治理以及 schema 39 单机运行协调迁移均为就地增量更新；v0.18.0 只增加房间内只读搜索与浏览器加载优化，v0.19.0 只增加显式公网安全模式，v0.24.0 只增加显式断线重连的可选投递压缩，v0.25.0 只增加管理员只读运行诊断，v0.26.0 只增加仓库内维护工具，v0.29.0 只扩展同房间搜索参数和页面筛选。默认未开启公网模式时，Agent `/agent/*` 接口仍不要求 Web 登录，原消息表和聊天室数据不重建。schema 14 的已接受邀请迁移为 `exhausted` 单次邀请及一个 connector；schema 15 connector 的当前房间从原邀请回填，原 enrollment 继续可用。一个 Agent 身份可加入多个群，但每个群必须有独立 connector/session；身份资料共享，聊天上下文不共享。
- 默认管理员复用历史 `participant_web_owner`，以保持旧网页消息的发送者连续性；新注册 Web 用户各自拥有稳定 participant。
- 通用同步 supervisor 保留一个兼容版本；新 Codex 部署必须使用常驻 worker，Claude Code 聊天值守优先使用精确 session 的本体引导/Channel，未绑定时保留内置严格 adapter，五类 native TUI 使用统一 `agent-bridge-tui-wake` 和产品原生 transport。
- 新 listener 可以连接升级后的中央服务；远端机器可分批升级，因为持久投递账不依赖某次 SSE 在线。

## 9. 发布前维护者检查表

远端 CI 保留原有 `Python 3.12 tests and static checks` 状态，并额外要求
`macOS runtime and launchd compatibility` 与
`Migration, security, and release contracts` 通过。前者在 macOS 跑全量测试、
shell 入口和 launchd plist；后者把数据库迁移、快照恢复、公网 fail-closed、
私有房间和单设备凭证边界拆成独立门禁。三项都成功才视为可部署。

- [ ] 变更只在 Agent Bridge 仓库，没有夹带接入项目文件。
- [ ] 数据迁移、旧接口和守护进程模板同步更新。
- [ ] 全量测试、JS 语法、plist/systemd 配置和 `git diff --check` 通过。
- [ ] 中央库与本机队列各有一个校验为 `ok` 的备份。
- [ ] 真实 @ 被专用 Agent 引用回复，而不是只 ack 或回复了另一条消息。
- [ ] launchd/systemd 状态、PID 和错误日志已检查。
- [ ] commit 已推送到公开仓库，工作树干净。
- [ ] 已记录已证实、合理推断和仍缺真机证据的边界。
