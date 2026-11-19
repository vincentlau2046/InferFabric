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
   records each ref in a project ledger (IFF_DATA_DIR/image_ledger.json) —
   keyed by `repo:tag` **and the repo digest** observed at pull time, so a
   repointed tag can never take an unrelated image down with it. Ledger
   writes are lock-serialised and carry a persisted revision counter (pool-
   registry pattern). "Managed image" = present in the ledger.

Prune discipline (invariant, issue #3 ruling): the gateway only ever removes
images recorded in its own ledger and not referenced by any models.d config,
and only after re-reading the digest and confirming it still matches. A
global `docker image prune` is never issued.

Q5 corollary (documented in issue #3): `~/miniconda3/pkgs` is a user-level
shared cache — never auto-cleaned by anything in this module.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
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

    Entry: ref → {first_seen, last_seen, digest}. The digest is what makes an
    entry *removable*: a repointed tag (user re-tag, upstream moves a
    `:latest`-style tag) must never take an unrelated image down with it.

    Writes follow the pool-registry pattern: an in-process lock serialises
    record/forget, every write bumps a persisted `revision`, and a writer
    re-syncs from disk first when another writer advanced the revision (same
    failure class as pool CAS — concurrent starts must not drop each other's
    entries). Cross-process CAS is not claimed beyond that re-sync; the loss
    direction is safe either way (untracked ⇒ never pruned). Atomic replace
    on save; a corrupt/missing file yields an empty ledger (never raises to
    callers — bookkeeping must not gate engine starts).
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._images: dict[str, dict] = {}
        self._revision = 0
        self._load()

    @property
    def revision(self) -> int:
        return self._revision

    def _load(self):
        try:
            with open(self._path) as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            log.warning("managed-image ledger unreadable (%s) — starting empty", e)
            return
        if not isinstance(raw, dict):
            log.warning("managed-image ledger malformed (not a mapping) — starting empty")
            return
        images = raw.get("images")
        if isinstance(images, dict):
            self._images = {str(k): dict(v) for k, v in images.items()
                            if isinstance(v, dict)}
        rev = raw.get("revision")
        self._revision = rev if isinstance(rev, int) and rev >= 0 else 0

    def _sync_from_disk(self):
        """另一个写者推进过 revision → 重载，避免整字典覆盖丢条目。"""
        try:
            with open(self._path) as f:
                raw = json.load(f)
        except Exception:
            return
        rev = raw.get("revision") if isinstance(raw, dict) else None
        if isinstance(rev, int) and rev > self._revision:
            self._load()

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump({"revision": self._revision, "images": self._images},
                      f, indent=2, sort_keys=True)
        os.replace(tmp, self._path)

    def record(self, ref: str, *, digest: str | None = None,
               now: float | None = None):
        """Idempotent insert; refreshes last_seen and digest on re-record.

        A re-record without a digest keeps the previously recorded one
        (digest=None means "unknown this time", not "forget the digest").
        """
        ref = str(ref).strip()
        if not ref:
            return
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._sync_from_disk()
            entry = self._images.get(ref)
            if entry is None:
                self._images[ref] = {"first_seen": ts, "last_seen": ts,
                                     "digest": digest or None}
            else:
                entry["last_seen"] = ts
                if digest:
                    entry["digest"] = digest
            self._revision += 1
            self._save()

    def forget(self, ref: str):
        ref = str(ref).strip()
        with self._lock:
            self._sync_from_disk()
            if ref not in self._images:
                return
            self._images.pop(ref, None)
            self._revision += 1
            self._save()

    def digest(self, ref: str) -> str | None:
        entry = self._images.get(str(ref).strip()) or {}
        d = entry.get("digest")
        return str(d) if d else None

    def refs(self) -> set[str]:
        return set(self._images)

    def entries(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self._images.items()}


def _default_ledger(data_dir=None) -> ManagedImageLedger:
    return ManagedImageLedger(default_ledger_path(data_dir))


def record_image(image: str, *, ledger: ManagedImageLedger | None = None,
                 data_dir=None, digest: str | None = None) -> None:
    """Record an image ref (and its repo digest) as managed. Bookkeeping only —
    failures are logged, never raised (engine start must not depend on the
    ledger)."""
    try:
        (ledger if ledger is not None else _default_ledger(data_dir)).record(
            image, digest=digest)
    except Exception as e:
        log.warning("failed to record managed image %s: %s", image, e)


def image_digest(image: str, run=None) -> str | None:
    """Current repo digest of `image` (`repo@sha256:…`), None if unavailable.

    This is the preferred removable-image key: the ledger records it at pull
    time and prune re-reads it before `rmi`, so a repointed tag is skipped
    instead of deleting an image the ledger never saw. Locally built images
    have no RepoDigests — see ``image_identity``.
    """
    run = run or subprocess.run
    try:
        r = run(["docker", "image", "inspect", "--format",
                 "{{index .RepoDigests 0}}", image],
                capture_output=True, text=True, timeout=30)
    except Exception as e:
        log.warning("image digest inspect failed for %s: %s", image, e)
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    d = (getattr(r, "stdout", "") or "").strip()
    return d or None


def image_id(image: str, run=None) -> str | None:
    """Local image ID (`sha256:…`), None if unavailable.

    Key for locally built images: `docker build` produces no RepoDigests, so
    a digest-only key would make every local image permanently unremovable.
    """
    run = run or subprocess.run
    try:
        r = run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                capture_output=True, text=True, timeout=30)
    except Exception as e:
        log.warning("image id inspect failed for %s: %s", image, e)
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    i = (getattr(r, "stdout", "") or "").strip()
    return i or None


def image_identity_value(image: str, run=None) -> str | None:
    """Removable identity: repo digest if present, else local image ID.

    One string covers both cases; `repo@sha256:…` means pulled (digest),
    bare `sha256:…` means locally built (image ID). Verification and prune
    compare the full string, so a re-pull/re-tag changing the kind is a
    mismatch (skipped), never a silent deletion.
    """
    return image_digest(image, run) or image_id(image, run)


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
            record_image(image, ledger=ledger, data_dir=data_dir,
                         digest=image_identity_value(image, run))
            return {"status": "ok", "pulled": False, "image": image}
    except Exception as e:
        log.warning("image inspect failed for %s: %s", image, e)
    detail = ""
    try:
        r = run(["docker", "pull", image],
                capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            record_image(image, ledger=ledger, data_dir=data_dir,
                         digest=image_identity_value(image, run))
            return {"status": "ok", "pulled": True, "image": image}
        detail = ((getattr(r, "stderr", "") or "")
                  or (getattr(r, "stdout", "") or "")).strip()[:300]
        log.warning("docker pull failed for %s (rc=%d): %s",
                    image, r.returncode, detail)
    except Exception as e:
        detail = str(e)[:300]
        log.warning("docker pull raised for %s: %s", image, e)
    # issue #8-③：失败结果携带原因摘要（detail），启动路径据此 fail-fast，
    # 不再落入 docker run 的无超时隐式重拉。
    return {"status": "error", "pulled": False, "image": image,
            "detail": detail}


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
    - **identity gate**: before `rmi`, the current identity is re-read and
      must equal the one recorded at pull/build time (repo digest for pulled
      images, local image ID for locally built ones — see `image_identity_value`).
      A repointed tag →
      `action: "skipped", reason: "identity changed"` and the entry stays in
      the ledger; an entry without a recorded identity is likewise skipped
      ("unverifiable") — we never delete what we cannot prove is ours;
    - rmi failures are fail-visible in the report and the ref STAYS in the
      ledger (an image we cannot remove remains our responsibility);
    - dry_run performs zero docker calls (a plan; digests are re-verified at
      execution).
    """
    run = run or subprocess.run  # call-time lookup: module-level patching works
    report: list[dict] = []
    for ref in sorted(ledger.refs()):
        if ref in referenced:
            report.append({"ref": ref, "action": "kept", "reason": "referenced"})
            continue
        want = ledger.digest(ref)
        if dry_run:
            report.append({"ref": ref, "action": "would-remove",
                           "digest_recorded": bool(want)})
            continue
        if not want:
            report.append({"ref": ref, "action": "skipped",
                           "reason": "no digest recorded (unverifiable)"})
            continue
        current = image_identity_value(ref, run)
        if current != want:
            report.append({"ref": ref, "action": "skipped",
                           "reason": f"identity changed (ledger {want!r} != "
                                     f"current {current!r})"})
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
