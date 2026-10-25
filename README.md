# InferFabric — Blackwell 本地 AI 工作站的推理平台与统一 Gateway

> **为 NVIDIA RTX 5090 等 Blackwell 系列显卡的本地 AI 工作站设计——把单卡 GPU 变成一台统一推理服务器：推理平台 + 统一 Gateway，模型即插件，本地+云端统一，一个 API 管所有。**
>
> NVIDIA Blackwell（RTX 5090 / 5090D · NVFP4）· 推理平台 + 统一 Gateway · 模型即插件 · 三态 GPU 状态机 · 8 引擎适配器 · macOS Dashboard · 9 云端预设 · 双协议路由

---

## 定位

**InferFabric 是专为 NVIDIA RTX 5090 等 Blackwell 系列显卡的本地 AI 工作站设计的推理平台与统一 Gateway 平台。**

这里的 Gateway 是有状态网关——不同于无状态转发代理，它管理模型进程生命周期（启动/停止/切换/睡眠/唤醒），不是 vLLM 包装器（多引擎适配器抽象），也不只是本地推理工具（统一管理本地+云端+多模态）。它把你的 GPU 从零散的推理环境变成一个**可编程的统一推理服务**，OpenAI + Anthropic 双协议统一入口 `:8999`。

**Blackwell 针对性**：NInfer 引擎的 NVFP4 权重 + NVFP4 KV cache 是 Blackwell 专属加速路径（SM 12.x）——RTX 5090 32GB 可跑 27B NVFP4 大模型（峰值显存 ~30.5GB）+ 600K+ token 级 KV 池 + MTP 投机解码；非 Blackwell 显卡可走 vLLM / SGLang / Ollama 路径（INT8/FP8 KV）。

核心差异化：

| 维度 | InferFabric | 替代方案 |
|---|---|---|
| 单卡多模型 | 三态 GPU 状态机（idle/exclusive/shared），自动切换，永不 OOM | 手动停旧启新，或一个模型占死 GPU |
| API 入口 | OpenAI + Anthropic 双协议同一端口 `:8999` | 每个引擎一个端口、一种格式 |
| 模型定义 | 一个 YAML = 一个模型（零代码） | 写启动脚本、配 conda env |
| 推理引擎 | 9 种适配器：vLLM/NInfer/SGLang/Ollama/Ollama.cpp/ComfyUI/TTS/ASR/OllamaDaemon | 一种引擎一套工具链 |
| 云端代理 | 9 个预设 + 自动发现 + 同一 API 路由 | 每个云商一套 SDK/Key 管理 |
| 治理 | 重试/熔断/缓存/限流/超时/负载均衡/异常采集/全链路日志 | 无（直连引擎） |
| 管理界面 | macOS 风格 Dashboard（监控/切换/部署/OpenAPI 阅览） | 终端 curl 或 YAML 编辑 |

**设计哲学**：吃自己的狗粮。InferFabric 把本地模型暴露为 Anthropic 兼容 API——你可以把 `ANTHROPIC_BASE_URL` 指回本地 `localhost:8999`，用 Claude Code 驱动开发同时验证推理网关的兼容性和性能。反馈闭环即产品验证。**自 5.8.0 起是一条命令加 `--async` 即可启用生产级 aiohttp 异步引擎。**

---

## What It Solves

**The Problem**: Running multiple LLM models on a single GPU is painful. Model switching is manual. Cloud API keys are scattered in config files. Each client needs its own backend configuration.

**InferFabric solves this**:
- **Model switching without OOM** — Three-state GPU (idle/exclusive/shared) with safe transitions and health checks
- **One API, any model** — Every model—local vLLM/NInfer, local ollama.cpp, or cloud OpenAI/Anthropic—is accessed through the same `/v1/chat/completions` or `/v1/messages` endpoint
- **API keys never in plaintext** — `${ENV_VAR}` auto-conversion, secrets stored in `chmod 600` file
- **Dashboard, not YAML editing** — macOS sidebar dashboard for model switching, monitoring, chat testing, and cloud provider management
- **Dual-mode proxy** — Threaded (stdlib, default) or async (aiohttp, `--async`), both with zero forwarding-core change

---

## Architecture

