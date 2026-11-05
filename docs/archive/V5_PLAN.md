# edge-LLM v5.0 — 统一 AI 运行时管理器

> 设计目标：一个 CLI 管理四种推理后端，角色别名路由，Dashboard 可视化部署

## 架构总览

```
                        用户 / 客户端
                    OpenAI SDK / curl / Dashboard
                              │
                    ┌─────────▼──────────┐
                    │   Proxy :8999      │
                    │  alias → served     │
                    │  → backend:port     │
                    │  OpenAI-compatible  │
                    └─────────┬──────────┘
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
    ┌─────▼─────┐    ┌───────▼───────┐    ┌──────▼──────┐
    │  vLLM     │    │  Ollama       │    │ Llama.cpp  │
    │ :8000/01  │    │  :11434       │    │ :11435+     │
    │ exclusive │    │  shared       │    │ CPU/shared  │
    │ GPU 24GB  │    │  GPU/CPU      │    │ GGUF only   │
    └───────────┘    └───────────────┘    └─────────────┘
                              │
                    ┌─────────▼──────────┐
                    │  ComfyUI :8188     │
                    │  shared            │
                    │ 图像/视频生成       │
                    └────────────────────┘
```

## 当前进度

### ✅ 已完成

| 组件 | 状态 | Commit |
|---|---|---|
| **Config 扩展** — 4 种后端类型 (vllm/comfyui/ollama/llama_cpp) | ✅ | `1cf2a8e` |
| **Proxy 通用化** — get_target_port/_wait_healthy/_handle_chat/_v1_models | ✅ | `1cf2a8e` |
| **Ollama YAML** — llama3-1b, phi3-mini, ollama-daemon | ✅ | `3f27ade` |
| **Llama.cpp YAML** — gemma-cpu + process_manager 启停 | ✅ | `3f27ade` |
| **CLI 扩展** — pull / list-downloaded 命令 | ✅ | `3f27ade` |
| **模型别名** — fast/powerful/cheap/code/balanced → 具体模型 | ✅ | `b6d2319` |
| **P0/P1 回归测试** — 23/23 全部通过 | ✅ | — |
| **Dashboard 本地模型发现** — GET /local-models, POST /deploy, UI | ✅ | `c6d2f19` |
| **Ollama 安装** — v0.17.5 运行中 | ✅ | — |
| **Ollama 模型拉取** — llama3.2:1b (1.3GB), phi3:mini (2.2GB) | ✅ | — |
| **Llama.cpp 编译** — llama-server v9820 from gitee mirror | ✅ | — |
| **GGUF 模型下载** — gemma-2-9b-it-Q8_0.gguf (9.8GB) | ✅ | — |
| **全链路端到端测试** — 8 模型 switch + chat | ✅ | `2b90b4a` |

## 四后端矩阵

| | vLLM | Ollama | Llama.cpp | ComfyUI |
|---|---|---|---|---|
| **端口** | 8000/8001/8002 | 11434 | 11435+ | 8188 |
| **GPU模式** | exclusive/shared | shared | CPU/shared | shared |
| **进程管理** | conda Popen+PGID | 外部 daemon | Popen+PGID | conda Popen+PGID |
| **模型格式** | HF safetensors | Ollama registry | GGUF | safetensors |
| **API** | /v1/chat/completions | /v1/chat/completions | /v1/chat/completions | /system_stats |
| **部署方式** | YAML→switch | YAML→daemon验证 | YAML→Popen | YAML→switch |
| **适用场景** | 大模型(27B+) | 中小模型(1-8B) | CPU推理/边缘 | 图像/视频 |
| **当前模型数** | 3 | 2 (待装) | 1 (待装) | 1 |

## 模型配置 (models.d/)

```
models.d/
├── qwen36-27b.yaml      # vLLM exclusive, 168K ctx, 0.83 GPU
├── qwen35-9b.yaml       # vLLM shared, 64K ctx
├── gemma4-26b.yaml      # vLLM exclusive, MoE 26B
├── comfyui.yaml         # ComfyUI shared
├── ollama-daemon.yaml   # Ollama 基础设施
├── llama3-8b.yaml       # Ollama shared, llama3.1:8b
├── phi3-mini.yaml       # Ollama shared, phi3:mini
├── gemma-cpu.yaml       # Llama.cpp CPU, 16 threads
└── aliases.yaml         # 角色映射
```

## 角色别名 (aliases.yaml)

