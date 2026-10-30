"""inferfabric/model_pool/dispatch.py — OpenAI/Anthropic 入口的 iff/ 池调度。

在既有入口（handle_chat / _handle_messages）的鉴权段之前调用：
- `iff/` 前缀检查（非池请求原样放行，返回 False）
- 完整逻辑名鉴权（不做 split("/")[-1]，guest 白名单不自动扩权）
- 捕获不可变快照 → select() → run_attempts 预算内回退（P2，spec §6.2–§6.4）
- 池请求跳过响应缓存（GET/PUT 均不触碰）
- 本地候选 ready_only：绝不调用 ensure_service / 任何启动切换逻辑
- 错误契约（spec §12）按入站协议生成对应信封；已提交响应不写第二份信封
"""

import copy
import json
import logging
import time

from inferfabric import forwarder
from inferfabric.model_pool.pool_forward import (
    AttemptResult,
    run_attempts,
)
from inferfabric.model_pool.selector import (
    RequestRequirements,
    RuntimeView,
    select,
)
from inferfabric.proxy.chat_handlers import (
    _ENGINE_API_PATHS,
    _forward_request,
    _normalize_tools_for_openai,
    handle_ollama_native,
)
from inferfabric.proxy.request_logger import RequestLog

log = logging.getLogger("inferfabric.model_pool.dispatch")

POOL_PREFIX = "iff/"

# 错误码 → (HTTP status, 是否带 Retry-After)
_ERROR_CONTRACT = {
    "unknown_pool": (404, False),
    "no_compatible_target": (400, False),
    "pool_disabled": (503, True),
    "no_ready_target": (503, True),
    "pool_unavailable": (503, True),
}

_RETRY_AFTER_S = 10


def is_pool_name(model: str) -> bool:
    return isinstance(model, str) and model.startswith(POOL_PREFIX)


def pool_name_of(model: str) -> str:
    return model[len(POOL_PREFIX):]


# ── 请求能力提取 ──

def _has_vision_openai(data: dict) -> bool:
    for msg in data.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    return True
    return False


def _has_vision_anthropic(data: dict) -> bool:
    for msg in data.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image":
                    return True
    return False


def extract_requirements(protocol: str, data: dict) -> RequestRequirements:
    """从请求体提取本次必要能力。未知能力不默认当作支持。"""
    if protocol == "anthropic":
        vision = _has_vision_anthropic(data)
        tools = bool(data.get("tools"))
        structured = False  # Anthropic 无独立结构化输出字段
    else:
        vision = _has_vision_openai(data)
        tools = bool(data.get("tools"))
        structured = bool(data.get("response_format") or data.get("json_schema"))
    return RequestRequirements(
        protocol=protocol,
        stream=bool(data.get("stream", False)),
        tools=tools,
        vision=vision,
        structured=structured,
    )


# ── 公共视图/日志辅助 ──

def runtime_view(pm) -> RuntimeView:
    cooldowns = getattr(pm, "pool_cooldowns", None)
    # 就绪集合用 served_name（候选引用的是 served_name；active_services
    # 存内部 name — name≠served_name 的模型此前永远不 ready，评审 #15）
    active: set = set()
    get_model = getattr(pm.mgr, "get_model", None)
    for name in set(pm.mgr.active_services):
        model_obj = None
        if get_model is not None:
            try:
                model_obj = get_model(name)
            except Exception:
                model_obj = None
        if model_obj is not None:
            active.add(getattr(model_obj, "served_name", None) or name)
        active.add(name)
    return RuntimeView(
        active_services=active,
        cloud_models=dict(getattr(pm.cloud, "cloud_models", None) or {}),
        # P2：冷却端点在选择期排除（reason=cooldown）
        cooling_fingerprints=(cooldowns.cooling_fingerprints()
                              if cooldowns is not None else frozenset()),
    )


def _pool_has_cloud_candidates(pool_snap) -> bool:
    return any(c.candidate.kind == "cloud" for c in pool_snap.candidates)


def _ensure_cloud_discovered(pm, pool_snap) -> None:
    """池含云候选时补一次云发现（惰性；pm 未提供入口则跳过，评审 #14）。"""
    ecd = getattr(pm, "ensure_cloud_discovered", None)
    if ecd is None or not _pool_has_cloud_candidates(pool_snap):
        return
    try:
        ecd()
    except Exception as e:
        log.warning("cloud discovery failed on pool path (%s) — "
                    "cloud candidates use last known state", type(e).__name__)


