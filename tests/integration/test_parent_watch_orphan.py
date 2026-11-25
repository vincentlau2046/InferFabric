"""孤儿进程防护 e2e（issue #50 验收判据）：

模拟 acceptance 脚本被 SIGKILL —— 其启动的网关（IFF_PARENT_WATCH=1）
必须在父进程死后 N 秒内自行退出（reparent 检测）。

真实进程链：pytest → zsh -c（网关的父）→ 网关。
zsh 被 SIGKILL（不可捕获）→ 网关 ppid 变化 → parent_watch 退出。
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PY = REPO / ".venv" / "bin" / "python"
if not PY.exists():
    PY = Path(sys.executable)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_health(port: int, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=2) as r:
                return json.loads(r.read())
        except Exception as e:      # 连接拒绝 = 还没起好
            last_err = e
            time.sleep(0.4)
    raise AssertionError(f"gateway not ready on :{port}: {last_err}")


def test_gateway_exits_after_parent_sigkill():
    port = _free_port()
    tmp = tempfile.mkdtemp(prefix="iff-orphan-test.")
    pidfile = Path(tmp) / "gateway.pid"

    # 网关的父是 zsh；zsh 收到 SIGKILL 后网关成为孤儿（reparent → 1）
    # env 同 scripts/iff-acceptance.sh：HOME/数据/模型目录全隔离 + no_proxy
    # （issue #56 belt-and-braces，health.py 直连为主防线）
    shell = subprocess.Popen(
        ["/bin/zsh", "-c",
         f'exec env HOME={tmp} IFF_DATA_DIR={tmp}/.inferfabric '
         f'IFF_MODELS_DIR={tmp}/models.d '
         f'IFF_PARENT_WATCH=1 EDGE_PROXY_HOST=127.0.0.1 '
         f'EDGE_PROXY_PORT={port} PYTHONPATH={REPO} '
         f'no_proxy=localhost,127.0.0.1,::1 NO_PROXY=localhost,127.0.0.1,::1 '
         f'{PY} -m inferfabric.proxy.handler --async & '
         f'echo $! > {pidfile}; wait'],
        cwd=str(REPO),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    try:
        health = _wait_health(port)
        assert health["parent_watch"] is True       # 防护已武装
        assert health["port"] == port
        gw_pid = int(pidfile.read_text().strip())
        assert health["data_dir"] == f"{tmp}/.inferfabric"

        # 模拟 acceptance 脚本被强杀
        shell.send_signal(signal.SIGKILL)
        shell.wait(timeout=5)

        # 网关须在 parent_watch 轮询间隔内自退（默认 5s + 启动余量）
        deadline = time.time() + 20.0
        while time.time() < deadline:
            try:
                os.kill(gw_pid, 0)
            except ProcessLookupError:
                return                                # 已退出 ✓
            time.sleep(0.5)
        raise AssertionError(
            f"gateway pid {gw_pid} still alive 20s after parent SIGKILL "
            "— orphan protection failed")
    finally:
        # 兜底清理（断言失败时不留孤儿 — 本测试自身也遵守 #50）
        try:
            gw_pid = int(pidfile.read_text().strip())
            os.kill(gw_pid, signal.SIGKILL)
        except (OSError, ValueError, FileNotFoundError):
            pass
