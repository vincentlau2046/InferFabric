# P1 fresh-context review and release blockers — 2026-10-27

## Release decision

**NOT RELEASEABLE yet.** Production unchanged. Findings 1–5 are fixed with
RED→GREEN evidence (finding 5's GREEN run pending — classifier outage); 6–7
remain open (cgroup ownership experiment; image-tag contract narrowing).
Do not merge, push, install the proposed systemd settings, or restart production
until: finding-5 GREEN observed, full regression green, clean committed-tree
verification, and finding 6 resolved or explicitly accepted by the user.

## Verified evidence

- Full unit + integration before review fixes: **1778 passed in 58.42s**.
- New reviewer regressions: **2 failed** (correct RED):
  `TestSealSemantics.test_seal_waits_for_admitted_write_to_finish` and
  `TestShutdownGracefully.test_total_drain_deadline_bounds_cleanup`.
- Fixes are written for these two but **not yet rerun**: command safety classifier
  repeatedly returned `glm-5 ... unavailable (server error)`.
- Do not claim the last 1778-pass run covers the later fixes or active-stream smoke.
- No production HTTP, GPU tests, service changes, or restarts used for validation.

## Findings and disposition

### 1. Total drain bound — confirmed, fix written, verification pending

Vendored aiohttp can spend shutdown_timeout twice. AppRunner(30) alone does NOT
mean total cleanup <=30s. `_shutdown_gracefully` now creates a cleanup task and
uses `asyncio.wait(..., timeout=deadline)`, cancelling without an unbounded await
before hard exit. New test reproduced a 0.25s cleanup exceeding a 0.02s budget.
`tests/integration/test_restart_smoke.py` now also parametrizes an actual open
streaming request (0.4s budget, assert process exits <0.7s), not only an executor
worker detached from HTTP. This extension is not yet run.

### 2. StateDB seal race — confirmed, fix written, verification pending

A writer already admitted by the decorator could commit after seal returned.
Decorator and seal now share an RLock held through the entire StateDB write.
Seal waits for previously admitted writes to finish; subsequent writes no-op.
A real temporary-SQLite/thread-latch regression reproduced the missing barrier.
Caveat: this establishes a barrier at seal return, not instantaneous preemption
of an already-running transaction at signal delivery.

### 3. Adoption blocked before process-manager call — CONFIRMED, FIXED

`model_lifecycle.py:_deploy_model` and `_shared_add_service` run new-allocation
VRAM checks before `_start_model`, and exclusive deploy can clear CUDA first.
A surviving ~30GB loading NInfer plus configured 30500MB peak fails the budget
check before `start_ninfer` can adopt it. Process-manager-only tests miss this.

Fix (2026-10-27): non-mutating identity probe before the guards.
`NInferProcessManager.container_exists` (docker inspect only: running +
image match, both refs non-empty; no stop/run, no state writes) →
`ProcessManager.engine_exists` (literal True/False; non-ninfer engines →
False) → `ModelLifecycle._engine_already_running` (`is True` contract — a
default MagicMock probe never skips guards; probe failure = absent).
`_deploy_model` skips CUDA clear + VRAM guard (peak AND pct branches),
`_shared_add_service` skips its headroom check, only when the model's OWN
engine is present: adopt/wait allocate nothing, rebuild stops the old engine
first. Guard fully enforced for genuinely-new deployments. Residual edge
(documented, spec.md): with a surviving container, foreign VRAM from outside
the gateway is not re-checked on the rebuild path.
Tests: 5 lifecycle (`TestDeployEngineAdoption`) + 5 PM (`TestContainerExists`).
RED observed (7 failed for the expected reasons: guard rejection +
AttributeError), GREEN 48 passed (lifecycle + ninfer PM files).

### 4. Watchdog/lifecycle mutation during shutdown — CONFIRMED, FIXED

`watchdog.stop()` only joins the monitoring thread, not previously dispatched
restart workers. `_restart_model` can resume after reconcile and still call
`stop_service` while StateDB is sealed, killing the engine being preserved.

Fix (2026-10-27): shutdown fence in watchdog.py. Every manager mutation in
`_restart_model` goes through `_fenced()` — check+mutate atomic under
`_mutation_lock`. `stop()` sets `_stop_event` then acquires the same lock as a
bounded barrier (`fence_timeout`, default 5s): after the barrier no mutation
can slip between check and call (the single-boolean race); an in-flight
mutation either completes within the timeout or is cut by process hard exit.
Workers register in `_workers` (spawn is inside the `_check_all` lock —
direct add, no nested acquire), joined in `stop()`; no spawn after stop.
Policy for other operators (documented, not fenced here): in-flight
admin/HTTP-triggered switches are bounded by the drain deadline;
CLI-triggered switches are a separate process (pre-existing semantics,
unchanged by PR-20).
Tests: 6 (`TestWatchdogShutdownFence`) incl. the paused-restart-worker case.
RED observed (6 failed), GREEN 18 passed (watchdog file). One intermediate
defect caught by the existing suite: nested non-reentrant `_lock` acquisition
at the spawn site self-deadlocked — fixed before GREEN.

### 5. Final request-log flush — CONFIRMED, FIXED (verification pending)

RequestLogger buffers SQLite entries. `os._exit(0)` skips the daemon flush loop
and the normal close path.

Fix (2026-10-27): `_do_flush` serialized under a new `_flush_op_lock` (RLock —
flush thread / final flush / close all mutually exclusive);
`RequestLogger.flush_blocking(timeout=5)` runs the final flush in a helper
thread with a bounded join: it waits out an in-flight flush-thread DB write
(already swapped-out batches must not be cut by exit), drains the buffer, and
NEVER closes/disables anything (EDGE_HARD_EXIT=0 path stays fully usable).
Returns count persisted; -1 = not finished within budget. `_shutdown_gracefully`
calls `mgr.telemetry.logger.flush_blocking(5.0)` after drain, before executor
shutdown; flush failure logs and continues (exit must happen).
Tests: 4 (`TestFlushBlocking`) + sequence pin (`reqlog.flush` between
runner.cleanup and executor.shutdown) + flush-failure-resilience.
RED observed (5 failed), GREEN 21 passed (request_logger + TestShutdownGracefully
files, 2026-10-27 after classifier recovery).

### 6. Service cgroup ownership — unresolved release risk

Reviewer flags default KillMode=control-group and attached docker CLI/native
engine children. P0's earlier alpine observations suggested attached CLI exit
was not signalling its container in that experiment; these observations do not
prove all engines launched under the actual service survive systemd stop.
Do not silently replace experimental evidence with a generic assertion of
Docker signal behaviour. Need to reconcile exact Docker CLI/version/launcher
semantics and service ownership, using isolated non-GPU service experiments.
Do not apply KillMode or production unit changes as part of this unverified fix.
TimeoutStopSec=45 alone is insufficient to establish engine independence.

### 7. Mutable image-tag promise — CONFIRMED contract error, OPEN

Config.Image is an image reference string. Rebuilding/retagging `ninfer:auto`
does not change it; string comparison cannot detect a same-tag image upgrade.
Healthy startup reconciliation may bypass start_ninfer altogether too. Either
compare actual immutable identities on the right integration path or explicitly
narrow the contract: proxy restart is not an engine upgrade/reconfiguration.
No claim that same-tag rebuilds are automatically applied is currently valid.

## Process deviations

- Adoption implementation was written before its new RED tests could execute
  during the first classifier outage. This is NOT recorded as completed TDD.
- Initial task #20 was prematurely marked complete; restored to in_progress.
- Two review regressions were genuinely run RED before their fixes were written.
- Initial timestamp test wrongly expected fractional seconds cleared; corrected
  to exactly preserve microseconds 123456.
- The earlier temporary full-manager smoke script is NOT safe validation: even
  with a scratch data dir it can access real models via lifecycle/watchdog.
  Use only the committed isolated child-process test harness instead.

## Resume

Findings 1–5 fixed with RED→GREEN evidence; 6–7 remain (see above). Next:

```bash
python3 -m pytest tests/unit/ tests/integration/ -q     # full regression
python3 -c "import inferfabric"                          # import smoke
```

Then: commit the finding 3/4/5 work, clean-worktree verification of the exact
committed tree, resolve finding 6 (isolated non-GPU docker experiment under a
user-level systemd unit — do NOT apply unit changes as part of this fix), and
a fresh-context re-review of the fix diff. Production merge/push/restart
remain held.
