# 能力包平台适配 — STATUS

| 项 | 状态 | 备注 |
|---|---|---|
| 调查 | ✅ 2026-11-20 | 运行态核实：ninfer/sglang docker 现役（PM=container lifecycle）、vLLM 双形态、llama.cpp 原生；pack schema 无 platform 字段；sandbox.py=Mac SBPL PK0 |
| 用户裁决 | ✅ 2026-11-20 | 混合方案（manifest platform 字段+过滤+tab 分组）；平台框架+隔离底座先定，asr/tts 随后；向 Mac 端提案 |
| spec.md | ✅ 2026-11-20 | §1–§5：platforms 字段 / substrate 矩阵 / 模板注册表 / 准入 / 呈现 / 首批包 |
| Mac 提案 issue | ⬜ | Linux 端发起，征询 darwin-arm 对接意见（审阅性意见不阻塞 PR-A） |
| PR-A 实现 | ⬜ | schema+准入+单测 |
| PR-B 实现 | ⬜ | docker 模板族+首批包（llm/vl/embedding/rerank） |
| PR-C 实现 | ⬜ | dashboard 平台分组+浏览器门禁 |
| 全量回归 / 冒烟 / 干净树复验 / cross-review | ⬜ | 每批各自执行 |
