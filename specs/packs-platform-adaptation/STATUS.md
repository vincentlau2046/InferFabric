# 能力包平台适配 — STATUS

| 项 | 状态 | Owner / Next action |
|---|---|---|
| 方向 | ✅ 2026-11-20 | 已确认：共用目录和 Dashboard、平台各用适配运行时；先落隔离契约和真实试点，后扩 ASR/TTS。 |
| 阶段 0 · 修订规格 | ✅ 沙箱文档 | [spec.md](spec.md) 取代先上平台字段的旧方案；不把旧包当通配，不把探针当隔离证据。 |
| 阶段 0 · Mac 对齐 | ⏳ 已更新提案 | [#23 更新](https://github.com/vincentlau2046/InferFabric/issues/23#issuecomment-7041823042)；Mac 待确认平台键、实际 provider 与原生试点。 |
| 阶段 1 · 实施计划 | ✅ 已执行（Task 1–5 + 终审修复） | [计划](../../docs/superpowers/plans/2026-11-20-packs-runtime-pilot.md)；sandbox 分支 `feat/packs-runtime-pilot` 提交 `7a5f229..ca427c0`（Task 1 平台探针+platforms 声明 / Task 2 三维准入+目录决策预览 / Task 3 受管运行时契约 / Task 4 NInfer 受管 launcher / Task 5 BGE CPU embedding）；终审修复 `6e6d746`（F1 容器所有权绑定 / F3 fail-closed 默认 / F4 already_active 真实态+锁内 idle 复核 / F5+F6 持久化所有权与设备域权威 / F7 SM 下限）。 |
| 旧实现草稿 | ⏸ 未提交 | `schema.py`、`installer.py`、`test_packs_platform.py` 原样保留，不作为新基线；先前专项结果 11 通过、2 失败，未修复或重跑。 |
| 阶段 1 · 验证 | 🟡 代码门全绿，真机门待授权 | ✅ 全量离线回归 **2651** 通过（tests/unit + tests/integration，2026-11-21，含终审 17 个新测试）；✅ fresh-context cross-review 完成（1 Critical + 7 Important 修复入 `6e6d746`，4 Minor 已记账缓议；裁定与缓议清单见 SDD ledger）；✅ 干净提交树复验（worktree @ `6e6d746` 全量 2651 通过）；✅ 独立代理冒烟（:18999 临时实例 /health、/api/snapshot、/admin/packs/catalog 全 200，目录决策元数据诚实）；⏸ 真机 BGE/NInfer 运行验证需授权窗口（含 TTFT/吞吐/峰值 VRAM 基线对比，健康探测不构成性能对等证据）；⏸ ManagedRuntime 生产装配注入随合并/部署授权落地（当前未注入 = 受管包 fail-closed 拒绝）。 |
| 阶段 2 · Dashboard | ⬜ 等真实运行证据 | 共用展示规则，双平台真实浏览器验证。 |
| 阶段 3 · 扩目录 | ⬜ 后续 | SGLang/vLLM/rerank 与正式 ASR/TTS；vLLM-Omni 双试点仍独立。 |
