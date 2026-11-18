"""inferfabric/uninstaller.py — project-scoped uninstall for models / conda envs / images.

issue #3 L2 (approved: main body of the issue, CLI first). Three kinds of
removable objects, one discipline:

- **Engine reference counting decides removability.** An artifact (conda env /
  docker image) can only be removed when *no* models.d config references it.
  `collect_engine_refs` is the project-side reference index; callers may pass
  `extra_sources` (callables or EngineRefs) so platform forks and future
  layouts (project-owned env prefixes, pack directories) can contribute
  references without hardcoding paths here — no path assumption is baked in.
- **Images: ledger-scoped.** Only refs recorded in the managed-image ledger
  (image_manager, L3b) are "ours"; a user-pulled image is never removed,
  referenced or not.
- **Envs: contained in the project root.** The prefix must resolve inside
  `<IFF_DATA_DIR>/envs` (project-managed prefixes); `~/miniconda3/envs` is
  user-level legacy and never removable here. The canonical removal is
  `conda env remove -p <prefix> -y`; a direct directory delete is opt-in
  (`force=True`) and still contained.
- **Q5 (final): `~/miniconda3/pkgs` is a user-level shared cache — never
  touched here, not even on env removal.**
- **Model weights are user assets.** `remove model` deletes the models.d YAML
  (it is project config) and reports the weight paths it did *not* delete.

Everything destructive is dry-run-first: `dry_run=True` returns the planned
actions and performs zero side effects (no stop, no file writes, no docker).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from inferfabric.image_manager import (
    ManagedImageLedger,
    default_ledger_path,
    image_identity_value,
)

log = logging.getLogger("inferfabric.uninstaller")


class PoolsConfigError(RuntimeError):
    """model_pools.yaml unreadable/invalid — the reference gate must fail closed.

    The live pool registry keeps its last good snapshot in memory; silently
    treating an unreadable file as "no references" would delete a model the
    proxy still routes to (PR #7 review, blocking 2).
    """


# ── Engine reference index ─────────────────────────────────────

@dataclass
class EngineRefs:
    """artifact → set of owning model names (both directions for reports)."""

    conda_envs: dict[str, set[str]] = field(default_factory=dict)
    images: dict[str, set[str]] = field(default_factory=dict)

    def merge(self, other: "EngineRefs") -> "EngineRefs":
        for attr in ("conda_envs", "images"):
            dst = getattr(self, attr)
            for key, owners in getattr(other, attr).items():
                dst.setdefault(key, set()).update(owners)
        return self


_CONDA_ATTRS = ("vllm", "sglang", "comfyui", "tts", "asr")
_IMAGE_ATTRS = ("vllm", "sglang", "ninfer")


def collect_engine_refs(models: dict, extra_sources=()) -> EngineRefs:
    """Scan models.d configs (+ optional extra sources) for engine artifacts.

    extra_sources: iterable of EngineRefs or zero-arg callables returning
    EngineRefs — the injection point for fork-specific reference providers
    (e.g. pack directories). They are merged, never required.
    """
    refs = EngineRefs()
    for name, m in (models or {}).items():
        for attr in _CONDA_ATTRS:
            sub = getattr(m, attr, None)
            env = getattr(sub, "conda_env", None) if sub is not None else None
            if env:
                refs.conda_envs.setdefault(str(env), set()).add(name)
        for attr in _IMAGE_ATTRS:
            sub = getattr(m, attr, None)
            img = getattr(sub, "docker_image", None) if sub is not None else None
            if img:
                refs.images.setdefault(str(img), set()).add(name)
    for src in extra_sources or ():
        other = src() if callable(src) else src
        if isinstance(other, EngineRefs):
            refs.merge(other)
    return refs


def collect_conda_env_refs(models: dict) -> set[str]:
    """Back-compat convenience: just the referenced conda env names."""
    return set(collect_engine_refs(models).conda_envs)


# ── Model removal ──────────────────────────────────────────────

def find_model(models: dict, target: str):
    """Resolve a CLI target to (config name, ModelConfig).

    Accepts the models.d key (file stem / config name) or the served_name —
    the proxy-facing name operators actually see in requests.
    """
    if not target:
        return None, None
    if target in (models or {}):
        return target, models[target]
    for name, m in (models or {}).items():
        try:
            served = m.served_name
        except Exception:
            served = None
        if served == target:
            return name, m
    return None, None


def find_model_yaml(models_dir, name: str, cfg=None) -> Path | None:
    """Locate the models.d YAML backing `name` (stem match, then served_name)."""
    models_dir = Path(models_dir)
    direct = models_dir / f"{name}.yaml"
    if direct.exists():
        return direct
    for y in sorted(models_dir.glob("*.yaml")):
        if y.stem == name:
            return y
        if cfg is not None:
            try:
                raw = yaml.safe_load(y.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(raw, dict) and raw.get("name") == name:
                return y
    return None


def weight_paths(cfg) -> list[str]:
    """Weight paths a model consumes — reported, never deleted."""
    paths: list[str] = []
    vllm = getattr(cfg, "vllm", None)
    sglang = getattr(cfg, "sglang", None)
    ninfer = getattr(cfg, "ninfer", None)
    for sub in (vllm, sglang):
        model_dir = getattr(sub, "model_dir", None) if sub is not None else None
        if model_dir:
            from inferfabric.config import MODEL_BASE
            paths.append(str(Path(MODEL_BASE) / model_dir))
    wp = getattr(ninfer, "weight_path", None) if ninfer is not None else None
    if wp:
        paths.append(str(Path(wp).expanduser()))
    return paths


# ── model pools (IFF_DATA_DIR/model_pools.yaml) ────────────────

def _read_pools_strict(pools_path) -> dict:
    """Read + shape-check model_pools.yaml; any problem raises PoolsConfigError.

    Fail-visible on purpose: "unreadable" must never degrade to "no
    references" (that path deletes a model the live registry still routes).
    """
    try:
        raw = yaml.safe_load(Path(pools_path).read_text(encoding="utf-8"))
    except Exception as e:
        raise PoolsConfigError(f"model pool config {pools_path} unreadable: {e}") from e
    if not isinstance(raw, dict):
        raise PoolsConfigError(
            f"model pool config {pools_path} is not a mapping — refusing to "
            f"treat it as reference-free")
    pools = raw.get("pools")
    if pools is not None and not isinstance(pools, dict):
        raise PoolsConfigError(
            f"model pool config {pools_path} has a non-mapping 'pools' key — "
            f"refusing to treat it as reference-free")
    return raw


def _drop_candidates(raw: dict, model_names: set[str]) -> tuple[dict, list[dict]]:
    new_raw = copy.deepcopy(raw)
    removed = []
    for pool, p in ((new_raw.get("pools") or {}) if isinstance(
            new_raw.get("pools"), dict) else {}).items():
        if not isinstance(p, dict):
            continue
        keep = []
        for c in (p.get("candidates") or []):
            if isinstance(c, dict) and c.get("kind") == "local" \
                    and c.get("model_name") in model_names:
                removed.append({"pool": pool, "candidate_id": c.get("id"),
                                "model_name": c.get("model_name")})
            else:
                keep.append(c)
        p["candidates"] = keep
    return new_raw, removed


def pool_refs(pools_path, model_names: set[str]) -> list[dict]:
    """Local pool candidates that name any of `model_names`.

    Raises PoolsConfigError when the file exists but cannot be trusted.
    """
    pools_path = Path(pools_path) if pools_path else None
    if pools_path is None or not pools_path.exists():
        return []
    raw = _read_pools_strict(pools_path)
    rows = []
    for pool, p in (raw.get("pools") or {}).items():
        if not isinstance(p, dict):
            continue
        for c in (p.get("candidates") or []):
            if isinstance(c, dict) and c.get("kind") == "local" \
                    and c.get("model_name") in model_names:
                rows.append({"pool": pool, "candidate_id": c.get("id"),
                             "model_name": c.get("model_name")})
    return rows


def _write_pools_local(pools_path: Path, new_raw: dict) -> int:
    """Atomic local write with revision bump + pre-write validation.

    Used only when no live registry is reachable (the writer is then the
    single writer). With a live proxy the purge goes through its CAS instead.
    """
    from inferfabric.model_pool.config import load_pools_dict
    try:
        rev = int(new_raw.get("revision") or 0) + 1
    except (TypeError, ValueError):
        rev = 1
    new_raw["revision"] = rev
    parsed, issues = load_pools_dict(new_raw, current_revision=rev, strict=False)
    if parsed is None:
        raise PoolsConfigError(
            "refusing to write invalid model_pools.yaml after candidate purge: "
            + "; ".join(issues))
    fd, tmp = tempfile.mkstemp(dir=str(pools_path.parent),
                               prefix=".model_pools.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(new_raw, f, sort_keys=False, allow_unicode=True)
        os.replace(tmp, pools_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return rev


def _admin_request(method: str, path: str, *, host, port, token,
                   payload=None, timeout: float = 10.0):
    url = f"http://{host}:{port}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if token:
        headers["X-Admin-Token"] = token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode() or "{}"
        return resp.status, json.loads(body)


def purge_pool_candidates(pools_path, model_names: set[str], *, host=None,
                          port=None, token=None,
                          timeout: float = 10.0) -> dict:
    """Drop local candidates naming `model_names`; returns removed + reload state.

    Order of preference (PR #7 review, blocking 3):
    1. **Live registry CAS** — GET current raw+revision, PUT the purged raw
       with `expected_revision`. The registry validates, bumps revision and
       writes the file itself, so the running proxy picks the change up
       immediately and a concurrent dashboard save cannot be silently lost.
    2. **No reachable proxy** — the CLI is the single writer: local atomic
       write with revision bump + validation; reload state is reported as
       pending (takes effect at next proxy start / reload).

    An unreadable/invalid config raises PoolsConfigError either way.
    """
    pools_path = Path(pools_path)
    if not pools_path.exists():
        return {"removed": [], "reload": {"status": "absent"}}
    raw = _read_pools_strict(pools_path)
    new_raw, removed = _drop_candidates(raw, model_names)
    if not removed:
        return {"removed": [], "reload": {"status": "noop"}}

    host = host or os.environ.get("EDGE_PROXY_HOST", "127.0.0.1")
    port = int(port or os.environ.get("EDGE_PROXY_PORT", "8999"))
    token = token if token is not None else os.environ.get("IFF_ADMIN_TOKEN", "")

    last_err = None
    for _attempt in range(2):   # one refresh+retry on revision conflict
        try:
            _s, view = _admin_request("GET", "/admin/model-pools", host=host,
                                      port=port, token=token, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise PoolsConfigError(
                f"pool admin API refused the read (HTTP {e.code}) — refusing "
                f"to rewrite model_pools.yaml behind a live registry") from e
        except (urllib.error.URLError, OSError):
            rev = _write_pools_local(pools_path, new_raw)
            return {"removed": removed,
                    "reload": {"status": "pending", "revision": rev,
                               "detail": "proxy not reachable — file written; "
                                         "applies on next proxy start or "
                                         "/reload-config"}}
        except ValueError as e:
            # 有 HTTP 应答但 JSON 损坏：绝不回落本地写（活 registry 在跑）
            raise PoolsConfigError(
                f"pool admin API returned invalid JSON: {e} — refusing purge") from e
        live_raw = view.get("raw") if isinstance(view, dict) else None
        live_rev = view.get("revision") if isinstance(view, dict) else None
        if not isinstance(live_raw, dict) or not isinstance(live_rev, int):
            raise PoolsConfigError(
                "pool admin API returned an unexpected shape — refusing purge")
        live_new, live_removed = _drop_candidates(live_raw, model_names)
        if not live_removed:
            return {"removed": [], "reload": {"status": "noop"}}
        payload = dict(live_new)
        payload.pop("revision", None)
        payload["expected_revision"] = live_rev
        try:
            _s, res = _admin_request("PUT", "/admin/model-pools", host=host,
                                     port=port, token=token, payload=payload,
                                     timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 409:
                last_err = e
                continue
            raise PoolsConfigError(
                f"pool admin API rejected the purge (HTTP {e.code}) — "
                f"model_pools.yaml left untouched") from e
        except (urllib.error.URLError, OSError) as e:
            raise PoolsConfigError(
                f"pool admin API became unreachable during the purge: {e} — "
                f"model_pools.yaml left untouched") from e
        except ValueError as e:
            raise PoolsConfigError(
                f"pool admin API returned invalid JSON during the purge: {e} — "
                f"model_pools.yaml left untouched") from e
        return {"removed": live_removed,
                "reload": {"status": "applied-live",
                           "revision": (res or {}).get("revision")}}
    raise PoolsConfigError(
        "pool revision conflict persisted after a refresh — a concurrent "
        "writer keeps winning; model_pools.yaml left untouched") from last_err


def scenario_refs(models_dir, applied_path, model_names: set[str]) -> list[dict]:
    """Drifting scenario references after a model config is deleted.

    `models.d/scenarios.yaml` (manual sidecar) and the applied overlay
    (`~/.inferfabric/active_scenarios.yaml`, tune-written) are keyed by model
    name. Neither is purged here — they are user/derived config — but the
    removal report must show the drift instead of leaving it warning-only on
    the next load.
    """
    out: list[dict] = []
    candidates = [(Path(models_dir) / "scenarios.yaml", "scenarios.yaml")]
    if applied_path:
        candidates.append((Path(applied_path), "active_scenarios.yaml"))
    for path, label in candidates:
        if not path.exists():
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            out.append({"file": label, "status": "unreadable"})
            continue
        if not isinstance(raw, dict):
            continue
        for key in raw:
            if key in model_names:
                out.append({"file": label, "model": key})
    return out


def remove_model(target: str, *, models: dict, models_dir, pools_path=None,
                 applied_scenarios_path=None,
                 stop_fn=None, is_running_fn=None, purge_pools: bool = False,
                 dry_run: bool = False) -> dict:
    """Remove a model: stop if running, delete its models.d YAML.

    Blocked when any local pool candidate references the model, unless
    `purge_pools=True` (then those candidates are dropped in the same run,
    through the live registry's CAS when the proxy is reachable).
    Weights are reported, never deleted. A failed stop aborts the removal —
    deleting config under a live process would orphan it invisibly.
    """
    name, cfg = find_model(models, target)
    if cfg is None:
        return {"status": "error", "message": f"unknown model: {target!r}"}

    yaml_path = find_model_yaml(models_dir, name, cfg)
    if yaml_path is None:
        return {"status": "error",
                "message": f"config YAML for model {name!r} not found in {models_dir}"}

    names = {name}
    try:
        names.add(cfg.served_name)
    except Exception:
        pass

    try:
        refs = pool_refs(pools_path, names) if pools_path else []
    except PoolsConfigError as e:
        return {"status": "error", "model": name,
                "message": f"pool reference gate unavailable: {e}"}
    if refs and not purge_pools:
        return {"status": "blocked",
                "model": name,
                "message": "model is referenced by model pools — remove those "
                           "candidates first or re-run with --purge-pools",
                "pool_refs": refs}

    drift = scenario_refs(models_dir, applied_scenarios_path, names)

    actions: list[dict] = []
    running = None
    if is_running_fn is not None:
        try:
            running = bool(is_running_fn())
        except Exception:
            running = None

    if stop_fn is not None and running is not False:
        if dry_run:
            actions.append({"action": "would-stop",
                            "running": "unknown" if running is None else running})
        else:
            try:
                res = stop_fn() or {}
            except Exception as e:
                return {"status": "error", "model": name,
                        "message": f"stop failed, aborting removal: {e}",
                        "actions": actions}
            if res.get("status") == "error":
                return {"status": "error", "model": name,
                        "message": f"stop failed, aborting removal: "
                                   f"{res.get('message', 'unknown error')}",
                        "actions": actions}
            actions.append({"action": "stopped", "status": res.get("status")})
    elif running is False:
        actions.append({"action": "stop", "status": "skipped",
                        "reason": "not running"})

    if purge_pools and refs:
        if dry_run:
            actions.append({"action": "would-purge-pool-candidates",
                            "candidates": refs})
        else:
            try:
                purged = purge_pool_candidates(pools_path, names)
            except PoolsConfigError as e:
                return {"status": "error", "model": name,
                        "message": f"pool purge failed, config kept: {e}",
                        "actions": actions}
            actions.append({"action": "purged-pool-candidates",
                            "candidates": purged["removed"]})
            actions.append({"action": "pool-reload",
                            "status": purged["reload"]["status"],
                            "detail": purged["reload"].get("detail")
                            or f"revision {purged['reload'].get('revision')}"})

    if dry_run:
        actions.append({"action": "would-delete-config", "path": str(yaml_path)})
        status = "would-remove"
    else:
        try:
            yaml_path.unlink()
        except OSError as e:
            return {"status": "error", "model": name,
                    "message": f"failed to delete {yaml_path}: {e}",
                    "actions": actions}
        actions.append({"action": "deleted-config", "path": str(yaml_path)})
        status = "removed"

    out = {"status": status, "model": name, "actions": actions,
           "weights_kept": weight_paths(cfg),
           "note": "weights are user assets — never deleted by this command"}
    if drift:
        out["scenario_refs"] = drift
        out["note"] += ("; scenario files still name this model — remove those "
                        "entries manually (drift would be skipped on load)")
    return out


# ── Conda env removal ─────────────────────────────────────────

def _envs_root(envs_root) -> Path:
    """Root for removable env prefixes: project data dir (issue #3 ruling).

    `~/miniconda3/envs` is user-level legacy and read-only for this tool;
    removable envs are the project-managed prefixes under <IFF_DATA_DIR>/envs
    (L2 env-prefix layout). No such prefix exists yet, so the default root is
    safe and forward-compatible: an explicit `envs_root` stays injectable for
    tests and future layouts.
    """
    if envs_root is not None:
        return Path(envs_root).expanduser()
    from inferfabric.config import IFF_DATA_DIR
    return Path(IFF_DATA_DIR).expanduser() / "envs"


def remove_env(env: str, *, models: dict, envs_root=None, run=None,
               dry_run: bool = False, force: bool = False,
               conda_bin=None, extra_sources=()) -> dict:
    """Remove a conda env that no model config references.

    The prefix must be a direct child of the project env root
    (`<IFF_DATA_DIR>/envs` by default) — user-level conda envs are never
    removable here. `force=True` allows deleting the prefix directory
    directly when conda is unavailable; containment still applies.
    `~/miniconda3/pkgs` is never touched (Q5, declared final).
    """
    if not env or "/" in env or env in (".", ".."):
        return {"status": "error", "message": f"invalid env name: {env!r}"}

    refs = collect_engine_refs(models, extra_sources)
    owners = refs.conda_envs.get(env)
    if owners:
        return {"status": "blocked", "env": env,
                "message": f"env {env!r} is referenced by models: "
                           f"{', '.join(sorted(owners))} — change/remove those "
                           f"configs first",
                "referenced_by": sorted(owners)}

    root = _envs_root(envs_root)
    prefix = root / env
    try:
        root_r = root.resolve()
        prefix_r = prefix.resolve()
    except OSError as e:
        return {"status": "error", "env": env, "message": f"path resolution failed: {e}"}
    if prefix_r.parent != root_r:
        return {"status": "error", "env": env,
                "message": f"env prefix {prefix_r} escapes {root_r} — refusing"}
    if not prefix_r.exists():
        return {"status": "error", "env": env,
                "message": f"env prefix does not exist: {prefix_r} — removable "
                           f"envs are project-managed prefixes under {root_r}; "
                           f"user-level conda envs (~/miniconda3/envs) are "
                           f"read-only legacy (issue #3 ruling)"}

    if dry_run:
        return {"status": "would-remove", "env": env, "prefix": str(prefix_r),
                "actions": [{"action": "would-remove-env",
                             "prefix": str(prefix_r)}]}

    conda = conda_bin
    if conda is None:
        conda = shutil.which("conda")
        if conda is None:
            candidate = root.parent / "bin" / "conda"
            conda = str(candidate) if candidate.exists() else None

    if conda is not None:
        run = run or subprocess.run
        try:
            r = run([conda, "env", "remove", "-p", str(prefix_r), "-y"],
                    capture_output=True, text=True, timeout=600)
        except Exception as e:
            return {"status": "error", "env": env, "message": f"conda env remove raised: {e}"}
        if r.returncode != 0:
            return {"status": "error", "env": env,
                    "message": "conda env remove failed: "
                               f"{(r.stderr or r.stdout or '').strip()[:500]}"}
        return {"status": "removed", "env": env, "prefix": str(prefix_r),
                "actions": [{"action": "conda-env-remove",
                             "prefix": str(prefix_r)}],
                "note": "~/miniconda3/pkgs (shared package cache) is left untouched"}

    if not force:
        return {"status": "error", "env": env,
                "message": "conda not found on PATH or under <root>/../bin/conda — "
                           "re-run with --force to delete the prefix directly"}
    shutil.rmtree(prefix_r)
    return {"status": "removed", "env": env, "prefix": str(prefix_r),
            "actions": [{"action": "rmtree-env-prefix",
                         "prefix": str(prefix_r)}],
            "note": "~/miniconda3/pkgs (shared package cache) is left untouched"}


# ── Docker image removal ──────────────────────────────────────

def remove_image(ref: str, *, models: dict, ledger: ManagedImageLedger | None = None,
                 run=None, dry_run: bool = False, extra_sources=()) -> dict:
    """Remove a project-managed image that no model config references.

    "Managed" = recorded in the image ledger (image_manager L3b) *and* its
    recorded identity (repo digest for pulled images, image ID for locally
    built ones) still matches — a repointed tag is refused, not deleted.
    User-pulled images are never touched; `docker image prune` is never
    issued.
    """
    if not ref:
        return {"status": "error", "message": "image ref required"}

    refs = collect_engine_refs(models, extra_sources)
    owners = refs.images.get(ref)
    if owners:
        return {"status": "blocked", "image": ref,
                "message": f"image {ref!r} is referenced by models: "
                           f"{', '.join(sorted(owners))} — change/remove those "
                           f"configs first",
                "referenced_by": sorted(owners)}

    ledger = ledger if ledger is not None else ManagedImageLedger(default_ledger_path())
    if ref not in ledger.refs():
        return {"status": "blocked", "image": ref,
                "message": f"image {ref!r} is not in the managed-image ledger — "
                           f"only images this project pulled are removable"}

    # identity 门禁（与 prune_managed 同一纪律）：台账记录的是 pull/build 时的
    # 镜像身份（repo digest，或本地构建镜像的 image ID）；tag 可被重指，不
    # 复核身份就可能删掉台账从未见过的镜像。
    want = ledger.digest(ref)
    if not want:
        # dry-run 必须是忠实计划：执行会 blocked 的条目不能报 would-remove
        return {"status": "blocked", "image": ref,
                "message": "no image identity recorded for this ref "
                           "(unverifiable) — re-pull or redeploy it so the "
                           "ledger records its digest/image ID"}
    if dry_run:
        return {"status": "would-remove", "image": ref,
                "actions": [{"action": "would-rmi", "ref": ref,
                             "identity_recorded": want}]}
    current = image_identity_value(ref, run)
    if current != want:
        return {"status": "blocked", "image": ref,
                "message": f"image identity changed (ledger {want!r} != current "
                           f"{current!r}) — the tag now points at a different "
                           f"image; refusing"}

    if dry_run:
        return {"status": "would-remove", "image": ref,
                "actions": [{"action": "would-rmi", "ref": ref}]}

    run = run or subprocess.run
    try:
        r = run(["docker", "rmi", ref], capture_output=True, text=True, timeout=120)
    except Exception as e:
        return {"status": "error", "image": ref, "message": f"docker rmi raised: {e}"}
    if r.returncode != 0:
        return {"status": "error", "image": ref,
                "message": f"docker rmi failed: {(r.stderr or '').strip() or r.returncode}"}
    ledger.forget(ref)
    return {"status": "removed", "image": ref,
            "actions": [{"action": "rmi", "ref": ref}]}