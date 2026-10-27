"""Real HTTP/signals in a child process; no ModelManager, GPU, or production DB."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest


CHILD = r'''
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import signal
import sys
import threading
from unittest.mock import MagicMock

from inferfabric.proxy import async_server as server
from inferfabric import dashboard
from inferfabric.config_reloader import ConfigReloader
from aiohttp import web

logging.basicConfig(level=logging.INFO)
root = Path(sys.argv[1])
dashboard._DASHBOARD_DIR = root / "dashboard"
reloader = ConfigReloader(MagicMock())
reloader.setup()

async def main():
    app = web.Application()
    async def index(request):
        return web.Response(text=dashboard.get_html(), content_type="text/html")
    app.router.add_get("/", index)
    async def blocked(request):
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"started\n")
        await asyncio.Event().wait()
        return response
    app.router.add_get("/blocked", blocked)
    runner = server._build_runner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    executor = ThreadPoolExecutor(max_workers=1)
    # Simulate an upstream worker that outlives the drain deadline. A normal
    # Python exit joins this worker; the production hard-exit must not.
    executor.submit(threading.Event().wait, 60)
    stopped = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopped.set)
    (root / "ready.json").write_text(json.dumps({"port": port}))
    await stopped.wait()
    await server._shutdown_gracefully(
        MagicMock(), MagicMock(), threading.Event(), site, runner, executor,
        lambda message: None)

asyncio.run(main())
'''


@pytest.mark.parametrize("active_request", [False, True])
def test_hup_dashboard_reload_and_term_bypasses_worker_join(tmp_path, active_request):
    assets = tmp_path / "dashboard"
    assets.mkdir()
    base = assets / "base.html"
    base.write_text("<html>P1-before</html>")
    script = tmp_path / "restart_child.py"
    script.write_text(CHILD)
    env = dict(os.environ, EDGE_SHUTDOWN_DRAIN="0.4", EDGE_HARD_EXIT="1")
    repo = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = str(repo)
    log_path = tmp_path / "child.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen([sys.executable, str(script), str(tmp_path)],
                                cwd=repo, env=env, stdout=log, stderr=log)
        try:
            ready = tmp_path / "ready.json"
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                assert proc.poll() is None, log_path.read_text()
                time.sleep(0.05)
            assert ready.exists(), log_path.read_text()
            port = json.loads(ready.read_text())["port"]
            def fetch():
                with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                    return response.read().decode()
            assert "P1-before" in fetch()
            base.write_text("<html>P1-after</html>")
            assert "P1-before" in fetch()  # prove the cache really was populated
            proc.send_signal(signal.SIGHUP)
            deadline = time.monotonic() + 3
            while "P1-after" not in fetch():
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.05)
            stream = None
            try:
                if active_request:
                    stream = urlopen(f"http://127.0.0.1:{port}/blocked", timeout=2)
                    assert stream.readline() == b"started\n"
                started = time.monotonic()
                proc.terminate()
                assert proc.wait(timeout=5) == 0, log_path.read_text()
                # Two sequential aiohttp windows would take >=0.8s. The whole
                # drain must instead fit the one 0.4s budget (+process jitter).
                assert time.monotonic() - started < 0.7
            finally:
                if stream is not None:
                    stream.close()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
