# Architecture Review: `gpu_role` vs `type` Orthogonality

> **Date:** 2026-07-07
> **Scope:** `cpu_only -> gpu_role` migration, cross-cutting impact on proxy routing, GPU state machine, and future extensibility.

---

## 1. Current Design Orthogonality: Adequate but with a Hidden Coupling

### 1.1 Semantic Analysis

| Dimension | Values | Meaning |
|-----------|--------|---------|
| `type` | `vllm`, `ollama`, `llama_cpp`, `comfyui`, `ollama_daemon` | **推理后端** — 请求转发目标 |
| `gpu_role` | `exclusive`, `shared`, `none` | **GPU 资源角色** — 调度策略 |

**Verdict:** 语义上真正正交。`type` 回答"请求去哪", `gpu_role` 回答"占不占 GPU"。三者组合空间是 5 x 3 = 15，当前仅使用 7 个组合。

### 1.2 Current Combination Matrix

| type \ gpu_role | exclusive | shared | none |
|----------------|-----------|--------|------|
| `vllm` | Qwen3.6-27B, Qwen3.6-27B-VL, Gemma4-26B | Qwen3.5-9B | — |
| `comfyui` | — | ComfyUI | — |
| `ollama` | — | — | llama3-1b, phi3-mini |
| `llama_cpp` | — | — | qwen25-omni-3b |
| `ollama_daemon` | — | — | ollama-daemon |

### 1.3 The Hidden Coupling

**Present assumption:** `type in (ollama, llama_cpp) -> gpu_role == none`

This is implicit, not enforced. Code evidence:

- **config.py L373:** Default `gpu_role = "none"`. No validation that `type: ollama` requires `gpu_role: none`.
- **manager.py L181:** `is_cpu_only` filter for GPU state machine works by accident — it happens that all non-CPU models are `vllm` or `comfyui`.
- **proxy.py L502:** `model_obj.is_ollama or model_obj.is_llama_cpp` groups both as "ollama-family -> use /api/chat". This is correct for routing but couples type to endpoint, not gpu_role.

**Risk:** Low today, medium long-term. If someone adds `type: ollama + gpu_role: shared` without realizing the implications, nothing prevents it at the YAML level — but the manager doesn't handle it in `_deploy_model`, `_shared_add_service`, or `stop_service`.

---

## 2. Proxy Routing: Already Correctly Decoupled

### 2.1 Current Routing Logic (proxy.py L498-510)

```
if is_ollama or is_llama_cpp -> /api/chat (Ollama native)
else -> /v1/chat/completions (vLLM)
```

**Analysis:** This routes by `type`, not `gpu_role`. The routing is:

- `type -> upstream endpoint` (correct, type determines the wire protocol)
- `gpu_role -> scheduling decision` (correct, gpu_role determines GPU mode transitions)

**Conclusion:** Proxy routing is already correctly decoupled. It routes by type (protocol), which is independent of gpu_role (resource policy). No refactoring needed.

### 2.2 What Changes if `type: ollama + gpu_role: shared` Appears?

Proxy behavior: **Zero changes.** The proxy routes by `type` regardless of `gpu_role`. An `ollama` model with `gpu_role: shared` still goes to `/api/chat`.

Manager behavior: **Needs changes.** See section 3.

---

## 3. GPU State Machine: Gap Analysis

### 3.1 Current Behavior (manager.py L180-190)

```python
gpu_services = [s for s in actual_services
                if not (self._models.get(s) and self._models[s].is_cpu_only)]
```

Only non-CPU services participate in GPU mode determination. Currently this means only `vllm` and `comfyui` services set `gpu_mode` to `exclusive` or `shared`.

### 3.2 Future Scenario: `type: ollama + gpu_role: shared`

**Problem:** The current state machine has a fundamental mismatch with Ollama's architecture.

#### Ollama GPU Model: Process-Level, Not Model-Level

Ollama daemon (`ollama serve`) holds GPU memory at the **process level**, not the model level. When you load `llama3.1:8b` with GPU, the daemon allocates VRAM for that model. When you load `qwen2.5:7b` next, it either:

