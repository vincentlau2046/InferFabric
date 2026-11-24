# forwarder committed 流超时解耦 — STATUS（7.11.4）

| 项 | 状态 | 备注 |
|---|---|---|
| 事故诊断 | ✅ 2026-11-24 | peer 会话（host-website-13）定位 + 本会话独立复核：池路径把 deadline 剩余预算当 urlopen/HTTPConnection timeout 下发——该参数是 socket 级「任意两次 recv 的最大静默间隔」，非总时长。tathr headers 秒回 → committed=True（§6.4 不可重放）→ 大请求（compact 20 万 token）prefill/长推理静默 > remaining → resp.read() TimeoutError → 流截断。request_log 实证：11-20 起 90 条 error=upstream_http_200+tokens_in=0，62 条落 55–95s 时长窗，69 条集中 pool:max/cloud:tathr/gpt-6.2-sol |
| 实现方案 | ✅ | peer B 方案（用户授权）：committed（headers 已到、即将转发客户端）后 socket 超时重置为独立 idle timeout——云端 = provider_cfg.timeout（tathr 600s），本地 Anthropic = 既有非池路径 300s；committed 前的 connect/headers 阶段仍用剩余预算（排队堆积防线不变）；§6.4 语义零变化。timeout=None（非池路径）行为原样（PR-14 其余部分仍冻结）。方案 A（改生产 YAML deadline_ms）不采纳——用户裁决「修复不得绑定配置迁移」 |
| TDD | ✅ | `tests/unit/proxy/test_forwarder_stream_idle_timeout.py` 2 例，真实本地 HTTP server（headers 即发 + body 静默 2.5s > 剩余 1.0s、< idle 8s）：云端/本地两路径旧行为 RED（upstream_stream_failed_after_commit / local_stream_failed_after_commit——正是生产错误串）→ GREEN |
| PR | ⏳ | 离线分支 `fix/7.11.4-forwarder-stream-idle-timeout`，**不合入生产**；Mac 端实施/评审（用户 2026-11-24 指派），合入仍统一 Linux 端 |
| 明确不做 | ✅ | Retry-After 透传（Responses 桥接）；Responses wire-call 独立预算；准入后剩余时间重算——均为最终评审裁决缓办项 |

## 执行记录

- RED→GREEN 于 sandbox 分支；`_relax_post_commit_timeout` helper（CPython 内部结构拿不到时静默退回旧行为）
