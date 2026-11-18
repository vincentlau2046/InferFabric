# Spec: Diffusion Factory API（扩散模型工厂标准 API 网关）

| 项 | 值 |
|---|---|
| 状态 | **NEEDS_REVIEW（审计修订版，未批准实施）** |
| 日期 | 2026-11-15 |
| 目标版本 | 7.11.x（实际版本由实现时按项目规则决定） |
| 关联定位 | 本地 AI 工作站推理平台 + 有状态统一 Gateway；工作站转型为图片/短视频生成工厂 |

> 本文档是需求、特性和方案的修订版。它将“标准兼容 API”和 iff 自有扩展分开描述；在用户批准前，不创建产品代码、不安装依赖、不进入实施计划。

---

## 1. 意图与需求边界

云端模型预算充足后，工作站的主要职责转为批量生产图片和短视频。ComfyUI 是本地执行引擎；agent 或工具负责理解业务需求、扩写 prompt、选择合适的模型，并通过唯一入口 `iff proxy :8999` 调用生成能力。

**网关不负责：**

- prompt 扩写、翻译、改写或质量判断；
- 替 agent 决定使用哪一种扩散模型；
- 把普通 LLM 请求转换成图片/视频 prompt。

**网关负责：**

- 提供 agent 可直接使用的 OpenAI-compatible Images API 和 Videos API 核心；
- 根据 `model` 将标准请求转换成对应 ComfyUI workflow；
- 管理单 GPU 任务队列、生命周期、产物和任务状态；
- 保证 prompt 原值进入 workflow 指定输入；
- 通过 `/v1/models` 让 agent 发现可用生成模型。

核心调用形态：

```text
Agent（自己完成 prompt）
  │
  │ POST /v1/images/generations
  │ model=qwen-image
  │ prompt=<已经扩写完成的原始 prompt>
  ▼
iff proxy :8999
  │ 标准请求校验
  │ model → diffusion descriptor
  │ descriptor → ComfyUI API-format workflow
  │ 任务调度 / GPU lease / artifact
  ▼
ComfyUI :8188
```

---

## 2. 目标、非目标与反目标

### 2.1 产品目标

- **G1 图片生成**：`POST /v1/images/generations`，提供 OpenAI Images API 的兼容核心。
- **G2 图片编辑**：`POST /v1/images/edits`，以 OpenAI-compatible multipart 形式接收图片和 mask。
- **G3 视频生成**：`POST /v1/videos`、`GET /v1/videos/{id}`、`GET /v1/videos/{id}/content`，提供 Videos API 的兼容核心。
- **G4 模型选择**：agent 只通过标准请求中的 `model` 选择模型，不需要知道 ComfyUI workflow。
- **G5 模型发现**：扩散 descriptor 不依赖 GPU 当前 active 状态，始终可通过 `GET /v1/models` 发现；模型不可运行时，调用返回明确错误。
- **G6 网关编排**：任务持久化、单 GPU 调度、ComfyUI workflow 转译、产物落盘和重启恢复。
- **G7 单入口**：agent 和工具只访问 `:8999`，不要求直接访问 `:8188`。
- **G8 行为隔离**：不修改现有 chat、embeddings、rerank 的转发核心；扩散 API 是新增路由和新增模块。

### 2.2 非目标

- N1：iff 不做 prompt 扩写、翻译、润色、内容审核或自动重写。
- N2：iff 不负责云端扩散模型路由；agent 如需云端模型自行调用云端 provider。
- N3：不在本 PR 中开发 workflow 可视化编辑器。
- N4：不在本 PR 中开发训练、LoRA 训练或素材标注产线。
- N5：不把每一个扩散模型伪装成一个独立 OS 进程或独立 GPU 服务。
- N6：不承诺任意 ComfyUI 社区 workflow 可以零配置接入；首批模型必须有经过验证的 descriptor 和模板。

### 2.3 反目标（出现即失败）

- A1：agent 必须学习 iff 私有 prompt 或 workflow 协议才能生成。
- A2：标准字段被静默忽略或静默改写，导致 agent 误以为参数生效。
- A3：扩散模型 alias 被写入 GPU `active_services`，造成一个 ComfyUI 进程被重复计数或重复停止。
- A4：生成任务与本地重型 LLM 共享 GPU 导致无保护 OOM。
- A5：视频 API 的本地扩展（例如 cancel、list、seed）被错误描述为 OpenAI 标准行为。
- A6：破坏现有 chat/embeddings/rerank 路由行为。