1. Shares the same GPU context if `num_gpu` allows coexistence
2. Unloads the previous model and re-allocates

This is **fundamentally different** from vLLM, where each model runs in its own process and holds VRAM independently.

#### Implications for State Machine

| Scenario | vLLM model | Ollama model |
|----------|-----------|------------|
| GPU allocation | Per-process, independent | Per-model within single daemon process |
| Coexistence with shared vLLM | Yes — separate processes | **Uncertain** — Ollama daemon shares GPU with vLLM via the same physical GPU, but they compete for VRAM without coordination |
| `typical_vram_pct` | Meaningful — vLLM self-reports | **Approximate** — Ollama doesn't expose per-model VRAM to InferFabric |
| Sleep/Wake | Supported (L2) | **Not applicable** — Ollama manages its own model lifecycle via `keep_alive` |

### 3.3 Recommendation

**Short-term (current codebase):**

The `is_cpu_only` filter at L181 is correct as-is. Ollama models should NOT participate in the GPU state machine because:

1. They are served by a shared daemon (ollama-daemon), not independent processes
2. The daemon's GPU usage is opaque to InferFabric's `gpu_used_mb()` / `gpu_total_mb()` measurements
3. Mixing Ollama's process-level GPU with vLLM's model-level GPU creates false mode transitions

**Medium-term (when `type: ollama + gpu_role: shared` is needed):**

Consider a **third GPU state**: `ollama_shared`. Or better, introduce a `gpu_scope` field:

```yaml
gpu_role: shared
gpu_scope: model   # vLLM, ComfyUI — per-model VRAM
# vs
gpu_role: shared
gpu_scope: process # Ollama — daemon-level GPU
```

This allows the state machine to reason about which services actually compete for GPU resources.

**Alternatively (simpler):** Add a boolean `manages_gpu: true/false` that gates state machine participation, independent of `gpu_role`. This is cleaner because it separates "uses GPU" from "InferFabric manages GPU."

---

## 4. Model Card `gpu_role` Strategy: Assessment

### 4.1 Proposed Strategy

| Model Scale | Recommended `gpu_role` | Rationale |
|------------|-------------------------|-----------|
| < 3B quantized | `none` | CPU is sufficient (e.g. 1B Q8 on 16 cores ~ adequate throughput) |
| 7-13B quantized | `shared` | GPU acceleration meaningful but doesn't dominate VRAM |
| > 27B | `exclusive` | VRAM demand is total, exclusive optimal |
| ComfyUI | `shared` | Coexists with shared vLLM |

### 4.2 Assessment

**Overall: Correct, with two caveats.**

#### Caveat 1: The 7-13B `shared` Assumption Depends on GPU Hardware

On a 48GB GPU (RTX 6000 Ada), a 9B GPTQ-4bit model uses ~5GB — `shared` makes sense. On a 24GB GPU (RTX 4090), that same model uses ~20% but the headroom for another shared model is tighter.

**Recommendation:** The `typical_vram_pct` field is the right mechanism for this. The strategy should be:
- Default `gpu_role` by model scale as proposed
- Override based on `typical_vram_pct` calculation: if `sum(typical_vram_pct for all shared) > 80`, bump to `exclusive`

#### Caveat 2: llama.cpp `gpu_layers` Creates a Split-GPU Scenario

`llama_cpp.gpu_layers` allows partial GPU offload (e.g. 20 layers on GPU, rest on CPU). This means an `llama_cpp` model can have `gpu_role: shared` where it only uses ~30% VRAM but still needs GPU compute.

The current `is_cpu_only` check at manager.py L181 would exclude it if `gpu_role: none`, but if you set `gpu_role: shared` for a partial-GPU llama.cpp model, it would participate in state machine transitions — which is correct because it does hold VRAM.

**Verdict on llama.cpp + shared:** This is actually the easiest case. llama.cpp runs as an independent process (like vLLM), so VRAM usage is independent and measurable. The only difference is that you need to track the `gpu_layers` -> VRAM mapping, which `typical_vram_pct` already covers.

