"""unit/proxy/test_admin_jobs_api.py — /admin/jobs 控制面长任务注册表（mac prod ③）.

覆盖：JobRegistry 生命周期（create/start/cancel/finish/get/prune/run_async）、
_job_route GET /admin/jobs/{id}、POST /admin/jobs/{id}/cancel。
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from inferfabric.jobs import JobRegistry


# ═══════════════════════════════════════════════════════════════
# JobRegistry 生命周期
# ═══════════════════════════════════════════════════════════════


def test_registry_create_start_finish():
    reg = JobRegistry()
    job = reg.create("discover", label="t1")
    jid = job["job_id"]
    assert job["status"] == "queued"
    reg.start(jid)
    assert reg.get(jid)["status"] == "running"
    reg.finish(jid, "done", result={"found": 3})
    done = reg.get(jid)
    assert done["status"] == "done"
    assert done["result"] == {"found": 3}
    assert done["finished_at"] is not None


def test_registry_get_hides_internal_cancel_flag():
    reg = JobRegistry()
    jid = reg.create("k")["job_id"]
    reg.request_cancel(jid)
    assert reg.get(jid).get("cancel_requested") is None
    assert reg.cancel_requested(jid) is True


def test_registry_cancel_requested_flag():
    reg = JobRegistry()
    jid = reg.create("discover")["job_id"]
    payload, code = reg.request_cancel(jid)
    assert code == 200 and payload["status"] == "cancelling"
    assert reg.cancel_requested(jid) is True
    payload404, code404 = reg.request_cancel("no-such-job")
    assert code404 == 404 and "unknown" in payload404["error"]


def test_registry_cancel_after_finish_conflict_409():
    reg = JobRegistry()
    jid = reg.create("k")["job_id"]
    reg.finish(jid, "done")
    payload, code = reg.request_cancel(jid)
    assert code == 409


def test_registry_unknown_get():
    reg = JobRegistry()
    assert reg.get("missing") is None


def test_registry_finish_idempotent_and_validates_status():
    reg = JobRegistry()
    jid = reg.create("k")["job_id"]
    with pytest.raises(ValueError):
        reg.finish(jid, "succeeded")  # 非法终态（契约：done|failed|cancelled）
    reg.finish(jid, "done")
    reg.finish(jid, "failed")  # 幂等：已完结忽略
    assert reg.get(jid)["status"] == "done"


def test_registry_prune_keeps_max_finished():
    reg = JobRegistry(max_finished=2)
    ids = []
    for i in range(5):
        j = reg.create("k", label=str(i))
        reg.finish(j["job_id"], "done")
        ids.append(j["job_id"])
        time.sleep(0.001)
    assert reg.get(ids[0]) is None
    assert reg.get(ids[-1]) is not None


def test_registry_run_async_executes_and_finishes():
    reg = JobRegistry()
    jid = reg.create("switch", label="async")["job_id"]
    t = reg.run_async(reg.get(jid), lambda should_cancel: ("done", {"ok": True}),
                      on_error_result=lambda e: {"error": str(e)})
    t.join(timeout=2)
    done = reg.get(jid)
    assert done["status"] == "done"
    assert done["result"] == {"ok": True}


def test_registry_run_async_error_path():
    reg = JobRegistry()
    jid = reg.create("switch", label="boom")["job_id"]

    def boom(should_cancel):
        raise RuntimeError("x")

    t = reg.run_async(reg.get(jid), boom,
                      on_error_result=lambda e: {"error": str(e)})
    t.join(timeout=2)
    done = reg.get(jid)
    assert done["status"] == "failed"
    assert "x" in done["result"]["error"]


# ═══════════════════════════════════════════════════════════════
# 端点：GET /admin/jobs/{id}、POST /admin/jobs/{id}/cancel
# ═══════════════════════════════════════════════════════════════


def _make_handler(path, method="GET"):
    import inferfabric.proxy.handler as handler_module
    h = handler_module.ProxyHandler.__new__(handler_module.ProxyHandler)
    h.headers = {}
    h.path = path
    h.command = method
    h._send_json = MagicMock()
    h._read_body = MagicMock(return_value={})
    return h


def test_get_job_endpoint_ok():
    import inferfabric.proxy.handler as handler_module
    reg = handler_module._JOB_REGISTRY
    jid = reg.create("discover", label="ep")["job_id"]
    h = _make_handler(f"/admin/jobs/{jid}")
    handler_module._job_route(h, None)
    job = h._send_json.call_args.args[0]
    assert job["job_id"] == jid


def test_get_job_endpoint_unknown_404():
    import inferfabric.proxy.handler as handler_module
    h = _make_handler("/admin/jobs/missing-id")
    handler_module._job_route(h, None)
    assert h._send_json.call_args.args[-1] == 404


def test_cancel_job_endpoint():
    import inferfabric.proxy.handler as handler_module
    reg = handler_module._JOB_REGISTRY
    jid = reg.create("discover", label="cx")["job_id"]
    h = _make_handler(f"/admin/jobs/{jid}/cancel", method="POST")
    h._handle_job_cancel(None)
    args = h._send_json.call_args.args
    assert args[0].get("status") == "cancelling"
    assert args[-1] == 200
    assert reg.cancel_requested(jid) is True


def test_dynamic_segments_not_shadowed_by_static_tables():
    """动态段路径不在静态路由表内（do_GET/do_POST 分派条件成立）。"""
    import inferfabric.proxy.handler as handler_module
    from urllib.parse import urlparse
    assert urlparse("/admin/jobs/j-1").path not in handler_module._GET_ROUTES
    assert urlparse("/admin/jobs/j-1").path.startswith("/admin/jobs/")
    p2 = urlparse("/admin/jobs/j-1/cancel").path
    assert p2 not in handler_module._POST_ROUTES
    assert p2.startswith("/admin/jobs/") and p2.endswith("/cancel")
