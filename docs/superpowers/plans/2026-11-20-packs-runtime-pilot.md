# Capability Pack Runtime Pilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the same capability-pack contract works for NInfer TXT on Linux Docker and BGE embedding on Linux CPU, with a matching native pilot owned by the Mac side.

**Architecture:** Platform facts and reviewed templates drive one admission evaluator shared by the catalog and lifecycle. Managed instances go through existing engine lifecycle with a verified runtime policy and distinct ownership; temporary verification and normal start use the same policy. No public preset ships until the evidence gates pass.

**Tech Stack:** Python 3.10+, YAML manifests, Docker/NVIDIA, llama.cpp, pytest, existing InferFabric control plane.

**Spec:** [`specs/packs-platform-adaptation/spec.md`](../../../specs/packs-platform-adaptation/spec.md)

## Global Constraints

- Python 3.10+; do not upgrade existing vLLM 0.24 or change the proxy forwarding core.
- Keep existing deployment names, ports, configs and GPU services untouched. No VL requests or switch, production restart, Docker cleanup of existing instances, or real GPU validation without separate authorization.
- 4B and 8192 MiB remain the legacy defaults. No manifest-controlled policy exception or Docker flags.
- Work in sandbox first, starting from a clean baseline and carrying just the approved docs. Keep previous schema/installer/test drafts out of the new change set; do not delete them without permission. At checkpoints review owned diff and commit only when authorized.
- This plan implements phase 1 only. No public catalog rollout, new Dashboard controls, arbitrary link importer, audio packages, or vLLM-Omni work here.

## Review Focus

1. `nvidia-smi` present but telemetry times out → unknown, not CPU or GPU-ready (Task 1).
2. Written managed config identity points to a missing/colliding installed record → deny before stopping the old engine; never adopt by port/name (Task 3).
3. GPU busy between preflight and temporary launch → detect under lock, never stop the active model or reset the GPU (Task 3).
4. Health passes from an unintended responder or drifted container → reject; health alone never proves policy is active (Task 4).
5. Failure during reload and later uninstall of a package borrowing existing weights → rollback owned state only; external assets and service remain intact (Tasks 3 and 5).

---

## File map

- `inferfabric/platform/pack_probe.py`: detect platform facts; reuse existing OS and telemetry primitives, not pack schema.
- `inferfabric/packs/{schema,admission}.py`: strict optional platform declaration; trusted template registry and pure decision contract.
- `inferfabric/packs/{installer,config_gen,store,uninstaller}.py`: resolve and persist ownership and validated runtime identity; transaction/rollback. `inferfabric/proxy/pack_handlers.py`: feed trustworthy facts into catalog/install/custom flow, report decisions on existing control-plane APIs.
- `inferfabric/packs/managed_runtime.py`, `inferfabric/{manager,model_lifecycle}.py`: select managed launch policy and use the existing GPU coordination and engine dispatch; no new engine supervisor.
- `inferfabric/engine_adapter/{ninfer,llama_cpp}.py`, `inferfabric/process_manager/{ninfer,llama_cpp}.py`: apply validated settings in the existing launchers, keep legacy execution unchanged.
- `tests/unit/test_packs_*`, `tests/integration/test_managed_pack_pilot.py`: offline tests. Pilot data outside bundled catalog; evidence in `specs/packs-platform-adaptation/STATUS.md`.

### Task 1: Host facts and parsed platform constraints

**Files:** Create `inferfabric/platform/pack_probe.py`, `tests/unit/test_packs_platform_contract.py`. Modify `inferfabric/packs/schema.py`.

**Produces:** `probe_pack_host() -> PackHostFacts` (`os: str`, `arch: str`, `accelerator: Literal['nvidia','none','unknown']`, `compute_capability: tuple[int,int] | None`); `PackManifest.platforms: tuple[str, ...] | None`.

- [ ] Write `test_absent_platforms_remain_unspecified()` (`manifest.platforms is None`; `platforms` omitted on roundtrip), `test_invalid_platforms_rejected()` (empty/duplicate/unknown/`any` rejected), `test_intel_mac_not_apple_silicon()` (x86_64 facts do not match Apple Silicon), and `test_nvidia_probe_timeout_is_unknown()` (timeout → `accelerator == 'unknown'`, not `'none'`).
- [ ] Run `python3 -m pytest tests/unit/test_packs_platform_contract.py -q`; expect the new interface/behavior tests to fail on the baseline.
- [ ] Implement `probe_pack_host()` with normalized ARM/x86 architectures, a bounded device probe and explicit uncertain results; separate it from parsing. Implement `platforms` parsing and roundtrip; preserve unspecified old manifests and strict allowlists.
- [ ] Rerun the new file and `tests/unit/test_packs_schema.py`; expect all pass. Review that Task 1 does not claim runtime support or change execution.

### Task 2: Admission and scoped resource policy

**Files:** Create `inferfabric/packs/admission.py`, `tests/unit/test_packs_admission.py`. Modify `inferfabric/proxy/pack_handlers.py` for non-executing catalog preview.