def _has_non_migratable_history(protocol: str, data: dict) -> bool:
    """spec §7：不可迁移的历史（无原目标归属时拒绝跨候选迁移）。

    - OpenAI：assistant.tool_calls / role:tool 消息（工具调用历史）
    - Anthropic：带 signature 的 thinking 块、redacted_thinking 块、
      tool_use/tool_result 块
    单候选池不受影响（无从迁移 — 同一物理目标内重试是安全的）。
    """
    msgs = data.get("messages")
    if not isinstance(msgs, list):
        return False
    for msg in msgs:
        if not isinstance(msg, dict):
            continue
        if protocol == "openai":
            if msg.get("role") == "tool" or msg.get("tool_calls"):
                return True
        else:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "thinking" and block.get("signature"):
                    return True
                # redacted_thinking 是加密归属块：换目标即校验失败（终审 #3）
                if block.get("type") == "redacted_thinking":
                    return True
                if block.get("type") in ("tool_use", "tool_result"):
                    return True
    return False


def _agent_kwargs(handler) -> dict:
    hit = getattr(handler, "_agent_hit", None)
    return {
        "agent": hit.agent if hit else "",
        "ua": hit.ua if hit else "",
        "x_app": (hit.x_app if hit else "") or "",
    }


def _sanitize_error(err) -> str | None:
    """日志安全：上游错误串压平换行并截断，防止大段响应内容/凭证进日志。"""
    if err is None:
        return None
    flat = " ".join(str(err).split())
    return flat[:300]


def _log(pm, handler, *, req_id, key_name, model, status, error=None,
         route=None, cloud_provider=None, usage=None, ttft_ms=None):
    usage = usage or {}
    pm.logger.log(RequestLog(
        req_id=req_id, key_name=key_name, model=model,
        status=status, error=_sanitize_error(error), route=route,
        cloud_provider=cloud_provider,
        tokens_in=usage.get("prompt_tokens", 0),
        tokens_in_cached=usage.get("prompt_tokens_cached", 0),
        tokens_out=usage.get("completion_tokens", 0),
        ttft_ms=ttft_ms,
        duration_ms=(time.monotonic() - getattr(handler, "_req_start", time.monotonic())) * 1000,
        **_agent_kwargs(handler),
    ))


# ── OpenAI 入口 ──

def _send_pool_error_openai(handler, pm, *, req_id, key_name, model, code, message):
    status, retry = _ERROR_CONTRACT.get(code, (503, True))
    extra = {"Retry-After": str(_RETRY_AFTER_S)} if retry else None
    _log(pm, handler, req_id=req_id, key_name=key_name, model=model,
         status=status, error=code)
    handler._send_json({"error": message, "status": code}, status,
                       extra_headers=extra)


def _no_eligible_message(model: str, sel) -> str:
    """no eligible target 的自解释消息（OpenAI/Anthropic 共用）。

    7.8.0：逐候选给出 id + 原因 + feature 明细；能力约束导致的拒绝
    附修复指引 — 「feature_unsupported」读起来像模型不支持，实际是
    候选显式声明了约束，得告诉用户去哪解除。
    """
    parts = []
    for d in sel.decisions:
        if d.included:
            continue
        s = f"{d.candidate_id}: {d.reason}"
        if d.detail:
            s += f" [{d.detail}]"
        parts.append(s)
    msg = f"No eligible target for {model} ({', '.join(parts) or 'no candidates'})"
    if any((not d.included) and d.reason == "feature_unsupported"
           for d in sel.decisions):
        msg += (" — candidate capability is declared-constrained; if the model "
                "supports this request, clear its features constraint in the "
                "pool config (dashboard: 模型池 → 编辑 → 能力)")
    return msg


