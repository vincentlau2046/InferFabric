# models.d — 模型配置文件目录

## 目录用途

`models.d/` 存放所有模型的 YAML 配置文件。每个文件描述一个模型的类型、资源需求、运行时参数等。IFF 启动时自动扫描此目录，加载所有 `.yaml` 文件作为可用模型。

旧架构的 `profiles.yaml` 已被此目录取代。每个模型是自描述的（self-describing plugin），不再需要中心化的 profile 定义。

---

## YAML 模板规范

### 通用字段（所有模型类型）

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `name` | ✅ | string | 模型唯一标识符，同时也是文件名（不含扩展名） |
| `description` | ✅ | string | 模型描述，显示在 `iff models` 输出中 |
| `type` | ✅ | string | 模型类型: `vllm` / `sglang` / `ninfer` / `comfyui` / `ollama_cpp` / `ollama_daemon` / `tts_server` / `asr_server` / `alias_map` |
| `gpu_role` | ✅ | string | GPU 角色: `exclusive` / `shared` / `none` |
| `model_type` | 否 | string | 模型类别: `llm` / `vl` / `embedding` / `aigc` |
| `modality` | 否 | string | 输入输出模态: `text` / `text-vision` / `multimodal` / `embedding` / `aigc` |
| `quantization` | 否 | string | 量化格式: `NVFP4` / `GPTQ-4bit` / `Q4_K_M` / `Q8_0` / `Q4_0` |
| `peak_vram_mb` | 否 | int | 峰值显存 (MB)，用于 GPU 调度估算 |

### 类型特有字段

#### vllm
用于 vLLM 推理引擎。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `vllm.model_dir` | ✅ | string | 模型目录名（相对于 `~/models/`） |
| `vllm.served_name` | ✅ | string | vLLM serve 注册名 |
| `vllm.port` | ✅ | int | 服务端口 |
| `vllm.gpu_memory_utilization` | ✅ | float | GPU 显存利用率 (0.0–1.0) |
| `vllm.max_model_len` | 否 | int | 最大上下文长度 |
| `vllm.max_num_seqs` | 否 | int | 最大并发序列数 |
| `vllm.kv_cache_dtype` | 否 | string | KV cache 数据类型（`auto` / `fp8` / `fp8_e4m3`） |
| `vllm.conda_env` | ✅ | string | Conda 环境名 |
| `vllm.startup_timeout` | 否 | int | 健康检查超时秒数（0 = 使用全局默认值） |
| `vllm.extra_env` | 否 | dict | 注入子进程的环境变量（最高优先级） |
| `vllm.extra_flags` | 否 | string | 额外 vLLM 启动参数（以空格分隔，经 shlex.split 追加到命令行） |

#### comfyui
用于 ComfyUI 图像生成。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `comfyui.port` | ✅ | int | Web 服务端口 |
| `comfyui.working_dir` | 否 | string | ComfyUI 工作目录（默认 `~/ComfyUI`） |

#### ninfer
用于 NInfer Docker 推理引擎（Blackwell NVFP4 专属，支持 VL/多模态）。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `ninfer.weight_path` | ✅ | string | `.ninfer` artifact 路径 |
| `ninfer.docker_image` | ✅ | string | 镜像名（VL 版需用含 `--vision` 支持的镜像，如 `ninfer:vl`） |
| `ninfer.served_name` | 否 | string | proxy 路由名，缺省用模型名 |
| `ninfer.model_id` | 否 | string | 覆盖 artifact identity（`--model-id`） |
| `ninfer.port` | ✅ | int | 服务端口（查端口登记表取唯一端口） |
| `ninfer.container_name` | 否 | string | Docker 容器名 |
| `ninfer.max_context` | 否 | int | 单请求最大上下文（tokens） |
| `ninfer.kv_capacity` | 否 | int | KV cache 池（tokens）；`0` = auto |
| `ninfer.kv_dtype` | 否 | string | KV 精度: `int8` / `fp8` / `nvfp4` / `bf16` |
| `ninfer.vision` | 否 | bool | 多模态 VL：`true` 时追加 `--vision` 启用视觉塔+media（默认 `false`） |
| `ninfer.enable_mtp` | 否 | bool | 启用 MTP 投机解码（默认 `true`） |
| `ninfer.draft_tokens` | 否 | int | MTP draft token 数 |
| `ninfer.media_cache_mib` | 否 | int | media 复用缓存 MiB；`0` 关闭跨请求复用（默认 `1024`） |
| `ninfer.media_live_mib` | 否 | int | media 在线 BF16 patch 上限 MiB；视觉下 minimum≈384MiB、不可为 0（默认 `2048`） |

> ⚠️ **多模态模型 `vision: true` 时，顶到显卡显存上限，端口务必避开 CPU/桌面占用的空间**；用 `ninfer:vl` 这类带视觉支持的镜像。