---

## 3. 现状与约束

### 3.1 已有资源

当前工作站已存在：

- ComfyUI adapter，宿主端口 `8188`；
- SDXL 系列：AnimagineXL、雪碧MIX、JuggernautXL；
- Qwen-Image 2512 和 Qwen-Image-Edit 2511 相关权重；
- Wan2.2 I2V 14B fp8 high/low noise 权重；
- HunyuanVideo I2V 备选权重；
- `~/.inferfabric` 数据目录和 SQLite 基础设施；
- aiohttp 异步 proxy 启动形态：`python3 -m inferfabric.proxy.handler --async`。

`sd1.5` 目前只是用户提出的模型类别示例，不作为首批可用模型承诺，除非实际权重和 workflow 已验证。

### 3.2 技术约束

- 单卡 RTX 5090D 32GB。
- ComfyUI 是唯一生成执行宿主；同一时刻只允许一个 gateway-owned 生成 workflow 占用执行槽。
- 生产形态使用 async proxy；同步 handler 不作为扩散 API 的生产入口。
- 不升级 vLLM 0.24。
- 不修改 PR-14 排除的普通请求转发核心。
- 现有 `ModelConfig`/engine adapter/state machine 是面向可部署服务的，不直接承载扩散模型 descriptor；见 §5。
- 现有迁移文件已到 v010；新任务表使用 control-plane IFFDB 的下一版本 **v011**，不能再写成 v006。

---

## 4. 方案比选与裁决

| 方案 | 说明 | 裁决 |
|---|---|---|
| A. ComfyUI + proxy 内置 DiffusionGateway + 独立 descriptor catalog | ComfyUI 执行；proxy 做标准 API 转译、调度和产物管理；扩散模型不是独立服务 | **采纳** |
| B. 将每个扩散模型注册为 `type: diffusion` 的 ModelConfig | 复用现有模型目录表面简单，但会污染 lifecycle、GPU state、watchdog、reconcile 和 active service | 排除 |
| C. 自建扩散推理服务替代 ComfyUI | 要重写 Qwen-Image、Wan2.2 多阶段 workflow 和模型装卸，成本高且失去 ComfyUI 生态 | 排除 |
| D. 生成主力全部上云 | 不符合把工作站变成生产工厂的目标 | 排除 |
| E. proxy 外再起独立 DiffusionGateway 进程 | 隔离性更好，但增加生命周期、端口和鉴权对象；当前单机规模不值得 | 暂不采用 |

**关键裁决：**

1. `models.d/` 继续表示可部署 engine service。
2. 新增独立扩散目录，例如 `diffusion.d/*.yaml`，由 `DiffusionCatalog` 加载为 `DiffusionDescriptor`。
3. `models.d/comfyui.yaml` 是唯一真实宿主服务；descriptor 通过 `host: comfyui` 引用它。
4. descriptor 不进入 `active_services`，不参与 GPU mode 推导，不独立 start/stop，不由 watchdog 单独重启。
5. `/v1/models` 合并普通模型和 diffusion catalog，而不是从 active service 反推扩散模型。

---

## 5. 修订后架构

```text
Agent / 工具
  │ OpenAI-compatible API
  ▼
iff proxy :8999（aiohttp）
  ├─ auth key-only policy
  ├─ diffusion route handlers
  ├─ DiffusionCatalog
  │    └─ diffusion.d/*.yaml → DiffusionDescriptor
  ├─ DiffusionGateway
  │    ├─ request validator
  │    ├─ standard → workflow translator
  │    ├─ fair scheduler
  │    ├─ durable job store（IFFDB v011）
  │    ├─ GPU generation lease
  │    └─ ArtifactManager
  └─ /v1/models（普通模型 + diffusion descriptors）
       │
       ▼
models.d/comfyui.yaml（唯一宿主服务）
  │ :8188
  ▼
ComfyUI API
  ├─ /prompt
  ├─ /queue
  ├─ /history/{prompt_id}
  ├─ /view
  ├─ /ws（M2 进度）
  └─ /interrupt / queue delete（受 ownership 约束）
```

### 5.1 宿主与 descriptor 关系

