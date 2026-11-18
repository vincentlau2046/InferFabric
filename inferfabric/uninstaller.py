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
- **Envs: contained.** The prefix must resolve inside CONDA_ENVS; the canonical
  removal is `conda env remove -p <prefix> -y`. A direct directory delete is
  opt-in (`force=True`) and still contained.
- **Q5 (final): `~/miniconda3/pkgs` is a user-level shared cache — never
  touched here, not even on env removal.**
- **Model weights are user assets.** `remove model` deletes the models.d YAML
  (it is project config) and reports the weight paths it did *not* delete.

Everything destructive is dry-run-first: `dry_run=True` returns the planned
actions and performs zero side effects (no stop, no file writes, no docker).
"""

from __future__ import annotations

import copy
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from inferfabric.image_manager import (
    ManagedImageLedger,
    default_ledger_path,
)


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

def pool_refs(pools_path, model_names: set[str]) -> list[dict]:
    """Local pool candidates that name any of `model_names`."""
    pools_path = Path(pools_path) if pools_path else None
    if pools_path is None or not pools_path.exists():
        return []
    try:
        raw = yaml.safe_load(pools_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = []
    for pool, p in ((raw or {}).get("pools") or {}).items():
        if not isinstance(p, dict):
            continue
        for c in (p.get("candidates") or []):
            if isinstance(c, dict) and c.get("kind") == "local" \
                    and c.get("model_name") in model_names:
                rows.append({"pool": pool, "candidate_id": c.get("id"),
                             "model_name": c.get("model_name")})
    return rows


def purge_pool_candidates(pools_path, model_names: set[str]) -> list[dict]:
    """Drop local candidates naming `model_names`, keeping everything else.

    Read-modify-write with an atomic replace and a revision bump (the file is
    CAS-managed by the registry; a stale writer must not overwrite newer
    edits). Invalid resulting config is rejected, not written.
    """
    pools_path = Path(pools_path)
    if not pools_path.exists():
        return []
    raw = yaml.safe_load(pools_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return []
    new_raw = copy.deepcopy(raw)
    removed = []
    for pool, p in (new_raw.get("pools") or {}).items():
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
    if not removed:
        return []
    # 写前校验（strict=False：沿用磁盘存量的容忍语义）；非法结果绝不落盘
    from inferfabric.model_pool.config import load_pools_dict
    try:
        rev = int(raw.get("revision") or 0) + 1
    except (TypeError, ValueError):
        rev = 1
    new_raw["revision"] = rev
    parsed, issues = load_pools_dict(new_raw, current_revision=rev, strict=False)
    if parsed is None:
        raise ValueError("refusing to write invalid model_pools.yaml after "
                         "candidate purge: " + "; ".join(issues))
    fd, tmp = tempfile.mkstemp(dir=str(pools_path.parent), prefix=".model_pools.", suffix=".tmp")
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
    return removed


def remove_model(target: str, *, models: dict, models_dir, pools_path=None,
                 stop_fn=None, is_running_fn=None, purge_pools: bool = False,
                 dry_run: bool = False) -> dict:
    """Remove a model: stop if running, delete its models.d YAML.

    Blocked when any local pool candidate references the model, unless
    `purge_pools=True` (then those candidates are dropped in the same run).
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

    refs = pool_refs(pools_path, names) if pools_path else []
    if refs and not purge_pools:
        return {"status": "blocked",
                "model": name,
                "message": "model is referenced by model pools — remove those "
                           "candidates first or re-run with --purge-pools",
                "pool_refs": refs}

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
            removed = purge_pool_candidates(pools_path, names)
            actions.append({"action": "purged-pool-candidates",
                            "candidates": removed})

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

    return {"status": status, "model": name, "actions": actions,
            "weights_kept": weight_paths(cfg),
            "note": "weights are user assets — never deleted by this command"}


# ── Conda env removal ─────────────────────────────────────────

def _envs_root(envs_root) -> Path:
    if envs_root is not None:
        return Path(envs_root).expanduser()
    from inferfabric.config import CONDA_ENVS
    return Path(CONDA_ENVS).expanduser()


def remove_env(env: str, *, models: dict, envs_root=None, run=None,
               dry_run: bool = False, force: bool = False,
               conda_bin=None, extra_sources=()) -> dict:
    """Remove a conda env that no model config references.

    `force=True` allows deleting the prefix directory directly when conda is
    unavailable; the prefix must still be a direct child of CONDA_ENVS.
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
                "message": f"env prefix does not exist: {prefix_r}"}

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

    "Managed" = recorded in the image ledger (image_manager L3b). User-pulled
    images are never touched; `docker image prune` is never issued.
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