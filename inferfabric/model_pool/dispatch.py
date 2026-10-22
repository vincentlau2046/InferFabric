"""inferfabric/model_pool/dispatch.py — OpenAI/Anthropic 入口的 iff/ 池调度。

在既有入口（handle_chat / _handle_messages）的鉴权段之前调用：
- `iff/` 前缀检查（非池请求原样放行，返回 False）
- 完整逻辑名鉴权（不做 split("/")[-1]，guest 白名单不自动扩权）
- 捕获不可变快照 → select() → 单目标转发（P1 不跨候选回退）
- 池请求跳过响应缓存（GET/PUT 均不触碰）
- 本地候选 ready_only：绝不调用 ensure_service / 任何启动切换逻辑
- 错误契约（spec §12）按入站协议生成对应信封

P2（pool_forward.py）把「单目标转发」替换为预算内 attempt 循环；
本文件负责的鉴权/选择/日志/错误契约不变。
"""

import json
import logging
import time

from inferfabric import forwarder
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
    return RuntimeView(
        active_services=set(pm.mgr.active_services),
        cloud_models=dict(getattr(pm.cloud, "cloud_models", None) or {}),
    )


def _agent_kwargs(handler) -> dict:
    hit = getattr(handler, "_agent_hit", None)
    return {
        "agent": hit.agent if hit else "",
        "ua": hit.ua if hit else "",
        "x_app": (hit.x_app if hit else "") or "",
    }


def _log(pm, handler, *, req_id, key_name, model, status, error=None,
         route=None, cloud_provider=None, usage=None, ttft_ms=None):
    usage = usage or {}
    pm.logger.log(RequestLog(
        req_id=req_id, key_name=key_name, model=model,
        status=status, error=error, route=route,
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

    sel = select(pool_snap, extract_requirements("openai", data), runtime_view(pm))
    if sel.chosen is None:
        _send_pool_error_openai(
            handler, pm, req_id=req_id, key_name=key_name, model=model,
            code=sel.error or "no_compatible_target",
            message=f"No eligible target for {model} "
                    f"({', '.join(d.reason for d in sel.decisions if not d.included) or 'no candidates'})")
        return True

    cand = sel.chosen.candidate
    if cand.kind == "local":
        return _forward_pool_local_openai(
            handler, pm, data, model=model, pool=pool_snap.pool.name, cand=cand,
            req_id=req_id, key_name=key_name)
    return _forward_pool_cloud(
        handler, pm, data, protocol="openai", model=model,
        pool=pool_snap.pool.name, cand=cand, req_id=req_id, key_name=key_name,
        error_sender=_send_pool_error_openai)


def _forward_pool_local_openai(handler, pm, data, *, model, pool, cand,
                               req_id, key_name):
    """本地候选：ready_only（select 已保证 active），改写 served_name 后单目标转发。"""
    model_obj = pm.mgr.find_model_by_served_name(cand.model_name)
    if model_obj is None or model_obj.name not in pm.mgr.active_services:
        # select 后的竞态窗口（模型刚好被停）— 不自动启动，明确报未就绪
        _send_pool_error_openai(
            handler, pm, req_id=req_id, key_name=key_name, model=model,
            code="no_ready_target",
            message=f"Local target '{cand.model_name}' is no longer active")
        return True

    target_port = pm.get_target_port(cand.model_name)
    if not target_port:
        _send_pool_error_openai(
            handler, pm, req_id=req_id, key_name=key_name, model=model,
            code="no_ready_target",
            message=f"Local target '{cand.model_name}' has no available port")
        return True

    data["model"] = model_obj.served_name or cand.model_name
    stream = bool(data.get("stream", False))

    # ollama 原生路径（与 handle_chat 同判据）
    if model_obj.is_ollama or model_obj.is_ollama_cpp:
        handle_ollama_native(handler, pm, data, target_port, model_obj)
        return True

    _normalize_tools_for_openai(data)
    body = json.dumps(data).encode("utf-8")
    upstream_path = _ENGINE_API_PATHS.get(model_obj.type, "/v1/chat/completions")

    gate = pm.dual_gate.acquire(model)  # 限流键 = 逻辑名（跨候选共享池配额）
    if not gate.ok:
        _log(pm, handler, req_id=req_id, key_name=key_name, model=model,
             status=429, error=gate.reason, route=f"pool:{pool}/local")
        handler._send_json(
            {"error": f"Rate limited: {gate.reason}", "status": "rate_limit"}, 429)
        return True
    try:
        # P1：同目标重试与直连路径一致（3 次），不跨候选；cache_body=None
        # —— 池请求不写响应缓存（映射变化后旧答案不可复用，spec §9.3）
        sent = False
        for attempt in range(3):
            if _forward_request(handler, pm, target_port, body, stream,
                                model_name=model, upstream_path=upstream_path,
                                cache_body=None):
                sent = True
                usage = getattr(handler, "_usage", {}) or {}
                _log(pm, handler, req_id=req_id, key_name=key_name, model=model,
                     status=200, route=f"pool:{pool}/local",
                     usage=usage, ttft_ms=getattr(handler, "_ttft_ms", None))
                break
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
        if not sent:
            _log(pm, handler, req_id=req_id, key_name=key_name, model=model,
                 status=502, error="upstream_unavailable", route=f"pool:{pool}/local")
            handler._send_json({"error": "Upstream unavailable after retry",
                                "status": "upstream_unavailable"}, 502)
    finally:
        gate.release()
    return True


def _forward_pool_cloud(handler, pm, data, *, protocol, model, pool, cand,
                        req_id, key_name, error_sender):
    """云候选：查 provider/模型，改写上游 model_id 后透传（零协议转换）。"""
    provider_cfg = pm.cloud.get_provider_config(cand.provider)
    cloud_model = pm.cloud.cloud_models.get(f"{cand.provider}/{cand.model_id}")
    if provider_cfg is None or cloud_model is None:
        error_sender(handler, pm, req_id=req_id, key_name=key_name, model=model,
                     code="pool_unavailable",
                     message=f"Cloud target '{cand.provider}/{cand.model_id}' unavailable")
        return True

    data["model"] = cand.model_id
    result = forwarder.forward_to_cloud(
        handler, data, provider_cfg, cloud_model,
        protocol=protocol, original_model=model)
    usage = result.usage or {}
    _log(pm, handler, req_id=req_id, key_name=key_name, model=model,
         status=result.status, route=f"pool:{pool}/cloud:{cand.provider}",
         cloud_provider=cand.provider, usage=usage,
         ttft_ms=result.ttft_ms, error=result.error)
    return True
