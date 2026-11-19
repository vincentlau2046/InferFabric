"""scripts/install.sh 平台感知安装器 hermetic 测试（issue #13）。

双平台分支均通过 IFF_FORCE_PLATFORM 强制 + 临时目录覆写验证，
不依赖真机平台、不触碰真实 systemd/launchd、不重建 _deps。
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "install.sh"


def run_install(tmp_path: Path, platform: str, *flags: str) -> subprocess.CompletedProcess:
    env_overrides = {
        "IFF_FORCE_PLATFORM": platform,
        "IFF_SKIP_DEPS": "1",
        "IFF_INSTALL_UNIT_DIR": str(tmp_path / "systemd"),
        "IFF_INSTALL_PLIST_DIR": str(tmp_path / "LaunchAgents"),
        "IFF_INSTALL_LABEL": "test.inferfabric.proxy",
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *flags],
        cwd=REPO, capture_output=True, text=True,
        env={**env_overrides},
    )


def test_linux_branch_writes_systemd_unit(tmp_path):
    r = run_install(tmp_path, "linux")
    assert r.returncode == 0, r.stderr
    unit = tmp_path / "systemd" / "inferfabric-proxy.service"
    assert unit.exists()
    body = unit.read_text()
    assert "[Service]" in body
    assert "inferfabric.proxy.handler --async" in body
    assert f"WorkingDirectory={REPO}" in body
    assert "WantedBy=default.target" in body
    # 无 secret：unit 模板不含 key/token 字样
    assert "KEY" not in body and "TOKEN" not in body
    # Linux 分支不产出 plist
    assert not (tmp_path / "LaunchAgents").exists()


def test_darwin_branch_writes_launchd_plist(tmp_path):
    r = run_install(tmp_path, "darwin")
    assert r.returncode == 0, r.stderr
    plist = tmp_path / "LaunchAgents" / "test.inferfabric.proxy.plist"
    assert plist.exists()
    body = plist.read_text()
    assert "<key>Label</key><string>test.inferfabric.proxy</string>" in body
    assert "inferfabric.proxy.handler" in body
    assert "<key>KeepAlive</key><true/>" in body
    # darwin 分支不产出 systemd unit
    assert not (tmp_path / "systemd" / "inferfabric-proxy.service").exists()


def test_existing_unit_not_overwritten_without_force(tmp_path):
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    (unit_dir / "inferfabric-proxy.service").write_text("# user-customized\n")
    r = run_install(tmp_path, "linux")
    assert r.returncode == 0, r.stderr
    assert (unit_dir / "inferfabric-proxy.service").read_text() == "# user-customized\n"
    assert "skip" in r.stdout


def test_force_service_overwrites_existing_unit(tmp_path):
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    (unit_dir / "inferfabric-proxy.service").write_text("# stale\n")
    r = run_install(tmp_path, "linux", "--force-service")
    assert r.returncode == 0, r.stderr
    assert "WorkingDirectory=" in (unit_dir / "inferfabric-proxy.service").read_text()


def test_no_service_flag_skips_both(tmp_path):
    r = run_install(tmp_path, "linux", "--no-service")
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "systemd" / "inferfabric-proxy.service").exists()


def test_unknown_flag_rejected(tmp_path):
    r = run_install(tmp_path, "linux", "--bogus")
    assert r.returncode == 2
