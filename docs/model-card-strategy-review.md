# Model Card `gpu_role` Strategy Review

> Date: 2026-07-07
> Scope: Threshold rationale, current model alignment, and code gap impact under the new shared/exclusive/none strategy.

---

## 1. Threshold Assessment

### 1.1 `< 1B → none` (CPU-only)

**Verdict: Reasonable but borderline for Q8_0 models.**

| Model | Params | Quant | ~VRAM | CPU Viable? |
|-------|--------|-------|-------|------------|
| Llama3.2-1B | 1.1B | Q8_0 | ~1.1 GB | Yes — fits entirely in RAM, CPU inference is fast enough for chat |

The `< 1B` threshold is a reasonable **practical** boundary for CPU-only, but the label is slightly misleading. A better framing would be:

> **"< 2B quantized → `none`"** — models under ~2B parameters in Q4/Q8 quantization run acceptably on modern multi-core CPUs (16+ threads). The 1B threshold was chosen to avoid wasting GPU on trivial workloads.

**Caveat:** If a model is 1.5B Q8_0 (~1.5 GB), it still fits on CPU comfortably. If it's 2.2B Q4_0 (~1.4 GB), it's still fine on CPU but could benefit from GPU offload. The threshold is a *recommendation*, not a hard law — individual model cards should override based on actual latency benchmarks.

### 1.2 `2–13B → shared`

**Verdict: Sound, but the gap between 1B and 2B is worth calling out.**

| Model | Params | Quant | ~VRAM | Current Role | Strategy Role |
|-------|--------|-------|-------|-------------|---------------|
| Phi-3 Mini | 3.8B | Q4_0 | ~2 GB | `none` | `shared` ✅ should change |
| Qwen2.5-Omni-3B | 3B | Q8_0 | ~3 GB | `none` | `shared` (borderline) |
| Qwen3.5-9B | 9B | GPTQ-4bit | ~5 GB | `shared` | `shared` ✅ |

The 2–13B band captures the "sweet spot" for shared GPU: small enough to coexist with other shared services on a 48GB GPU, large enough that GPU acceleration is meaningful.

**Gap concern (1–2B):** A 1.5B Q4_0 model (~0.75 GB VRAM) could live in either bucket. Recommendation: keep `< 1B` as the hard `none` boundary, and treat 1–2B as "operator's choice" — defaults to `none` but can be overridden to `shared` if latency is critical.

### 1.3 `> 27B → exclusive`

**Verdict: Correct, but the boundary could be tighter.**

| Model | Params | Quant | ~VRAM | Current Role | Strategy Role |
|-------|--------|-------|-------|-------------|---------------|
| Gemma4-26B | 26B | NVFP4 | ~13 GB | `exclusive` | Boundary case |
| Qwen3.6-27B | 27B | NVFP4 | ~13.5 GB | `exclusive` | ✅ |
| Qwen3.6-27B-VL | 27B | NVFP4 | ~14 GB | `exclusive` | ✅ |

27B NVFP4 uses ~13–14 GB VRAM. On a 48GB GPU, you could technically fit 3 of these in shared mode. However:

- NVFP4 models with MTP (multi-token prediction) have high peak memory during KV cache allocation.
- Exclusive mode guarantees no OOM surprises during context-heavy workloads.
- `gpu_memory_utilization: 0.85` in the model cards already signals "this model wants most of the GPU."

**Recommendation:** Keep `> 27B → exclusive` as the default, but document that operators can override to `shared` if they know their workloads stay under 8K context length and won't trigger peak memory spikes.

---

## 2. Current Model Alignment

| Model | Size/Quant | Current `gpu_role` | Strategy Says | Verdict |
|-------|-----------|-------------------|---------------|---------|
| `llama3-1b` | 1.1B Q8_0 | `none` | `none` (< 2B) | ✅ |
| `phi3-mini` | 3.8B Q4_0 | `none` | `shared` (2–13B) | ⚠️ **should be `shared`** |
| `qwen25-omni-3b` | 3B Q8_0 | `none` | `shared` (borderline 1–2B) | ⚠️ **borderline — see below** |
| `qwen35-9b` | 9B GPTQ-4bit | `shared` | `shared` (2–13B) | ✅ |
| `gemma4-26b` | 26B NVFP4 | `exclusive` | boundary of >27B | ✅ (close enough) |
| `qwen36-27b` | 27B NVFP4 | `exclusive` | `exclusive` (>27B) | ✅ |
| `qwen36-27b-vl` | 27B NVFP4 | `exclusive` | `exclusive` (>27B) | ✅ |
| `comfyui` | N/A | `shared` | `shared` (by policy) | ✅ |
| `ollama-daemon` | N/A | `none` | `none` (infra) | ✅ |

### Models needing adjustment:

