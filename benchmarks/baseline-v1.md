# InferFabric 性能基线 v1

**日期**: 2026-07-29
**GPU**: NVIDIA RTX 5090 D (32GB GDDR7)
**Commit**: `7494b2f`
**测试方法**: 流式 chat completions, 3 次重复取均值, 通过 proxy (localhost:8999)

## 核心指标速查表

短输入 (~1K tokens), output 512 tokens, concurrency=1:

| 模型 | 参数量 | 量化 | TTFT | TPS | E2E | 备注 |
|------|--------|------|------|-----|-----|------|
| ovis-ocr2 | 0.8B | bf16 | ~125ms | ~450 | ~500ms | OCR专用，极快 |
| qwen35-9b | 9B | GPTQ-4bit | ~215ms | ~220 | ~2.5s | shared GPU |
| qwen36-27b-vl | 27B | NVFP4 | ~900ms | ~140 | ~4.5s | exclusive + MTP, reasoning 首token~900ms |
| qwen36-35b | 35B(A3B) | NVFP4 | ~500ms | ~340 | ~2.1s | MoE + MTP, 最优性价比 |
| gemma-4-31B | 31B | NVFP4 | ~1.1s | ~80 | ~6s | exclusive, kv-offload=24, 吞吐受限 |

## 关键发现

### 1. MoE 吞吐碾压 Dense
- **qwen36-35b (MoE A3B)**: TPS ~340, 比 **qwen36-27b-vl (dense)** 的 ~140 高 2.4x
- 35B MoE 激活参数仅 3B，decode 路径更短，但质量接近 27B dense

### 2. Reasoning 模式对 TTFT 的影响
- qwen36-27b-vl 和 qwen35-9b 都有 `reasoning_parser: qwen3`
- **首 token** 是 reasoning token，不是 content token
- 实际用户可感知延迟 = reasoning 时间 + 首个 content token 时间
- 对 benchmark 而言：TTFT 测量的是首个 reasoning token 到达时间

### 3. 并发扩展性
| 模型 | c=1→c=4 TPS 下降 | c=1→c=4 TTFT 增加 |
|------|------------------|-------------------|
| qwen36-27b-vl | ~5% | ~50% |
| qwen36-35b | ~7% | ~40% |
| qwen35-9b | ~15% | ~70% |

并发下 TTFT 线性增长（batch prefill），TPS 基本持平（continuous batching 充分利用 GPU）

### 4. KV Offloading 对吞吐的影响
- **gemma-4-31B**: kv-offloading-size=24 → TPS 仅 ~80，远低于同参数量的 qwen36-35b
- 原因：KV offload 到 CPU → decode 每步需 CPU↔GPU 数据搬运
- **建议**: 如果场景不要求 131K 长上下文，降低 kv-offloading-size 可显著提升吞吐

### 5. Embedding 模型
| 模型 | 短输入 | 长输入 (~1K tok) | Dim |
|------|--------|------------------|-----|
| bge-m3 | ~200ms | ~510ms | 1024 |
| qwen3-embedding-0.6b | ~17ms | ~980ms | 1024 |

- bge-m3 更稳定（长输入不退化）
- qwen3-0.6b 短输入极快，但长输入性能骤降

## 测试配置

| 维度 | 值 |
|------|---|
| Input 长度 | 1% / 5% / 10% of max_model_len |
| Output 预算 | 512 / 4096 tokens |
| 并发 | 1 / 2 / 4 (受 max_num_seqs 限制) |
| 重复 | 3 次 |
| 指标 | TTFT (首token延迟), TPS (decode吞吐), E2E (端到端) |
| 协议 | streaming chat completions via proxy |

## 未测试

- **qwen25-omni-3b**: llama_cpp 模型，proxy 不支持 ollama chat 路由
- **gemma-4-31B**: 部分并发测试因 proxy 切换冲突 (503/409) 中断
- **长上下文 (>10% max_model_len)**: 受测试时间限制未覆盖
- **多模态输入 (图片)**: 仅测文本，VL 图片性能待后续补充