def handle_pool_chat(handler, pm, data, model) -> bool:
    """OpenAI /v1/chat/completions 的 iff/ 池分支。

    Returns:
        True  — 请求已完整处理（含错误响应已发）
        False — 不是池请求，调用方继续原路径
    """
    if not is_pool_name(model):
        return False

    req_id = pm.new_request_id()
    handler._req_start = getattr(handler, "_req_start", None) or time.monotonic()
    auth_header = handler.headers.get("Authorization", "") or handler.headers.get("x-api-key", "")
    key_name = pm.auth.key_name(auth_header) if pm.auth.enabled else "anonymous"

    # 完整逻辑名鉴权 — 不 strip（spec §9.1：不自动扩权，不按短名放行）
    if pm.auth.enabled:
        auth_ok, auth_reason = pm.auth.check(auth_header, model)
        if not auth_ok:
            _log(pm, handler, req_id=req_id, key_name=key_name, model=model,
                 status=401, error=auth_reason)
            handler._send_json({"error": auth_reason, "status": "unauthorized"}, 401)
            return True

    registry = getattr(pm, "pool_registry", None)
    snapshot = registry.snapshot() if registry is not None else None
    pool_snap = (snapshot.pools.get(pool_name_of(model))
                 if snapshot is not None else None)
    if pool_snap is None:
        _send_pool_error_openai(
            handler, pm, req_id=req_id, key_name=key_name, model=model,
            code="unknown_pool",
            message=f"Unknown logical model: {model}")
        return True

    _ensure_cloud_discovered(pm, pool_snap)

    sel = select(pool_snap, extract_requirements("openai", data), runtime_view(pm))
    if sel.chosen is None:
        _send_pool_error_openai(
            handler, pm, req_id=req_id, key_name=key_name, model=model,
            code=sel.error or "no_compatible_target",
            message=_no_eligible_message(model, sel))
        return True

    # spec §7：不可迁移历史 + 多候选可服务 → 拒绝（无从安全迁移）。
    # 用静态兼容数而非当前就绪数：冷却/宕机中的备用随时可能恢复成
    # 迁移目标，就绪数波动不得改变拒绝决策（终审 #3）
    if sel.compatible_count > 1 and _has_non_migratable_history("openai", data):
        _send_pool_error_openai(
            handler, pm, req_id=req_id, key_name=key_name, model=model,
            code="no_compatible_target",
            message=f"Request for {model} carries tool-call history and the pool "
                    f"has multiple eligible targets — refusing to migrate it "
                    f"(session affinity is not yet supported)")
        return True

    # 统一限流：gate 以逻辑名为键、覆盖所有转发分支（local/ollama/cloud），
    # 避免任何分支绕过 RPM/并发限制（安全审查 2026-10-23 #1）
    budget_start = time.monotonic()   # 排队等待计入预算（§6.3，评审 #7）
    gate = pm.dual_gate.acquire(model)
    if not gate.ok:
        _log(pm, handler, req_id=req_id, key_name=key_name, model=model,
             status=429, error=gate.reason, route=f"pool:{pool_snap.pool.name}")
        handler._send_json(
            {"error": f"Rate limited: {gate.reason}", "status": "rate_limit"}, 429)
        return True
    try:
        pool_name = pool_snap.pool.name

        def transport(cand, remaining):
            if cand.candidate.kind == "local":
                return _attempt_local_openai(handler, pm, data, model=model,
                                             pool=pool_name, cand=cand,
                                             remaining=remaining)
            return _attempt_cloud(handler, pm, data, protocol="openai",
                                  model=model, pool=pool_name, cand=cand,
                                  remaining=remaining)

        # P2：预算内回退循环（统一 max_attempts/deadline，spec §6.2–§6.4）
        outcome = run_attempts(pool_snap.pool, sel.ordered, transport,
                               cooldowns=getattr(pm, "pool_cooldowns", None),
                               deadline_start=budget_start,
                               history=getattr(pm, "pool_history", None))
        _log_failed_attempts(pm, handler, outcome, req_id=req_id,
                             key_name=key_name, model=model)
    finally:
        gate.release()
    _finish_outcome_openai(handler, pm, outcome, req_id=req_id,
                           key_name=key_name, model=model)
    return True


# ── Anthropic 入口 ──

# IFF 错误码 → (HTTP status, Anthropic error type, 是否带 Retry-After)
_ANTHROPIC_CONTRACT = {
    "unknown_pool": (404, "not_found_error", False),
    "no_compatible_target": (400, "invalid_request_error", False),
    "pool_disabled": (503, "overloaded_error", True),
    "no_ready_target": (503, "overloaded_error", True),
    "pool_unavailable": (503, "overloaded_error", True),
}


