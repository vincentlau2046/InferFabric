# IFF Product Vision

InferFabric 是专为 NVIDIA Blackwell（RTX 5090 等 RTX 50 系列）本地 AI 工作站设计的**推理平台 + 统一 Gateway**。

- 推理平台：模型即插件（YAML），9 引擎适配器，三态 GPU 状态机（idle/exclusive/shared），进程生命周期管理（start/stop/switch/sleep/wake）
- 统一 Gateway：有状态网关（区别于无状态转发代理）——OpenAI + Anthropic 双协议统一入口 `:8999`，本地模型 + 9 云端预设同一路由；Auth/RateLimit/Cache/Log/Metrics/Anomaly 治理链（v4.6.x 引入）
- Blackwell 针对性：NInfer 引擎 NVFP4 权重 + NVFP4 KV cache（SM 12.x 专属加速路径）；非 Blackwell 走 vLLM/SGLang/Ollama 路径
