# 原生 TUI 真产品 E2E 证据

状态：2026-08-14（America/Los_Angeles）完成。对应 Agent Bridge v0.32.4、schema 35。

## 结论

OpenCode、Pi、Hermes、Qwen Code 与 DeepSeek Harness 均在隔离 Linux 测试机上启动了真实产品 runtime，并由发布版 `NativeTuiClient` 完成只读探活和真实模型回合。每个产品至少建立两个原生 session，使用不同随机标记核对路由；已执行的历史检查没有发现跨 session 标记。这里的“重连”指产品 runtime 和原生 session 保持运行、Bridge 客户端对象重新建立后继续使用同一绑定，符合“用户不关闭 TUI/runtime”的目标边界，不宣称机器重启后的恢复能力。

测试没有连接生产 `bridge.db`，没有注册测试 Agent，也没有向生产聊天室写消息。模型凭据只进入测试机 `0600` 临时文件或进程环境，从未写入仓库、测试输出或 Bridge 数据库。测试完成后，临时产品目录和凭据已不可恢复地删除，19100—19103 端口关闭，临时进程为零，测试机没有安装 `gcc-c++`/`libstdc++-devel` 系统 RPM。

机器可读结果见 [real-product-e2e-2026-08-14.json](evidence/real-product-e2e-2026-08-14.json)。

## 验收矩阵

| 产品 | 真机版本与原生通道 | 会话/回合 | 已验证结果 | 发现与处理 |
|---|---|---:|---|---|
| OpenCode | 1.15.13，官方 loopback HTTP server | 2 个 session，多轮模型调用 | 两个 session 只读探活成功；请求与回复相关；随机标记未串线 | OpenCode 自定义端点的 base URL 需要保留 `/v1`；Bridge 无需修改 |
| Pi | 0.78.0，官方 extension 私有文件 relay | 2 个 session，4 回合 | 新 session 首条消息、session 切换、探活与隔离全部通过 | Pi 在首条消息前已有稳定 session 路径但 JSONL 尚未创建；v0.32.2 允许“当前活动 session”的该状态，仍拒绝不存在的非活动 session |
| Hermes | 0.19.1，官方 loopback WebSocket gateway | 2 个 session，3 回合 | `session.history` 探活、回合相关与标记隔离通过 | Bridge 无需修改 |
| Qwen Code | 0.21.12，官方 `qwen serve` daemon | 2 个 session，4 回合 | 两个 session 探活、Bridge 重连、上下文延续、转录隔离全部通过 | 0.21 把 ACP 块嵌套到 `data.update`；v0.32.3 同时读取新旧事件结构。测试端点另需 Qwen 原生 `customHeaders.x-api-key` 配置，这不是 Bridge 权限或身份状态 |
| DeepSeek Harness | npm `@deepseek-ai/dsh` 0.1.0-rc.6，官方 `dsh web` RPC | 2 个 session，4 回合 | `session.history` 探活、Bridge 重连、上下文延续与历史隔离全部通过 | 授权测试端点通过 Harness 自带 `llm-pi-ai` Anthropic route 使用；Linux npm 包的 `node-pty` 没有预编译件，在临时目录编译后通过，未安装系统 RPM。仓库参考源码为 rc.5，故没有把 rc.6 行为误写成 rc.5 源码证据 |

## 测试方法

每项测试都使用同一套最小验收步骤：

1. 在独立 home、workspace 和 loopback 端口启动真实产品，禁用遥测；不使用生产数据库。
2. 通过产品原生 API 创建两个不同的 session，并为每个 session 建立独立 Bridge binding。
3. 调用 adapter 的只读 `probe()`，确认探测的是绑定 session，不以 worker PID 冒充在线。
4. 分别写入随机 A/B 标记并执行真实模型回合；随后重建 Bridge 客户端并在支持该场景的产品上读取上一轮上下文。
5. 读取产品原生历史或转录，断言 A 中没有 B 标记、B 中没有 A 标记。
6. 终止测试进程，删除临时 home、安装目录和凭据，再核对端口、进程、shell 配置与系统 RPM 状态。

## 配置与权限边界

- Bridge binding 只保存产品类型、稳定端点、原生 session 和 loopback/file transport，不保存 `full-access`、`read-only` 或任何可推断的权限副本。
- DeepSeek Harness 本轮以其自身进程的 `DSH_PERMISSION_MODE=danger-full-access` 运行；Qwen、Pi、Hermes 与 OpenCode 同样服从各自 runtime 的本机配置。聊天室文字不能提升这些权限。
- Qwen 的测试端点兼容性由 Qwen 自己的 provider 配置解决：非 Anthropic 官方域名时，0.21 默认使用 Bearer Token；该端点要求 `x-api-key`，因此在 `modelProviders.anthropic[].generationConfig.customHeaders` 中提供该头。Bridge 不接收也不保存模型 API key。
- DeepSeek Harness 的公开 npm 版本比本机参考源码新一个候选版本。本轮运行结果证明 rc.6 npm 发行物可接入，不代表对 rc.6 源码做了本地逐行审查。

## 发布证据

- Pi 新 session 修复：`4249f1b776695f6a8a85ff6c7951356800979c20`，远端 CI `31860975104` 全部通过，v0.32.2 已执行快照、恢复演练和 viewer-only 部署。
- Qwen 0.21 SSE 修复：`14371d49e4638b2010e9f07bc52e41e82629f45c`，远端 CI `31862579018` 全部通过；快照 `snapshot-20260815T034547Z-v0.32.3-e04441e9` 校验 14 个工件、恢复演练通过，部署前后 35 个 Agent PID 不变。
- OpenCode、Hermes 与 DeepSeek Harness 的真机测试没有暴露 Bridge 代码缺陷，因此没有制造额外 fallback 或产品专属影子实现。