def _send_pool_error_anthropic(handler, pm, *, req_id, key_name, model, code, message):
    status, err_type, retry = _ANTHROPIC_CONTRACT.get(code, (503, "overloaded_error", True))
    extra = {"Retry-After": str(_RETRY_AFTER_S)} if retry else None
    _log(pm, handler, req_id=req_id, key_name=key_name, model=model,
         status=status, error=code)
    handler._send_json(
        {"type": "error", "error": {"type": err_type, "message": message}},
        status, extra_headers=extra)


def handle_pool_messages(handler, pm, data, model) -> bool:
    """Anthropic /v1/messages 的 iff/ 池分支。

    Returns:
        True  — 请求已完整处理（含错误响应已发）
        False — 不是池请求，调用方继续原路径
    """
    if not is_pool_name(model):
        return False

    req_id = pm.new_request_id()
    handler._req_start = getattr(handler, "_req_start", None) or time.monotonic()
    auth_header = handler.headers.get("Authorization", "") or handler.headers.get("x-api-key", "")
    key_name = pm.auth.key_name(auth_header) if pm.auth.enabled else "anonymous"

    if pm.auth.enabled:
        auth_ok, auth_reason = pm.auth.check(auth_header, model)
        if not auth_ok:
            # 鉴权失败走标准 401 信封（authentication_error）
            _log(pm, handler, req_id=req_id, key_name=key_name, model=model,
                 status=401, error=auth_reason)
            handler._send_json(
                {"type": "error",
                 "error": {"type": "authentication_error", "message": auth_reason}},
                401)
            return True

    registry = getattr(pm, "pool_registry", None)
    snapshot = registry.snapshot() if registry is not None else None
    pool_snap = (snapshot.pools.get(pool_name_of(model))
                 if snapshot is not None else None)
    if pool_snap is None:
        _send_pool_error_anthropic(
            handler, pm, req_id=req_id, key_name=key_name, model=model,
            code="unknown_pool",
            message=f"Unknown logical model: {model}")
        return True

    _ensure_cloud_discovered(pm, pool_snap)

    sel = select(pool_snap, extract_requirements("anthropic", data), runtime_view(pm))
    if sel.chosen is None:
        _send_pool_error_anthropic(
            handler, pm, req_id=req_id, key_name=key_name, model=model,
            code=sel.error or "no_compatible_target",
            message=_no_eligible_message(model, sel))
        return True

    # spec §7：不可迁移历史（thinking 签名/tool_use/tool_result）+ 多候选
    # 可服务 → 拒绝（签名只对生成它的目标有效）
    if sel.compatible_count > 1 and _has_non_migratable_history("anthropic", data):
        _send_pool_error_anthropic(
            handler, pm, req_id=req_id, key_name=key_name, model=model,
            code="no_compatible_target",
            message=f"Request for {model} carries non-migratable history "
                    f"(thinking signatures / tool results) and the pool has "
                    f"multiple eligible targets — refusing to migrate it")
        return True

    # 统一限流（同 OpenAI 路径；Anthropic 直连路径带 30s 等待语义，保持一致）
    budget_start = time.monotonic()   # 排队等待计入预算（§6.3，评审 #7）
    gate = pm.dual_gate.acquire(model, timeout=30)
    if not gate.ok:
        _log(pm, handler, req_id=req_id, key_name=key_name, model=model,
             status=429, error=gate.reason, route=f"pool:{pool_snap.pool.name}")
        handler._send_json(
            {"type": "error",
             "error": {"type": "rate_limit_error",
                       "message": f"Rate limited: {gate.reason}"}},
            429)
        return True
    try:
        pool_name = pool_snap.pool.name

        def transport(cand, remaining):
            if cand.candidate.kind == "local":
                return _attempt_local_anthropic(handler, pm, data, model=model,
                                                pool=pool_name, cand=cand,
                                                auth_header=auth_header,
                                                remaining=remaining)
            return _attempt_cloud(handler, pm, data, protocol="anthropic",
                                  model=model, pool=pool_name, cand=cand,
                                  remaining=remaining)

        outcome = run_attempts(pool_snap.pool, sel.ordered, transport,
                               cooldowns=getattr(pm, "pool_cooldowns", None),
                               deadline_start=budget_start,
                               history=getattr(pm, "pool_history", None))
        _log_failed_attempts(pm, handler, outcome, req_id=req_id,
                             key_name=key_name, model=model)
    finally:
        gate.release()
    _finish_outcome_anthropic(handler, pm, outcome, req_id=req_id,
                              key_name=key_name, model=model)
    return True


