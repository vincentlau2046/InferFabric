# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Identity

InferFabric is a **local-AI-workstation inference platform + unified gateway for NVIDIA Blackwell GPUs (RTX 5090/5090D and the RTX 50 series)**. "Gateway" here means a *stateful* gateway — it manages engine process lifecycles (start/stop/switch/sleep/wake), unlike a stateless forwarding proxy; it is not a vLLM wrapper. It turns a GPU from scattered inference environments into a programmable unified inference service. Blackwell-specific: the NInfer engine's NVFP4 weights + NVFP4 KV cache (SM 12.x) is the dedicated acceleration path; non-Blackwell cards fall back to vLLM/SGLang/Ollama routes. See [项目定位](README.md#定位) for the full positioning.

## Model Usage Constraints

- **不要主动使用 VL 模型（`Qwen38-27B-VL`）**。VL 模型平时不处于激活状态，未激活时发 VL（图像）请求会 500。需要 VL 能力时，**先向用户申请、由用户切换**（`iff switch Qwen38-27B-VL`），确认已切到 VL 模型后再使用。

## Commands

```bash
# ── CLI (./iff is a python launcher at repo root; equivalent to python3 -m inferfabric) ──
./iff status                # GPU state + active services
./iff models                # List all models in models.d/
./iff switch <model|idle>   # Switch model (auto-starts if stopped)
./iff stop <model>         # Stop a shared service
./iff reset                # Force reset to idle
./iff reconcile            # Fix state.db vs reality
./iff history              # Switch history
./iff pull <url>          # Pre-download model (ollama pull / huggingface-cli)
./iff list-downloaded     # List downloaded models
./iff sleep <model>       # L2 sleep: discard vLLM weights, wake in ~3-6s
./iff wake <model>        # Wake a sleeping model (exclusive models require GPU=idle)
./iff gpu-clear           # Clear GPU CUDA state (defrag VRAM after ComfyUI exit)

# ── Tests (run from repo root; package imports resolve via CWD) ──
python3 -m pytest tests/unit/ -v --tb=short           # Unit (pure mocks, no GPU/network)
python3 -m pytest tests/integration/ -v --tb=short    # Integration (mock HTTP + SQLite)
IFF_OPERATIONAL_CONFIRM=1 python3 -m pytest tests/operational/ -v --tb=short  # Operational (real GPU — requires confirmation)
python3 -m pytest tests/unit/proxy/test_auth.py -v    # Single test file
python3 -m pytest "tests/unit/proxy/test_auth.py::test_name" -v  # Single test case

# ── Proxy (dev server) ──
python3 -m inferfabric.proxy.handler          # Start proxy on :8999
# Optional env overrides:
#   EDGE_PROXY_HOST=0.0.0.0     Bind address (default 127.0.0.1)
#   EDGE_PROXY_PORT=8999        Listen port
#   EDGE_AUTO_SWITCH=1          Auto-switch model on request (default: off)
#   EDGE_HEALTH_CHECK=60        Health check interval (seconds)
#   IFF_ADMIN_TOKEN=<secret>    Admin route auth token

# ── Benchmarks (results → bench_results/) ──
bash benchmarks/run_all.sh
python3 benchmarks/bench_ninfer.py

# ── Recovery ──
./iff reset                          # Force idle
./iff reconcile                      # Fix state.db vs reality
bash scripts/iff-recovery.sh --full # Nuclear: SIGKILL all vLLM + nvidia-smi --gpu-reset
```

## Governance & Workflow (from `steering/constitution.md`)

- **Sandbox-first**: all changes are developed in `sandbox/` (a full gitignored working copy of the repo) → pytest → smoke test → diff review → merge into production. `sandbox/` is in `.gitignore`.
- **Quality gates**: full pytest suite passes; `python3 -c "import inferfabric"` smoke; runtime smoke (core API endpoints return 200); cross-review via LLM (AtomCode GLM-5.2).
- **Technical constraints**: Python 3.10+, vLLM 0.24 (do not upgrade), do not modify the proxy forwarding core path (excluded per PR-14).
- Per-PR specs live in `specs/` (one spec dir + `STATUS.md` per PR); product language docs in `steering/` (bounded-context.md, product.md, ubiquitous-lang.md) and inside `inferfabric/steering/`.

## Architecture Overview

### Core Design: Model as Plugin + Engine Adapters

```
YAML in models.d/
       │
       ▼  auto-discovered
   ModelConfig (config.py)
       │
       ▼  dispatch by model.type
   EngineAdapter (engine_adapter/*.py)
       │  — vllm.py, sglang.py, ninfer.py, ollama.py,
       │    ollama_cpp.py, comfyui.py, tts.py, asr.py
       ▼
   ProcessManager (process_manager/*.py)
       │  starts/stops actual subprocesses (vLLM, Docker, etc.)
       ▼
   vLLM / SGLang / NInfer / Ollama / …
```

Every `type` (vllm/sglang/ninfer/ollama/ollama_cpp/comfyui/tts/asr) maps to an `EngineAdapter` registered via `engine_adapter/__init__.py` `register()`. The adapter handles start/stop/health-check/validate/sleep for that engine; `process_manager/facade.py` (ProcessManager) dispatches to the per-engine subprocess launchers.

### Dual-Protocol Routing & Proxy Chain

```
Client (OpenAI or Anthropic API)
       │
       ▼  :8999
   ProxyHandler (proxy/handler.py)
       │
       ├─ Auth (proxy/auth.py)
       ├─ Rate Limiter (ratelimit.py — DualGateLimiter: per-model RPM + concurrency semaphore)
       ├─ Response Cache (proxy/response_cache.py — cachetools LRU, temp=0 only)
       ├─ Switch Guard — 503 + Retry-After while a model switch is in progress
       │
       ├─ Local route ──▶ find_model_by_served_name() → target_port → forward_to_backend
       │                   (auto-switch if inactive when EDGE_AUTO_SWITCH=1; cloud retry 3x)
       │
       └─ Cloud route ──▶ CloudDiscovery.resolve_route() → forward_to_cloud()
                             (9 provider presets, zero-protocol-translation passthrough)
```

