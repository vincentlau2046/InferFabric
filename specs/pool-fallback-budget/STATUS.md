# 池回退预算修复 — STATUS

| 项 | 状态 | 备注 |
|---|---|---|
| 事故诊断 | ✅ 2026-11-24 | fabric-max 组 503 `pool_unavailable`×549（11-15–11-24）：主选 5xx/429 的同目标重试与候选回退共用 `max_attempts` 预算，`max_attempts=2`（配置默认）下重试即耗尽预算，候选 2/3 从未尝试。journal 930 条 attempt 链中 870 条为「530→530 同目标重试后终止」形态；候选 3（tathr-glm/glm-5）同窗为 pool:lite 服务 1379 次成功、触达 0 次。慢失败（SSL 握手超时）烧满 60s deadline 为同效次要路径 |
| 方案评审 | ✅ 2026-11-24 | fresh-context subagent 对抗评审（计划级）：3 important（probe 钉时钟零检出力 / 「向后兼容」假命题 / strict CAS 整份配置影响面未承认）+ 4 nit 全部采纳修入计划；评审确认死循环不可能、重试上界 2×max_attempts、既有测试逐个推演通过 |
| 后端实现 | ✅ | ① `pool_forward.run_attempts`：`attempts` 计数器 → `tried: set[str]` 首试槽位记账，预算检查改为 `cand not in tried and len(tried) >= max_attempts`（位置保持在 claim_probe 前）；同目标重试不计槽位（retried 集仍限每候选全程 1 次）；签名不变，dispatch 零改动。② `config._parse_pool`：`max_attempts < 候选数` 解析层**自动扩展**有效槽位至全覆盖 + 告警（用户裁决 2026-11-24：修复不得绑定配置迁移，strict 拒绝设计被推翻）；CAS 写入与磁盘存量同语义；`flip_pool_enabled` docstring 同步 |
| TDD | ✅ | Task 1：6 例新钉（4 红 2 回归钉）全 GREEN，99 passed（pool_forward + final_fixes + transport_contract）；Task 2：3 例新钉（1 红 2 边界钉）全 GREEN，42 passed（config + registry） |
| 版本 | ✅ 7.11.3 | patch（自主升级）；README 版本历史行 |
| 全量回归 | ✅ 2026-11-24 | unit + integration 2739 passed（终审修复后终态；夹具对齐后曾 2737×2 连续全量）。首跑 8 失败已归因闭合：6 例=strict CAS 正确拒绝夹具旧形态（夹具更新为 max_attempts=3 合法形态，测试意图不变）；1 例=新语义行为钉（max_attempts=1 + 503 同目标重试现会执行，预置两次 503）；1 例=瞬态（packs 探针 mock urlopen 窗口疑遭跨测试遗留线程干扰，隔离通过/修复后全量通过，未复现） |
| 整分支终审 | ✅ 2026-11-24 | fresh-context subagent 评审：0 Critical / 4 Important / 3 Minor，verdict「With fixes」。Important-1 已修（TDD）：包卸载校验闸前移——purge 在一切删除之前，校验拒绝 → 解除路由禁用 + 可操作 400 零删除副作用；legacy 卸载响应新增 pool_purge_error。Important-3/4 文档面已收口（candidate-attempts 口径 + deadline 非硬端到端界限）。Important-2 随自动扩展设计**消解**（无 fail-closed 态可修，原裁决作废）；3/4 传输面裁决不修（见 ledger Ruling：既有传输层行为 + PR-14）。3 Minor 缓办 |
| 合并/部署 | ⏳ 待用户确认 | 净 diff 核对完成 → 用户确认 ff-merge 到生产 main → 生产部署另批授权。**部署零配置变更**：解析层自动扩展自愈 2 槽/3 候选存量形态（max/flash 两池），生产 YAML max_attempts 对齐为可选日常整理 |
| 明确不做 | ✅ | 慢失败烧 deadline 的传输层优化；404 隔离/401 冷却/incompatible 等既有动作表条目；直连路径（PR-14 冻结） |

## 执行记录

- 计划：`docs/superpowers/plans/2026-11-24-pool-fallback-budget.md`（含 §根因与语义定义 与评审记录全溯源）
- 行为变更提示：`max_attempts=1` 的池，主选同目标重试由旧版「被预算截断（不发生）」变为「执行一次」（对齐动作表文档本意）——部署说明需含此条。