### 4.3 Refined Strategy Table

| Model Scale | Framework | `gpu_role` | Reasoning |
|-----------|-----------|-----------|-----------|
| < 3B quantized | Any | `none` | CPU sufficient, no VRAM needed |
| 3-7B quantized | vLLM | `shared` | ~4-8GB VRAM, shares well |
| 3-7B quantized | llama_cpp | `shared` (with `gpu_layers > 0`) | Partial GPU offload |
| 7-13B quantized | vLLM | `shared` | ~6-12GB VRAM, shares well on 48GB |
| 7-13B quantized | ollama (daemon) | `shared` — **but** consider `gpu_scope: process` | See section 3.3 |
| > 27B | Any GPU | `exclusive` | Dominates VRAM |
| ComfyUI | N/A | `shared` | ~50% VRAM, coexists with 1 shared vLLM |

---

## 5. Code Gaps in Current Migration

### 5.1 `switch()` Doesn't Handle `gpu_role: none` Targets

**Location:** manager.py L306
```python
target_mode = model.gpu_role  # 'exclusive' or 'shared'
```

When `target_mode == "none"`:
- `validate_transition(current_mode, "none")` -> `None` (not in `_VALID_TRANSITIONS`)
- Falls through to the generic error case
- **Result:** `switch("llama3-1b")` will error with "Invalid transition: idle -> none"

**Fix:** The `switch()` method needs to handle `gpu_role: none` models as a special path that bypasses GPU state machine transitions. CPU-only models don't change GPU mode — they just start/stop alongside whatever GPU mode is active.

### 5.2 `_shared_add_service()` Only Handles vLLM and ComfyUI

**Location:** manager.py L625-628
```python
if model.is_vllm:
    results[model.name] = self._proc.start_vllm(...)
elif model.is_comfyui:
    results[model.name] = self._proc.start_comfyui(...)
```

Missing `is_ollama` and `is_llama_cpp` branches. If you switch to an `llama_cpp` model in shared mode, `_shared_add_service` silently produces no results and the model never starts.

**Fix:** Add branches for `is_ollama` and `is_llama_cpp` to `_shared_add_service`, or centralize the deployment logic to avoid this duplication.

### 5.3 `stop_service()` Calls `wait_gpu_free()` for All `needs_gpu` Models

**Location:** manager.py L685
```python
if model.needs_gpu:
    if not wait_gpu_free(timeout=20):
```

For `ollama` type models, `wait_gpu_free()` is misleading — the Ollama daemon still holds GPU after "stopping" a model (because the model was loaded into the daemon, not a separate process). The `keep_alive` setting controls this, but InferFabric can't distinguish "daemon still holds GPU for another model" from "orphaned GPU usage."

---

## 6. Summary of Findings

| Question | Verdict | Action |
|----------|---------|--------|
| 1. Orthogonality sufficient? | **Yes, semantic gap exists but is manageable** | Document the gap; no code change needed for current models |
| 2. Proxy routing needs refactoring? | **No — already correctly decoupled** | `type -> endpoint`, `gpu_role -> scheduling` is correct separation |
| 3. GPU state machine affected by ollama? | **Yes — `switch()` breaks for `gpu_role: none` targets** | Add `none` handling to switch logic; consider `manages_gpu` flag for future ollama+GPU models |
| 4. Model card strategy sound? | **Yes, with hardware-aware caveats** | Use `typical_vram_pct` for overrides; watch ollama daemon vs llama.cpp distinction |

### Critical Fix (Blocks Future `gpu_role: none` Switching)

The `switch()` method must handle `gpu_role: none` models. Currently they cannot be switched to via `iff switch` because `validate_transition(x, "none")` fails. This is the most immediate code gap to address.

### Evolutionary Enhancement (For `ollama + shared` Future)

When ready to support GPU-accelerated Ollama models, introduce `manages_gpu: bool` to separate "uses GPU" from "InferFabric controls GPU lifecycle." This is cleaner than adding a third GPU mode or scope dimension.