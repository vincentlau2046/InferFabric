# 能力包平台适配 — STATUS

| 项 | 状态 | Owner / Next action |
|---|---|---|
| 方向 | ✅ 2026-11-20 | 已确认：共用目录和 Dashboard、平台各用适配运行时；先落隔离契约和真实试点，后扩 ASR/TTS。 |
| 阶段 0 · 修订规格 | ✅ 沙箱文档 | [spec.md](spec.md) 取代先上平台字段的旧方案；不把旧包当通配，不把探针当隔离证据。 |
| 阶段 0 · Mac 对齐 | ⏳ 已更新提案 | [#23 更新](https://github.com/vincentlau2046/InferFabric/issues/23#issuecomment-7041823042)；Mac 待确认平台键、实际 provider 与原生试点。 |
| 阶段 1 · 实施计划 | ✅ 已写，待审阅 | [NInfer Docker / BGE CPU 双路径](../../docs/superpowers/plans/2026-11-20-packs-runtime-pilot.md)；Linux 只在 sandbox 中恢复实现。 |
| 旧实现草稿 | ⏸ 未提交 | `schema.py`、`installer.py`、`test_packs_platform.py` 原样保留，不作为新基线；先前专项结果 11 通过、2 失败，未修复或重跑。 |
| 阶段 1 · 验证 | ⬜ 未执行 | 离线回归、独立代理冒烟、cross-review、干净提交树；真机 GPU 需要明确授权与空闲窗口。 |
| 阶段 2 · Dashboard | ⬜ 等真实运行证据 | 共用展示规则，双平台真实浏览器验证。 |
| 阶段 3 · 扩目录 | ⬜ 后续 | SGLang/vLLM/rerank 与正式 ASR/TTS；vLLM-Omni 双试点仍独立。 |