> 💡 **KV 池调优（2026-11-04 起，v3 规划器镜像线）**：**默认一律 `kv_capacity: 0`(auto)**——auto 留 1GiB headroom + 按剩余显存自取池顶（TXT ≈583K / VL ≈388K tokens，qwen3.8-27b NVFP4 KV 实测 ≈23.8KiB/token）。**显式 kv 在 v3 规划器下极易启动即 FATAL**（预算 = 权重后余量 − 1GiB，稍超即 `resolve_kv_capacity` 失败，亚秒 exit 1）；确需显式值先按 23.8KiB/token 心算并留足余量。桌面/远程客户端（ToDesk 等）占用会压低可用上限。参见 [timeline]。

#### ollama_daemon
用于 Ollama 守护进程（基础设施）。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `ollama_daemon.port` | ✅ | int | Ollama API 端口 |

#### ollama_cpp
用于 Ollama.cpp 直接推理（CPU/GPU）。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `ollama_cpp.port` | ✅ | int | OpenAI-compatible API 端口 |

#### tts_server
用于 TTS 语音合成服务（OpenAI-compatible `/v1/audio/speech`）。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `tts.port` | ✅ | int | API 端口 |
| `tts.conda_env` | ✅ | string | Conda 环境名 |

#### asr_server
用于 ASR 语音识别服务（OpenAI-compatible `/v1/audio/transcriptions`）。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `asr.port` | ✅ | int | API 端口 |
| `asr.conda_env` | ✅ | string | Conda 环境名 |

---

## 命名约定

- 文件名: `{model-name}.yaml`，全小写，短横线分隔
- `name` 字段值必须与文件名（不含扩展名）一致
- 别名文件: `model_affinity.yaml`（固定名称，云端模型路由）

---

## 端口登记表

**端口分配原则**：
- `8000-8009`: vLLM / NInfer 推理服务（LLM / VL）
- `8188`: ComfyUI
- `8800-8899`: TTS / ASR
- `11000-11999`: Ollama 基础设施

⚠️ **每次新增/删除/修改任意 YAML（含端口、字段、模板）都必须同步更新本表及本文档**，防端口冲突与文档漂移。详见下方「新增/删除 YAML 流程」的强制规则。

| 端口 | 文件名 | served_name | 类型 | GPU 角色 | 描述 |
|------|--------|-------------|------|----------|------|
| 8003 | `qwen3-vl-4b.yaml` | `qwen3-vl-4b` | vllm | shared | Qwen3-VL-4B GPTQ W4A16（AICF 质检） |
| 8004 | `ovis-ocr2.yaml` | `ovis-ocr2` | vllm | shared | OvisOC2 0.8B 端到端文档OCR |
| 8005 | `gemma4-31b-vl.yaml` | `gemma4-31b-vl` | vllm | exclusive | Gemma4-31B IT NVFP4 Dense VL |
| 8006 | `muse-glimmer-vl.yaml` | `muse-glimmer` | vllm | exclusive | Meta Muse Glimmer 30B NVFP4 VL （SGLang） |
| 8007 | `Qwen38-27B-TXT.yaml` | `Qwen38-27B-TXT` | ninfer | exclusive | Qwen3.8-27B NVFP4 NInfer（纯文本，MTP，v3 产物 + kv=0 auto≈583K） |
| 8008 | `qwen36-35b-vl.yaml` | `qwen36-35b-vl` | vllm | exclusive | Qwen3.6-35B A3B MoE NVFP4 VL |
| 8009 | `Qwen38-27B-VL.yaml` | `Qwen38-27B-VL` | ninfer | exclusive | Qwen3.8-27B NVFP4 NInfer 多模态 VL（视觉塔 + NVFP4 KV，kv=0 auto≈388K） |
| 8010 | `qwen36-35b-ninfer.yaml` | `qwen36-35b-ninfer` | ninfer | exclusive | Qwen3.6-35B A3B MoE NVFP4 NInfer（官方 v3 产物，vision+MTP3；就绪未 serve，DFlash adapter 缺口） |
| 8188 | `comfyui.yaml` | — | comfyui | shared | ComfyUI 图像生成 |
| 8880 | `tts-qwen3.yaml` | — | tts_server | shared | Qwen3-TTS 1.7B 语音合成 |
| 8881 | `asr-sensevoice.yaml` | — | asr_server | shared | ASR SenseVoice-Small 中文语音识别 |
| 11434 | `ollama-daemon.yaml` | — | ollama_daemon | — | Ollama 守护进程 |
| 11441 | `bge-m3.yaml` | — | ollama_cpp | none | BGE-M3 Q4_K_M 嵌入（CPU） |
| 11442 | `bge-reranker-v2-m3.yaml` | — | ollama_cpp | none | BGE-Reranker-v2-m3 Q8_0 重排序（CPU） |