```text
comfyui（真实服务）
  ├── qwen-image（descriptor）
  ├── qwen-image-edit（descriptor）
  ├── sdxl-juggernaut（descriptor）
  └── wan22-i2v（descriptor）
```

以下行为禁止：

- `iff switch qwen-image` 直接把 `qwen-image` 当进程启动；
- 将 qwen-image、qwen-image-edit、sdxl-juggernaut 都写入 active service；
- 对每个 descriptor 重复调用 ComfyUI start/stop；
- 让 GPU state 认为四个模型同时占用四份 GPU 服务。

### 5.2 ComfyUI 人工 UI 共存策略

第一版采用 **gateway-owned factory queue**：

- 人工可以打开 ComfyUI UI 查看状态；
- 生产期间不把人工直接提交视为受 gateway 调度的任务；
- gateway 派发前检查 `/queue`，发现外部 running/pending 作业时暂缓派发；
- gateway 只删除自己记录的 `comfy_prompt_id`；
- 对运行中 gateway 任务调用 `/interrupt` 前再次确认当前执行归属；
- 如果外部任务已经介入，取消只标记 `cancel_requested`，不保证立即中断；
- 不承诺 gateway 能在 ComfyUI 全局 interrupt 竞态下保护外部任务。

后续若要真正支持人工和 gateway 并行，需要单独设计 ComfyUI ownership/namespace，不能由本 spec 默认为已解决。

---

## 6. 标准 API 与 iff 扩展分层

本文档不再把全部能力统称为“严格标准兼容”，而分成两层：

### 6.1 OpenAI-compatible core（规范主路径）

- `POST /v1/images/generations`
- `POST /v1/images/edits`
- `POST /v1/videos`
- `GET /v1/videos/{id}`
- `GET /v1/videos/{id}/content`
- `GET /v1/models`

这些端点使用标准请求 content type、主要字段、错误 envelope、对象结构和成功状态码。实现时必须用官方 SDK/HTTP 样例做 contract test。

### 6.2 iff extensions（明确标记为扩展）

- `POST /v1/videos/{id}/cancel`：取消 gateway 任务；
- `GET /v1/videos`：列出本地任务；
- `seed` 或 `iff_seed`：可复现扩展；
- `x-iff-*` 响应头：队列位置、实际 seed 等；
- JSON/base64 形式的 `input_reference`；
- 本地 artifact URL 签名参数。

扩展不得改变标准端点的基本语义，也不得被文档描述为 OpenAI 标准字段。

---

## 7. Images API

### 7.1 文生图

```http
POST /v1/images/generations
Content-Type: application/json
```

最小请求：

```json
{
  "model": "qwen-image",
  "prompt": "agent 已经扩写完成的完整 prompt",
  "n": 1,
  "size": "1024x1024",
  "response_format": "url"
}
```

支持字段策略：

| 字段 | 处理 |
|---|---|
| `model` | 必须；解析到 descriptor |
| `prompt` | 必须；Unicode 值原样保存并注入指定 binding |
| `n` | 按 descriptor 上限处理；不支持的数量返回 400 |
| `size` | descriptor 白名单；不支持返回 400，不静默取最近尺寸 |
| `response_format` | `url` 或 `b64_json`，按实现能力返回 |
| `quality/style/background/user` | descriptor 明确支持才接受；否则返回 400 或明确记录为不支持，禁止静默忽略 |
| `seed` | iff 扩展；不作为标准字段承诺 |

响应 `200`：

```json
{
  "created": 1763164800,
  "data": [
    {"url": "http://127.0.0.1:8999/artifacts/2026/11/img_<random>.png"}
  ]
}
```

`b64_json` 返回对应 data item 的 base64；`n > 1` 时每个输出必须有独立 artifact 和 data item。

### 7.2 图片编辑

```http
POST /v1/images/edits
Content-Type: multipart/form-data
```

至少支持：

- `image` 文件 part；兼容 SDK 使用的单文件编码；
- `mask` 可选文件 part；
- `prompt`；
- `model`；
- `n`；
- `size`；
- `response_format`。

descriptor 必须显式声明：

- 单图还是多图；
- 多图是合并、取第一张还是拒绝；
- mask 是否必须 PNG；
- mask 是否必须带 alpha；
- image/mask 尺寸不一致时的错误；
- 输入图片保存到任务临时目录后的清理策略。

