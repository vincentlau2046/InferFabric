# vLLM-Omni 文生图试点 — STATUS

| 项 | 状态 | 备注 |
|---|---|---|
| 调研 | ✅ 2026-11-20 | vllm-omni=官方 omni-modal 框架（T2I/早期）；sm_120 需 CUDA 12.8+（可能 nightly）；VRAM Flux~16GB / Qwen-Image~23GB 均 32GB 可跑 |
| 用户裁决 | ✅ 2026-11-20 | 双试点：Flux.1-dev + Qwen-Image；与 ComfyUI 并存 |
| spec.md | ✅ 2026-11-20 | docker 载体锁定版本耦合；P1 手动→P2 接线→P3 pack 化；逐段裁决 |
| P1 手动验证 | ⬜ | Flux.1-dev 先行；产出生图冒烟+镜像 tag/显存/延迟记录 |
| P2 平台接线 | ⬜ | 待 P1 数据 + 用户裁决 |
| P3 pack 化 | ⬜ | 依赖 packs-platform-adaptation PR-B docker 模板族 |
