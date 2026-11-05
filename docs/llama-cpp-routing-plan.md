# Llama.cpp Proxy 路由方案

> 状态：方案分析（不修改逻辑）
> 日期：2026-07-07
> 涉及文件：`inferfabric/proxy.py`、`inferfabric/config.py`、`models.d/qwen25-omni-3b.yaml`

---

## 1. 问题复现

### 1.1 现状路由逻辑（`proxy.py` L459–524）

`_handle_chat` 的路由判定集中在 L497–507：

```python
ollama_model_obj = None
if service_name:
    model_obj = pm.mgr.get_model(service_name)
    if model_obj and model_obj.served_name:
        data["model"] = model_obj.served_name
    if model_obj and model_obj.is_ollama:          # ← 仅匹配 type == "ollama"
        ollama_model_obj = model_obj

if ollama_model_obj and ollama_model_obj.ollama and ollama_model_obj.ollama.num_gpu >= 0:
    self._handle_chat_ollama_native(pm, data, target_port, stream, ollama_model_obj)
    return

# vLLM path — apply dynamic rate limiter   ← 否则走这里
```

### 1.2 `qwen25-omni-3b` 实际走向

| 属性 | 值 | 来源 |
|---|---|---|
| `type` | `llama_cpp` | `models.d/qwen25-omni-3b.yaml` L3 |
| `is_ollama` | `False` | `config.py` L243 (`type == "ollama"`) |
| `is_llama_cpp` | `True` | `config.py` L247 (`type == "llama_cpp"`) |
| `ollama` (字段) | `None` | 只有 `llama_cpp` 字段被填充 |
| `llama_cpp` (字段) | `LlamaCppConfig(...)` | `config.py` L164 |
| `target_port` | `11436` | `config.py` L206 (`llama_cpp.port`) |

**实际走向**：`is_ollama` 为 `False` → `ollama_model_obj` 保持 `None` → 跳过 native 分支 → 落入 **vLLM path**（L509+）→ `pm.make_conn(11436)` 后以 OpenAI Chat Completions schema `POST /v1/chat/completions` 转发。

**结果**：`llama.cpp` server 暴露的是 Ollama 兼容的 `/api/chat`（以及 llama.cpp 的 `/completion`），并不实现 `/v1/chat/completions`。请求要么 404、要么解析失败、要么走错 schema，**无法正确路由**。同时还会误触 vLLM 专属的 rate limiter 和 `enable_thinking` 注入逻辑（L470 的 `is_vllm` 判定虽会跳过，但 rate limiter 会错误叠加）。

---

## 2. 修复方案

### 方案 A：新增 `is_llama_cpp` 独立分支，复用 native handler

在 L502 旁加一个并列判断，将 `llama_cpp` 模型也路由到 `_handle_chat_ollama_native`：

```python
if model_obj and model_obj.is_ollama:
    ollama_model_obj = model_obj
if model_obj and model_obj.is_llama_cpp:        # ← 新增
    ollama_model_obj = model_obj
```

**问题**：`_handle_chat_ollama_native` 内部硬编码访问 `model_obj.ollama.num_gpu`（L534）和 `model_obj.ollama.keep_alive`（L538）。对 `llama_cpp` 模型，`model_obj.ollama` 为 `None` → **`AttributeError`**。

要让方案 A 可行，必须改造 `_handle_chat_ollama_native` 内部的字段访问（加 `getattr` / 分支），改动量随即增大，不再是最小改动。**纯方案 A（仅加分支）不可行**。

### 方案 B：合并 `is_ollama or is_llama_cpp`，统一 native handler（推荐）

在路由判定处合并，并在 native handler 内部做字段兼容：

**路由判定**（L502–507）：

```python
if model_obj and (model_obj.is_ollama or model_obj.is_llama_cpp):
    ollama_model_obj = model_obj

if ollama_model_obj and ollama_model_obj.is_ollama and \
   ollama_model_obj.ollama and ollama_model_obj.ollama.num_gpu >= 0:
    self._handle_chat_ollama_native(pm, data, target_port, stream, ollama_model_obj)
    return
if ollama_model_obj and ollama_model_obj.is_llama_cpp:
    self._handle_chat_ollama_native(pm, data, target_port, stream, ollama_model_obj)
    return
```

**handler 内部字段兼容**（L534–539）：