**Consumes:** `PackHostFacts`, `PackManifest`. **Produces:** `evaluate_pack(manifest: PackManifest, facts: PackHostFacts, template: RuntimeTemplate | None, runtime: RuntimeEvidence, resources: ResourceEvidence, *, operation: Literal['install','start']) -> PackDecision`. Decision fields: `platform`, `isolation`, `resources`, `allowed: bool`, `reason_code: str | None` as in spec §3. `RuntimeTemplate` includes allowed platforms, allowed degraded mode, protected resource profile, and immutable expected artifact identity, all resolved by server registry; `RuntimeEvidence` and `ResourceEvidence` carry the already measured states, not client declarations.

- [ ] Write `test_template_cannot_be_widened_by_manifest()` (old Mac template + Linux → `platform_mismatch`; unknown template → `platform_unverified`), `test_docker_outage_does_not_fall_back()` (matching Linux device with missing runtime → `runtime_unavailable`), `test_native_degraded_requires_template_and_manifest_consent()`, and `test_ninfer_27b_exception_requires_exact_reviewed_identity()` (unreviewed 27B still rejected; tested profile + unknown VRAM → `resource_unknown`). Test `test_catalog_and_executor_serialize_same_reason()` using identical facts; no real process/GPU calls for catalog.
- [ ] Run `python3 -m pytest tests/unit/test_packs_admission.py -q`; expect failure until evaluator is implemented.
- [ ] Implement pure `evaluate_pack()` and registry. Use template intersection, actual isolation evidence, independent RAM/VRAM and disk gates. Use a private reviewed profile keyed by package/version/template and immutable artifact identity for the NInfer pilot; retain legacy 4B/8192 MiB defaults. Catalog attaches additive decisions to eligible entries without replacing legacy metadata; runtime unverified entries cannot advertise verified isolation.
- [ ] Rerun admission, schema and `tests/unit/test_packs_api.py`; expect all pass. Confirm rendering a catalog preview never downloads, launches, or allocates a device.

### Task 3: Transaction and shared launch decision

**Files:** Create `inferfabric/packs/managed_runtime.py`, `tests/integration/test_managed_pack_pilot.py`. Modify pack installer/config/store/uninstaller, `inferfabric/proxy/pack_handlers.py`, `inferfabric/manager.py`, `inferfabric/model_lifecycle.py`.

**Consumes:** Task 2 decisions. **Produces:** `ManagedRuntime.resolve(model: ModelConfig) -> ResolvedRuntime | None` (legacy only returns `None`; invalid managed reference raises `PackRuntimeError` with reason code) and `ManagedRuntime.verify(instance: ResolvedRuntime) -> None` (ephemeral instance; uses existing engine dispatch under shared coordination, raises `PackRuntimeError` on refusal). A resolved instance contains the reviewed template, verified identities, ownership, role and binding. Keep test replacements injectable with no runtime effects.

- [ ] Write integration tests `test_busy_after_preflight_never_launches_or_stops_existing()` (`resource_busy`, zero launch/stop/reset calls), `test_managed_reference_missing_denies_before_switch()` (zero calls to stop active model; `force` cannot override), and `test_prep_or_reload_failure_rolls_back_owned_files_only()` (injected failure preserves old YAML/route/external weights and removes only new-owned state). Also test `test_port_or_name_collision_never_adopts_or_stops_foreign_instance()` and `test_policy_rechecked_at_dispatch_for_managed_model()` (invalid managed identity never falls back through generic start).
- [ ] Run `python3 -m pytest tests/integration/test_managed_pack_pilot.py -q`; expect managed contract tests to fail on baseline.
- [ ] Add pack-only ownership persistence and versioned marker; distinguish absent legacy marker from malformed or unresolved managed marker. Check conflicts before download/config mutation; repeat immediately before publish. Reuse `GPULock` and occupancy probes at the temporary GPU check's actual launch point. Keep CPU pilot validation independent of GPU lock. Install/upgrade use the same admitted validation path, including built-ins that declare a managed policy. Private pilot bootstrap injects only a reviewed local entry and uses `verify()` before admitting/publishing; test `test_unverified_private_pilot_is_not_catalog_installable()` (catalog/install reject it before evidence; private `verify()` may run only the reviewed isolated fixture with no config publish on failure). Guard any managed custom path before download or keep it ineligible; don't route it through a weaker handler.
- [ ] Check start admission after resolving the target and before any switch/stop side effect, then revalidate resolved identity at the common adapter start boundary. Keep pilot admission idle-only for new GPU launches (no auto-displacement); temporary start never calls `switch` or GPU reset. A validated idle check and launch must be serialized by the same existing host GPU lock; do not reacquire inside an already locked path. Legacy managed YAML and hand-authored configs retain existing paths.
- [ ] Run pilot integration and `tests/unit/test_packs_installer.py`, `test_packs_upgrade.py`, `test_packs_uninstall.py`, `test_packs_wiring.py`, `tests/unit/state/test_model_lifecycle.py` (first four filenames under `tests/unit/`); expect all pass.