# ── P2 单候选传输（返回 AttemptResult；客户端写只发生在成功/已提交路径） ──

def _log_failed_attempts(pm, handler, outcome, *, req_id, key_name, model):
    """spec §9.2：逐 attempt 结构化观测 — 失败尝试进进程日志。

    不写 RequestLog（终审 #15）：attempt 行会污染请求计数与费用归属
    （metrics 按行聚合，一次池请求的失败尝试不是独立请求）。
    req_id 把进程日志与最终那条 RequestLog 行关联起来。
    """
    for rec in getattr(outcome, "attempts", None) or ():
        if rec.ok:
            continue
        log.warning("pool attempt failed: req=%s pool_model=%s ordinal=%d "
                    "target=%s status=%s committed=%s admitted=%s "
                    "retry_after=%s error=%s route=%s",
                    req_id, model, rec.ordinal, rec.candidate_id,
                    rec.status, rec.committed, rec.admitted,
                    rec.retry_after, rec.error, rec.route)


def _gate_timeout(remaining: float | None) -> float | None:
    """物理门等待上限：受剩余预算约束（终审 #7），不超过默认 30s。"""
    if remaining is None:
        return 30.0
    return max(1.0, min(30.0, float(remaining)))


def _attempt_local_openai(handler, pm, data, *, model, pool, cand,
                          remaining=None) -> AttemptResult:
    """本地候选（OpenAI 入站）：ready_only，绝不调用 ensure_service。

    _forward_request 单次语义：True=响应已完整交付（committed）；False=连接
    失败、headers 未发送（未提交 → 池层切下一候选）。上游错误响应会被直发
    （committed → 终止不回退）— 这是 PR-14 冻结核心路径的既有语义。
    ollama 引擎走同一 OpenAI 兼容端点（/v1/chat/completions，终审 #13）—
    不再调 handle_ollama_native（该路径无返回信号，无法区分已交付/未提交）。
    """
    route = f"pool:{pool}/local:{cand.candidate.model_name}"
    c = cand.candidate
    model_obj = pm.mgr.find_model_by_served_name(c.model_name)
    if model_obj is None or model_obj.name not in pm.mgr.active_services:
        # select 后的竞态窗口（模型刚好被停）— 不自动启动；未提交、零上游调用
        return AttemptResult(status=None, route=route,
                             error=f"local target '{c.model_name}' is no longer active")

    data["model"] = model_obj.served_name or c.model_name
    stream = bool(data.get("stream", False))

    # 物理目标容量门（spec §8，评审 #16）：同一物理目标跨池/跨直连共享并发
    # 预算 — 键 = served_name（与直连路由一致）。acquire_target 只占
    # per-model 并发，不双扣外层已计费的 RPM/全局并发（终审 #8）。
    # 门先于端口预留（终审 #14）：失败时不占用副本端口。
    gate = pm.dual_gate.acquire_target(c.model_name, timeout=_gate_timeout(remaining))
    if not gate.ok:
        return AttemptResult(admitted=False, route=route,
                             error=f"physical target '{c.model_name}' rate "
                                   f"limited: {gate.reason}")
    try:
        target_port = pm.get_target_port(c.model_name)
        if not target_port:
            return AttemptResult(status=None, route=route,
                                 error=f"local target '{c.model_name}' has no available port")

        _normalize_tools_for_openai(data)
        body = json.dumps(data).encode("utf-8")
        upstream_path = _ENGINE_API_PATHS.get(model_obj.type, "/v1/chat/completions")

        # 池请求不写响应缓存（映射变化后旧答案不可复用，spec §9.3）。
        # model_name 必须是真实模型名 — _forward_request 以它释放副本配额
        if _forward_request(handler, pm, target_port, body, stream,
                            model_name=model_obj.name, upstream_path=upstream_path,
                            cache_body=None, timeout=remaining):
            return AttemptResult(ok=True, status=200, committed=True, route=route,
                                 usage=getattr(handler, "_usage", {}) or {},
                                 ttft_ms=getattr(handler, "_ttft_ms", None))
        return AttemptResult(status=None, route=route,
                             error=f"local target '{c.model_name}' unreachable")
    finally:
        gate.release()