### 7.3 Images 错误

统一使用：

```json
{
  "error": {
    "message": "size 1280x720 is not supported by model qwen-image",
    "type": "invalid_request_error",
    "param": "size",
    "code": "invalid_size"
  }
}
```

不得把不支持的字段静默降级成默认值。

---

## 8. Videos API

### 8.1 创建视频：multipart 为规范主路径

```http
POST /v1/videos
Content-Type: multipart/form-data
```

规范主路径字段：

```text
model=wan22-i2v
prompt=<agent 已扩写的 prompt>
seconds=4
size=1280x720
input_reference=<image file，可选或按 descriptor 要求>
```

`input_reference` 文件支持格式和尺寸由 descriptor 声明；不符合时返回 400。

JSON + base64 `input_reference` 可以作为 iff 扩展，但不能作为标准兼容的唯一实现路径。

### 8.2 创建响应与生命周期

创建成功使用当前 Videos API 契约的标准成功状态码和视频对象；实现阶段必须用官方 contract fixture 锁定状态码和字段，不能自行改成只服务于本地队列的 202 语义。

视频状态采用标准核心：

```text
queued | in_progress | completed | failed
```

本地取消状态不混入标准核心。取消通过 iff 扩展：

```http
POST /v1/videos/{id}/cancel
```

取消响应和状态转换：

- queued：从 gateway 队列移除，标记本地 cancelled；
- in_progress：若 ownership 可确认则 best-effort interrupt；
- completed：返回不可取消错误；
- failed：返回不可取消错误；
- external job 介入：只记录 `cancel_requested`，不能保证中断。

### 8.3 视频读取与内容

```http
GET /v1/videos/{id}
GET /v1/videos/{id}/content
```

- status 查询返回任务对象；
- content 仅在 completed 且 MP4 artifact 已完成原子写入后返回；
- 未完成返回明确的冲突/未就绪错误；
- 视频内容响应必须有正确 `Content-Type`、`Content-Length`，实现时评估 `Range` 支持；
- `GET /v1/videos` 作为 iff 扩展，不作为标准兼容核心。

### 8.4 视频参数

| 参数 | 规则 |
|---|---|
| `seconds` | 只接受 descriptor 声明的标准支持档位；不支持时 400，不静默钳制 |
| `size` | descriptor 白名单；不支持时 400 |
| `n` | 视频首版固定为 1；传其他值返回 400 |
| `input_reference` | multipart 是标准主路径；base64 JSON 是 iff 扩展 |
| `seed` | iff 扩展；必须写入 job/artifact metadata |

### 8.5 视频输出链路

视频 descriptor 必须声明：

```text
ComfyUI output node → 临时输出 → ffmpeg/容器转换 → 原子 rename → .mp4 artifact
```

必须明确：

- 输出节点和中间帧格式；
- ffmpeg 依赖是否存在；
- 转码失败的状态和错误；
- 临时文件清理；
- 最终 MIME 和扩展名。

不能假定 ComfyUI `/view` 会自动把 GIF/WebP/image sequence 转换为 MP4。

---

## 9. Diffusion Descriptor Catalog

### 9.1 目录与数据模型

推荐目录：

```text
inferfabric/diffusion/
  catalog.py
  descriptor.py
  workflows/

diffusion.d/
  sdxl-juggernaut.yaml
  qwen-image.yaml
  qwen-image-edit.yaml
  wan22-i2v.yaml
```

`DiffusionDescriptor` 至少包含：

```text
name
api: images | videos
operation: generation | edit | i2v | t2v
host: comfyui
workflow
bindings
allowed_sizes
allowed_seconds
input_policy
output_policy
peak_vram_mb
defaults
```

### 9.2 YAML 示例

```yaml
name: qwen-image
description: "Qwen-Image 2512 文生图"
api: images
operation: generation
host: comfyui
workflow: qwen-image-t2i.json
allowed_sizes: ["1024x1024", "1536x1024", "1024x1536"]
defaults:
  size: "1024x1024"
  n: 1
peak_vram_mb: 28000
bindings:
  prompt: {node: "6", input: "text"}
  width: {node: "5", input: "width"}
  height: {node: "5", input: "height"}
  seed: {node: "3", input: "noise_seed"}
  batch: {node: "5", input: "batch_size"}
output:
  kind: image
  node: "9"
```

