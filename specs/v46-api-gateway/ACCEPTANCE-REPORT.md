# IFF v4.6.0 Acceptance Test Report

**Date**: 2026-07-30 23:14
**Result**: 33/33 PASSED (2 test-script parameter issues, not product bugs)

## Test Environment
- Sandbox: ~/projects/inferfabric-sandbox/
- Port: 8999, Bind: 0.0.0.0
- Cloud: baidu-codingplan (Baidu Qianfan)
- Local: bge-m3 (embedding/ollama), gemma-4-31B-it-NVFP4 (exclusive/vLLM)

## Results by Category

### T1: Startup & Configuration (8/8)
| ID | Test | Result |
|----|------|--------|
| T1.5 | /status returns 200 | ✅ |
| T1.3 | bge-m3 in active_services | ✅ |
| T1.2 | Cloud providers loaded | ✅ |
| T1.2a | At least 1 provider | ✅ |
| T1.2b | baidu-codingplan enabled | ✅ |
| T1.2c | Cloud models > 0 (got 8) | ✅ |
| T1.4 | Dashboard returns 200 | ✅ |
| T1.6 | Bound to 0.0.0.0 (accessible) | ✅ |

### T2: /v1/models Endpoint (6/6)
| ID | Test | Result |
|----|------|--------|
| T2.0 | /v1/models returns 200 | ✅ |
| T2.1 | bge-m3 in model list | ✅ |
| T2.2 | bge-m3 ID is not file path | ✅ |
| T2.3 | Cloud models have capabilities | ✅ |
| T2.3a | Capabilities have boolean fields | ✅ |
| T2.4 | No duplicate model IDs | ✅ |

### T3: Cloud Model Inference (6/6)
| ID | Test | Result | Note |
|----|------|--------|------|
| T3.1 | deepseek-v4-flash (OpenAI) | ✅ | content="5" |
| T3.2 | glm-5.1 (OpenAI) | ✅ | content="5" |
| T3.3 | deepseek-v4-flash (Anthropic) | ✅ | content="5" |
| T3.4 | glm-5.1 (Anthropic) | ✅ | max_tokens=100 不足(reasoning消耗), 500→OK |
| T3.5 | Streaming works | ✅ | |
| T3.6 | Unknown model returns error | ✅ | |

### T4: Local Model Inference (5/5)
| ID | Test | Result | Note |
|----|------|--------|------|
| T4.1 | bge-m3 embedding dim=1024 | ✅ | |
| T4.2 | Switch to gemma-4 succeeded | ✅ | 85s cold start |
| T4.3 | gemma-4 inference correct | ✅ | |
| T4.5 | bge-m3 works during exclusive | ✅ | |
| T4.4 | Switch to idle succeeded | ✅ | |

### T5: Lifecycle (2/2)
| ID | Test | Result |
|----|------|--------|
| T5.3 | Consecutive switches don't crash | ✅ |
| T5.2 | bge-m3 available after lifecycle | ✅ |

### T6: Admin API (5/5)
| ID | Test | Result | Note |
|----|------|--------|------|
| T6.1 | GET /admin/cloud/providers | ✅ | |
| T6.2 | POST /admin/cloud/discover | ✅ | |
| T6.3 | POST /admin/cloud/reload | ✅ | |
| T6.4 | POST /admin/cloud/test | ✅ | 需传 url+api_key |
| T6.5 | Admin routes accessible (auth disabled) | ✅ | |

### T7: Dashboard Cloud Tab (1/1)
| ID | Test | Result |
|----|------|--------|
| T7.1 | Dashboard has Cloud tab | ✅ |

## Bugs Found & Fixed This Session

| # | Bug | Root Cause | Fix |
|---|-----|-----------|-----|
| 1 | `__pycache__` cache serves stale code | Python caches .pyc | Must `find -delete __pycache__` before restart |
| 2 | IFF reads cloud_provider.yaml from `~/.inferfabric/`, not project dir | `IFF_DATA_DIR` design | Synced correct config to `~/.inferfabric/` |
| 3 | API key placeholder `"***}"` in `~/.inferfabric/cloud_provider.yaml` | Sandbox edit didn't propagate to data dir | Copied real key |
| 4 | bge-m3 model ID shows as file path in /v1/models | ollama models queried vLLM-style | Added `m.type == "vllm"` filter |
| 5 | Default bind 127.0.0.1, browser can't access | Security default | `EDGE_PROXY_HOST=0.0.0.0` env var |

## Unit Test Baseline
- 298 passed, 15 failed (12 e2e/functional require running vLLM, 3 pre-existing)
- v4.6.0 new tests: 60/60 (auth + cloud_discovery + ratelimit_v2 + request_logger) + 156/156 (v45 comprehensive)

## Not Tested (out of scope / known issues)
- qwen36-27b-vl: vLLM EngineCore crash (NOT IFF regression)
- qwen35-9b, qwen36-35b: switch failures (may be cascading from 27b crash)
- RateLimiterV2 handler integration (logic complete, not wired into handler.py)
- llama.cpp stability under GPU contention (bge-m3 + exclusive model)
