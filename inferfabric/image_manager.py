"""inferfabric/image_manager.py — managed-image ledger + scoped prune (issue #3 L3b).

Docker image labels are bake-time only — a pulled image cannot be labelled
post-hoc, so `docker image prune --filter label=…` can never match pulled
images. "Managed" is therefore expressed two ways:

1. **Container labels (real docker labels)**: every docker run issued by the
   gateway carries `--label com.inferfabric.managed=true` — containers are
   created by us, so they can be labelled at run time; scoped container
   cleanup works via
   `docker container prune --filter label=com.inferfabric.managed=true`.
2. **Image ledger (functional equivalent of the image label)**: the gateway
   pulls images explicitly (replacing implicit docker-run auto-pull) and
   records each ref in a project ledger (IFF_DATA_DIR/image_ledger.json).
   "Managed image" = present in the ledger.

Prune discipline (invariant, issue #3 ruling): the gateway only ever removes
images recorded in its own ledger and not referenced by any models.d config.
A global `docker image prune` is never issued.

Q5 corollary (documented in issue #3): `~/miniconda3/pkgs` is a user-level
shared cache — never auto-cleaned by anything in this module.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger("inferfabric.image_manager")

# Label applied to every container the gateway runs (build-time labels are
# impossible for pulled images — see module docstring).
MANAGED_LABEL = "com.inferfabric.managed"

LEDGER_FILENAME = "image_ledger.json"


def default_ledger_path(data_dir: str | Path | None = None) -> Path:
    """Ledger location: <IFF_DATA_DIR>/image_ledger.json.

    IFF_DATA_DIR resolution mirrors config.py; data_dir injection keeps this
    import-light for tests (no config import at module level).
    """
    if data_dir is not None:
        return Path(data_dir) / LEDGER_FILENAME
    env_dir = os.environ.get("IFF_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / LEDGER_FILENAME
    from .config import IFF_DATA_DIR
    return Path(IFF_DATA_DIR) / LEDGER_FILENAME


class ManagedImageLedger:
    """Persistent set of image refs the gateway put on disk.

    JSON mapping ref → {first_seen, last_seen}; atomic replace on save.
    A corrupt/missing file yields an empty ledger (never raises to callers —
    the ledger is bookkeeping, engine starts must not depend on it).
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._images: dict[str, dict] = {}
        self._load()

    def _load(self):
        try:
            with open(self._path) as f:
                raw = json.load(f)
            images = raw.get("images")
            if isinstance(images, dict):
                self._images = {str(k): dict(v) for k, v in images.items()
                                if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("managed-image ledger unreadable (%s) — starting empty", e)

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump({"images": self._images}, f, indent=2, sort_keys=True)
        os.replace(tmp, self._path)

    def record(self, ref: str, *, now: float | None = None):
        """Idempotent insert; refreshes last_seen on re-record."""
        ref = str(ref).strip()
        if not ref:
            return
        ts = float(now if now is not None else time.time())
        entry = self._images.get(ref)
        if entry is None:
            self._images[ref] = {"first_seen": ts, "last_seen": ts}
        else:
            entry["last_seen"] = ts
        self._save()

    def forget(self, ref: str):
        self._images.pop(str(ref).strip(), None)
        self._save()

    def refs(self) -> set[str]:
        return set(self._images)

    def entries(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self._images.items()}


def _default_ledger(data_dir=None) -> ManagedImageLedger:
    return ManagedImageLedger(default_ledger_path(data_dir))


def record_image(image: str, *, ledger: ManagedImageLedger | None = None,
                 data_dir=None) -> None:
    """Record an image ref as managed. Bookkeeping only — failures are logged,
    never raised (engine start must not depend on the ledger)."""
    try:
        (ledger if ledger is not None else _default_ledger(data_dir)).record(image)
    except Exception as e:
        log.warning("failed to record managed image %s: %s", image, e)


def ensure_image_pulled(image: str, *, ledger: ManagedImageLedger | None = None,
                        data_dir=None, run=None,
                        timeout: float = 600.0) -> dict:
    """Explicit `docker pull` (replaces implicit docker-run auto-pull) + ledger
    record. Observation semantics: a failed pull is logged (warning) and
    reported as status="error"; it does not raise — the subsequent docker run
    fails visibly on its own if the image is truly absent.
    """
    run = run or subprocess.run  # call-time lookup: module-level patching works
    try:
        r = run(["docker", "image", "inspect", image],
                capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            record_image(image, ledger=ledger, data_dir=data_dir)
            return {"status": "ok", "pulled": False, "image": image}
    except Exception as e:
        log.warning("image inspect failed for %s: %s", image, e)
    try:
        r = run(["docker", "pull", image],
                capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            record_image(image, ledger=ledger, data_dir=data_dir)
            return {"status": "ok", "pulled": True, "image": image}
        log.warning("docker pull failed for %s (rc=%d): %s",
                    image, r.returncode, (r.stderr or "").strip())
    except Exception as e:
        log.warning("docker pull raised for %s: %s", image, e)
    return {"status": "error", "pulled": False, "image": image}


def collect_image_refs(models: dict) -> set[str]:
    """Scan models.d configs for docker images the gateway may run
    (vllm / sglang / ninfer sub-configs). Used as the prune reference gate."""
    refs: set[str] = set()
    for m in models.values():
        for attr in ("vllm", "sglang", "ninfer"):
            sub = getattr(m, attr, None)
            img = getattr(sub, "docker_image", None) if sub is not None else None
            if img:
                refs.add(str(img))
    return refs


def prune_managed(ledger: ManagedImageLedger, referenced: set[str], *,
                  run=None, dry_run: bool = False,
                  rmi_timeout: float = 120.0) -> list[dict]:
    """Scoped prune: `docker rmi` only ledger entries not in `referenced`.

    Invariants (issue #3 L3b):
    - never issues `docker image prune` (global cleanup stays impossible);
    - refs in `referenced` (any models.d config) are always kept;
    - rmi failures are fail-visible in the report and the ref STAYS in the
      ledger (an image we cannot remove remains our responsibility);
    - dry_run reports would-remove without touching docker.
    """
    run = run or subprocess.run  # call-time lookup: module-level patching works
    report: list[dict] = []
    for ref in sorted(ledger.refs()):
        if ref in referenced:
            report.append({"ref": ref, "action": "kept", "reason": "referenced"})
            continue
        if dry_run:
            report.append({"ref": ref, "action": "would-remove"})
            continue
        try:
            r = run(["docker", "rmi", ref], capture_output=True, text=True,
                    timeout=rmi_timeout)
        except Exception as e:
            report.append({"ref": ref, "action": "error", "reason": str(e)})
            continue
        if r.returncode == 0:
            ledger.forget(ref)
            report.append({"ref": ref, "action": "removed"})
        else:
            report.append({"ref": ref, "action": "error",
                           "reason": (r.stderr or "").strip() or f"rc={r.returncode}"})
    return report