编辑和视频 descriptor 另行声明 image/mask/reference/output bindings。不存在的 node 或 input 在 catalog 加载时失败，不等到运行时静默跳过。

### 9.3 Descriptor 与生命周期的边界

- descriptor 不注册到 engine adapter registry；
- descriptor 不参与 `GpuStateMachine._derive_gpu_mode()`；
- descriptor 不进入 `active_services`；
- `host: comfyui` 解析到唯一的 `models.d/comfyui.yaml`；
- ComfyUI 健康检查失败时，descriptor 仍可被 `/v1/models` 发现，但生成请求返回 `engine_unavailable`；
- `iff models` 可以单独显示 descriptor catalog，但不能把 descriptor 当成可 stop 的进程。

### 9.4 `/v1/models`

`GET /v1/models` 必须在以下状态下都能看到合法 descriptor：

1. ComfyUI 未启动；
2. ComfyUI 已启动；
3. 某个本地 LLM active。

扩散模型对象可以增加以下 iff 扩展字段：

```json
{
  "id": "qwen-image",
  "object": "model",
  "owned_by": "inferfabric-diffusion",
  "iff_api": "images",
  "iff_operation": "generation"
}
```

---

## 10. Proxy、鉴权与可观测性

### 10.1 Async proxy 路由

在 `async_server.py` 增加原生 aiohttp 路由，包括动态 `/v1/videos/{id}`。不要假定现有 async route table 自动提供 auth、限流或日志；每条新增路由必须显式经过统一的 diffusion request pipeline：

```text
parse request
  → key-only auth
  → request validation
  → RPM limit
  → job submit / dispatch
  → request log + metrics + anomaly on failure
```

同步 handler 当前不承诺扩散 API。若收到扩散路径，应明确返回“不支持 async-only”错误；若不实现显式 501，则文档不得声称一定返回 501。

普通 chat/embeddings/rerank 的 `forward_to_backend`、协议转换和缓存路径不改动。扩散请求显式旁路 ResponseCache。

### 10.2 鉴权策略（用户选择的折中方案）

- `/v1/images/*`、`/v1/videos*`：鉴权激活时只验证 key 有效性，不要求扩散模型进入 per-model scope；鉴权关闭时按现有本地策略放行。
- 新增明确的 key-only auth API/策略，不把 diffusion model name 传给现有 model-scoped `auth.check()` 造成误拒绝。
- key 缺失/无效返回 401；不得绕过 key 校验。
- `/artifacts/*`、`/v1/videos/{id}/content`：默认 URL 即凭据，仅适用于默认 localhost 边界和随机 artifact path。
- 设置 `IFF_ADMIN_TOKEN` 或配置非本地公开地址时，产物 URL 使用带 expiry 的 HMAC 签名；签名规则、canonical path、过期时间和 constant-time compare 必须写入测试。
- 使用 `IFF_PUBLIC_BASE_URL` 生成产物 URL，不直接信任任意请求 `Host`。
- `POST /v1/videos/{id}/cancel`、状态查询和任务列表沿用 diffusion API 的 key-only 策略。

### 10.3 速率和并发

- 现有 RPM 限制作用于请求入口；
- GPU 执行并发由 DiffusionScheduler 控制，不使用普通 chat concurrency semaphore 代替任务队列；
- 队列位置和预计等待时间属于 `x-iff-*` 扩展，不能改变标准响应结构。

---

## 11. 调度、GPU lease 与任务状态

### 11.1 GPU 资源政策

生成任务与本地重型 LLM 不默认共享 32GB GPU。gateway 在派发前必须取得 generation lease：

```text
submit job
  → scheduler 获得队列槽位
  → generation lease 检查冲突服务和 peak_vram
  → 必要时停止/等待重型本地服务
  → 确认 ComfyUI host ready
  → submit workflow
```

首版至少要求：

- descriptor 声明 `peak_vram_mb`；
- 未声明峰值的重视频 descriptor 不允许进入 production；
- lease 不能与 exclusive LLM 同时持有；
- 当前显存不足时返回/保持明确的等待状态，不盲目提交导致 OOM；
- 释放 lease 必须覆盖成功、失败、取消和 proxy shutdown。

### 11.2 调度公平性