```yaml
aliases:
  fast: llama3-8b        # Ollama, 最快首 token
  powerful: qwen36-27b   # vLLM, 最强质量
  cheap: phi3-mini       # Ollama, 资源最少
  code: qwen36-27b       # vLLM, 编程专用
  balanced: qwen35-9b    # vLLM, 均衡
```

**用法**：
```bash
# 用户用别名，不关心后端
curl http://localhost:8999/v1/chat/completions \
  -d '{"model":"fast", "messages":[...]}'

# proxy 自动：
# fast → llama3-8b → Ollama 11434 → llama3.1:8b
```

## Dashboard 本地模型发现 (Claude 执行中)

```
Dashboard UI:
┌──────────────────────────────────────────┐
│ 📦 Local Models                          │
│ ┌──────────────────────────────────────┐ │
│ │ gemma-2-2b-it  2.1 GB  [Deploy] →  │ │
│ │ ~/models/gemma-2-2b-it              │ │
│ └──────────────────────────────────────┘ │
│ ┌──────────────────────────────────────┐ │
│ │ qwen3-vl-14b    28 GB  [Deploy] →   │ │
│ │ ~/models/Qwen3-VL-14B-Instruct      │ │
│ └──────────────────────────────────────┘ │
│ All local models configured ✓           │
└──────────────────────────────────────────┘
```

**API**：
- `GET /local-models` → 返回 `{discovered: [...], configured: [...]}`
- `POST /deploy` → 自动生成 YAML + switch + 返回结果

**扫描规则**：
| 路径 | 检测条件 | 分类 |
|---|---|---|
| `~/models/<dir>/config.json` | 有 config.json | `vllm` |
| `~/models/<dir>/*.gguf` | 有 .gguf 文件 | `llama_cpp_gguf` |
| `~/ComfyUI/models/checkpoints/*.safetensors` | .safetensors | `comfyui_checkpoint` |
| `~/ComfyUI/models/loras/*.safetensors` | .safetensors | `comfyui_lora` |

## 交互流程

### 场景 1：用户下载了 Qwen3-VL-14B 到 ~/models/

```bash
# 1. 打开 Dashboard → "Discovered Models" 出现
# 2. 点击 [Deploy] → 自动：
#    - 生成 models.d/qwen3-vl-14b.yaml (auto port=800X, mode=shared)
#    - iff switch qwen3-vl-14b
# 3. 模型上线，Dashboard Active Services 中出现
# 4. 用户发送请求：
curl -d '{"model":"qwen3-vl-14b",...}' localhost:8999/v1/chat/completions
```

### 场景 2：用户想用 fast 模型做快速问答

```bash
# 不需要知道底层是 llama3-8b 还是 phi3-mini
curl -d '{"model":"fast",...}' localhost:8999/v1/chat/completions
# proxy: fast → llama3-8b → Ollama daemon :11434
```

### 场景 3：用户下载了新的 SDXL checkpoint

```bash
# 放到 ~/ComfyUI/models/checkpoints/
# Dashboard 自动发现，记录到 ComfyUI 配置
```

## 部署清单 (Vincent 手动操作)

```bash
# === Ollama ===
# 1. 安装
curl -fsSL https://ollama.com/install.sh | sudo sh

# 2. 拉取模型 (每个 4-8GB)
ollama pull llama3.1:8b
ollama pull phi3:mini

# 3. 启动 daemon
ollama serve &

# 4. 验证
iff switch llama3-8b
curl http://localhost:8999/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi"}]}'

# === Llama.cpp (可选 — CPU推理场景) ===
# 1. 编译
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp && mkdir build && cd build && cmake .. && make -j llama-server
cp bin/llama-server ~/miniconda3/envs/base/bin/

# 2. 下载模型
# 从 HuggingFace 下载 GGUF 到 ~/models/gguf/

# 3. 验证
iff switch gemma-cpu
iff status
```

## 技术决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| Anthropic 协议 | ❌ 不做 | 四后端都不原生支持，Proxy 层翻译维护成本高 |
| 模型下载 | 独立 `pull` 命令 | 网络下载不应阻塞 switch() |
| Ollama 生命周期 | 监控，不托管 | daemon 有复杂内部逻辑，PGID 不够 |
| Llama.cpp 生命周期 | 托管，独立进程 | 一模型一进程，和 vLLM 一致 |
| CPU 模型 | `cpu_only` 标签 | 完全跳过 GPU 状态机 |
| Proxy 路由 | alias → served_name → port | 角色透明，后端透明 |
| CC-Switch 借鉴 | 别名 + 格式转换 | React/TS 代码不可直拷，提取设计模式 |
| Claude Code | 增量开发默认工具 | 用户明确要求，准确度高 |