```python
# ollama (daemon) 字段：num_gpu / keep_alive
if model_obj.ollama and model_obj.ollama.num_gpu >= 0:
    ollama_req["options"]["num_gpu"] = model_obj.ollama.num_gpu
if model_obj.ollama and model_obj.ollama.keep_alive:
    ollama_req["keep_alive"] = model_obj.ollama.keep_alive

# llama_cpp 字段：gpu_layers（映射到 llama.cpp 的 ngl，语义不同于 num_gpu）
if model_obj.llama_cpp and model_obj.llama_cpp.gpu_layers != 0:
    ollama_req["options"]["num_gpu"] = model_obj.llama_cpp.gpu_layers
# llama.cpp 进程无 keep_alive 概念（常驻进程），不注入
```

**依据**：`llama.cpp` (llama.cpp 的 Ollama API shim) 接受 `/api/chat` 的 `options.num_gpu` 字段，对应其 `--n-gpu-layers` 参数；无 `keep_alive` 概念（进程已常驻，不像 Ollama daemon 可卸载模型）。

### 方案 C：llama_cpp 走独立的 llama.cpp `/completion` 路径

为 `llama_cpp` 新写一个 `_handle_chat_llamacpp`，走 llama.cpp 原生 `/completion` endpoint，schema 为 `{ prompt, n_predict, ... }` 而非 Ollama 的 `{ model, messages, ... }`。

**优点**：不依赖 `llama.cpp` 是否完整实现 Ollama API shim；schema 更贴合 llama.cpp。
**缺点**：需要自行拼装 messages → prompt 的 chat template（Qwen2.5 Omni 的 VL 模板复杂）、自行处理多模态（图像）载荷、重复实现 SSE 转换（L566–612）。改动量最大，且 `qwen25-omni-3b.yaml` 的 `model_path` 指向 `llama.cpp` server（已实现 `/api/chat` shim），方案 C 属过度工程。

---

## 3. 风险与兼容性评估

### 3.1 `num_gpu` 对 llama_cpp 是否适用

| 维度 | Ollama daemon (`type=ollama`) | llama.cpp (`type=llama_cpp`) |
|---|---|---|
| 字段名 | `ollama.num_gpu` | `llama_cpp.gpu_layers` |
| 语义 | Ollama 的 `num_gpu` 选项，运行时透传 | llama.cpp 的 `ngl` (GPU 层数) |
| 取值惯例 | `0` = CPU，`-1` = 全 GPU，`N` = 部分 | `0` = CPU，`-1` = 全 GPU，`N` = 部分 |
| 注入位置 | `options.num_gpu` | `options.num_gpu`（llama.cpp shim 接受同一 key） |

**结论**：语义一致，可映射。**但 `qwen25-omni-3b.yaml` 当前 `gpu_layers: 0`**（CPU only，`cpu_only: true`），若按 `num_gpu >= 0` 判定会恒真注入 `num_gpu: 0`，需确认 `0` 是否要显式传（Ollama daemon 当前也是 `>= 0` 即注入，行为一致，无新风险）。

### 3.2 `keep_alive` 对 llama_cpp 是否适用

- Ollama daemon：`keep_alive` 控制模型在内存中的驻留时长，可 `"5m"` / `"-1"` 等。
- llama.cpp：进程启动时即加载 GGUF（`model_path`），**常驻不卸载**，无 `keep_alive` 语义。
- **结论**：**不应注入** `keep_alive`。方案 B 正确地对其忽略。

### 3.3 `served_name` 重写（L500–501）

`config.py` L218–219：`llama_cpp` 的 `served_name` 返回 `self.name`（即 `qwen25-omni-3b`）。`llama.cpp` server 启动时以 `--alias` 或模型名注册 `/api/chat` 的 `model` 字段，需确认 upstream 接受该名称。当前 yaml 未显式设 `alias`，`llama.cpp` 默认用文件名或 `unknown`。**低风险**：`llama.cpp` 的 `/api/chat` 通常对 `model` 字段宽松处理（单模型进程）。

### 3.4 rate limiter 误触（现状 bug）

现状 `llama_cpp` 走 vLLM path 会调用 `_get_model_rate_limiter` / `limiter.acquire`。方案 B 把它路由到 native path 后，**自动绕过 rate limiter**——这是正确行为（`llama.cpp` 是单模型常驻 CPU 进程，无需 vLLM 式限流）。