**注释**：
- `model_affinity.yaml` 为云模型路由配置，不占用端口。
- GPU 角色：`exclusive` = 独占 GPU、`shared` = 共享 GPU、`none` = CPU 运行。
- `qwen3-vl-4b-prefill` / `qwen3-vl-4b-decode`（P/D 分离，端口 8010/8011）已归档至 `archive/*.yaml.bak`（2026-09-21，暂不启用 PD 分离；如需恢复：还原文件名回 `models.d/` 并在本表加回两行；8010 现已重新分配给 `qwen36-35b-ninfer`，恢复时需先腾挪端口）。
- 2026-11-04/05: NInfer 引擎镜像线切换——TXT/VL `docker_image` 统一 `ninfer:accept-map-v2`（1ca85a95 线 + disable_parallel 接受并映射 + thinking.display=updates；TXT 侧引擎探针未验证，VL 全套已验证）；TXT weight_path 升 v3 产物 `qwen3_8_27b_nvfp4_v3.ninfer`；TXT YAML + `scenarios.yaml` TXT 三场景 `kv_capacity` 全部改 0（auto，v3 规划器显式 kv 预算不足即 FATAL）。
- 2026-11-03: 新增 `qwen36-35b-ninfer.yaml`（8010，官方 NVFP4 v3 产物，kv_dtype fp8、MTP3 首版；DFlash 投机 adapter 缺口，就绪未 serve）。

---

## 新增/删除 YAML 流程

> **📌 强制规则（必须遵守）：任何对 `models.d/*.yaml` 或本文档"模板规范 / 端口登记表"的更改，都必须同步更新本 README，否则视为配置无效。** 否则会导致端口冲突 / 文档漂移（历史教训：`8002` 行曾保留旧文件名，`Qwen38-27B-TXT` 曾漏登）。

1. **新增**：创建 `{model-name}.yaml`，选唯一端口（查上表），填入必填字段。
2. **删除**：移文件到 `archive/` 子目录（保留回溯可能），而非直接 `rm`。
3. **修改任何 YAML**：无论是改字段、改类型、改端口，均须同步——
   - 若端口变更 → 更新**端口登记表**对应行；
   - 若新增/改模板字段 → 更新**YAML 模板规范**对应类型区块；
   - 每次变更 → 在**更新时间线**追加一行记录（日期 + 变更内容）。
4. **验证**：`python3 ~/projects/inferfabric-sandbox/inferfabric/cli.py models`，确认新模型在列表中出现，且端口无冲突。

> **注意**：模型配置完全由 YAML 文件声明，IFF 启动时自动扫描 `models.d/` 目录。本文档（本 README）是该目录配置的**唯一权威登记**——YAML 与本文档不一致即视为配置漂移，需要修正。git 提交时建议将对应 YAML 与本文档改动放在同一 commit 便于审查。

---

## 更新时间线

- 2026-09-28: 归档 `Qwen38-27B-VL.yaml`（vllm，端口 8002，Abliterated NVFP4，`Huihui-Qwen3.8-27B-abliterated-NVFP4`）→ `archive/Qwen38-27B-VL.yaml.bak`（如需恢复：还原至 `models.d/` 并把本表 8002 行加回；注意 8009 的 served_name 已占用 `Qwen38-27B-VL`，恢复时需改回旧名或换名）。`NI-Qwen38-27B-VL.yaml`（NInfer，8009）更名为 `Qwen38-27B-VL.yaml`：name/served_name/model_id 全部改为 `Qwen38-27B-VL`，weight_path 与容器名不变；`scenarios.yaml` 同步改 key。
- 2026-09-22: 新增 `NI-Qwen38-27B-VL.yaml`（NInfer 多模态 VL，端口 8009）。补登 8007 `Qwen38-27B-TXT`；修正 8002 行文件/served_name 为 `Qwen38-27B-VL`；模板 type 增补 `sglang`/`ninfer` 并新增 ninfer 字段说明。
- 2026-09-22: `NI-Qwen38-27B-VL.yaml` 调显存寻更多 KV：压 `media_cache_mib: 0` + `media_live_mib: 1024`（default 是 1024/2048），并将 `kv_capacity` 由 `0`(auto) 改为显式（auto 只到 ~378K；显式上限实测 ≈434K/卡上、ToDesk 占用时）。NInferConfig 新增 `media_cache_mib`/`media_live_mib` 字段（config.py 代码改动 → 需重启 proxy）。
- 2026-09-22: `NI-Qwen38-27B-VL.yaml` `kv_capacity` 由 420000 降到 `410000`（留 ~0.7 GiB 显存余量防 OOM，比 420K 的 487 MiB free 更稳）。
- 2026-09-22: `Qwen38-27B-TXT.yaml` `kv_capacity` 由 auto 固定为 `600000`（无视觉塔，auto≈566K；显式 600K 实测）。
- 2026-08-23: 创建端口登记表。清理旧版中已删除模型的记录（aliases.yaml、phi3-mini.yaml、qwen25-omni-3b.yaml、qwen3-embedding-0.6b.yaml、qwen35-9b.yaml、qwen36-27b-vl.yaml）。迁入全部 12 个活跃模型的端口映射。