### Task 4: NInfer managed Docker launch

**Files:** Modify `inferfabric/engine_adapter/ninfer.py`, `inferfabric/process_manager/ninfer.py`; extend `tests/unit/engine/test_pm_ninfer.py` and `tests/integration/test_managed_pack_pilot.py`.

**Consumes:** `ResolvedRuntime` via existing adapter route. **Produces:** `start_ninfer(cfg, container_name, *, managed: ResolvedRuntime | None = None) -> dict`; existing two-argument callers remain unchanged. Expose no untrusted Docker options through manifest/YAML.

- [ ] Write `test_managed_ninfer_flags_enforce_reviewed_scope()` (same launcher policy for temporary and normal starts; host bind loopback, scoped read-only weights and allowed device, no host IPC/network or privileged, pinned identity, package-scoped writable/cache paths). Write `test_managed_ninfer_rejects_healthy_wrong_container()` (HTTP success from a mismatched identity → `runtime_identity_mismatch`, never adopt/kill foreign container), and `test_legacy_ninfer_launch_unchanged()`.
- [ ] Run `python3 -m pytest tests/unit/engine/test_pm_ninfer.py tests/integration/test_managed_pack_pilot.py -q`; expect the managed launcher checks to fail before implementation.
- [ ] Apply reviewed policy in the existing launch path; preserve existing NVFP4/MTP/context tuning. Inspect actual launched identity, selected devices and policy before treating HTTP health as readiness; separately verify out-of-host no access to the listener and no unintended egress using owned fixtures. If an enforced private network cannot supply a loopback-only serving port on the target without additional host networking, leave the pilot blocked; never widen network scope automatically.
- [ ] Repeat targeted tests; expect pass. Confirm all mock assertions work with the legacy suite and no real Docker action occurred.

### Task 5: Linux BGE CPU embedding launch and stack verification

**Files:** Modify `inferfabric/packs/{config_gen,runtime_prep}.py`, `inferfabric/engine_adapter/llama_cpp.py`, `inferfabric/process_manager/llama_cpp.py`; extend `tests/integration/test_managed_pack_pilot.py`.

**Consumes:** Task 3 lifecycle and policy contract. **Produces:** `start_llama_cpp(cfg, model_type='', *, managed: ResolvedRuntime | None = None) -> dict`, with managed embedding necessarily CPU and the approved runtime path. Old callers are unchanged.

- [ ] Write `test_bge_cpu_uses_approved_binary_and_explicit_degraded_mode()` (CPU, `/v1/embeddings` mechanism, process-group cleanup, same restrictions in temporary and normal launch, no GPU lock/reset), and `test_uninstall_managed_pack_never_deletes_borrowed_weights()`.
- [ ] Run `python3 -m pytest tests/integration/test_managed_pack_pilot.py -q`; expect new managed CPU behavior tests to fail on baseline.
- [ ] Use template-specific health/readiness and existing llama.cpp lifecycle. Bind loopback, enforce `gpu_layers=0` for this pilot, probe the embedding response beyond port health, and only allow the explicitly degraded profile. Keep the old Mac runtime template and do not claim process-group containment of files/network.
- [ ] Run regression `python3 -m pytest tests/unit/ tests/integration/ -q`; expect zero failed. Run `python3 -c 'import inferfabric'` from the same tree; expect exit 0. Smoke a separately configured local proxy with `/health`, `/api/snapshot` and authorized `/admin/packs/catalog`; expect 200 and honest decision metadata. Do not use production for these tests.

### Task 6: Evidence and release gates

**Files:** Modify `specs/packs-platform-adaptation/STATUS.md` with results only. Mac implementation and later catalog/UI have separate plans.

- [ ] Have one fresh-context reviewer inspect Tasks 1–5 (especially ownership, lock behavior and truthful status); address blockers, then rerun full test/import/smoke gates. If authorized to commit, repeat from a clean worktree at the exact reviewed revision; otherwise mark clean-tree gate pending. No publishing on the basis of workspace-only tests.
- [ ] In an isolated proxy and an authorized execution window, verify Linux BGE with real embedding and prove stop/rollback/uninstall ownership. Separately, only after explicit GPU authorization and if preflight confirms no active GPU tenant, do the same lifecycle for NInfer TXT with the pinned legacy configuration parity: record image/weight identity, profile, driver, binding, policy and device evidence. Repeat warmed runs under the same fixture/concurrency for baseline and managed path; record TTFT p50/p95, output tokens/s and peak VRAM. Do not claim perf parity from an idle/health-only probe. Keep hardware gates pending if no safe window.
- [ ] Request Mac evidence through #23 for one native pack: same decision values, actual provider effect in both launch paths, actual API request and rollback/uninstall. No Mac `verified` status without evidence; no dual-platform rollout until both sides confirm semantics. The later Dashboard plan must test both platforms via isolated real-browser sessions.
- [ ] Record which gates passed, failed, or were not run. Hand off to the Dashboard plan only after real-runtime results are reviewed; subsequent preset additions use the verified template and workload-specific checks.
