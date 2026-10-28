# P1 fresh-context review and release blockers — 2026-10-27

## Release decision

**RELEASEABLE — pending user release decision.** Production unchanged.
Findings 1–5 fixed with RED→GREEN evidence; 6 resolved experimentally
(cgroup ownership, no KillMode change needed); 7 resolved by contract
narrowing in spec.md. Full regression **1802 passed** in the sandbox working
tree AND in a clean worktree of the exact committed tree; import smoke passes
(7.5.0). The fresh-context re-review of a2863c1..87bce42 returned
**no Critical, no Important findings** (see below) — 7 Minors are deferred to
the user. Do not merge, push, install the proposed systemd settings, or
restart production without explicit user authorization.

## Fresh-context re-review — CLEAN (2026-10-28)

Independent re-review of the full branch diff (a2863c1..87bce42), including
both security-review dispositions. **Verdict: no Critical, no Important.**
The reviewer independently re-ran the focused PR-20 suites (186 passed) and
the full suite (**1803 passed in 59.79s** in its tree — one-test count drift
vs our 1802, all green in both runs). Verified-correct list includes: seal
barrier soundness (no lock-order inversion, no unsealed state writer, RLock
required), watchdog fence airtight for new mutations with no remaining nested
non-reentrant acquisition, request_logger lock ordering uniform with no
close/flush_blocking deadlock path, async_server order matching spec with
every failure branch still reaching hard exit, model_lifecycle guard-skip
correct for both deploy paths with no non-ninfer escape, ninfer identity
probe strictly no-worse-than-before, smoke-test isolation (no real
ModelManager/GPU/production DB), and PR-14 exclusion (forwarding core files
untouched across the series).

Both security-review dispositions were judged and **agreed**:
(a) TOCTOU/rebuild residual — keep as is (window byte-identical to
pre-PR-20; the suggested re-check misfires both ways; benign self-reporting
failure mode); (b) bounded seal — sound (monotonic flag + RLock-held writes;
single warned straggler on ≥5s-stuck write; unbounded seal would regress the
SIGTERM path to systemd SIGKILL).

### Deferred Minors (user decision, none block release)

1. `watchdog.stop()` can raise from joining a never-started worker (thread
   exhaustion at `worker.start()`; shutdown still continues via the outer
   except, remaining workers not joined, log misattributes cause). Fix:
   skip `w.ident is None` workers before join.
2. Proposed `TimeoutStopSec=45` does not cover the sum of component bounds
   (worst case ≈65s + 5N: seal 5 + health_monitor 3 + token_collector 10 +
   power_sampler 2 + watchdog 10 + drain 30 + flush 5). Intended case fits
   (~32s). Reviewer recommends `TimeoutStopSec=75`, or computing drain
   deadline as (budget − elapsed). Seal-first already protects state if
   SIGKILL fires; loss limited to drain tail + final flush. **Decision
   needed at release time: 45 (as approved) vs 75 (reviewer
   recommendation).**
3. Thread-version dev server (`python3 -m inferfabric.proxy.handler` without
   `--async`) has no begin_shutdown seal/fence/drain — parity gap; production
   (systemd `--async`) covered. Fix: one-line `begin_shutdown` call in its
   signal handler, or explicit scope-out in spec.md.
4. Misleading final-flush log: `flush_blocking` returning `-1` is truthy →
   "Final request-log flush: -1 entries persisted" after the timeout warning.
   Fix: `if flushed and flushed > 0:`.
5. Comment overstates the barrier guarantee in the bounded-timeout case
   (state.py English comment + watchdog.py "无在途 worker 变更" vs the
   bounded barrier actually implemented and correctly documented in the
   Chinese docstrings). Fix: reword to "except an already-admitted straggler
   cut off by the bounded barrier (warned in log)".
6. Smoke-test timing margin thin (SIGTERM→exit <0.7s vs 0.4-0.5s typical;
   ~0.2-0.3s jitter margin can flake on a loaded machine). Fix: one retry on
   that assertion or bump to 0.75 (below the 0.8 double-window floor).
7. SQLite request-log entries for handlers cancelled at the drain deadline
   can be lost (executor work continues until `os._exit`; log() after the
   final buffer swap never reaches SQLite). Bounded to >30s requests; JSONL
   (sync, per-entry flush) retains the data when enabled. No code change —
   worth one docstring line in `flush_blocking`.

## Verified evidence

- Focused suites after all fixes: lifecycle+ninfer-PM 48 passed, watchdog 18,
  request-logger+shutdown 21, seal 12, async-server 34.
