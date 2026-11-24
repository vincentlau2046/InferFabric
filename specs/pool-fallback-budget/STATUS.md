# 池回退预算修复 — STATUS

| 项 | 状态 | 备注 |
|---|---|---|
| 事故诊断 | ✅ 2026-11-24 | fabric-max 组 503 `pool_unavailable`×549（11-15–11-24）：主选 5xx/429 的同目标重试与候选回退共用 `max_attempts` 预算，`max_attempts=2`（配置默认）下重试即耗尽预算，候选 2/3 从未尝试。journal 930 条 attempt 链中 870 条为「530→530 同目标重试后终止」形态；候选 3（tathr-glm/glm-5）同窗为 pool:lite 服务 1379 次成功、触达 0 次。慢失败（SSL 握手超时）烧满 60s deadline 为同效次要路径 |
| 方案评审 | ✅ 2026-11-24 | fresh-context subagent 对抗评审（计划级）：3 important（probe 钉时钟零检出力 / 「向后兼容」假命题 / strict CAS 整份配置影响面未承认）+ 4 nit 全部采纳修入计划；评审确认死循环不可能、重试上界 2×max_attempts、既有测试逐个推演通过 |
| 后端实现 | ✅ | ① `pool_forward.run_attempts`：`attempts` 计数器 → `tried: set[str]` 首试槽位记账，预算检查改为 `cand not in tried and len(tried) >= max_attempts`（位置保持在 claim_probe 前）；同目标重试不计槽位（retried 集仍限每候选全程 1 次）；签名不变，dispatch 零改动。② `config._parse_pool`：strict（CAS 写入）拒绝 `max_attempts < len(candidates)`（可操作文案）；磁盘存量载入 warning 容忍（与 MAX_ATTEMPTS_RANGE 先例同构）；`flip_pool_enabled` docstring 同步（整份配置校验可拒绝 enabled 翻转） |
| TDD | ✅ | Task 1：6 例新钉（4 红 2 回归钉）全 GREEN，99 passed（pool_forward + final_fixes + transport_contract）；Task 2：3 例新钉（1 红 2 边界钉）全 GREEN，42 passed（config + registry） |
| 版本 | ✅ 7.11.3 | patch（自主升级）；README 版本历史行 |
| 全量回归 | ✅ 2026-11-24 | unit + integration 2737 passed ×2（连续两次全量）。首跑 8 失败已归因闭合：6 例=strict CAS 正确拒绝夹具旧形态（夹具更新为 max_attempts=3 合法形态，测试意图不变）；1 例=新语义行为钉（max_attempts=1 + 503 同目标重试现会执行，预置两次 503）；1 例=瞬态（packs 探针 mock urlopen 窗口疑遭跨测试遗留线程干扰，隔离通过/修复后全量两次通过，未复现） |
| 合并/部署 | ⏳ | Task 4：净 diff 核对 → 用户确认 ff-merge → 生产部署另批授权（先调生产 YAML max_attempts ≥ 候选数，后加载新代码；顺序颠倒的已接受后果 = 窗口期内 dashboard CAS 写入 400 fail-closed） |
| 明确不做 | ✅ | 慢失败烧 deadline 的传输层优化；404 隔离/401 冷却/incompatible 等既有动作表条目；直连路径（PR-14 冻结） |

## 执行记录

- 计划：`docs/superpowers/plans/2026-11-24-pool-fallback-budget.md`（含 §根因与语义定义 与评审记录全溯源）
- 行为变更提示：`max_attempts=1` 的池，主选同目标重试由旧版「被预算截断（不发生）」变为「执行一次」（对齐动作表文档本意）——部署说明需含此条。