def _attempt_local_anthropic(handler, pm, data, *, model, pool, cand,
                             auth_header, remaining=None) -> AttemptResult:
    """本地候选（Anthropic 入站）：ready_only，suppress 模式取 CloudResult 信号。

    forward_anthropic_local(suppress_client_errors=True) 返回 CloudResult：
    - committed=False → 未写客户端（连接失败/可重试状态）→ 切下一候选
    - committed=True + status=200 → 已交付
    - committed=True + 其他状态 → 已直发（committed → 终止）
    """
    route = f"pool:{pool}/local:{cand.candidate.model_name}"
    c = cand.candidate
    model_obj = pm.mgr.find_model_by_served_name(c.model_name)
    if model_obj is None or model_obj.name not in pm.mgr.active_services:
        return AttemptResult(status=None, route=route,
                             error=f"local target '{c.model_name}' is no longer active")

    data["model"] = model_obj.served_name or c.model_name

    # 物理目标容量门（同 OpenAI 本地候选，spec §8；终审 #8/#14）
    gate = pm.dual_gate.acquire_target(c.model_name, timeout=_gate_timeout(remaining))
    if not gate.ok:
        return AttemptResult(admitted=False, route=route,
                             error=f"physical target '{c.model_name}' rate "
                                   f"limited: {gate.reason}")
    try:
        result = forwarder.forward_anthropic_local(
            handler, pm, data, auth_header, model_obj, model,
            suppress_client_errors=True, timeout=remaining)
    finally:
        gate.release()
    committed = getattr(result, "committed", False)
    status = getattr(result, "status", None)
    if committed and status == 200 and not getattr(result, "error", None):
        return AttemptResult(ok=True, status=200, committed=True, route=route,
                             usage=getattr(handler, "_usage", {}) or {},
                             ttft_ms=getattr(handler, "_ttft_ms", None))
    if committed:
        return AttemptResult(ok=False, committed=True, status=status, route=route,
                             error=getattr(result, "error", None))
    # 未提交失败：保留状态/Retry-After 供回退决策（429 等待、5xx 重试）
    return AttemptResult(ok=False, committed=False, status=status, route=route,
                         error=getattr(result, "error", None),
                         retry_after=getattr(result, "retry_after", None))


def _attempt_cloud(handler, pm, data, *, protocol, model, pool, cand,
                   remaining=None) -> AttemptResult:
    """云候选：suppress 模式取真实状态码/Retry-After，失败不写客户端。"""
    c = cand.candidate
    route = f"pool:{pool}/cloud:{c.provider}/{c.model_id}"
    provider_cfg = pm.cloud.get_provider_config(c.provider)
    cloud_model = pm.cloud.cloud_models.get(f"{c.provider}/{c.model_id}")
    if provider_cfg is None or cloud_model is None:
        return AttemptResult(
            status=None, route=route,
            error=f"cloud target '{c.provider}/{c.model_id}' unavailable")

    # 快照路由（终审 #5）：mid-flight 请求用编译期捕获的端点，不读 live
    # provider 配置 — provider 热更新不得改变已选中候选的目标
    snap_base = (cand.route or {}).get(f"{protocol}_base") if cand.route else None
    if snap_base:
        provider_cfg = copy.copy(provider_cfg)
        setattr(provider_cfg, f"{protocol}_base", snap_base)

    data["model"] = c.model_id

    # 物理目标容量门（spec §8）：键 = provider/model_id（与云直连一致；
    # 终审 #8 acquire_target / 终审 #7 等待受剩余预算约束）
    gate = pm.dual_gate.acquire_target(f"{c.provider}/{c.model_id}",
                                       timeout=_gate_timeout(remaining))
    if not gate.ok:
        return AttemptResult(admitted=False, route=route,
                             error=f"cloud target '{c.provider}/{c.model_id}' "
                                   f"rate limited: {gate.reason}")
    try:
        result = forwarder.forward_to_cloud(
            handler, data, provider_cfg, cloud_model,
            protocol=protocol, original_model=model,
            suppress_client_errors=True, timeout=remaining)
    finally:
        gate.release()
    committed = getattr(result, "committed", True)
    if committed and (result.status or 0) == 200 and not result.error:
        return AttemptResult(ok=True, status=200, committed=True, route=route,
                             usage=result.usage or {},
                             ttft_ms=result.ttft_ms)
    return AttemptResult(ok=False, committed=committed, status=result.status,
                         error=result.error, route=route,
                         retry_after=getattr(result, "retry_after", None),
                         usage=result.usage or {}, ttft_ms=result.ttft_ms)


