# Unified Start: Metadata-Driven Start Path

**Status:** Design
**Date:** 2026-09-20
**Author:** Nova + Claude Code
**Predecessor:** `docs/superpowers/plans/2026-09-20-unified-stop-metadata-driven.md` (merged to main)
**Branch:** `unified-start-metadata-driven` (to be created)

## Goal

Make model **start** symmetric with the already-unified, metadata-driven **stop**
path. Today the dispatch layer (`_start_model`, `model_lifecycle.py:50-65`) is
symmetric — both start and stop do `get_adapter(model.type).{start,stop}(model)`
— but **below dispatch, start does not read the metadata** that stop reads.
`adapter.stop` reads `model.resolved_deployment` / `model.container_name`;
`adapter.start` re-derives container names inline (ninfer, sglang) or has no
docker branch at all (vllm). The result: a future `docker+vllm` YAML passes
validation and stops correctly but **start hard-fails**, and sglang/ninfer
container names work only by coincidence (derived in two places that must agree).

This spec closes that gap: every `adapter.start` reads the same metadata its
`adapter.stop` reads, container names come from the single `ModelConfig.container_name`
property, and the facade start signatures thread `container_name` symmetrically
with stop. As part of the work, **ninfer gains a real ProcessManager launcher**
(mirroring vllm/sglang/comfyui), resolving the "ninfer has no PM framework"
oddity noted in Q3, and **sglang stop is unified to graceful `docker stop`**
(matching vllm + ninfer, so in-flight requests survive every engine's stop).

## Binding Decisions (locked during brainstorming)

| # | Decision | Rationale |
|---|---|---|
| D1 | **ninfer: full PM launcher, both start and stop.** New `process_manager/ninfer.py` with `NInferProcessManager`; `ninfer.stop` migrates off the `base._stop_docker_container` helper onto the new launcher. | ninfer joins vllm/sglang/comfyui with a real PM; fixes Q3 oddity both directions. |
| D2 | **vllm docker start: scaffold + validate gate.** `vllm.start` dispatches by `resolved_deployment` (mirrors `vllm.stop:58-65`); docker branch returns a clear error + `log.warning`; `validate_config` rejects `deployment:docker` for vllm. No speculative `build_docker_cmd`. | No vllm-docker model exists (all 5 are conda); no docker image pinned. Honest YAGNI — stop already works generically; start explicitly declines + validate prevents the trap. |
| D3 | **docker stop semantics: unify to graceful, change sglang.** All docker-deployed engines use `docker stop` (SIGTERM→wait→SIGKILL). sglang's `docker kill`/`docker rm -f` → `docker stop`. | In-flight requests survive every engine's stop. sglang was the lone brute-force outlier; vllm+ninfer were already graceful. |
| D4 | **Adapter→PM metadata passing: Approach A (thread values).** Adapter reads `model.container_name` / `resolved_deployment`, passes `container_name` as a value to `start_<engine>(cfg, container_name)`. PMs stay cfg-centric, never import `ModelConfig`. | Literal mirror of stop (`stop_sglang(port, container_name)`). No new coupling. |

## Architecture

### Three-layer contract (start mirrors stop; layering unchanged)

```
adapter.start(model)                          ← metadata reader (same layer as stop)
    │  reads model.resolved_deployment / model.container_name
    │  dispatches by deployment
    ▼
ProcessManager facade  start_<engine>(cfg, container_name)
    │  ← new signature: symmetric with stop_<engine>(port, container_name)
    ▼
per-engine launcher  process_manager/<engine>.py
    │  ninfer: NEW; vllm/sglang: existing, signature change
```

The dispatch layer (`_start_model`) is **not modified** — it is already
`get_adapter(model.type).start(model)`, symmetric with stop. All changes are
in `adapter.start` bodies, PM start signatures, and the new ninfer launcher.

### New component: `process_manager/ninfer.py`

`NInferProcessManager(BaseProcessManager)` — mirrors the structure of
`VLLMProcessManager` / `SGLangProcessManager`:

- `start_ninfer(cfg: NInferConfig, container_name: str) -> dict` — migrates
  the inline `docker run` currently in `engine_adapter/ninfer.py:72-124`.
  `container_name` is a **parameter** (no longer `cfg.container_name or
  f"ninfer-{port}"` re-derivation). Preserves the pre-start cleanup
  (`docker stop` any stale same-name container + 2s settle, lines 73-75).
  Tracks PID + container state.
- `stop_ninfer(container_name: str) -> dict` — `docker stop` with full
  guard (None-name / TimeoutExpired / FileNotFoundError / non-zero rc, each
  with `log.warning`), mirroring `base._stop_docker_container:66-100` (which
  restores the fix-1 `log.warning` observability). **No `docker rm`** —
  ninfer's `docker run` uses `--rm` (ninfer.py:81), so `docker stop` auto-removes.
- `is_ninfer_alive(port) -> bool` — mirrors `sglang.is_sglang_alive`.
- State tracking: `ninfer_pid` / `ninfer_container` properties +
  `_set_ninfer_pid` / `_set_ninfer_container` (mirror sglang.py:34-43).

### vllm scaffold (D2)

`VLLMAdapter.start` dispatches by `resolved_deployment`, mirroring
`vllm.stop:58-65`:

```python
def start(self, model: ModelConfig) -> dict:
    if self._proc is None:
        raise RuntimeError("ProcessManager not set")
    if model.resolved_deployment == "docker":
        log.warning("vllm docker start not implemented for %s — set deployment: conda", model.name)
        return {"status": "error",
                "message": "vllm docker start not implemented — set deployment: conda"}
    cfg = getattr(model, 'vllm')
    return self._proc.start_vllm(cfg)
```

`validate_config` gains (after the existing conda_env check at vllm.py:43):

```python
if model.resolved_deployment == "docker":
    issues.append("vllm docker start not yet supported — use deployment: conda")
```

### sglang convergence (D3 + container_name single source)

- `SGLangAdapter.start` → `self._proc.start_sglang(cfg, model.container_name)`.
- `SGLangProcessManager.start_sglang(cfg, container_name)` — signature gains
  `container_name`; deletes the inline `f"sglang-{cfg.served_name}"` (sglang.py:49);
  passes `container_name` to `cfg.build_docker_cmd(container_name)`.
- `SGLangConfig.build_docker_cmd(self, container_name)` — gains parameter;
  `--name` (config.py:190) uses the parameter instead of `f"sglang-{self.served_name}"`.
- `SGLangProcessManager.stop_sglang` — `docker kill` (sglang.py:113) → `docker stop`;
  **deletes `docker rm -f`** (sglang.py:115) since `--rm` auto-removes
  (config.py:181). Preserves the container-name fallback scan (sglang.py:91-111).
  Docstring updated (docker stop, not kill).

## Components / Data Flow

### container_name single source of truth

```
ModelConfig.container_name (property, config.py:464)   ← ONLY truth
    │  adapter.start(model) reads it
    ▼
facade.start_<engine>(cfg, container_name)             ← threads value (D4)
    ▼
PM.start_<engine>: uses container_name for docker --name
```

No re-derivation anywhere. `config.py:464-477` property unchanged (it is
already the canonical source that stop uses).

### Change manifest (verified by grep, post-audit)

| File | Change | Evidence |
|---|---|---|
| **NEW `process_manager/ninfer.py`** | `NInferProcessManager`: `start_ninfer(cfg, container_name)`, `stop_ninfer(container_name)`, `is_ninfer_alive(port)`, state tracking | mirrors sglang.py structure |
| `engine_adapter/ninfer.py` | `start`→`self._proc.start_ninfer(cfg, model.container_name)`; `stop`→`self._proc.stop_ninfer(model.container_name)` (off base helper); delete inline docker run/stop + `subprocess`/`time`/`Path` imports (moved to PM) | ninfer.py:63-130 |
| `engine_adapter/sglang.py` | `start`→`self._proc.start_sglang(cfg, model.container_name)` | sglang.py:53 |
| `engine_adapter/vllm.py` | `start` dispatch by `resolved_deployment` (mirror stop:58-65); docker branch error+log.warning; `validate_config` docker gate | vllm.py:51-56, 43 |
| `process_manager/sglang.py` | `start_sglang(cfg, container_name)` sig + delete L49 re-derive; `stop_sglang` kill→stop, delete rm -f (L115); keep fallback scan; docstring | sglang.py:49, 84-116 |
| `process_manager/facade.py` | `start_sglang`/`start_ninfer`/`stop_ninfer` thread container_name; add `ninfer_container` property + `_set_ninfer_pid`/`_set_ninfer_container`; `stop_all` (L304-310) add ninfer state clear; register `NInferProcessManager` in `__init__` (L41-46) | facade.py:38-46, 80-123, 304-310 |
| `config.py` | `SGLangConfig.build_docker_cmd(self, container_name)` param; L190 `--name` uses param | config.py:175-197 |
| `process_manager/base.py` | `_stop_docker_container` docstring: "Shared by docker-deployed adapters" → "vllm-docker stop" (only vllm-docker remains after ninfer migrates) | base.py:66-72 |

### Unchanged

- `_start_model` dispatch (model_lifecycle.py:50-65) — already symmetric.
- comfyui / tts / asr / llama_cpp / ollama `adapter.start` bodies + facade
  signatures — single deployment, no docker branch to miss.
- `ModelConfig.container_name` / `resolved_deployment` properties — already
  canonical (from the stop effort).
- `base._stop_docker_container` helper — retained (vllm-docker still uses it).

## Error Handling

| Path | Handling | Source |
|---|---|---|
| `ninfer.start_ninfer` (PM) | Preserve ninfer.py:107-124: Popen fail→error; immediate-exit detect (6-round poll)→error+log; health timeout→`stop_ninfer`+timeout | migrate existing |
| `ninfer.stop_ninfer` (PM) | `docker stop` full guard: None-name→warning; TimeoutExpired→warning; FileNotFoundError→warning; non-zero rc→warning+stderr[:200]; rc==0→ok. Mirrors base helper (preserves fix-1 log.warning). No `docker rm` (--rm). | base.py:66-100 |
| `sglang.stop` (graceful) | `docker stop` replaces `docker kill`; delete `docker rm -f` (--rm auto-removes); keep container-name fallback scan | sglang.py:84-116 |
| `vllm-docker start` | docker branch: error + log.warning; validate_config前置拦截 | D2 |
| ninfer PID validation | Reuse `BaseProcessManager._validate_pid` / `_pkill_by_port` / `_cleanup_pid_files` / `_wait_gpu_idle` | base.py:32-125 |

## Testing (mirrors stop's TDD structure)

| Test file / class | Tests | Mirrors stop's |
|---|---|---|
| `tests/unit/engine/test_engine_adapters.py::TestNInferAdapterStartContainerName` | start delegates `start_ninfer(cfg, model.container_name)`; container_name from property not re-derive | 3.3 stop |
| `…::TestSGLangAdapterStartContainerName` | start delegates `start_sglang(cfg, model.container_name)` | 3.2 stop |
| `…::TestVLLMAdapterStartDispatch` | conda→start_vllm; docker→error+log.warning; validate rejects docker | 3.1 stop |
| `tests/unit/engine/test_pm_ninfer.py` (NEW) | `start_ninfer`/`stop_ninfer` via mock subprocess: cmd shape, container_name threading, `docker stop` (not kill), state tracking, error branches | mirrors vllm/sglang PM tests |
| `tests/unit/engine/test_pm_sglang.py` | `stop_sglang` uses `docker stop` (not kill), no `docker rm` | sglang stop change |
| `tests/integration/test_engine_lifecycle.py` | existing autouse fixture covers isolation; add ninfer PM delegation integration assertion | reuses fixture |

**TDD order** (RED→GREEN per task): PM launchers first (ninfer new + sglang
graceful) → adapter delegation → vllm scaffold last. Same shape as stop's 3-step.

## Global Constraints (from CLAUDE.md + project)

- Python 3.10+; vLLM 0.24 (do not upgrade).
- Do not modify the proxy forwarding core path (PR-14 excluded).
- Sandbox-first governance (changes developed in working copy → pytest → smoke → merge).
- Commits end with `Co-Authored-By: Claude Code <noreply@anthropic.com>`.
- Serial SDD execution (user binding constraint: no parallel implementers).

## Out of Scope

- Actually implementing vllm-docker start (D2 explicitly defers — scaffold + gate only).
- A shared `_start_docker_container` helper (rejected: each engine's `docker run` cmd differs; unification is at the metadata level, not the cmd level).
- Passing `ModelConfig` to PMs (Approach B rejected: would couple PMs to ModelConfig and diverge from stop).
- The shelved legacy items from the stop effort (ninfer.py:186-187 dead code, `_stop_model_process` singleton mutation, base.py/ninfer.py trailing newline) — independent cleanup, not this effort.

## Risks

- **sglang stop speed regression (+~10s):** `docker stop` waits up to 10s for
  graceful shutdown vs `docker kill`'s instant. Accepted (D3) — in-flight
  request safety > switch speed. Mitigation: `docker stop -t 10` default is
  already the cap; SGLang's `--rm` means no separate rm step, partly offsetting.
- **ninfer.stop migration touches merged stop code:** the just-merged
  `ninfer.stop` (base helper path) is refactored onto the PM launcher. This is
  a refinement within the same unified-metadata-driven effort, expected per D1.
  Behavior preserved (same `docker stop` + same guards + same log.warning).
- **facade state-layer duplication:** sglang defines `sglang_container`/
  `_set_sglang_container` in both facade and sglang.py (redundant but harmless —
  same state object, same key). ninfer follows the same pattern for consistency
  rather than refactoring the duplication away (out of scope).