1. **`phi3-mini`** (3.8B Q4_0): Clearly falls in the 2–13B band. Should be `shared`, but this is currently blocked by **Issue B** (see §3.2) — `_shared_add_service()` doesn't handle `ollama` type models.

2. **`qwen25-omni-3b`** (3B Q8_0, `llama_cpp`): This is an interesting edge case. At 3B Q8_0 it's ~3 GB if fully GPU-loaded, but its `gpu_layers: 0` means it currently runs entirely on CPU. If the operator wants GPU acceleration, they'd change `gpu_layers > 0` and should set `gpu_role: shared`. Until then, `none` is correct.

---

## 3. Impact on the Two Blocking Issues

### Issue A: `switch()` fails for `gpu_role: none` targets

**Code:** `manager.py:329` — `validate_transition(current_mode, target_mode)` where `target_mode == "none"`.

`validate_transition()` only knows about `idle`, `exclusive`, and `shared`. It has no entry for `"none"` in `_VALID_TRANSITIONS`.

**Impact under current model set:** **Low.** Currently, the only `none` models are `llama3-1b`, `phi3-mini`, `qwen25-omni-3b`, and `ollama-daemon`. Switching to these is a valid user operation (`iff switch llama3-1b`), but it will produce an "Invalid transition" error.

**Impact if `phi3-mini` moves to `shared`:** **Resolved.** If phi3-mini is recategorized as `shared`, the most commonly switched small model would no longer hit this bug. But `qwen25-omni-3b` (still `none`) and `ollama-daemon` would still trigger it.

**Fix recommendation:** High priority. The fix is small — handle `target_mode == "none"` as a special case in `switch()` that bypasses GPU state machine logic entirely:

```python
# In switch(), before validate_transition:
if target_mode == GPUMode.NONE:  # or "none" as plain string
    # CPU-only model: no GPU mode transition needed
    # Just deploy alongside whatever is currently running (or solo if idle)
    result = self._deploy_cpu_only(model)
    return result
```

This should be implemented regardless of the threshold policy, since some models will always be `none`.

---

### Issue B: `_shared_add_service()` missing `ollama`/`llama_cpp` branches

**Code:** `manager.py:625-628` — only handles `is_vllm` and `is_comfyui`.

**Impact under current model set:** **Currently latent, but will surface immediately if phi3-mini moves to `shared`.**

If `phi3-mini` is changed to `gpu_role: shared`, then `switch(phi3-mini)` would reach `_shared_add_service()` at line 367, fall through both `if` branches, produce an empty `results` dict, and silently return success with no process actually started.

**Impact of `qwen25-omni-3b` as shared:** Same issue — it's `llama_cpp` type, not handled by `_shared_add_service()`.

**Fix recommendation:** High priority. Two approaches:

**Option 1 — Add branches (simpler, more explicit):**
```python
elif model.is_llama_cpp:
    results[model.name] = self._proc.start_llama_cpp(model.llama_cpp)
elif model.is_ollama:
    # Ollama models use the daemon; shared mode means
    # load into daemon and track via pull/push lifecycle
    results[model.name] = self._proc.start_ollama(model.ollama)
```

**Option 2 — Centralize deployment (cleaner long-term):**
```python
results[model.name] = self._start_model(model)
```
Where `_start_model()` dispatches by `model.type`. This avoids duplicating the type-check cascade in both `_shared_add_service` and `_deploy_model`.

**Recommendation:** Use Option 2. It eliminates the exact class of bug (forgetting a branch) and makes future backend additions (e.g., SGLang, TensorRT-LLM) automatically supported.

---

## 4. Summary & Priority Matrix

| Item | Priority | Effort | Reason |
|------|----------|--------|--------|
| **Fix `switch()` for `gpu_role: none`** | **P0** | Small | Breaks today for CPU-only model switches |
| **Fix `_shared_add_service()` for ollama/llama_cpp** | **P0** | Small | Will break immediately if phi3-mini moves to shared |
| **Update `phi3-mini` → `gpu_role: shared`** | **P1** | Trivial | Aligns with 2–13B shared policy |
| **Document 1–2B "operator's choice" zone** | **P2** | Documentation | Clarifies threshold for edge cases |
| **Consider `_start_model()` centralization** | **P2** | Medium | Prevents future branch-forgetting bugs |

---

## 5. Refined Strategy (Recommended)

```
Model parameter range (quantized) → gpu_role

<  2B    → none       (CPU sufficient, no GPU needed)
2–13B   → shared      (GPU-accelerated, fits alongside peers)
> 27B   → exclusive   (dominates VRAM, isolation preferred)
Special → shared      (ComfyUI — coexists with shared vLLM)
Special → none        (ollama-daemon — infrastructure, not a model)
```

**Override rule:** Any model can be overridden by the operator. Use `typical_vram_pct` to validate that shared models don't exceed ~80% combined VRAM on the target GPU.