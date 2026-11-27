# STATUS — dashboard-ux-v3

- **阶段（2026-11-27）**：用户批准设计及样机定稿，**PR #83 已合并**（`03cabb1`）。三轮裁决：模型池独立为「路由」，最终导航为六区；模型页保留本地/云端清单与在池提示。
- **产物**：[设计](design.md)、[可点击样机](preview.html)（示例数据）、[A–F 实施计划](../../docs/superpowers/plans/2026-11-27-dashboard-ux-v3-impl.md)、[接口与 UI 对接契约](interface-contract.md)（后两者随草稿 PR #86 审阅）。
- **C 期补充裁决（2026-11-27）**：用户确认平台适配层供数、UI 按能力消费；Mac 展示已可采集的统一内存和压力，NVIDIA 展示 GPU 遥测，本期不新增 Mac GPU 探针。
- **下一步**：计划与契约审阅后，由 `iff-main-imp` 从最新 main 发起 Phase A Draft PR，完成双端开工握手，再 sandbox-first 实施。B1 的额外读侧文件仍待裁决；未经批准不扩大定稿范围。
- **未开始**：A–F 任何生产代码、运行验收或部署。