- Full regression after all fixes: **1802 passed in 59.87s** (sandbox working
  tree) and **1802 passed in 60.28s** in a clean worktree of the exact
  committed tree (committed-tree verification — sandbox-green ≠ committed-green).
- Import smoke: `python3 -c "import inferfabric"` passes, version 7.5.0, both
  trees.
- Seven commits on `restart-p1`: a2863c1 (spec), d22c462 (seal),
  3328459 (findings 1-2 + adopt + smoke + docs), ebf6da1 (finding 3),
  57816e3 (finding 4), 8cd15f1 (finding 5), 87bce42 (bounded seal).
- Post-fix automated security review of model_lifecycle/state raised TOCTOU
  (finding 3) and unbounded seal on the SIGTERM path; the seal issue was fixed
  (87bce42, TDD), the TOCTOU disposition is recorded under finding 3.
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

Post-fix automated security review flagged the probe/alloc TOCTOU and the
unrechecked rebuild allocation. Disposition — acknowledged, not changed:
- Container appearing between lifecycle probe and start_ninfer's inspect
  → rebuild without re-guard: this window is byte-identical to pre-PR-20
  behavior (guard and allocation were always two calls), not a regression.
- Surviving-engine rebuild (adopt failed → stop → run) allocating without
  a fresh budget check is the designed residual, documented above. The
  suggested in-PM re-check misfires both ways: before `docker stop` it
  counts the old container's own footprint and re-introduces the exact
  self-rejection pathology this fix removed; after stop it races VRAM
  teardown lag (2s settle) and can false-reject the primary recovery path
  (broken engine → replace), leaving the GPU with no engine. Failure mode
  of the residual is benign and self-reporting: fresh engine fails health
  startup → error status → operator intervenes. Local single-user
  workstation, no multi-tenant attacker model. Follow-up hardening
  (allocation retry/backoff) deferred to user decision.

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

### 6. Service cgroup ownership — RESOLVED experimentally (2026-10-27)

Tested both forms under a real throwaway user-level systemd unit
(`iff-exp-cgroup.service`, alpine `sleep 600`, no GPU):

- **Detached** (`ExecStart=docker run -d …`): after `systemctl --user stop`,
  container still running.
- **Attached** (`ExecStart=docker run …` — the CLI IS the unit's main process,
  faithful to the gateway's Popen topology): after `systemctl --user stop`,
  unit → "failed" (main process killed by signal — expected experiment
  artifact; an explicitly stopped unit is not restarted by Restart=always),
  **container still running**. The CLI did not forward the unit's SIGTERM to
  the container in this configuration.

Mechanism confirmed directly: containers are dockerd children in a
system-scope cgroup (`docker-<id>.scope` under `system.slice`), never in the
user unit's cgroup. Killing the unit's cgroup kills only the CLI client.
Default KillMode=control-group is sufficient — no KillMode change needed.

Production unit checked (non-secret directives only): `Type=simple`,
`Restart=always`, no `KillMode` (default control-group), no `TimeoutStopSec`
(default 90s). Deployment step stands: add `TimeoutStopSec=45` only.

Experiment artifacts (probe container, unit file, script) removed;
`daemon-reload` + `reset-failed` after the experiment.

### 7. Mutable image-tag promise — RESOLVED by contract narrowing (spec.md)

Config.Image is an image reference string; string comparison cannot detect a
same-tag image upgrade, and identity-based comparison (image digest) is not
warranted for a single-user workstation. The contract is instead explicitly
narrowed in `spec.md` (revised 2026-10-27): image-reference string changes
still trigger rebuild; `ninfer:auto` same-tag updates are outside detection
capability; image upgrades and parameter changes (models.d YAML) require an
explicit `iff stop`/restart — **restarting the gateway ≠ reconfiguring the
engine**. No claim that same-tag rebuilds are automatically applied.

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

All gates passed: findings 1–7 resolved, regression + import smoke + clean
committed-tree verification green, fresh-context re-review clean (no
Critical/Important; 7 Minors deferred to the user — see above). The branch is
releaseable. Remaining: commit the REVIEW.md/STATUS.md updates (finding-6/7
records + re-review outcome), clean up the finding-6 experiment artifacts
(probe container/unit/script) if not already done, then hold for the user's
release decision — merge to production main, push, systemd
TimeoutStopSec edit (45 vs 75 — see deferred Minor 2), and production
restart all require explicit authorization.
