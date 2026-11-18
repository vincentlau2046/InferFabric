"""Process guardianship — launchd (macOS) / systemd (Linux) / none.

Pure render functions are unit-testable; install()/uninstall() shell out to
the platform service manager and are exercised only in production paths.
The gateway keeps its existing scripts (scripts/gateway-launchd.sh); this
module exposes the same behaviour as an importable, upstreamable unit.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol


class ProcGuard(Protocol):
    def install(self) -> bool: ...
    def uninstall(self) -> bool: ...
    def status(self) -> str: ...


def render_launchd_plist(label: str, program_args: list[str],
                         keep_alive: bool = True,
                         log_path: str | None = None) -> str:
    """Render a launchd plist. Pure function (unit-tested)."""
    args = "".join(f"    <string>{a}</string>\n" for a in program_args)
    ka = "<true/>" if keep_alive else "<false/>"
    log = ""
    if log_path:
        log = (f"  <key>StandardOutPath</key><string>{log_path}</string>\n"
               f"  <key>StandardErrorPath</key><string>{log_path}</string>\n")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
{args}  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>{ka}
{log}</dict>
</plist>
"""


class LaunchdProcGuard:
    """macOS guardianship via launchd (per-user agent)."""

    def __init__(self, label: str, program_args: list[str],
                 log_path: str | None = None):
        self.label = label
        self.program_args = program_args
        self.log_path = log_path

    @property
    def _plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{self.label}.plist"

    def install(self) -> bool:
        try:
            self._plist_path.parent.mkdir(parents=True, exist_ok=True)
            self._plist_path.write_text(render_launchd_plist(
                self.label, self.program_args, log_path=self.log_path),
                encoding="utf-8")
            r = subprocess.run(["launchctl", "load", "-w", str(self._plist_path)],
                               check=False, capture_output=True, timeout=15)
            return r.returncode == 0  # fail visible: rejected plist ≠ installed
        except Exception:
            return False

    def uninstall(self) -> bool:
        try:
            subprocess.run(["launchctl", "unload", "-w", str(self._plist_path)],
                           check=False, capture_output=True, timeout=15)
            self._plist_path.unlink(missing_ok=True)
            return True
        except Exception:
            return False

    def status(self) -> str:
        r = subprocess.run(["launchctl", "list", self.label],
                           check=False, capture_output=True, text=True, timeout=10)
        return "running" if r.returncode == 0 else "stopped"


class SystemdProcGuard:
    """Linux guardianship via a user systemd unit (upstream hosts)."""

    def __init__(self, unit: str, program_args: list[str]):
        self.unit = unit if unit.endswith(".service") else f"{unit}.service"
        self.program_args = program_args

    def install(self) -> bool:
        try:
            unit_dir = Path.home() / ".config" / "systemd" / "user"
            unit_dir.mkdir(parents=True, exist_ok=True)
            args = " ".join(self.program_args)
            (unit_dir / self.unit).write_text(
                "[Unit]\nDescription=InferFabric gateway\n\n[Service]\n"
                f"ExecStart={args}\nRestart=on-failure\n\n[Install]\n"
                "WantedBy=default.target\n", encoding="utf-8")
            for cmd in (["systemctl", "--user", "daemon-reload"],
                        ["systemctl", "--user", "enable", "--now", self.unit]):
                r = subprocess.run(cmd, check=False, capture_output=True,
                                   timeout=15)
                if r.returncode != 0:
                    return False  # fail visible; unit written but not started
            return True
        except Exception:
            return False

    def uninstall(self) -> bool:
        try:
            p = Path.home() / ".config" / "systemd" / "user" / self.unit
            subprocess.run(["systemctl", "--user", "stop", self.unit],
                           check=False, capture_output=True, timeout=15)
            p.unlink(missing_ok=True)
            return True
        except Exception:
            return False

    def status(self) -> str:
        r = subprocess.run(["systemctl", "--user", "is-active", self.unit],
                           check=False, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "unknown"


class NullProcGuard:
    """Foreground / manually-managed gateway — always a no-op."""

    def install(self) -> bool:
        return True

    def uninstall(self) -> bool:
        return True

    def status(self) -> str:
        return "foreground"


def guard_for_profile(profile, label: str, program_args: list[str],
                      log_path: str | None = None) -> ProcGuard:
    """Pick the guardian matching a PlatformProfile.

    The caller MUST supply the real service identity (label/args) — this
    factory never fabricates one, so it can never overwrite a production
    plist with placeholder content.
    """
    if profile.procguard == "launchd":
        return LaunchdProcGuard(label, program_args, log_path=log_path)
    if profile.procguard == "systemd":
        return SystemdProcGuard(label, program_args)
    return NullProcGuard()
