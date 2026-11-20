# vLLM-Omni 文生图试点 — Flux.1-dev + Qwen-Image（spec）

状态：草案（2026-11-20，用户已裁决双试点，待终审后进入 P1）

## 0. 裁决记录

- 2026-11-20 用户裁决：立实验 spec 试点，**Flux.1-dev 与 Qwen-Image 两个都做**。
- 定位：验证「docker + 非 LLM 引擎 + 模型」包形态；与 ComfyUI **并存不替换**（ComfyUI=工作流工具，vLLM-Omni=OpenAI 兼容 API，可接入网关统一路由/池路由/指标）。

## 1. 调研结论（2026-11-20）

- [vllm-project/vllm-omni](https://github.com/vllm-project/vllm-omni)：vLLM 官方生态 omni-modal 推理框架；T2I 支持 Qwen-Image / Flux.1-dev / SD3.5 等；OpenAI 兼容服务；Apache-2.0；早期项目（~310 stars，文档不完善，vLLM pin 移动快）。
- Blackwell sm_120：需 PyTorch 2.7+ / CUDA 12.8，通常 vLLM nightly 或源码编译；社区有 5090 跑通案例（折腾型）。
- VRAM（5090D 32GB）：Flux.1-dev ~16GB ✅；Qwen-Image 20B ~23GB ✅（均独占形态跑）。
- 与主栈隔离：独立容器，**不触碰 vLLM 0.24 主推理栈约束**（该约束针对主栈 conda env）。

## 2. 方案

- **载体**：官方 `vllm/vllm-omni` Docker 镜像（DockerHub），容器内含匹配的 vLLM 版本——版本耦合风险锁死在镜像内，宿主零污染。
- **P1 手动验证（不接线）**：docker run 单模型起服务，冒烟生图（文生图 prompt → 图片落盘），记录镜像 tag / 启动参数 / 显存峰值 / 首图延迟。先 Flux.1-dev（体积小、风险低），后 Qwen-Image。
- **P2 平台接线**：`engine_adapter/omni_t2i.py`（新 adapter，health + 生图代理）或复用 vllm adapter 变体——P1 结束后按实际 API 形状定；models.d YAML（`type: omni_t2i`，gpu_role=exclusive）；网关路由（/v1/images/generations 透传 + chat 路由不劫持）。
- **P3 pack 化**：依赖 packs-platform-adaptation PR-B 的 `docker-*` 模板族，`t2i.omni-flux1dev` / `t2i.omni-qwen-image` 入 catalog（capability 扩 `t2i`，第二批语义）。
- **准入**：P2 完成前不入 catalog、不进默认可见列表——实验期失败不污染产品路径。

## 3. 风险与预案

| 风险 | 预案 |
|---|---|
| 官方镜像无 sm_120 wheel | P1 直接失败项记录 → 降级为「宿主 CUDA 12.8 env 自建镜像」评估，仍失败则试点关闭（诚实记录，不留半成品） |
| 镜像体积大（vLLM 全家桶 ~15GB+） | 磁盘预算检查进 P1 前置；`ensure_image_pulled` 语义复用 |
| 项目早期 API 变动 | P1 记录锁定 tag + digest；升级属人工动作不自动 |
| GPU 状态机交互 | t2i 容器按 exclusive 接入现有 idle→exclusive 状态机；与 ninfer 互斥语义不变 |

## 4. 验收

- P1：两模型各产出 ≥1 张生成图 + 启动参数/显存/延迟记录（写入本 spec STATUS）。
- P2：经 :8999 网关发起生图请求 200；不触碰主 vLLM 0.24 栈（conda env 清单不变）。
- P3：两 pack 在 dashboard 能力包页可见可装载（linux-nvidia 平台键）。

## 5. 分批与门禁

P1 → 用户看数据再决定 P2 是否立项（实验性定位，逐段裁决）；P2/P3 各自全量回归 + 干净 worktree 复验 + cross-review。