单卡执行槽为 1。调度采用“低延迟 + aging”的双车道，而不是绝对图片优先：

- 图片任务优先用于降低同步接口延迟；
- 最多连续执行 `K=4` 个图片任务后，若有等待视频则让出一个槽位；
- 等待超过 `T=120s` 的视频获得 aging priority；
- 视频不能永久饿死；
- `K`、`T` 为配置项，必须在测试中验证公平性。

外部 ComfyUI 作业占用执行槽时，gateway 不派发自己的任务。

### 11.3 Job 表与状态机

在 control-plane IFFDB 新增 v011 `diffusion_jobs`：

```text
job_id
kind
model
request_key_hash（不保存明文 key）
prompt
params_json
workflow_snapshot
status
progress
comfy_prompt_id
cancel_requested
error_json
created_at
started_at
finished_at
artifact_paths_json
idempotency_key
```

核心状态：

```text
queued → in_progress → completed
                    ├→ failed
                    └→ cancel_requested → cancelled/failed/completed
queued → cancelled
```

`cancelled` 是本地 job 状态，不伪装成标准 Videos 核心状态。

### 11.4 崩溃窗口和恢复规则

必须覆盖以下边界：

1. `/prompt` 已接受但 `comfy_prompt_id` 尚未写入；
2. history 已完成但 artifact 未完整写入；
3. artifact 已完成但 job 尚未标记 completed；
4. cancel 已发送但状态尚未提交；
5. ComfyUI 仍在运行而 proxy 重启。

恢复原则：

- 优先通过已知 `comfy_prompt_id` 查询 history；
- 已完成：认领输出并以原子 rename 完成 artifact；
- 仍运行：重新建立 watcher，不重复提交；
- 明确丢失且有安全幂等依据：才允许重投；
- 无法判断是否已提交：标记 `recovery_required`/failed 并保留诊断，不盲目重复生成；
- artifact 使用临时文件和 atomic rename；
- 相同 idempotency key 不重复创建任务。

客户端断连不取消已经提交的任务。

---

## 12. Artifact 管理

### 12.1 路径和元数据

```text
~/.inferfabric/artifacts/YYYY/MM/
  img_<random>_<index>.png
  vid_<random>.mp4
  <job_id>.json
```

metadata 至少保存：

```text
job_id
model
operation
raw_prompt
input_file_hashes
size/seconds
seed（如有）
workflow snapshot/hash
duration
created_at
```

`raw_prompt` 必须保留原始 Unicode 值；模板固定的 negative prompt 必须单独字段保存，不能与用户 prompt 混写。

### 12.2 URL 与安全

- URL 不列目录；随机段至少 128 bit；
- 使用配置的 `IFF_PUBLIC_BASE_URL`；
- 对外暴露或 `IFF_ADMIN_TOKEN` 设置时使用带过期时间的签名 URL；
- 签名覆盖 HTTP method、canonical path 和 expiry；
- 防止路径 traversal；
- 不把完整 prompt 放入 URL 或文件名；
- 默认支持正确 `Content-Type`，视频下载实现阶段评估 `Range`/`HEAD`。

首版不自动清理；`iff artifacts gc --days N` 可作为后续小工具，但不阻塞核心 API。

---

## 13. 实施里程碑

### M0：协议契约（无 GPU）

内容：

- Images JSON/multipart request parser；
- Videos multipart parser；
- 标准对象和 error envelope；
- 标准字段兼容矩阵；
- 官方 SDK/HTTP fixture；
- 标准核心与 iff extension 分层。

门禁：真实 SDK 请求与 fixture 通过，未调用 ComfyUI。

### M1a：单模型图片垂直切片

只接一个已验证的 `sdxl-juggernaut` descriptor：

```text
OpenAI-compatible request
  → catalog lookup
  → one ComfyUI workflow
  → one PNG artifact
  → standard image response
```

包含：

- DiffusionCatalog；
- descriptor 加载和 workflow binding 校验；
- `/v1/images/generations`；
- 单任务调度；
- artifact metadata；
- 基础 key-only auth；
- `/v1/models` 合并 descriptor。

暂不包含：视频、编辑、多图、dashboard、外部作业并行、复杂恢复。

### M1b：图片生产能力

加入：

