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

import datetime
import logging

from inferfabric.model_pool.catalog import build_catalog
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


def _json_safe(value):
    """管理端点边界 JSON 安全化（终审 I4）。

    YAML safe_load 会把 ISO 日期解析成 datetime.date；raw 原样透传时
    _send_json 内 json.dumps 直接 TypeError，GET /admin/model-pools 整个
    500。此处递归归一：date/datetime → isoformat，set → 排序列表，
    其余不可序列化类型 → str()。
    """
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (set, frozenset)):
        return sorted(str(v) for v in value)
    return str(value)


def handle_get_model_pools(handler, pm):
    reg = pm.pool_registry
    # 单锁原子读取：revision 与 raw 必须同代（终审 M6 — 分两次取锁时
    # 中间穿插的 update 会让编辑器以旧基点 POST 新内容，反复 409）
    snap, raw = reg.snapshot_and_raw()
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
    # v7.1.0 运行状态三态 + raw 编辑基底（GET 视图有损，编辑器读-改-写
    # 必须以 raw 为基底防丢数据 — 见 registry.raw_config 文档）
    handler._send_json(_json_safe({
        "revision": snap.revision,
        "pools": pools,
        "configured": reg.configured,
        "load_error": reg.last_load_error,
        "raw": raw,
    }))


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
    # v7.1.0 编辑态预览：可选 config（完整池配置 dict）→ 预览编辑后而非
    # 已发布快照；不传时行为不变（向后兼容）
    config = data.get("config")
    if config is not None:
        try:
            ps = pm.pool_registry.compile_pool(config, pool_name)
        except ValueError as e:
            handler._send_json({"error": str(e), "status": "invalid_config",
                                "issues": str(e).split("; ")}, 400)
            return
        except KeyError:
            handler._send_json(
                {"error": f"Unknown pool: {pool_name}",
                 "status": "unknown_pool"}, 404)
            return
    else:
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


def handle_get_model_pools_catalog(handler, pm):
    """GET /admin/model-pools/catalog — 池候选目录（v7.1.2，只读）。

    运营视图唯一的候选元数据总线：向导/编辑器下拉的数据源（替代前端
    自刮 /models 与 /admin/cloud/providers）。local=llm 模型（served_name），
    cloud=routable 云模型；protocols 来自发现结果/引擎映射。Phase 2/3
    在条目上做加法（latency/degraded），前端选择键编码不变。
    """
    handler._send_json(build_catalog(pm))


def handle_get_model_pools_runtime(handler, pm):
    """GET /admin/model-pools/runtime — 每池每候选运行态（v7.1.0，只读）。

    候选链实时视图数据源：active（本地=已启动/云=已发现）、cooling（冷却/
    隔离剩余与原因、半开探测在途）、last_attempt（最近一次真实传输尝试）。
    前端 5s 轮询；registry 未配置时返回空 pools（200，不 500）。
    """
    reg = pm.pool_registry
    snap = reg.snapshot()
    runtime = runtime_view(pm)
    cooldowns = getattr(pm, "pool_cooldowns", None)
    entries = cooldowns.entries() if cooldowns is not None else {}
    lasts = cooldowns.last_attempts() if cooldowns is not None else {}
    pools = {}
    for name, ps in snap.pools.items():
        cands = []
        for rc in ps.candidates:
            c = rc.candidate
            if c.kind == "local":
                active = c.model_name in runtime.active_services
            else:
                active = f"{c.provider}/{c.model_id}" in runtime.cloud_models
            cooling = entries.get(rc.endpoint_fingerprint)
            last = lasts.get(rc.endpoint_fingerprint)
            cands.append({
                "id": c.id,
                "kind": c.kind,
                "target": c.model_name if c.kind == "local"
                          else f"{c.provider}/{c.model_id}",
                "fingerprint": rc.endpoint_fingerprint,
                "active": active,
                "cooling": ({"remaining_s": cooling["remaining_s"],
                             "reason": cooling["reason"],
                             "probing": cooling["probing"]}
                            if cooling is not None else None),
                "last_attempt": ({"ok": last["ok"], "status": last["status"],
                                  "age_s": last["age_s"]}
                                 if last is not None else None),
            })
        pools[name] = {"candidates": cands}
    handler._send_json({"revision": snap.revision, "pools": pools})


def handle_get_model_pools_latency(handler, pm):
    """GET /admin/model-pools/latency — 每池每候选延迟环（P3 C1，只读）。

    数据源 PoolHistory（run_attempts 成功路径旁录）；每候选最近 20 点，
    新在后。pm 未挂 history（旧实例/测试）→ 空 pools（200，不 500）。
    """
    history = getattr(pm, "pool_history", None)
    snap = history.snapshot() if history is not None else {}
    pools = {
        name: {cid: ring[-20:] for cid, ring in cands.items()}
        for name, cands in snap.items()
    }
    handler._send_json({"pools": pools})


def handle_get_model_pools_events(handler, pm):
    """GET /admin/model-pools/events — 每池事件时间线（P3 C2，只读）。

    数据源 CooldownStore 事件环；fp → 池/候选映射同 runtime 写法，
    孤儿指纹（配置已移除的候选）丢弃；每池最近 50 条，新在后。
    """
    reg = pm.pool_registry
    snap = reg.snapshot()
    # fp → (pool_name, candidate_id, target)
    fp_map: dict[str, tuple[str, str, str]] = {}
    for name, ps in snap.pools.items():
        for rc in ps.candidates:
            c = rc.candidate
            fp_map[rc.endpoint_fingerprint] = (
                name, c.id,
                c.model_name if c.kind == "local"
                else f"{c.provider}/{c.model_id}")
    cooldowns = getattr(pm, "pool_cooldowns", None)
    events = cooldowns.events() if cooldowns is not None else []
    pools: dict = {}
    for e in events:
        hit = fp_map.get(e["fingerprint"])
        if hit is None:
            continue          # 孤儿指纹：候选已不在任何池配置里
        name, cid, target = hit
        pools.setdefault(name, []).append({
            "age_s": e["age_s"], "wall_ts": e["wall_ts"],
            "type": e["type"], "candidate_id": cid, "target": target,
            "reason": e["reason"],
        })
    handler._send_json({"pools": {n: evs[-50:] for n, evs in pools.items()}})