# ── 结果处理（记日志 + 按入站协议发信封；已提交不写第二份信封） ──

def _finish_outcome_openai(handler, pm, outcome, *, req_id, key_name, model):
    # 物理身份（终审 #15）：成功/已提交行的 model/cloud_provider 按实际目标
    # 填写（local=served_name / cloud=model_id）— 费用归因按物理模型计价；
    # 逻辑名只出现在 route。pool_unavailable 走 _send_pool_error_*（逻辑名）。
    phys = outcome.physical_model or model
    prov = outcome.cloud_provider
    if outcome.ok:
        _log(pm, handler, req_id=req_id, key_name=key_name, model=phys,
             status=200, route=outcome.route, usage=outcome.usage,
             ttft_ms=outcome.ttft_ms, cloud_provider=prov)
    elif outcome.committed:
        # 响应已提交（含流中断/上游错误已直发）→ 只记日志（spec §6.4）。
        # error 用固定错误码（终审 #16）：上游错误体可能含敏感内容，明细只
        # 进进程日志（_log_failed_attempts），不入 RequestLog
        _log(pm, handler, req_id=req_id, key_name=key_name, model=phys,
             status=outcome.status or 502,
             error=f"upstream_http_{outcome.status or 502}",
             route=outcome.route, cloud_provider=prov)
    elif outcome.status is not None:
        # 非回退类上游错误（400/404/422 等）→ 按上游状态返回明确错误。
        # 信封与日志都用固定文案/错误码（评审 #10、终审 #16）：上游错误体
        # 只进进程日志，不回传客户端、不入 RequestLog
        _log(pm, handler, req_id=req_id, key_name=key_name, model=phys,
             status=outcome.status,
             error=f"upstream_http_{outcome.status}",
             route=outcome.route, cloud_provider=prov)
        handler._send_json(
            {"error": f"upstream returned HTTP {outcome.status}",
             "status": "upstream_error"}, outcome.status)
    else:
        _send_pool_error_openai(
            handler, pm, req_id=req_id, key_name=key_name, model=model,
            code="pool_unavailable",
            message=f"All targets failed for {model}")


# 上游 HTTP 状态 → Anthropic error type（非回退类终止错误的信封）
_ANTHROPIC_STATUS_TYPE = {
    400: "invalid_request_error",
    404: "invalid_request_error",
    413: "request_too_large",
    422: "invalid_request_error",
    429: "rate_limit_error",
}


def _finish_outcome_anthropic(handler, pm, outcome, *, req_id, key_name, model):
    # 物理身份 + 固定错误码 — 同 _finish_outcome_openai（终审 #15/#16）
    phys = outcome.physical_model or model
    prov = outcome.cloud_provider
    if outcome.ok:
        _log(pm, handler, req_id=req_id, key_name=key_name, model=phys,
             status=200, route=outcome.route, usage=outcome.usage,
             ttft_ms=outcome.ttft_ms, cloud_provider=prov)
    elif outcome.committed:
        _log(pm, handler, req_id=req_id, key_name=key_name, model=phys,
             status=outcome.status or 502,
             error=f"upstream_http_{outcome.status or 502}",
             route=outcome.route, cloud_provider=prov)
    elif outcome.status is not None:
        # 固定文案回客户端（评审 #10）：上游错误体只进进程日志，
        # 不入信封、不入 RequestLog（终审 #16）
        _log(pm, handler, req_id=req_id, key_name=key_name, model=phys,
             status=outcome.status,
             error=f"upstream_http_{outcome.status}",
             route=outcome.route, cloud_provider=prov)
        err_type = _ANTHROPIC_STATUS_TYPE.get(outcome.status, "api_error")
        handler._send_json(
            {"type": "error",
             "error": {"type": err_type,
                       "message": f"upstream returned HTTP {outcome.status}"}},
            outcome.status)
    else:
        _send_pool_error_anthropic(
            handler, pm, req_id=req_id, key_name=key_name, model=model,
            code="pool_unavailable",
            message=f"All targets failed for {model}")