### 3.5 `enable_thinking` 注入（L470–474）

仅对 `is_vllm` 触发，`llama_cpp` 不受影响。方案 B 不改变此逻辑。

---

## 4. 方案对比

| 维度 | 方案 A（加分支复用） | 方案 B（合并 + 字段兼容） ✅推荐 | 方案 C（独立 llamacpp handler） |
|---|---|---|---|
| 最小改动原则 | ❌ 仅加分支会 `AttributeError`，不可行 | ✅ 路由 1 处 + handler 字段访问 2 处 | ❌ 新增 ~80 行 handler + template 拼装 |
| 正确性 | 不可行 | ✅ 正确路由到 `/api/chat`，字段映射正确 | ✅ 但需重写 SSE 与多模态 |
| `num_gpu` 兼容 | — | ✅ 映射 `gpu_layers` → `options.num_gpu` | ✅（用 `ngl`） |
| `keep_alive` 兼容 | — | ✅ llama_cpp 不注入 | ✅ 不涉及 |
| rate limiter | — | ✅ 自动绕过 | ✅ 不涉及 |
| 多模态 (VL) 支持 | — | ✅ `llama.cpp` shim 透传 `messages` 含图像 | ⚠️ 需自行处理 llama.cpp 图像载荷 |
| 风险 | 高（崩溃） | 低（字段宽松，`served_name` 需 upstream 认） | 中（template 易错） |
| 预计改动行数 | — | **~12 行**（路由 ~6 + handler ~6） | **~80–120 行**（新 handler + template） |

---

## 5. 推荐方案

**推荐方案 B**：合并 `is_ollama or is_llama_cpp` 路由判定 + 在 `_handle_chat_ollama_native` 内做字段兼容访问。

**理由**：

1. `llama.cpp` 已实现 Ollama `/api/chat` API shim，`_handle_chat_ollama_native` 的 SSE 转换、错误处理、`/api/chat` schema 可直接复用，无需重写（方案 C 的成本）。
2. 字段差异（`num_gpu` ↔ `gpu_layers`、无 `keep_alive`）可在 handler 内用 `if model_obj.ollama` / `if model_obj.llama_cpp` 分支隔离，改动集中、可读。
3. 路由判定合并为 `is_ollama or is_llama_cpp`，语义清晰：所有 Ollama 系后端统一走 native path。
4. 自动修复附带 bug：绕过 vLLM rate limiter 对 `llama_cpp` 的误限。

### 5.1 预计改动清单（方案 B）

| 文件 | 位置 | 改动 | 行数 |
|---|---|---|---|
| `inferfabric/proxy.py` | L502 | `is_ollama` → `is_ollama or is_llama_cpp` | +1 |
| `inferfabric/proxy.py` | L505 | 判定条件加 `is_ollama` 守卫（避免 `ollama` 为 None 时访问 `num_gpu`） | +2 |
| `inferfabric/proxy.py` | L507 后 | 新增 `is_llama_cpp` 分支调用 native handler | +3 |
| `inferfabric/proxy.py` | L534–539 | 字段访问加 `if model_obj.ollama` / `if model_obj.llama_cpp` 分支 | +6 |
| **合计** | | | **~12 行** |

### 5.2 验证建议（实施阶段，非本方案范围）

1. 对 `qwen25-omni-3b` 发 `POST /v1/chat/completions`，确认响应 200 + SSE 流。
2. 确认 `llama.cpp` upstream 接受 `model: "qwen25-omni-3b"`（`served_name`）；若不接受，需在 yaml 加 `alias` 或在 `config.py` L218 调整 `llama_cpp` 的 `served_name`。
3. 确认 `options.num_gpu: 0` 不会让 `llama.cpp` 报错（应等同于 CPU-only，与 `gpu_layers: 0` 一致）。

---

## 6. 结论

`qwen25-omni-3b` 当前因 `is_ollama` 不匹配 `llama_cpp` 类型，错误落入 vLLM 路径，请求 schema 与 upstream 不符。**采用方案 B**（合并路由 + 字段兼容），约 **12 行**改动即可正确路由到 `_handle_chat_ollama_native`，同时保留 `llama.cpp` 的字段语义差异（`gpu_layers` 映射、无 `keep_alive`）。
