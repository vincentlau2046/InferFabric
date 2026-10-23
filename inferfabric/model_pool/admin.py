"""inferfabric/model_pool/admin.py — 逻辑模型池管理接口。

路由（经 handler.py 的 _admin 装饰器注册，复用管理认证）：
  GET  /admin/model-pools           定义 + revision + 证据态 + 校验问题
  POST /admin/model-pools           原子更新（body 携带 expected_revision）
       （HTTP 动作用 POST：同步 server 仅路由 GET/POST，见 ledger ruling）
  POST /admin/model-pools/validate  静态校验，零出网、零副作用
  GET  /admin/model-pools/status    每池双协议当前选择视图
  POST /admin/model-pools/dry-run   给定协议/能力 → 选择/排除原因，零副作用
  POST /admin/model-pools/toggle    服务端原子翻转单池 enabled（CAS）

真实探测（PROBED 证据）不在本接口自动执行：需管理员显式操作 + 出网授权
（spec §5.1），推迟到独立任务。
"""

import logging

from inferfabric.model_pool.config import load_pools_dict
from inferfabric.model_pool.dispatch import runtime_view
from inferfabric.model_pool.registry import RevisionConflict
from inferfabric.model_pool.selector import RequestRequirements, select

log = logging.getLogger("inferfabric.model_pool.admin")


def _candidate_view(rc) -> dict:
    c = rc.candidate
    return {
        "id": c.id,
        "priority": c.priority,
        "kind": c.kind,
        "target": c.model_name if c.kind == "local" else f"{c.provider}/{c.model_id}",
        "features": list(c.features),
        "evidence": {proto: ev.state.value for proto, ev in rc.evidence.items()},
    }


def handle_get_model_pools(handler, pm):
    snap = pm.pool_registry.snapshot()
    pools = {}
    for name, ps in snap.pools.items():
        pools[name] = {
            "enabled": ps.pool.enabled,
            "strategy": ps.pool.strategy,
            "allowed_locations": list(ps.pool.allowed_locations),
            "accept_declared": ps.pool.accept_declared,
            "allow_cross_location": ps.pool.allow_cross_location,
            "max_attempts": ps.pool.max_attempts,
            "deadline_ms": ps.pool.deadline_ms,
            "issues": list(ps.issues),
            "candidates": [_candidate_view(rc) for rc in ps.candidates],
        }
    handler._send_json({"revision": snap.revision, "pools": pools})


def handle_put_model_pools(handler, pm):
    data = handler._read_body()
    if data is None:
        return
    expected = data.get("expected_revision")
    if not isinstance(expected, int):
        handler._send_json(
            {"error": "expected_revision (int) is required for conflict-safe update",
             "status": "bad_request"}, 400)
        return
    try:
        snap = pm.pool_registry.update(data, expected_revision=expected)
    except RevisionConflict:
        handler._send_json(
            {"error": "revision conflict — reload and retry",
             "status": "revision_conflict",
             "current_revision": pm.pool_registry.snapshot().revision}, 409)
        return
    except ValueError as e:
        handler._send_json(
            {"error": str(e), "status": "invalid_config",
             "issues": str(e).split("; ")}, 400)
        return
    handler._send_json({"status": "updated", "revision": snap.revision})


def handle_validate_model_pools(handler, pm):
    data = handler._read_body()
    if data is None:
        return
    # 纯静态校验：不查运行时、不出网、不改状态
    parsed, issues = load_pools_dict(data, current_revision=0)
    handler._send_json({"valid": parsed is not None, "issues": issues})


def _selection_view(sel) -> dict:
    return {
        "chosen": sel.chosen.candidate.id if sel.chosen else None,
        "error": sel.error,
        "decisions": [{"id": d.candidate_id, "included": d.included,
                       "reason": d.reason} for d in sel.decisions],
    }


def handle_get_model_pools_status(handler, pm):
    snap = pm.pool_registry.snapshot()
    runtime = runtime_view(pm)
    pools = {}
    for name, ps in snap.pools.items():
        protocols = {}
        for proto in ("openai", "anthropic"):
            sel = select(ps, RequestRequirements(protocol=proto), runtime)
            protocols[proto] = _selection_view(sel)
        pools[name] = {
            "enabled": ps.pool.enabled,
            "issues": list(ps.issues),
            "protocols": protocols,
        }
    handler._send_json({"revision": snap.revision, "pools": pools})


def handle_post_model_pools_dry_run(handler, pm):
    data = handler._read_body()
    if data is None:
        return
    pool_name = str(data.get("pool", ""))
    protocol = str(data.get("protocol", "openai"))
    if protocol not in ("openai", "anthropic"):
        handler._send_json(
            {"error": "protocol must be 'openai' or 'anthropic'",
             "status": "bad_request"}, 400)
        return
    snap = pm.pool_registry.snapshot()
    ps = snap.pools.get(pool_name)
    if ps is None:
        handler._send_json(
            {"error": f"Unknown pool: {pool_name}", "status": "unknown_pool"}, 404)
        return
    req = RequestRequirements(
        protocol=protocol,
        stream=bool(data.get("stream", False)),
        tools=bool(data.get("tools", False)),
        vision=bool(data.get("vision", False)),
        structured=bool(data.get("structured", False)),
    )
    sel = select(ps, req, runtime_view(pm))
    body = _selection_view(sel)
    body["pool"] = pool_name
    body["protocol"] = protocol
    handler._send_json(body)


def handle_post_model_pools_toggle(handler, pm):
    """单池 enabled 开关 — 服务端字段级原子修改。

    dashboard 开关走此端点而非 GET→PUT 读-改-写：GET 视图有损（悬空候选
    被剔除、declared_protocols 不回传），经视图重建配置会丢数据。body 携带
    expected_revision 做 CAS；409 时前端应重新拉取后再试。
    """
    data = handler._read_body()
    if data is None:
        return
    pool_name = data.get("pool")
    enabled = data.get("enabled")
    expected = data.get("expected_revision")
    if (not isinstance(pool_name, str) or not pool_name
            or not isinstance(enabled, bool)
            or not isinstance(expected, int)):
        handler._send_json(
            {"error": "pool (str), enabled (bool), expected_revision (int) required",
             "status": "bad_request"}, 400)
        return
    try:
        snap = pm.pool_registry.flip_pool_enabled(
            pool_name, enabled, expected_revision=expected)
    except KeyError:
        handler._send_json(
            {"error": f"Unknown pool: {pool_name}", "status": "unknown_pool"}, 404)
        return
    except RevisionConflict:
        handler._send_json(
            {"error": "revision conflict — reload and retry",
             "status": "revision_conflict",
             "current_revision": pm.pool_registry.snapshot().revision}, 409)
        return
    except ValueError as e:
        handler._send_json(
            {"error": str(e), "status": "invalid_config",
             "issues": str(e).split("; ")}, 400)
        return
    handler._send_json({"status": "updated", "revision": snap.revision,
                        "pool": pool_name, "enabled": enabled})