The proxy handles **both** `/v1/chat/completions` (OpenAI) and `/v1/messages` (Anthropic) on the same port 8999. Auto-switch is opt-in (`EDGE_AUTO_SWITCH=1`) — without it, a request to an inactive local model returns a clear 503. Unknown model → explicit 404 + `AnomalyEvent` (no silent fallback).

### GPU State Machine (no drift)

```
idle ─→ exclusive   (one heavy model, full GPU)
  │
  └──→ shared       (many small models, coexist)
         │
         └──→ idle   (return to idle anytime)
```

State is **derived from actual process scans**, not a persisted flag: `GpuStateMachine._derive_gpu_mode()` in `gpu_state.py` scans ports via `fuser` and health-checks every model; transitions are validated by `validate_transition()` in `state.py`.

### Key Modules

| Layer | Module | Responsibility |
|---|---|---|
| Model config | `config.py` | YAML loading, ModelConfig dataclass, constants |
| GPU state | `gpu_state.py`, `state.py` | State machine, reconciliation, transition validation |
| Model lifecycle | `model_lifecycle.py` | Start/stop/wake/sleep per engine type |
| Orchestration | `manager.py` | Thin facade coordinating lifecycle + GPU state + GPULock |
| Proxy | `proxy/handler.py` | HTTP server, routing, dashboard serving |
| Chat routing | `proxy/chat_handlers.py` | Chat completion dispatch (local/cloud/native Ollama) |
| Cloud | `cloud_discovery.py`, `cloud_presets.yaml` | Provider auto-discovery, 9 presets, route resolution |
| Forwarding | `forwarder.py` | Local/cloud HTTP forwarding with retry + timeout |
| Auth | `proxy/auth.py` | API key validation (YAML-based key store) |
| Rate limit | `ratelimit.py` | DualGateLimiter (per-model RPM + concurrency semaphore) |
| Response cache | `proxy/response_cache.py` | LRU cache for temperature=0 requests |
| Anomaly | `anomaly_collector.py` | Ring-buffer structured anomaly events (R9) |
| Metrics | `prometheus.py`, `proxy/metrics.py` | Prometheus `/metrics` (R6) + JSON `/api/metrics` |
| Request log | `proxy/request_logger.py`, `request_log_db.py` | JSONL + SQLite request log (R1) |
| Load balance | `proxy/replica_selector.py` | `least_busy`/`round_robin` per-replica selection (R7) |
| Watchdog | `watchdog.py` | Auto-restart failed model processes |
| Dashboard | `dashboard/` | macOS-style sidebar UI (HTML/CSS/JS in `dashboard/fragments/` + `dashboard/js/`) |
| Config hot-reload | `config_reloader.py`, `config_watcher.py` | `SIGHUP`/`/reload-config` watcher + drift detection |
| Persistence | `db.py` (IFFDB), `state.py` (StateDB) + `migrations/` v001–v005 | SQLite state (runtime `.state.db`, data in `~/.inferfabric`) |

### Test Structure

```
tests/
├── conftest.py              # Shared fixtures (tmp_state_db, tmp_iffdb) + markers
├── unit/                    # Pure unit tests (mocked deps): proxy/, engine/, config/, state/, infra/
├── integration/             # Multi-component: test_engine_lifecycle.py, test_request_log_db.py
├── operational/             # Real GPU ops, guarded by IFF_OPERATIONAL_CONFIRM=1
└── report/                  # Test reports (full-test-report-*.md)
```

Run any test from the repo root (it sets up PYTHONPATH to the repo root automatically; `inferfabric` imports resolve via CWD).

### Env Configuration

```bash
EDGE_PROXY_HOST=127.0.0.1         # Bind address
EDGE_PROXY_PORT=8999              # Listen port (default 8999)
EDGE_AUTO_SWITCH=0                # Auto-switch on request (1 to enable)
EDGE_HEALTH_CHECK=60              # Health check interval (s)
IFF_ADMIN_TOKEN=                  # Admin route auth (empty = localhost-only)
IFF_DATA_DIR=~/.inferfabric       # Data dir (state.db, logs, secrets, cloud_provider.yaml)
IFF_OPERATIONAL_CONFIRM=1         # Unlocks operational tests
```

### Dependency Note

Runtime dependencies (aiohttp, cachetools, attrs, multidict, yarl, etc.) are **vendored in `_deps/`** — `proxy_manager.py` appends `_deps` to `sys.path`. No `pip install` needed; `_deps/` is untracked but required at runtime.

### Performance Notes

- Single RTX 5090D 32GB GPU (512-bit GDDR7)
- NInfer engine at port 8007: NVFP4 KV cache, 615K tokens, MTP 4-token speculative decoding
- vLLM with NVFP4 KV cache
- Gateway hardening: retry (3x backoff), timeout (600s), ResponseCache (cachetools LRU), DualGateLimiter, ReplicaSelector (least_busy/round_robin), AnomalyCollector, Prometheus metrics

### Reference Docs

- `docs/diagrams/architecture-flow.md` — canonical end-to-end request flow (architecture, local routing, cloud routing, anomaly flow, cache flow, metrics pipeline)
- `api-spec/openapi.yaml` — OpenAPI 3.1 spec (served at `GET /api/openapi.json`)
- `steering/` + `inferfabric/steering/` — product language (bounded context, constitution)