### 分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│  CLI (./iff)                     Dashboard (:8999/)             │
├─────────────────────────────────────────────────────────────────┤
│  Proxy (:8999) — OpenAI + Anthropic 双协议                       │
│  ├─ 线程模式 (http.server, 默认)                                  │
│  └─ 异步模式 (aiohttp, --async, v5.8.0+ PR-19)                  │
│  ├─ Auth      → API key 验证                                    │
│  ├─ Cache     → temperature=0 响应缓存 (cachetools LRU)          │
│  ├─ RateLimit → DualGateLimiter (RPM + 并发)                     │
│  ├─ Anomaly   → AnomalyCollector 环形缓冲 (R9)                   │
│  ├─ Telemetry → RequestLog + Prometheus /metrics (R6)           │
│  └─ Replicas  → ReplicaSelector least_busy/round_robin (R7)    │
├─────────────────────────────────────────────────────────────────┤
│  Engine Adapter Layer — 模型类型无关的启动/停止/健康检查           │
│  ┌────────┬──────┬────────┬──────┬──────┬──────┬─────┬─────┐   │
│  │ vLLM   │NInfer│ SGLang │Ollama│ cpp  │Comfy │ TTS │ ASR │   │
│  └────────┴──────┴────────┴──────┴──────┴──────┴─────┴─────┘   │
├─────────────────────────────────────────────────────────────────┤
│  Process Managers — Docker / conda / subprocess 生命周期         │
├─────────────────────────────────────────────────────────────────┤
│  GPU 状态机 — idle → exclusive/shared → idle                    │
│  HealthMonitor — 去耦合健康检查 + 状态修正                       │
│  Watchdog — 模型异常自动重启                                     │
└─────────────────────────────────────────────────────────────────┘
```

### 引擎适配器（Engine Adapter）

每个 `type` 映射一个 `engine_adapter/*.py`，在 `__init__.py` 中注册：

| 引擎类型 | 适配器 | 启动方式 | 典型用途 |
|---------|--------|---------|----------|
| `vllm` | `vllm.py` | conda env + subprocess | 大模型推理（LLM/VL） |
| `ninfer` | `ninfer.py` | Docker container | NInfer 优化推理（NVFP4） |
| `sglang` | `sglang.py` | Docker/conda | SGLang 推理（RadixAttention） |
| `ollama` | `ollama.py` | HTTP to Ollama daemon | Ollama 模型 |
| `ollama_cpp` | `ollama_cpp.py` | subprocess (GGUF) | CPU 嵌入/重排序 |
| `comfyui` | `comfyui.py` | conda env + subprocess | 图像生成 |
| `tts_server` | `tts.py` | conda env + subprocess | TTS 语音合成 |
| `asr_server` | `asr.py` | subprocess | ASR 语音识别 |

每个适配器实现 `EngineAdapter` 接口（`base.py`）：
```python
def start(self, model: ModelConfig) -> int: ...
def stop(self, model: ModelConfig) -> bool: ...
def health_check(self, model: ModelConfig) -> bool: ...
def validate(self, config: dict) -> list[str]: ...
```

### 三态 GPU 状态机

```
idle ─→ exclusive   (one heavy model, full GPU)
  │
  └──→ shared       (many small models, coexist)
         │
         └──→ idle   (return to idle anytime)
```

Local models operate in one of three GPU modes. The gateway enforces safe transitions—you can't accidentally start two exclusive models.

The state machine is computed from actual service processes rather than a persisted flag, so **state never drifts**. HealthMonitor decoupled from state reconciliation eliminates race conditions.

### 代理路由链

所有请求到达 `:8999` 后经过统一的路由链：

1. **Auth** — API key 验证（YAML 密钥文件）
2. **Pool routing**（v7.0.0）— `iff/<pool>` 逻辑名 → 池选择（协议过滤 + 优先级回退，见[逻辑模型池](#逻辑模型池logical-model-poolsv700)）；无 `model_pools.yaml` 时直通
3. **Cache** — ResponseCache 精确匹配（`temperature=0` 且非流式）
4. **SWITCHING Guard** — 切换中模型 → 503 + Retry-After
5. **Local routing** — `find_model_by_served_name()` → 活跃模型直接转发，非活跃 auto-switch
6. **Multi-replica** — ReplicaSelector (`least_busy` / `round_robin`)
7. **Cloud routing** — CloudDiscovery → 零协议透明代理
8. **Unknown model** — 显式 404 + AnomalyEvent（无静默回退）

上游路径归一化（v5.8.0+，由 YAML `type` 字段自动驱动）：

| 入站路径 | 归一化目标 | 说明 |
|---------|-----------|------|
| `/v1/chat/completions` | `/v1/chat/completions` | OpenAI 标准 |
| `/v1/completions` | → `/v1/chat/completions` | 别名 |
| `/api/chat` | → `/v1/chat/completions` | Ollama 兼容别名 |
| `/api/generate` | → `/v1/chat/completions` | Ollama 兼容别名 |
| `/v1/messages` | `/v1/messages` | Anthropic 标准 |

### 双引擎模式（v5.8.0+）

| | 线程模式（默认） | 异步模式（`--async`） |
|---|---|---|
| 底层 | `http.server.ThreadedHTTPServer` | aiohttp 3.14.3 (`_deps/`) |
| 并发模型 | 每请求一线程 | 事件循环 + ThreadPoolExecutor(32) |
| 流式 SSE | 手工 chunked 分帧 | StreamResponse 原生分帧 |
| Connection | `Connection: close` | keep-alive（aiohttp 自管） |
| 回退 | — | 不传 `--async` 即回退线程版 |

---

## 模型即插件（YAML 插件系统）

添加模型 = 在 `models.d/` 放一个 YAML 文件。零代码。零配置键。

> 📖 **权威模型配置文档 → `[docs 指针] [models.d/README.md](models.d/README.md)`**
> 这里维护：字段/YAML 模板规范、**端口登记表（唯一权威）**、ninfer/vllm 等各类型字段说明、**「新增/删除/修改 YAML 必须同步更新 README」的强制规则**。配置 `models.d/*.yaml` 前请先查阅。
> ⚠️ 模型配置或其文档一旦更改，必须同步 `models.d/README.md`，否则视为配置漂移。

### 通用字段

```yaml
name: my-model               # ✅ 必填，必须与文件名（不含扩展名）一致
description: "..."           # 可选，人类可读描述
type: vllm                   # ✅ 必填，引擎类型（见下表）
gpu_role: exclusive          # exclusive | shared | none
model_type: llm              # llm | vl | omni | ocr | aigc | embedding | rerank | infra | tts | asr
quantization: NVFP4          # 量化格式字符串
peak_vram_mb: 30000          # 峰值显存，用于 OOM 保护
typical_vram_pct: 0          # 典型显存占比（0-100，comfyui 用）
modality: text-vision        # 可选，推导自 model_type
replicas: []                 # R7: 多副本端口列表
startup_timeout: 300         # 启动超时秒数
```

### 引擎类型 YAML 参考

#### vLLM

```yaml
type: vllm
gpu_role: exclusive
vllm:
  model_dir: my-model-name       # models/ 下的目录名
  served_name: my-model          # API 路由用的模型名
  port: 8005                     # 监听端口
  conda_env: my-conda-env        # conda 环境名
  gpu_memory_utilization: 0.92   # GPU 显存利用率
  max_model_len: 131072          # 最大上下文长度
  max_num_seqs: 4                # 并行请求数
  kv_cache_dtype: fp8            # KV 缓存精度
  kv_offloading_size: 0          # KV offload 大小（GB）
  extra_flags: >-                # 额外 vLLM 参数
    --enable-prefix-caching
    --enable-chunked-prefill
  extra_env:                     # 额外环境变量
    FLASHINFER_DISABLE_VERSION_CHECK: "1"
  model_id: my-model             # Docker multi-model 时的模型 ID
```

#### NInfer

```yaml
type: ninfer
gpu_role: exclusive
ninfer:
  served_name: Qwen38-27B-TXT
  weight_path: ~/models/ninfer-nvfp4/model.ninfer  # 权重路径
  docker_image: ninfer:auto                          # Docker 镜像
  port: 8007
  container_name: iff-ninfer-my-model
  max_context: 204800           # 最大上下文
  kv_capacity: 0                # KV 容量（0 = auto）
  kv_dtype: nvfp4               # KV 缓存精度
  weight_precision: NVFP4       # 权重精度
  max_concurrency: 4            # 最大并发
  default_max_tokens: 32000     # 默认最大输出 Token
  prefill_chunk: 4096           # Prefill 分块大小
  pending_timeout_ms: 600000    # 请求排队超时
  enable_mtp: true              # MTP 投机解码
  draft_tokens: 4               # 草案 Token 数
  enable_lm_head_draft: false   # LM Head Draft
  startup_timeout: 120
```

#### SGLang

```yaml
type: sglang
gpu_role: exclusive
sglang:
  model_dir: Muse-Glimmer-NVFP4
  served_name: muse-glimmer
  port: 8006
  conda_env: ""                 # 空 = 用 Docker
  docker_image: lmsysorg/sglang:dev-muse-glimmer
  mem_fraction: 0.90            # GPU 显存比例
  context_length: 163840        # 最大上下文
  max_running_requests: 8       # 最大并行请求
  cpu_offload_gb: 16            # CPU offload
  enable_lmcache: true          # 启用 LMCache
  extra_env:
    SGLANG_DISABLE_CUDA_GRAPH: "0"
```

#### Ollama

```yaml
type: ollama
gpu_role: shared
ollama:
  model_name: llama3.2          # Ollama 模型名
  port: 11434                   # Ollama 守护进程端口
```

> Ollama 需要先启动 `ollama_daemon` 基础设施服务。

#### Ollama.cpp

```yaml
type: ollama_cpp
gpu_role: none                  # CPU-only
ollama_cpp:
  model_path: ~/models/gguf/model.gguf  # GGUF 文件路径
  port: 11441
  threads: 8
  context_size: 8192
  gpu_layers: 0                 # GPU 层数（0 = 纯 CPU）
```

#### ComfyUI

```yaml
type: comfyui
gpu_role: shared
conda_env: comfyui              # 引擎通用字段
port: 8188
working_dir: ~/ComfyUI
health_url: http://localhost:8188/system_stats
health_check_timeout: 180
extra_flags: --enable-manager
```

> ComfyUI 不使用嵌套的 engine-specific 字典，字段在顶层。

#### TTS Server

```yaml
type: tts_server
gpu_role: shared
tts_server:
  conda_env: qwen3-tts
  port: 8880
  working_dir: ~/services/TTS-Server
  health_url: http://localhost:8880/health
  health_check_timeout: 180
  start_cmd: python -m api.main
  extra_env:
    TTS_BACKEND: official
    TTS_LOAD_ALL_MODELS: "true"
```

#### ASR Server

```yaml
type: asr_server
gpu_role: shared
asr_server:
  conda_env: sensevoice
  port: 8881
  working_dir: ~/services/funasr-asr
  health_url: http://localhost:8881/health
  health_check_timeout: 120
  start_cmd: funasr-server --model sensevoice --device cuda --port 8881
  extra_env:
    MODELSCOPE_CACHE: ~/models/funasr
```

#### Ollama Daemon（基础设施）

```yaml
type: ollama_daemon
gpu_role: none
ollama_daemon:
  port: 11434
  health_url: http://localhost:11434
  data_dir: ~/.ollama
```

> 这是一个基础设施服务，不消耗 GPU 资源，为 `type: ollama` 模型提供后端。

---

## 场景预设调优（iff tune）

为模型定义「场景」——命名的一组引擎参数预设，运行时一条命令（或 Dashboard 一次点击）切换。三层架构保证**模型 YAML 永不被改**：

```
models.d/<model>.yaml              ← 第 1 层：启动真相（default = 此值；对 tune 只读）
models.d/scenarios.yaml            ← 第 2 层：场景定义侧车（单一真相源，进 git）
~/.inferfabric/active_scenarios.yaml ← 第 3 层：应用层（机器本地，只由 tune 写入，带文件锁）
```

- **default 一等化**：default ≡ 模型 YAML 当前值。无应用条目 = default；`iff tune <model> default` 无条目且模型未运行 = no-op；有漂移（手改 YAML 后）= 清条目 + 重启使新值进容器。
- **启动即读**：`switch()` 部署非活跃模型前重读磁盘「YAML + 应用层」——长驻代理的内存不会启动旧场景。
- **展示读磁盘**：CLI / Dashboard / API 的「当前场景」一律直读应用层文件（`status().scenario_active`），不读代理内存。
- **单写者 + 文件锁**：应用层只由 `tune.apply` 写入（`fcntl.flock` 排他锁覆盖写文件 + 重启临界区），并发写者串行化；其余工具链只读。
- **kv_capacity 不是场景字段**：它是模型的固定物理 KV 池（Qwen38-27B-TXT=632000 / Qwen38-27B-VL=410000）。场景 C×W 池顶超出即「超卖」——预期行为（满载由引擎 preempt 兜底），`iff tune` 仅 ⚠ 提示、不阻塞。
- **draft>1 自动冒烟**：`draft_tokens > 1` 的场景应用后自动跑一条短请求冒烟，失败自动还原上一次配置。
- **场景变更事件日志**：每次生效的 apply（写应用层/重启/回滚，含失败路径）发一行 JSON 结构化事件 `[tune-event]`（from→to 场景、关键参数、池顶/超卖、MTP 冒烟、状态）——CLI 跑走 stdout、Dashboard 走 systemd journal；可按时间戳与 `/api/request_log`、指标日志 join，做「场景参数 × 指标」关联分析，持续优化部署参数。

### 当前场景定义（NInfer 双模型；TXT 2026-10-09 重校准，VL 维持 2026-09-26 值）

| 场景（使用档位） | Qwen38-27B-TXT（kv 池 632K） | Qwen38-27B-VL（kv 池 410K） |
|------|------|------|
| **short-ctx** 低延迟 | C6 · 131072 · MTP draft=2 · 超卖 25.0% | C5 · 131072 · MTP draft=2 · 超卖 40.5% |
| **small-batch** 顶窗批处理 | C3 · 262144 · MTP draft=3 · 超卖 18.0% | C2 · 262144 · MTP draft=3 · 超卖 16.1% |
| **big-batch** 长窗批处理 | C5 · 168000 · MTP draft=1 · 超卖 27.1% | C3 · 204800 · MTP draft=2 · 超卖 31.6% |

> 场景名 = 使用档位（并发/用途），**不代表窗口大小**（small-batch 反而是顶窗长档）。
> default = 模型 YAML 当前值：TXT C4 · 155000 · draft=1（池顶 620032，满载余量 1.9%，零抢占）；VL C4 · 204800 · draft=1（池顶 819200，超卖 50.0%）。
> 场景字段白名单：`max_concurrency / max_context / default_max_tokens / prefill_chunk / enable_mtp / draft_tokens`（越界自动钳制）。

### 用法

```bash
./iff tune                            # 列出所有模型及其场景
./iff tune Qwen38-27B-TXT             # 该模型场景清单 + 当前 live 值
./iff tune Qwen38-27B-TXT short-ctx   # 预览 diff → 确认 → 自动重启（~3-6s，在途请求 503 + Retry-After）
./iff tune Qwen38-27B-TXT big-batch --dry   # 只预览（不写盘、不重启）
./iff tune Qwen38-27B-TXT default     # 回到 YAML 基线（有漂移则重启生效）
```

Dashboard 推理页模型卡有 **⚙ 场景** 按钮：三阶段模态框（选场景 → diff/告警预览 → 应用），全走 `POST /admin/tune`（进程内，与 CLI 同一临界区），不直接改文件。

> ⚠️ VL 模型（Qwen38-27B-VL）的场景切换不会主动执行——按约定需先申请、由用户切换（见 `CLAUDE.md`）。

---

## 逻辑模型池（Logical Model Pools，v7.0.0）

`models_pools.yaml`（`IFF_DATA_DIR` 下，热加载）定义逻辑名 `iff/<pool>`，把一个逻辑模型映射到一组按优先级排序的本地/云端候选。客户端只依赖逻辑名——物理模型升级、故障回退对客户端完全透明；OpenAI `/v1/chat/completions` 与 Anthropic `/v1/messages` 同一逻辑名入口。

**核心性质**：

- **默认惰性**：不创建 `model_pools.yaml` 时全部新代码路径零激活（`iff/` 前缀检查 + 空快照直通），`/v1/models` 与直连行为与旧版完全一致
- **协议兼容过滤**：候选按声明协议（`declared_protocols`）+ 探测证据（PROBED）筛选；未声明的能力不投递（如 vision 未声明则拒绝图像请求，VL 限制不解除）
- **本地候选只用已启动端口**——池调度**绝不自动启动本地模型**
- **跨本地/云回退默认关闭**：逐池显式 `allowed_locations: [local, cloud]` 才开启
- **物理目标容量门**：同一物理目标跨池/跨直连共享 DualGateLimiter 并发预算（`acquire_target`），不重复计费
- **已提交不重放**：响应向客户端提交任何字节后永不重试/切换（`committed` 信号贯穿传输层）
- **错误信封固定文案**：上游错误体/凭证只进日志，客户端只见统一错误码（如 `upstream_http_503`）
- **热加载**：`SIGHUP` / `POST /reload-config`；文件损坏/不可读时保留上一份好快照（三态重载）

### 故障回退（固定错误动作表，spec §6.2）

| 上游结果 | 动作 |
|---|---|
| 200 | 成功；RequestLog 记录**物理**模型（本地 served_name / 云端 model_id）+ cloud_provider |
| 连接失败 / DNS / 不可达 | 立即切下一候选（受预算约束） |
| 429 | 预算内尊重 Retry-After（支持秒数与 RFC 7231 HTTP-date）重试原目标；等待 >30s、超剩余预算或已重试过 → 切下一候选，**绝不按截断值提前重试** |
| 5xx（未提交） | 最多重试原目标 1 次，之后切下一候选 |
| 401 / 403 | 端点冷却 60s（按 endpoint_fingerprint，双协议共享——凭证级故障）+ 切下一候选 |
| 400 / 404 / 422 / 内容安全 / 501 | **终止，不回退**（确定性错误，换候选也无解）；404 额外隔离端点 30 分钟 |
| 已提交任何字节 | 永不重试/切换；流中断上报截断遥测 |

统一预算：`max_attempts` 计**所有真实上游调用**（含同目标重试）；`deadline_ms` 覆盖限流排队与等待；准入失败（容量门超时/候选冷却中）**不消耗** attempt 预算。冷却期过半后以**半开单飞探测**恢复（同一端点并发只放一个探测请求）。

### 配置示例

```yaml
schema_version: 1
revision: 7                     # registry 管理，勿手改
pools:
  fast:
    enabled: true
    allowed_locations: [local]  # 跨本地/云回退默认关闭：显式加 cloud 才开
    accept_declared: true       # 显式接受管理员声明证据
    max_attempts: 2             # 全部真实上游调用总数（含同目标重试）
    deadline_ms: 60000
    candidates:
      - id: qwen-small
        priority: 1
        kind: local
        model_name: qwen-small
        declared_protocols: [openai, anthropic]
        features: [tools]       # 未声明的能力不投递（vision 未声明则拒绝）
      # 跨云回退示例（需 allowed_locations 含 cloud）：
      # - id: ds-flash
      #   priority: 2
      #   kind: cloud
      #   provider: baidu-codingplan
      #   model_id: deepseek-v4-flash
      #   declared_protocols: [openai]
```

### 管理

管理面端点见 [Model Pools (admin)](#model-pools-admin)；Dashboard「模型池」TAB 可视化同一能力，v7.1.0 起为运营视图：每池双协议健康灯（绿=首选服务中/黄=回退中/红=无可用候选）、候选链运行态（服务中/未启动/冷却/隔离倒计时/最近失败）、三段式编辑器（raw 基底防丢数据，编辑→预览→CAS 应用，会话内回滚）、空态初始化向导（fast/premium/small/classifier 四角色引导创建）与接入片段（OpenAI/Anthropic 复制即用代码）。`/v1/models` 列出启用池为 `{"id":"iff/fast","owned_by":"iff-pool"}`。

---

## Cloud Provider Presets

9 pre-configured cloud providers with one-click setup:

| Provider | Discovery | Protocol |
|----------|-----------|----------|
| 百度千帆 Coding Plan | Spec | OpenAI + Anthropic |
| 火山方舟 | Auto | OpenAI |
| 阿里百炼 | Auto | OpenAI |
| DeepSeek | Auto | OpenAI |
| 智谱AI | Auto | OpenAI |
| Moonshot (Kimi) | Auto | OpenAI |
| OpenAI | Auto | OpenAI |
| Anthropic | Spec | Anthropic |
| Custom Relay | Manual | OpenAI + Anthropic |

API Keys are never stored in plaintext—automatically converted to `${ENV_VAR}` references and persisted in a `chmod 600` secrets file.

---

## Dashboard (v6.0.1)

A macOS-inspired sidebar dashboard for model management, monitoring, and multi-engine inference:

| | |
|:---:|:---:|
| **Overview — GPU metrics + model cards** | **Inference — model lifecycle & engine metrics** |
| ![Dashboard Overview](docs/screenshots/00-dashboard-overview.png) | ![Inference](docs/screenshots/03-inference.png) |
| **Monitor — 统一时间档位三卡（功耗/Token/延迟）** | **Cloud providers management** |
| ![Metrics](docs/screenshots/04-metrics.png) | ![Cloud Providers](docs/screenshots/02-cloud-providers.png) |
| **Anomaly detection** | |
| ![Anomaly](docs/screenshots/05-anomaly.png) | |

**Features**:
- Sidebar navigation（总览/推理/监控/部署/云端/异常）
- Live status rail: GPU memory · GPU load · GPU temp · VRAM · System memory · CPU
- Overview: model card grid with macOS-icon-box layout, status badges, start/stop controls, live snapshot freshness (ETag / 304 + TTL single-flight cache)
- Monitor: **6-KPI 2×3 panel**（KV Cache · Batch Size · Seq Length · TPOT ms · TTFT s · Throughput）, GPU time-series ring buffer (3s sampling)
- **统一时间单位语义（分钟/小时/天/周，整体弃用 月）**：三张带档位卡按钮同词同序——功耗/电费 小时/天/周（周 = 近 90 天 · 7 天周桶，双轴单网格：左 W 固定 0–600 + 右温度 nice 步长，累计电量/电费逐点直连）；Token 用量 分钟/小时/天/周（60min·12×5min / 24h·24×1h / 30d·30×1d / 90d·13×1周）；模型延迟趋势 分钟/小时/天（60min·12×5min / 24h·24×1h / 30d·30×1d，严格定值桶数）
- Monitor 三卡：功耗/电费（上）、Token 用量 = 本地/云端双 scope 并排 + Cache Hit Rate 徽标（统一走 `/api/token-curve`）、模型延迟趋势 = TTFT/TPOT 逐桶分位（P50 / P50+P95）双卡，桶内空 → connectNulls
- **Engine-agnostic token stats**: DB-sourced instead of engine Prometheus counters — works across vLLM / sglang / ninfer; `/api/engine_metrics` route exposes per-model KV cache, batch size, sequence length, latency & throughput
- **Two-scope token charts**: local engine vs. cloud provider consumption split side-by-side
- Inference model card **⚙ 场景** control: three-phase modal — pick scenario (`GET /admin/tune/scenarios`) → diff/warning preview (`/admin/tune/preview`) → apply (`POST /admin/tune`, in-process + file lock); card badge shows the live scenario read straight from the applied-layer file
- Cloud provider management: CRUD, auto-discover, connection test, API key masked-then-expanded forwarding
- 模型池 TAB（v7.1.0 运营视图）：双协议健康灯 + 候选链运行态（服务中/未启动/冷却/隔离/最近失败）；三段式编辑器（raw 基底、替换候选继承优先级、validate 预览门禁、CAS 应用 + 会话内回滚）；空态四角色初始化向导；接入片段（OpenAI/Anthropic 复制即用，含 ACL 与 /v1 提示）；单池 enabled 原子开关（CAS 冲突 409 → 提示刷新）
- Anomaly detection: top-N anomalies with severity classification
- Dual-protocol routing: local vLLM/sglang + cloud OpenAI-compatible, unified via YAML engine-type aliases
- Dark mode (default) + light mode, typography hierarchy, WCAG AA contrast
- OpenAPI 3.1.0 spec viewer（📖 link in top bar, 37 endpoints）

---

## 安装

### 依赖

InferFabric 是单文件 Python 应用，运行时依赖 `aiohttp` + `cachetools`（及 aiohttp 生态的传递依赖）。`requirements.txt` 已 pin 全部版本。

```bash
git clone https://github.com/vincentlau2046/InferFabric.git
cd InferFabric
pip install -r requirements.txt
```

> **本地开发（vendored 依赖）：** 仓库根目录的 `_deps/`（未提交到 git）是 vendored 依赖快照。若存在，`proxy_manager` 启动时会优先把它插到 `sys.path` 前面，**无需 pip install**。克隆获取不到 `_deps/`，请用上面的 `pip install -r requirements.txt`。

### Python 与 GPU 环境

- **Python 3.10+**
- **vLLM 0.24**（不要升级——适配器针对此版本调优）
- NVIDIA GPU + CUDA（推理引擎自身依赖，非 InferFabric 直接依赖；Blackwell RTX 50 系列可获得 NVFP4 专属加速，其他代显卡可用 vLLM/SGLang/Ollama 路径）
- 各引擎按需安装：vLLM / NInfer / SGLang / Ollama / ComfyUI 等

### 启动

```bash
./iff status                                    # CLI（线程模式，stdlib http.server）
python3 -m inferfabric.proxy.handler --async    # 生产异步引擎（aiohttp, :8999）
# Dashboard: http://localhost:8999
```

---

## Quick Start

```bash
# View all models
iff status

# Switch to a model (auto-start if stopped)
iff switch gemma4-31b-vl

# Return to idle
iff switch idle

# Dashboard at http://localhost:8999

# Start with async engine (v5.8.0+)
python3 -m inferfabric.proxy.handler --async
```

---

## API Endpoints

### Core

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | macOS Dashboard |
| `GET` | `/health` | Simple health `{"status":"ok"}` |
| `GET` | `/status` | GPU state, active services, health |
| `GET` | `/models` | All configured models |
| `GET` | `/v1/models` | OpenAI-compatible model list |
| `GET` | `/system` | System info (CPU, RAM, uptime) |
| `GET` | `/api/snapshot` | Full state snapshot (state + models + history + token stats) |
| `GET` | `/api/metrics` | Aggregated request metrics (JSON, 24h window) |
| `GET` | `/metrics` | Prometheus text format (R6) |
| `GET` | `/api/request_log` | Request log history (R1) |
| `GET` | `/api/token-stats` | Historical token usage |
| `GET` | `/api/token-curve` | Token curve data（`?granularity=minute\|hour\|day\|week`，双 scope local/cloud） |
| `GET` | `/api/power` | GPU 功耗/电费（`?gran=hour\|day\|week`，双轴 W/温度） |
| `GET` | `/api/latency` | 模型延迟趋势（`?window=minute\|hour\|day`，TTFT/TPOT 逐桶分位） |
| `GET` | `/api/anomalies` | Structured anomaly events (R9) |
| `GET` | `/engine_metrics` | Engine-level metrics (with `?model=`) |
| `GET` | `/watchdog_status` | Watchdog fail counts + running state |
| `GET` | `/history` | Switch history (last 30) |
| `GET` | `/api/openapi.json` | OpenAPI 3.1 specification |
| `GET` | `/vllm_metrics` | vLLM-specific metrics |

### Inference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat |
| `POST` | `/v1/completions` | → alias for `/v1/chat/completions` |
| `POST` | `/v1/messages` | Anthropic-compatible messages |
| `POST` | `/api/chat` | → alias for `/v1/chat/completions` |
| `POST` | `/api/generate` | → alias for `/v1/chat/completions` |
| `POST` | `/v1/embeddings` | Embedding requests |
| `POST` | `/v1/rerank` | Reranking requests |

### Control (admin: X-Admin-Token or localhost)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/switch` | Switch model `{"model":"gemma4-31b-vl"}` |
| `POST` | `/stop` | Stop a shared service |
| `POST` | `/reset` | Force reset to idle |
| `POST` | `/reconcile` | Fix state.db vs reality |
| `POST` | `/reload-config` | SIGHUP hot-reload (config + cloud) |
| `POST` | `/sleep` | L2 sleep: discard weights, rapid wake |
| `POST` | `/wake` | Wake a sleeping model |
| `POST` | `/deploy` | Deploy a new model |
| `POST` | `/pull` | Pull a remote model |
| `POST` | `/admin/cache/toggle` | Toggle response cache on/off |
| `POST` | `/admin/gpu-clear` | Clear GPU CUDA state |
| `GET` | `/admin/tune/preview` | Scenario preset diff preview `?model=&preset=` |
| `POST` | `/admin/tune` | Apply scenario preset `{"model","preset","restart"}` (in-process, file-locked) |
| `GET` | `/admin/tune/scenarios` | Model scenario list + current active `?model=` (reads applied layer) |

### Model Pools (admin)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/model-pools` | 池定义 + revision + 证据态 + 校验问题 + 三态运行状态（`configured`/`load_error`）+ `raw`（编辑器读-改-写基底，防丢数据） |
| `POST` | `/admin/model-pools` | 原子更新（CAS，body 携带 `expected_revision`） |
| `POST` | `/admin/model-pools/validate` | 静态校验（零出网、零副作用） |
| `GET` | `/admin/model-pools/status` | 每池双协议当前选择视图（含排除原因） |
| `GET` | `/admin/model-pools/runtime` | 每池每候选运行态：active（本地=已启动/云=已发现）、cooling（冷却/隔离剩余与原因、半开探测）、last_attempt（v7.1.0） |
| `POST` | `/admin/model-pools/dry-run` | 给定协议/能力 → 候选选择/排除原因（零副作用）；v7.1.0 起支持 `config` 参数预览编辑态而非已发布快照 |
| `POST` | `/admin/model-pools/toggle` | 服务端原子翻转单池 `enabled`（CAS） |

### Cloud Admin (admin)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/cloud/presets` | Provider presets list |
| `GET` | `/admin/cloud/providers` | List providers |
| `POST` | `/admin/cloud/providers` | Add provider |
| `DELETE` | `/admin/cloud/providers` | Remove provider |
| `POST` | `/admin/cloud/reload` | Reload cloud config from disk |
| `POST` | `/admin/cloud/discover` | Run discovery now |
| `POST` | `/admin/cloud/test` | Test provider connection |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EDGE_PROXY_HOST` | `127.0.0.1` | Proxy bind address |
| `EDGE_PROXY_PORT` | `8999` | Proxy listen port |
| `EDGE_AUTO_SWITCH` | `1` | Auto-switch on request (v6.0 默认开)。优先级：显式 env > `iff.yaml` 的 `auto_switch.enabled` > 默认开。推理 tab 网关控制卡的"自动切换"开关可即时翻转（`POST /admin/auto-switch/toggle`，免重启）并持久化到 iff.yaml；env 显式设置时 UI 提示重启后回到 env 值 |
| `EDGE_HEALTH_CHECK` | `60` | Health check interval (seconds) |
| `EDGE_ASYNC_WORKERS` | `32` | Async mode executor threads (PR-19) |
| `IFF_ADMIN_TOKEN` | `""` | Admin route auth token (empty = localhost-only) |
| `IFF_CACHE_ENABLED` | (YAML) | Override cache enabled (via env) |
| `IFF_DATA_DIR` | `~/.inferfabric` | Data directory (state.db, logs, secrets, cloud_provider.yaml) |
| `NOTIFY_SOCKET` | — | systemd sd_notify socket path |
| `CLOUD_PROVIDER_KEY_*` | — | Per-provider API key env vars (auto-detected) |

---

## Port Map（当前部署）

> 📖 **端口唯一权威登记 → `[models.d/README.md](models.d/README.md)`（端口登记表）**
> 下表仅为概览，可能滞后；**端口分配/冲突排查一律以 `models.d/README.md` 端口登记表为准**。新增/删除/改端口时必须同步更新 models.d 文档（强制规则）。

| Port | Service | Engine | GPU Role |
|------|---------|--------|----------|
| 8003 | qwen3-vl-4b | vLLM | shared |
| 8004 | ovis-ocr2 | vLLM | shared |
| 8005 | gemma4-31b-vl | vLLM | exclusive |
| 8006 | muse-glimmer-vl | SGLang | exclusive |
| 8007 | Qwen38-27B-TXT | NInfer | exclusive |
| 8008 | qwen36-35b-vl | vLLM | exclusive |
| 8009 | Qwen38-27B-VL | NInfer | exclusive |
| 8188 | comfyui | ComfyUI | shared |
| 8880 | tts-qwen3 | TTS | shared |
| 8881 | asr-sensevoice | ASR | shared |
| 11434 | ollama-daemon | Ollama | none |
| 11441 | bge-m3 | Ollama.cpp | none |
| 11442 | bge-reranker-v2-m3 | Ollama.cpp | none |
| **8999** | **Proxy** | **HTTP** | **—** |

---

## CLI Commands

```bash
./iff status              # GPU state + active services
./iff models              # List all models in models.d/
./iff switch <model|idle> # Switch model (auto-starts if stopped)
./iff stop <model>        # Stop a shared service
./iff reset               # Force reset to idle
./iff reconcile           # Fix state.db vs reality
./iff history             # Switch history
./iff pull <url>          # Pre-download model
./iff list-downloaded     # List downloaded models
./iff sleep <model>       # L2 sleep: discard weights, wake in ~3-6s
./iff wake <model>        # Wake a sleeping model
./iff tune [model] [preset]   # Scenario preset tuning: list / preview / apply (auto-restart)
```

---

## Recovery

```bash
./iff reset                          # Force to idle
./iff reconcile                      # Fix state.db
bash scripts/iff-recovery.sh --full  # Nuclear: SIGKILL all + nvidia-smi -gpu-reset
```

---

## Version History

| Version | Date | Highlights |
|---------|------|------------|
| v4.0 | 2026-06 | Model plugin architecture, three-state GPU |
| v4.6 | 2026-07 | Cloud discovery, provider management, Dashboard |
| v5.4.0 | 2026-08 | macOS Dashboard: sidebar, chat, 12 SVG icons, dark mode |
| v5.5.0 | 2026-08 | GPU state computed property (no drift), HealthMonitor decoupled, SIGHUP ConfigReloader |
| v5.5.1 | 2026-08 | OpenAPI 3.1.0 specification (37 endpoints, shared schemas) |
| v5.6.7 | 2026-09 | R0: `ensure_service` cooldown fix |
| v5.6.8 | 2026-09 | **Gateway Hardening**: R1-R10, Prometheus /metrics, AnomalyCollector, silent fallback removal |
| **v5.8.0** | **2026-09** | **PR-19: Production-grade aiohttp async edge (`--async`)** — hybrid executor model, 7 stream routes with incremental SSE pump, 30+ buffered routes with full header propagation, chunked request body support, 100MB client_max_size, EADDRINUSE retry, systemd sd_notify, C extension wheel rebuild (2.7x perf), dead route cleanup, **path normalization fix**: `/v1/completions`/`/api/chat`/`/api/generate` aliased to `/v1/chat/completions` (engine-type-driven via YAML). |
| **v6.0.0** | **2026-09** | **Dashboard data-chain engine-agnostic + two-scope** — token stats DB-sourced (no longer vLLM Prometheus-only, works across vLLM/sglang/ninfer); local vs. cloud two-scope token charts; `/api/engine_metrics` route (KV cache / batch size / seq length / TPOT / TTFT / throughput); **6-KPI 2×3 panel** with Batch Size + unified TPOT(ms, 2dp)/TTFT(s, 2dp) units; 30-day standardized bar charts; live snapshot freshness (ETag/304 + TTL single-flight cache); AnomalyCollector tab. |
| **v6.0.1** | **2026-09** | **统一 Monitor 三卡时间档位单位语义（分钟/小时/天/周，整体弃用 月）** — 功耗/电费 小时/天/周（周 = 近 90 天 · 7 天周桶，双轴单网格 W 0–600 + 温度 nice 刻度）；Token 用量 分钟/小时/天/周（60min·12×5min / 24h·24×1h / 30d·30×1d / 90d·13×1周，双 scope + Cache Hit Rate）；模型延迟趋势 分钟/小时/天（相对年龄对齐、严格 12/24/30 桶）；`/api/token-curve` 档位重定义，`/api/latency`、`/api/power` 端点就位。 |
| **v7.3.0** | **2026-10** | **模型池 UI/UX P2：行内 diff 编辑 + 乐观 UI + 向导成果卡 + 键盘流 + 状态过渡** — A2 行内 diff 两步换模（池卡片原地展开选择条「替换为…」→ diff 预览（增删行/协议/能力变化）→ 应用，免进模态；候选目录手势预热 primeInline：mouseover/focusin 预取一次，纯渲染/轮询零目录 fetch，目录不可用降级提示用编辑器）；B2 乐观 UI（开关 POST 期间 mp-busy 骨架扫光 + 状态灯先置目标态，成功接真值、失败回滚 + 常驻 banner）；A4 向导成果卡（创建成功 → 出口卡：已创建 N 池 + 逐池复制 OpenAI/Anthropic 接入片段 + 完成返回列表，展示期间轮询暂停）；B4 键盘流（e 进编辑器、Esc 两级退出：行内条 → 模态；输入控件聚焦时不抢键；未过预览 apply 禁用 + title 告知原因）；B3 状态过渡（mp-dot/mp-lamp/mp-chosen 色彩过渡统一 var(--dur)/var(--ease) 杜绝硬切，reduced-motion 降级块逐类显式收录）。纯前端展示层，零后端改动。 |
| **v7.2.0** | **2026-10** | **模型池 UI/UX P1：动效令牌 + 候选体检卡 + 错误即导航 + 局部渲染** — Motion/Depth 令牌集中定义（`--dur-fast/--dur/--dur-slow/--ease` macOS 质感缓动）+ `prefers-reduced-motion` 全局降级一刀切；候选行表格 → 横向体检卡（状态灯五态 mp-dot：probe 脉冲 > cool 暗态+环形倒计时 > flow 真实数据交换呼吸（last_attempt.ok 且 <30s，用户拍板）> ok routable 静默 > off unroutable；协议徽章 declared‖evidence + tools/vision 能力点；冷却/隔离环形倒计时 --p 首见锚定总时长客户端递减）；A3 错误即导航（mp-action-banner 常驻行动横幅：开关/创建/回滚失败置横幅带重试入口，成功清除，toast 保留即时反馈）；B1 局部渲染（结构签名不变 → 不重写 innerHTML 只走 applyDynamic 按 id 定点更新，轮询零闪烁）+ 本地倒计时（1s interval 懒启动，以 runtime 拉取时刻为锚本地递减，零 fetch）。纯前端展示层，零后端改动。 |
| **v7.1.2** | **2026-10** | **池候选目录（后端权威单一元数据总线）** — 新增 `GET /admin/model-pools/catalog`（`model_pool/catalog.py`）：本地 = models.d 中 model_type==llm 模型（条目名 = served_name，TTS/ASR/embedding 不进 chat 池），云 = cloud_models 可路由集合（与 runtime_view 同源，provider.candidates 非 routable 别名按构造不进目录，「目录里 ⇔ dispatch 能路由」）；条目携带 protocols（云来自发现结果，本地按引擎映射 declared 态）与 tools/vision 能力。前端向导/编辑器不再刮 `/models` 与 `/admin/cloud/providers` 逆向猜 schema（上一版云目录因前端读不存在的 pv.models 静默恒空）— 选择键 `local:<name>`/`cloud:<provider>/<model_id>` 不变，候选声明协议/能力改由目录条目供给。目录失败时编辑器降级提示、向导可手填。 |
| **v7.1.1** | **2026-10** | **模型池终审修复收尾** — `GET /admin/model-pools` revision 与 raw 单锁原子读取（消除穿插 update 导致的旧基点 POST 反复 409）；CooldownStore 最近尝试记录加上限（512，超限淘汰最旧，防指纹演变单调增长）。 |
| **v7.1.0** | **2026-10** | **模型池运营视图（Dashboard ops）** — 三态配置视图（已配置/从未配置→初始化向导/文件损坏→错误横幅且保留最后有效快照）；每池双协议健康灯（首选服务中/回退中/无可用候选）；候选链实时运行态（服务中/未启动/冷却与隔离倒计时+原因、半开探测中、最近一次尝试结果）；三段式编辑器（raw 基底防丢数据、一键换模=直接替换并继承优先级、validate+dry-run 编辑态预览门禁、CAS 应用基点固定为打开时 revision 堵后台刷新旁路、会话内一键回滚）；空态四角色向导（fast/premium/small/classifier 预填启发式，CAS expected_revision=0）；接入片段（OpenAI/Anthropic 复制即用，含 ACL/`/v1` 陷阱提示）。新增 `GET /admin/model-pools/runtime`；dry-run 支持 `config` 编辑态预览。 |
| **v7.0.0** | **2026-10** | **逻辑模型池（iff/ 统一逻辑名 + P2 预算内故障回退）** — OpenAI/Anthropic 双协议同一逻辑名 `iff/<pool>` 调度（协议兼容矩阵过滤，零协议转换）；热映射 registry（不可变快照 + revision CAS + SIGHUP 三态重载）；P2 固定错误动作表回退（max_attempts/deadline 统一预算、429 Retry-After 预算内等待、401/403 端点冷却、404 隔离、半开单飞探测、提交后永不重放）；物理目标容量门 `acquire_target`（跨池/本地直连共享，不双扣 RPM/全局）；跨位置回退默认关闭逐池显式开启，本地候选 ready_only 绝不自动启动模型；RequestLog 按物理模型归属费用 + attempt 失败只进进程日志；dashboard 池面板 + admin API + dry-run；`/v1/models` 逻辑名 owned_by=iff-pool。无 `model_pools.yaml` 时全惰性，行为与现状一致。单元+集成 1543 例全绿（提交树 1502），终审（独立上下文）20 项发现全部闭环。 |
| **v6.1.2** | **2026-09** | **四卡统一墙钟对齐固定窗分桶 + Chart 5min 节流** — Dashboard 四张统计卡统一按墙钟对齐固定窗口分桶；图表数据 5 分钟节流刷新。 |
| **v6.1.4** | **2026-10** | **健壮性审计 5 项代码修复 + 仓库卫生** — H2 多副本端口竞态（副本改用请求局部 `dataclasses.replace`，不再改写共享 `model_obj.port`）；M1/M2 forwarder socket 泄漏（`conn.close()` 下沉 `finally` 三路径统一关闭 + header 发送段 try/except 早断关上游 `resp`）；M3 proxy_manager `switch` 异常时清空 `switching_target` 防 503 卡死至 reconcile；M7 gpu_state 孤儿存活探测 `os.killpg`→`os.kill`（fuser 恢复的非 leader 活 PID 不再误判 dead）。`.gitignore` 补 `_deps/`·`.playwright-mcp/`·`vault/`·`/*.png`；`scenarios.yaml` 去注释化 + kv_capacity 重校准。unit 1154 + integration 109 全绿。 |
| **v6.1.3** | **2026-10** | **R5 响应缓存排除空内容** — `choices[0].message.content` 空 / 纯 thinking（无 tool_calls）的响应不写缓存；HIT 命中陈旧空条目时按 MISS 处理并移除（记 replayed-empty 日志）；Anthropic 形 content blocks（thinking-only 不缓存，text/tool_use 缓存）同一判据；新增 13 个空内容排除用例。agents 委派后 reclassify 提前到响应前（前端刷新不再读到旧状态）。 |
| **v6.1.1** | **2026-09** | **KV 池场景化 + VL 统一 NInfer** — `kv_capacity` 成为场景可选字段（per-scenario KV 池，场景写它即覆盖模型 YAML 固定池，不写=沿用）；引擎硬约束校验（kv ≥ 窗口否则 ❌ 拦截启动失败）；KV C 联动防 OOM（高并发档降 KV）；NInfer 超卖%按场景 kv 数值化展示；Qwen38-27B-VL 统一为 NInfer 版（vllm 版归档备份）；修复误入库的 SQLite 运行态文件并补 gitignore。 |
| **v6.1.0** | **2026-09** | **场景预设调优（iff tune）** — 三层架构（模型 YAML 只读 / `scenarios.yaml` 侧车 / 机器本地应用层），default 一等化、启动即读、展示读磁盘、`fcntl.flock` 文件锁；NInfer 双模型统一 3 场景（short-ctx / small-batch / big-batch，MTP draft 按档位启用 2/3，应用后自动冒烟）；Dashboard 场景控件（三阶段模态框）；`/admin/tune*` 端点。 |

---

## Hardware

- **目标硬件**: NVIDIA Blackwell RTX 50 系列（RTX 5090 / 5090D / 5080 / 5070 Ti）——NVFP4 加速路径（NInfer 引擎）为 Blackwell 专属
- **验证环境**: NVIDIA GeForce RTX 5090D, 32 GB GDDR7, 512-bit, 1792 GB/s, Blackwell (SM 12.0)
- **RAM**: 64 GB DDR5
- **OS**: Ubuntu 25.04, Python 3.12+

---

[InferFabric](https://github.com/vincentlau2046/InferFabric) · MIT License