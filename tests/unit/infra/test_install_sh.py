"""scripts/install.sh 平台感知安装器 hermetic 测试（issue #13）。

双平台分支均通过 IFF_FORCE_PLATFORM 强制 + 临时目录覆写验证，
不依赖真机平台、不触碰真实 systemd/launchd、不重建 _deps。
"""

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "install.sh"


def run_install(tmp_path: Path, platform: str, *flags: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env_overrides = {
        "IFF_FORCE_PLATFORM": platform,
        "IFF_SKIP_DEPS": "1",
        "IFF_INSTALL_UNIT_DIR": str(tmp_path / "systemd"),
        "IFF_INSTALL_PLIST_DIR": str(tmp_path / "LaunchAgents"),
        "IFF_INSTALL_LABEL": "test.inferfabric.proxy",
        **(extra_env or {}),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *flags],
        cwd=REPO, capture_output=True, text=True,
        env={**os.environ, **env_overrides},
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
    # 评审 PR#14 B2：解释器烘焙绝对路径，不走 /usr/bin/env
    exec_line = next(l for l in body.splitlines() if l.startswith("ExecStart="))
    assert exec_line.startswith("ExecStart=/"), exec_line
    assert "/usr/bin/env" not in body
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
    # 评审 PR#14 B2：解释器烘焙绝对路径（launchd 不继承交互式 PATH）
    assert "/usr/bin/env" not in body
    args = body.split("ProgramArguments")[1]
    assert "<string>/" in args and "<string>python3</string>" not in args
    # 评审 PR#14 N1：日志落 ~/Library/Logs/inferfabric/（/tmp 会被清理）
    assert "Library/Logs/inferfabric/proxy.log" in body
    assert "/tmp/inferfabric-proxy" not in body
    # darwin 分支不产出 systemd unit
    assert not (tmp_path / "systemd" / "inferfabric-proxy.service").exists()


def test_plist_xml_escapes_repo_path(tmp_path):
    # 评审 PR#14 N3：repo 路径含 XML 特殊字符时 plist 不得损坏
    repo = tmp_path / "rep&o"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "install.sh").write_text(SCRIPT.read_text())
    r = subprocess.run(
        ["bash", str(repo / "scripts" / "install.sh")],
        cwd=repo, capture_output=True, text=True,
        env={
            **os.environ,
            "IFF_FORCE_PLATFORM": "darwin",
            "IFF_SKIP_DEPS": "1",
            "IFF_SKIP_SMOKE": "1",
            "IFF_INSTALL_PLIST_DIR": str(tmp_path / "LaunchAgents"),
            "IFF_INSTALL_LABEL": "test.inferfabric.proxy",
        },
    )
    assert r.returncode == 0, r.stderr
    body = (tmp_path / "LaunchAgents" / "test.inferfabric.proxy.plist").read_text()
    assert f"<key>WorkingDirectory</key><string>{tmp_path}/rep&amp;o</string>" in body


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
