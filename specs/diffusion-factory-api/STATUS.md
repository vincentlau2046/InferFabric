# STATUS

- 2026-11-14: 初版 spec 完成，曾标记 READY，但未进入实现。
- 2026-11-15: 完成一次独立反向审计：发现 `type: diffusion` 会污染现有 lifecycle/GPU state，Videos API 主契约与标准 multipart/状态语义未完全对齐，M1/M2 过宽，GPU lease、恢复幂等、ComfyUI 外部作业竞态未闭合。
- 2026-11-15: 根据审计完成全量修订。主要变化：
  - 扩散模型从可部署 ModelConfig 改为独立 `diffusion.d/*.yaml` descriptor catalog；
  - 明确 ComfyUI 是唯一宿主，descriptor 不进入 active_services；
  - API 拆为 OpenAI-compatible core 与 iff extensions；Videos 以 multipart 为规范主路径；
  - 增加 generation GPU lease、peak_vram、公平调度、外部作业弱保证和崩溃恢复状态机；
  - M1 拆为 M0/M1a/M1b，M2 单独实现视频；
  - 修正迁移版本为 control-plane IFFDB v011，增加协议、生命周期、恢复和真机验收门禁。
- 当前状态：**NEEDS_REVIEW，等待用户审核修订版；未实现。**