- `qwen-image`；
- `qwen-image-edit`；
- multipart image/mask；
- `n`；
- v011 durable jobs；
- 图片队列公平性；
- artifact URL 签名；
- job recovery 和 idempotency；
- GPU generation lease。

### M2：视频生产能力

加入：

- `wan22-i2v`；
- multipart `input_reference`；
- ComfyUI WebSocket progress；
- MP4 转码/产物；
- `/cancel` iff extension；
- history adoption 和恢复；
- 外部 ComfyUI 作业竞态的 best-effort 语义；
- 真机 operational 测试。

### M3：Dashboard（独立 PR）

- 队列视图；
- 任务状态和错误；
- 产物画廊；
- 重试/重生成；
- 浏览器真实交互测试（Playwright），文本断言不作为 UI 可用性证明。

---

## 14. 测试与验收

### 14.1 协议单测

- Images generation JSON；
- Images edit 的真实 multipart 编码；
- Videos 的真实 multipart 编码；
- 单文件和重复文件字段；
- 标准成功状态码和 Content-Type；
- error envelope；
- 不支持字段返回明确 400；
- prompt Unicode 原样保存和 binding 注入；
- `n` 个请求得到 `n` 个 artifact/data item。

### 14.2 Catalog/生命周期单测

- descriptor 可以被发现但不进入 active service；
- ComfyUI inactive/active/LLM active 时 `/v1/models` 都能发现 descriptor；
- 不存在 workflow node/input 在加载时失败；
- descriptor 不触发重复 start/stop/watchdog/reconcile；
- host 引用必须解析到唯一 ComfyUI 配置。

### 14.3 Queue/恢复集成测试

mock ComfyUI 至少覆盖：

- `/prompt`；
- `/queue`；
- `/history/{id}`；
- `/view`；
- `/interrupt`；
- queue delete；
- `/ws` progress（M2）。

测试：

- 图片/视频公平调度，视频不永久饿死；
- 外部 job 占槽；
- 只取消 gateway-owned job；
- client disconnect 后任务继续；
- proxy SIGTERM/重启；
- `/prompt` 与 SQLite 提交之间的崩溃窗口；
- history adoption 不重复生成；
- artifact atomic rename；
- idempotency key 不重复创建；
- ComfyUI down 时明确 engine_unavailable。

### 14.4 真机 operational 门禁

在用户明确确认并设置 `IFF_OPERATIONAL_CONFIRM=1` 后：

- M1a：真实生成一张 PNG；
- M1b：真实编辑一张图片；
- M2：真实生成一段 4 秒 MP4；
- 记录 peak VRAM、耗时、artifact、状态恢复结果；
- 验证本地重型 LLM 与生成 lease 不发生无保护并发。

---

## 15. 风险与缓解

| 风险 | 缓解 |
|---|---|
| descriptor 污染现有 lifecycle | 独立 catalog，不注册为 engine service |
| 视频 API 与官方契约漂移 | 官方 SDK/HTTP contract fixture；标准核心与 iff 扩展分层 |
| 重视频与 LLM OOM | generation lease、peak_vram、工厂模式资源隔离 |
| 图片持续提交导致视频饿死 | K=4 + aging T=120s 的公平调度 |
| ComfyUI 外部作业竞态 | gateway-owned queue；外部作业检测；取消 best-effort，不虚假承诺 |
| proxy 崩溃重复生成 | history adoption、幂等键、原子 artifact、恢复状态机 |
| artifact URL 泄漏 | 随机路径、配置 public base URL、过期签名、防 traversal |
| workflow 节点变化 | descriptor 加载时校验 node/input，模板版本/hash 入库 |

---

## 16. 待用户审核的最终决策点

以下事项已由方案作出默认调用，不阻塞用户审核，但若用户反对应在实施前调整：

1. 扩散模型采用独立 descriptor catalog，不采用 `type: diffusion` service；
2. ComfyUI 是 gateway-owned execution host，人工直接提交不纳入并发保证；
3. Videos 标准主路径使用 multipart；cancel/list/seed/base64 input_reference 是 iff 扩展；
4. 首版使用 generation GPU lease，禁止重型 LLM 与重型生成无保护共存；
5. M1a 先只验证一个 SDXL 文生图模型，再扩展图片编辑和视频。

**审核状态：NEEDS_REVIEW。用户批准本修订版后，才进入 `superpowers:writing-plans`，再开始代码实现。**
