"""Platform abstraction layer — the ONLY OS/hardware-aware package.

Upstream (InferFabric) core modules depend on the interfaces exported
here, never on a concrete platform backend. Cherry-pick friendly: this
package is self-contained (stdlib only, no gateway-core imports).

Layout:
    resource.py       ResourceLedger interface + shared types
    resource_umem.py  unified-memory backend (Apple Silicon)
    resource_vram.py  VRAM three-state backend (CUDA, upstream)
    probe.py          platform probes (ports, memory watermark, budget)
    procguard.py      process guardianship (launchd / systemd / none)
    engines.py        per-platform default engine matrix
"""
import os
import sys
from dataclasses import dataclass

__all__ = ["PlatformProfile", "detect"]


@dataclass(frozen=True)
class PlatformProfile:
    """Resolved platform facts; gateway core branches on these only."""

    os: str                 # "darwin" | "linux" | "other"
    resource_backend: str   # "umem" | "vram"
    procguard: str          # "launchd" | "systemd" | "none"


# os -> (resource_backend, procguard)
_BACKENDS = {
    "darwin": ("umem", "launchd"),
    "linux": ("vram", "systemd"),
    "other": ("vram", "none"),
}


def detect() -> PlatformProfile:
    """Resolve the current platform profile.

    IFF_FORCE_PLATFORM overrides the detected os ("darwin"|"linux"|
    anything else) — a test hook; production code must never set it.
    """
    forced = os.environ.get("IFF_FORCE_PLATFORM", "")
    if forced:  # explicit override wins, unknown values resolve to "other"
        os_name = forced if forced in _BACKENDS else "other"
    elif sys.platform == "darwin":
        os_name = "darwin"
    elif sys.platform.startswith("linux"):
        os_name = "linux"
    else:
        os_name = "other"
    backend, guard = _BACKENDS[os_name]
    return PlatformProfile(os=os_name, resource_backend=backend, procguard=guard)
