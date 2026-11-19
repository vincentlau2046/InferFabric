"""inferfabric/proxy/responses_bridge.py — OpenAI Responses API (/v1/responses) 入口。

用户裁决 2026-11-17（Issue #1，codex CLI 兼容）：
  原生能力优先 — Responses 请求体原样透传给上游 /responses 端点
  （云端 openai_base + /responses；本地引擎 /v1/responses），不做任何
  能力标注预判；
  转换仅兜底 — 只有上游明确返回失败（400/404/422/501 且未向客户端
  提交任何字节）才对该次尝试降级：Responses → chat completions 翻译，
  非流式探测转发，再把 chat 响应翻译回 Responses 形状交付。
  401/403/429/5xx 不属于"不支持"，交由既有池动作表（冷却/等待/回退）。

兜底路径设计约束：
  - chat 探测固定非流式（流式 chat SSE 形状与 Responses SSE 不兼容）；
    客户端要流式时由交付层合成 Responses SSE 事件序列（分块 output_text.delta）。
  - 池路径：每候选"原生尝试 → 显式失败 → chat 兜底"封装在同一个 transport
    回调内，run_attempts 只记一次 attempt；fallback 内不重复消耗预算。
  - 会话历史不可迁移：reasoning 项跳过（与 spec §7 一致）。
"""

import copy
import json
import logging
import time
import uuid
from urllib.request import Request, urlopen
from urllib.error import HTTPError as _HTTPError

from inferfabric import forwarder
from inferfabric.model_pool.dispatch import (
    _ensure_cloud_discovered,
    _error_summary,
    _finish_outcome_openai,
    _gate_timeout,
    _log,
    _log_failed_attempts,
    _send_pool_error_openai,
    extract_requirements,
    is_pool_name,
    pool_name_of,
    runtime_view,
)
from inferfabric.model_pool.pool_forward import (
    AttemptResult,
    build_pool_summary,
    run_attempts,
)
from inferfabric.model_pool.selector import select
from inferfabric.agent_registry import request_protocol
from inferfabric.proxy.request_logger import RequestLog

log = logging.getLogger("inferfabric.proxy.responses")

# 上游明确"没有这个能力"的状态集 — 仅这些触发翻译兜底（用户裁决：
# 只有明确返回失败不支持才兜底；其余状态交既有动作表/错误信封）。
RESPONSES_UNSUPPORTED_STATUSES = frozenset({400, 404, 422, 501})

_TEXT_BLOCK_TYPES = ("input_text", "output_text", "text", "refusal")


# ── Responses → chat completions 翻译（仅兜底路径使用） ──────────

def _flatten_content(content) -> str:
    """Responses content（str | block list）→ 纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") in _TEXT_BLOCK_TYPES:
                parts.append(str(blk.get("text") or ""))
        return "".join(parts)
    return ""


def responses_to_chat(data: dict) -> dict:
    """Responses 请求 → OpenAI chat completions 请求（兜底翻译）。

    覆盖：instructions/system、input 字符串与数组（message/
    function_call/function_call_output）、tools、tool_choice、
    max_output_tokens、temperature/top_p。reasoning 项跳过。
    输出固定 stream=False（交付层按客户端请求合成 Responses SSE）。
    """
    chat = {"model": data.get("model", ""), "stream": False}
    msgs = []
    instructions = data.get("instructions")
    if isinstance(instructions, str) and instructions:
        msgs.append({"role": "system", "content": instructions})
    inp = data.get("input")
    if isinstance(inp, str):
        msgs.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "message")
            if item_type == "message":
                role = item.get("role", "user")
                if role not in ("system", "developer", "user", "assistant"):
                    role = "user"
                msgs.append({"role": role, "content": _flatten_content(item.get("content"))})
            elif item_type == "function_call":
                msgs.append({
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": item.get("call_id", ""),
                        "type": "function",
                        "function": {"name": item.get("name", ""),
                                     "arguments": item.get("arguments", "")},
                    }],
                })
            elif item_type == "function_call_output":
                out = item.get("output")
                if isinstance(out, (dict, list)):
                    out = json.dumps(out, ensure_ascii=False)
                msgs.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                             "content": out or ""})
            # reasoning / web_search_call / input_image 等：兜底路径跳过
            # （原生透传路径不受影响；spec §7 不可迁移历史守卫同语义）
    if msgs:
        chat["messages"] = msgs
    if data.get("max_output_tokens"):
        chat["max_tokens"] = data["max_output_tokens"]
    for key in ("temperature", "top_p"):
        if data.get(key) is not None:
            chat[key] = data[key]
    tools = data.get("tools")
    if isinstance(tools, list):
        otools = []
        for t in tools:
            if isinstance(t, dict) and t.get("type") == "function":
                fn = {"name": t.get("name", "")}
                if t.get("description"):
                    fn["description"] = t["description"]
                fn["parameters"] = t.get("parameters") or {"type": "object", "properties": {}}
                otools.append({"type": "function", "function": fn})
        if otools:
            chat["tools"] = otools
    tool_choice = data.get("tool_choice")
    if isinstance(tool_choice, str):
        chat["tool_choice"] = tool_choice
    elif isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        chat["tool_choice"] = {"type": "function", "function": {"name": tool_choice.get("name", "")}}
    return chat


# ── chat completions → Responses 翻译（兜底路径交付形状） ────────

def chat_to_responses(chat_obj: dict, requested_model: str):
    """chat completion 响应 → Responses 形状。返回 (response_obj, usage_norm)；
    usage_norm 沿 RequestLog 口径（prompt_tokens/completion_tokens）。"""
    choices = chat_obj.get("choices") or [{}]
    first = choices[0] if isinstance(choices[0], dict) else {}
    msg = first.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(str(b.get("text") or "") for b in content
                       if isinstance(b, dict) and b.get("type") in _TEXT_BLOCK_TYPES)
    else:
        text = ""
    output = []
    if text:
        output.append({
            "type": "message", "id": "msg_" + uuid.uuid4().hex[:10],
            "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        output.append({
            "type": "function_call", "id": "fc_" + uuid.uuid4().hex[:10],
            "call_id": tc.get("id", ""), "name": fn.get("name", ""),
            "arguments": fn.get("arguments", ""), "status": "completed",
        })
    usage = chat_obj.get("usage") or {}
    ptoks = usage.get("prompt_tokens", 0) or 0
    ctoks = usage.get("completion_tokens", 0) or 0
    cached = usage.get("prompt_tokens_cached", 0) or 0
    resp = {
        "id": "resp_" + uuid.uuid4().hex[:10],
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": requested_model,
        "output": output,
        "output_text": text,
        "usage": {"input_tokens": ptoks, "output_tokens": ctoks,
                  "total_tokens": ptoks + ctoks},
        "parallel_tool_calls": True,
    }
    return resp, {"prompt_tokens": ptoks, "prompt_tokens_cached": cached,
                  "completion_tokens": ctoks}


# ── 交付（兜底路径：JSON 或合成 Responses SSE） ─────────────────

def _chunk_write(handler, data: bytes):
    handler._safe_write((str(len(data)) + "\r\n").encode("ascii"))
    handler._safe_write(data)
    handler._safe_write(b"\r\n")


def deliver_responses_payload(handler, resp_obj: dict, client_stream: bool):
    """兜底路径交付：非流式直接 JSON；流式合成 Responses SSE 事件序列
    （created → output_item.added → output_text.delta 分块 →
    output_item.done → completed）。"""
    if not client_stream:
        handler._send_json(resp_obj, 200)
        return
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()

    def ev(event, payload):
        body = ("event: " + event + "\ndata: " +
                json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")
        _chunk_write(handler, body)

    in_progress = dict(resp_obj)
    in_progress["status"] = "in_progress"
    in_progress["output"] = []
    ev("response.created", {"type": "response.created", "response": in_progress})
    for idx, item in enumerate(resp_obj.get("output") or []):
        ev("response.output_item.added",
           {"type": "response.output_item.added", "output_index": idx, "item": item})
        for ci, blk in enumerate(item.get("content") or []):
            if isinstance(blk, dict) and blk.get("type") == "output_text":
                text = str(blk.get("text") or "")
                for i in range(0, len(text), 96):
                    ev("response.output_text.delta",
                       {"type": "response.output_text.delta", "item_id": item.get("id", ""),
                        "output_index": idx, "content_index": ci, "delta": text[i:i + 96]})
        ev("response.output_item.done",
           {"type": "response.output_item.done", "output_index": idx, "item": item})
    ev("response.completed", {"type": "response.completed", "response": resp_obj})
    handler._safe_write(b"0\r\n\r\n")


# ── chat 兜底探测转发（capture 式：响应不直发客户端） ────────────

def _probe_local_chat(pm, port, chat_data, remaining):
    """本地引擎非流式 chat 探测。返回 (status|None, raw_bytes)。"""
    body = json.dumps(chat_data).encode("utf-8")
    conn = None
    try:
        conn = (pm.make_conn(port, timeout=remaining)
                if remaining is not None else pm.make_conn(port))
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        status = resp.status
        raw = resp.read()
        resp.close()
        return status, raw
    except Exception as e:
        return None, str(e).encode("utf-8")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _probe_cloud_chat(provider_cfg, chat_data, remaining):
    """云端非流式 chat 探测（Bearer 口径同 forward_to_cloud）。"""
    url = provider_cfg.openai_base.rstrip("/") + "/chat/completions"
    req = Request(url, data=json.dumps(chat_data).encode("utf-8"),
                  headers={"Content-Type": "application/json",
                           "Authorization": "Bearer " + provider_cfg.api_key,
                           "User-Agent": forwarder._CLOUD_UA},
                  method="POST")
    try:
        resp = urlopen(req, timeout=remaining if remaining is not None else provider_cfg.timeout)
        raw = resp.read()
        status = resp.status
        resp.close()
        return status, raw
    except _HTTPError as e:
        try:
            raw = e.read()
        except Exception:
            raw = b""
        try:
            e.close()
        except Exception:
            pass
        return e.code, raw
    except Exception as e:
        return None, str(e).encode("utf-8")


def _resolve_chat_target(handler, pm, chat_data, cand, *, pool, remaining):
    """兜底探测的物理目标解析 + 容量门。返回 (status, raw, route)；
    status None = 连接失败/目标不可用（run_attempts 连接失败语义）。"""
    c = cand.candidate
    if c.kind == "local":
        route = ("pool:" + pool + "/local:" + c.model_name) if pool else ("local:" + c.model_name)
        model_obj = pm.mgr.find_model_by_served_name(c.model_name)
        if model_obj is None or model_obj.name not in pm.mgr.active_services:
            return None, b"", route
        chat_data["model"] = model_obj.served_name or c.model_name
        gate = pm.dual_gate.acquire_target(c.model_name, timeout=_gate_timeout(remaining))
        if not gate.ok:
            return None, b"", route
        try:
            port = pm.get_target_port(c.model_name)
            if not port:
                return None, b"", route
            status, raw = _probe_local_chat(pm, port, chat_data, remaining)
        finally:
            gate.release()
        return status, raw, route
    route = ("pool:" + pool + "/cloud:" + c.provider + "/" + c.model_id) if pool \
        else ("cloud:" + c.provider + "/" + c.model_id)
    provider_cfg = pm.cloud.get_provider_config(c.provider)
    cloud_model = pm.cloud.cloud_models.get(c.provider + "/" + c.model_id)
    if provider_cfg is None or cloud_model is None:
        return None, b"", route
    snap_base = (cand.route or {}).get("openai_base") if cand.route else None
    if snap_base:
        provider_cfg = copy.copy(provider_cfg)
        provider_cfg.openai_base = snap_base
    chat_data["model"] = c.model_id
    gate = pm.dual_gate.acquire_target(c.provider + "/" + c.model_id,
                                       timeout=_gate_timeout(remaining))
    if not gate.ok:
        return None, b"", route
    try:
        status, raw = _probe_cloud_chat(provider_cfg, chat_data, remaining)
    finally:
        gate.release()
    return status, raw, route


def attempt_chat_fallback(handler, pm, chat_data, cand, *, requested_model,
                          pool, remaining, client_stream):
    """兜底：非流式 chat 探测 → 翻译回 Responses → 交付。
    非 200 未提交返回（交 run_attempts 动作表：400/404/422 终止、
    401/403 冷却、429 等待、5xx 重试/切换）。"""
    status, raw, route = _resolve_chat_target(handler, pm, chat_data, cand,
                                              pool=pool, remaining=remaining)
    if status is None:
        return AttemptResult(status=None, route=route,
                             error=raw.decode("utf-8", errors="replace") or "target unavailable")
    if status != 200:
        return AttemptResult(ok=False, committed=False, status=status,
                             route=route, error="upstream_http_" + str(status))
    try:
        chat_obj = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return AttemptResult(ok=False, committed=False, status=502,
                             route=route, error="invalid upstream json")
    resp_obj, usage = chat_to_responses(chat_obj, requested_model)
    deliver_responses_payload(handler, resp_obj, client_stream)
    return AttemptResult(ok=True, status=200, committed=True, route=route,
                         usage=usage, ttft_ms=None)


# ── 原生透传（用户裁决：优先，不做能力标注预判） ─────────────────

def _native_url(provider_cfg, local_port):
    """原生 /responses 端点 URL：云端 openai_base + /responses；
    本地 :port/v1/responses。base 尾带 /v1 时不重复拼接。"""
    if local_port is not None:
        return "http://127.0.0.1:" + str(local_port) + "/v1/responses"
    base = (provider_cfg.openai_base or "").rstrip("/")
    if base.endswith("/v1"):
        return base + "/responses"
    return base + "/v1/responses"


def _auth_headers(provider_cfg, local_port):
    if local_port is not None:
        return {"Content-Type": "application/json"}
    return {"Content-Type": "application/json",
            "Authorization": "Bearer " + provider_cfg.api_key,
            "User-Agent": forwarder._CLOUD_UA}


def _probe_native(handler, pm, data, cand, *, pool, remaining, client_stream):
    """原生 Responses 透传。返回 AttemptResult：
    - ok → 已交付
    - committed=False + status ∈ RESPONSES_UNSUPPORTED_STATUSES → fallback 信号
    - 其余未提交 → run_attempts 动作表（401/403 冷却、429 等待、5xx 重试/切换）
    """
    c = cand.candidate
    if c.kind == "local":
        route = ("pool:" + pool + "/local:" + c.model_name) if pool else ("local:" + c.model_name)
        model_obj = pm.mgr.find_model_by_served_name(c.model_name)
        if model_obj is None or model_obj.name not in pm.mgr.active_services:
            return AttemptResult(status=None, route=route,
                                 error="local target '" + c.model_name + "' is no longer active")
        data["model"] = model_obj.served_name or c.model_name
        gate = pm.dual_gate.acquire_target(c.model_name, timeout=_gate_timeout(remaining))
        if not gate.ok:
            return AttemptResult(admitted=False, route=route,
                                 error="physical target rate limited")
        # 评审 PR#17 B2a：gate 持有到上游返回/交付完毕（对齐 chat 直连）——
        # 原先 try/finally 在 urlopen 之前就 release，per-target 并发
        # 信号量对在途 native 流量零约束。统一在交付段 finally 释放。
        port = pm.get_target_port(c.model_name)
        if not port:
            gate.release()
            return AttemptResult(status=None, route=route,
                                 error="local target has no available port")
        url = _native_url(None, port)
        headers = _auth_headers(None, port)
        provider_cfg = None
    else:
        route = ("pool:" + pool + "/cloud:" + c.provider + "/" + c.model_id) if pool \
            else ("cloud:" + c.provider + "/" + c.model_id)
        provider_cfg = pm.cloud.get_provider_config(c.provider)
        cloud_model = pm.cloud.cloud_models.get(c.provider + "/" + c.model_id)
        if provider_cfg is None or cloud_model is None:
            return AttemptResult(status=None, route=route,
                                 error="cloud target unavailable")
        snap_base = (cand.route or {}).get("openai_base") if cand.route else None
        if snap_base:
            provider_cfg = copy.copy(provider_cfg)
            provider_cfg.openai_base = snap_base
        data["model"] = c.model_id
        gate = pm.dual_gate.acquire_target(c.provider + "/" + c.model_id,
                                           timeout=_gate_timeout(remaining))
        if not gate.ok:
            return AttemptResult(admitted=False, route=route,
                                 error="cloud target rate limited")
        url = _native_url(provider_cfg, None)
        headers = _auth_headers(provider_cfg, None)

    body = json.dumps(data).encode("utf-8")
    try:
        req = Request(url, data=body, headers=headers, method="POST")
        timeout = remaining if remaining is not None else (
            provider_cfg.timeout if provider_cfg is not None else 60)
        resp = urlopen(req, timeout=timeout)
    except _HTTPError as e:
        # 明确失败 → 兜底信号（未提交任何字节）；B2a：先释放 gate
        gate.release()
        try:
            e.read(); e.close()
        except Exception:
            pass
        if e.code in RESPONSES_UNSUPPORTED_STATUSES:
            return AttemptResult(ok=False, committed=False, status=e.code,
                                 route=route, error="responses_unsupported",
                                 incompatible=True)
        if e.code in (401, 403, 429) or 500 <= e.code <= 599:
            return AttemptResult(ok=False, committed=False, status=e.code,
                                 route=route, error="upstream_http_" + str(e.code))
        # 其他 4xx（畸形请求等）→ 终止不回退，直发声封
        handler._send_json(
            {"error": "upstream returned HTTP " + str(e.code),
             "status": "upstream_error"}, e.code)
        return AttemptResult(ok=False, committed=True, status=e.code,
                             route=route, error="upstream_http_" + str(e.code))
    except Exception as e:
        gate.release()
        return AttemptResult(status=None, route=route, error=str(e))

    # 2xx → 原生交付（headers/字节直通；committed 起点同 forwarder 契约）
    committed = False
    try:
        committed = True
        if client_stream:
            ct = resp.getheader("Content-Type") or "text/event-stream"
            handler.send_response(resp.status)
            handler.send_header("Content-Type", ct)
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                handler._safe_write((str(len(chunk)) + "\r\n").encode("ascii"))
                handler._safe_write(chunk)
                handler._safe_write(b"\r\n")
            handler._safe_write(b"0\r\n\r\n")
        else:
            raw = resp.read()
            handler.send_response(resp.status)
            handler.send_header("Content-Type",
                                resp.getheader("Content-Type") or "application/json")
            handler.send_header("Content-Length", str(len(raw)))
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.end_headers()
            handler._safe_write(raw)
        usage = {}
        if not client_stream:
            # 评审 PR#17 H1：非流式完整 JSON 已在手——解析 Responses usage
            # （input_tokens/output_tokens；cached 取 input_tokens_details）。
            # 此前恒 {}，RequestLog 成本归因全错。流式直通不缓冲，usage 留空。
            try:
                u = (json.loads(raw) or {}).get("usage") or {}
                usage = {
                    "prompt_tokens": int(u.get("input_tokens") or 0),
                    "prompt_tokens_cached": int(
                        (u.get("input_tokens_details") or {}).get("cached_tokens") or 0),
                    "completion_tokens": int(u.get("output_tokens") or 0),
                }
            except Exception:
                usage = {}
        return AttemptResult(ok=True, status=resp.status, committed=True,
                             route=route, usage=usage, ttft_ms=None)
    except Exception as e:
        return AttemptResult(ok=False, committed=committed, status=resp.status
                             if committed else None, route=route,
                             error="native delivery error: " + str(e))
    finally:
        gate.release()
        resp.close()


def _transport_factory(handler, pm, data, *, requested_model, pool, client_stream):
    """每候选传输回调：原生优先 → 显式不支持（400/404/422/501，未提交）→
    chat 翻译兜底（同一次 attempt 内，不重复消耗预算）。"""
    chat_data_holder = {}

    def transport(cand, remaining):
        payload = copy.deepcopy(data)
        res = _probe_native(handler, pm, payload, cand, pool=pool,
                            remaining=remaining, client_stream=client_stream)
        if res.ok or res.committed or res.status is None:
            return res
        if res.status not in RESPONSES_UNSUPPORTED_STATUSES:
            return res
        # 兜底：Responses → chat 翻译（仅显式不支持时，用户裁决 2026-11-17）
        if not chat_data_holder:
            chat_data_holder["data"] = responses_to_chat(copy.deepcopy(data))
            chat_data_holder["data"]["model"] = requested_model
        chat_data = copy.deepcopy(chat_data_holder["data"])
        return attempt_chat_fallback(handler, pm, chat_data, cand,
                                     requested_model=requested_model,
                                     pool=pool, remaining=remaining,
                                     client_stream=client_stream)
    return transport


# ── 池入口（/v1/responses + fabric-* 模型） ─────────────────────

def handle_pool_responses(handler, pm, data, model) -> bool:
    """/v1/responses 的 fabric- 池分支（鉴权/错误信封同 handle_pool_chat）。
    Returns True = 已处理；False = 非池请求。"""
    if not is_pool_name(model):
        return False

    req_id = pm.new_request_id()
    handler._req_start = getattr(handler, "_req_start", None) or time.monotonic()
    auth_header = handler.headers.get("Authorization", "") or handler.headers.get("x-api-key", "")
    key_name = pm.auth.key_name(auth_header) if pm.auth.enabled else "anonymous"

    if pm.auth.enabled:
        auth_ok, auth_reason = pm.auth.check(auth_header, model)
        if not auth_ok:
            _log_and_reject(handler, pm, req_id=req_id, key_name=key_name,
                            model=model, error=auth_reason)
            return True

    # PK4 T1 + 评审 PR#17 N1：pack gate（auth 之后；responses 池路径
    # 此前完全缺失）
    _pack_pm = getattr(pm, "pack_manager", None)
    if _pack_pm is not None and _pack_pm.is_model_disabled(model):
        handler._send_json(
            {"error": "pack_uninstalling", "status": "pack_uninstalling"}, 503)
        return True

    registry = getattr(pm, "pool_registry", None)
    snapshot = registry.snapshot() if registry is not None else None
    pool_snap = (snapshot.pools.get(pool_name_of(model))
                 if snapshot is not None else None)
    if pool_snap is None:
        _send_pool_error_openai(
            handler, pm, req_id=req_id, key_name=key_name, model=model,
            code="unknown_pool",
            message="Unknown logical model: " + model,
            route="pool:" + pool_name_of(model),
            metadata={"pool_summary": _error_summary(
                pool_name_of(model), "openai",
                snapshot.revision if snapshot is not None else None,
                model, "unknown_pool")})
        return True

    _ensure_cloud_discovered(pm, pool_snap)
    # 评审 PR#17 N2：需求提取吃真实消息（Responses→chat 翻译后的
    # messages）——空消息提取使 tools/vision 能力约束在 responses 路径
    # 空转，同逻辑请求在 chat 路径生效、responses 路径失效。
    req = extract_requirements("openai", responses_to_chat(copy.deepcopy(data)))
    sel = select(pool_snap, req, runtime_view(pm))
    if sel.chosen is None:
        _send_pool_error_openai(
            handler, pm, req_id=req_id, key_name=key_name, model=model,
            code=sel.error or "no_compatible_target",
            message=_no_eligible(model, sel),
            route="pool:" + pool_snap.pool.name,
            metadata={"pool_summary": _error_summary(
                pool_snap.pool.name, "openai", pool_snap.revision, model,
                sel.error or "no_compatible_target")})
        return True

    budget_start = time.monotonic()
    gate = pm.dual_gate.acquire(model, timeout=_gate_timeout(
        pool_snap.pool.deadline_ms / 1000.0))
    if not gate.ok:
        _log_and_reject(handler, pm, req_id=req_id, key_name=key_name,
                        model=model, error=gate.reason, status=429)
        return True
    try:
        client_stream = bool(data.get("stream", False))
        transport = _transport_factory(handler, pm, data,
                                       requested_model=model,
                                       pool=pool_snap.pool.name,
                                       client_stream=client_stream)
        outcome = run_attempts(pool_snap.pool, sel.ordered,
                               transport, cooldowns=getattr(pm, "pool_cooldowns", None),
                               deadline_start=budget_start,
                               history=getattr(pm, "pool_history", None))
        _log_failed_attempts(pm, handler, outcome, req_id=req_id,
                             key_name=key_name, model=model)
        pool_summary = build_pool_summary(
            outcome, pool_name=pool_snap.pool.name, protocol="openai",
            config_revision=pool_snap.revision, requested_model=model,
            candidates=pool_snap.candidates)
    finally:
        gate.release()
    _finish_outcome_openai(handler, pm, outcome, req_id=req_id,
                           key_name=key_name, model=model,
                           pool_summary=pool_summary)
    return True


def _no_eligible(model, sel):
    parts = []
    for d in sel.decisions:
        if not d.included:
            parts.append(d.candidate_id + ": " + d.reason)
    return ("No eligible target for " + model +
            " (" + (", ".join(parts) or "no candidates") + ")")


def _log_and_reject(handler, pm, *, req_id, key_name, model, error, status=401):
    """401/429 类拒绝（信封 + 日志 + route 池名）。"""
    _log(pm, handler, req_id=req_id, key_name=key_name, model=model,
         status=status, error=error, route="pool:" + pool_name_of(model))
    handler._send_json({"error": error, "status": "unauthorized" if status == 401 else "rate_limit"},
                       status)


# ── 直连入口（/v1/responses + 非 fabric-* 模型） ─────────────────

def handle_responses_direct(handler, pm, data, model) -> bool:
    """直连模型 Responses 入口。单目标：本地活跃模型或云直连路由。
    返回 True = 已处理。"""
    req_id = pm.new_request_id()
    handler._req_start = getattr(handler, "_req_start", None) or time.monotonic()
    auth_header = handler.headers.get("Authorization", "") or handler.headers.get("x-api-key", "")
    key_name = pm.auth.key_name(auth_header) if pm.auth.enabled else "anonymous"
    handler._usage = {"prompt_tokens": 0, "prompt_tokens_cached": 0, "completion_tokens": 0}
    _reg = getattr(pm, "agent_registry", None)
    if _reg is not None:
        handler._agent_hit = _reg.classify(
            request_protocol(getattr(handler, "path", "")),
            getattr(handler, "headers", {}))

    if pm.auth.enabled:
        model_for_auth = model.split("/")[-1] if "/" in model else model
        auth_ok, auth_reason = pm.auth.check(auth_header, model_for_auth)
        if not auth_ok:
            pm.logger.log(RequestLog(
                req_id=req_id, key_name=key_name, model=model_for_auth,
                status=401, error=auth_reason,
                agent=handler._agent_hit.agent if hasattr(handler, "_agent_hit") else "",
                ua=handler._agent_hit.ua if hasattr(handler, "_agent_hit") else "",
                x_app=getattr(handler, "_agent_hit", None) and handler._agent_hit.x_app or "",
                duration_ms=(time.monotonic() - handler._req_start) * 1000,
            ))
            handler._send_json({"error": auth_reason, "status": "unauthorized"}, 401)
            return True

    # 评审 PR#17 B2b：逻辑级限流（对齐 chat 直连 chat_handlers 的
    # pm.dual_gate.acquire）——此前 /v1/responses 直连完全不消耗
    # model 级 RPM/并发预算，本地引擎可被无限制流量压垮。
    _logic_gate = pm.dual_gate.acquire(model)
    if not _logic_gate.ok:
        pm.logger.log(RequestLog(
            req_id=req_id, key_name=key_name, model=model,
            status=429, error=_logic_gate.reason, route="direct",
            agent=handler._agent_hit.agent if hasattr(handler, "_agent_hit") else "",
            ua=handler._agent_hit.ua if hasattr(handler, "_agent_hit") else "",
            x_app=getattr(handler, "_agent_hit", None) and handler._agent_hit.x_app or "",
            duration_ms=(time.monotonic() - handler._req_start) * 1000,
        ))
        handler._send_json(
            {"error": "Rate limited: " + str(_logic_gate.reason),
             "status": "rate_limit"}, 429)
        return True

    client_stream = bool(data.get("stream", False))

    # PK4 T1 + 评审 PR#17 N1：pack gate（auth 之后；chat/messages 路径
    # 有、responses 路径此前完全缺失——chat 503 的模型可从 responses 侧门进）
    _pack_pm = getattr(pm, "pack_manager", None)
    if _pack_pm is not None and _pack_pm.is_model_disabled(model):
        handler._send_json(
            {"error": "pack_uninstalling", "status": "pack_uninstalling"}, 503)
        return True

    # 云直连路由（与 chat 直连同判定顺序）
    pm.ensure_cloud_discovered()
    local_model_names = {m.served_name for m in pm.mgr._models.values() if m.served_name}
    route = pm.cloud.resolve_route(model, local_model_names)
    if route and route.startswith("cloud:"):
        provider_name = route.split(":", 1)[1]
        provider_cfg = pm.cloud.get_provider_config(provider_name)
        cloud_model = pm.cloud.resolve_cloud_model(provider_name, model)
        if provider_cfg and cloud_model:
            # 评审 PR#17 B1：provider 用裸名——_probe_native 云分支以
            # cand.provider 直查 get_provider_config，带 "cloud:" 前缀必
            # None → status=None → fallback 条件永不满足 → 客户端空响应。
            cand = _DirectCandidate(provider=provider_name,
                                    model_id=model, kind="cloud")
            # 直连用单候选 AttemptResult 路径：原生 → 显式不支持 → chat 兜底
            res = _probe_native(handler, pm, data, cand, pool=None,
                                remaining=None, client_stream=client_stream)
            if (not res.ok and not res.committed and res.status is not None
                    and res.status in RESPONSES_UNSUPPORTED_STATUSES):
                chat_data = responses_to_chat(copy.deepcopy(data))
                chat_data["model"] = model
                dc = _DirectCandidate(provider=provider_name, model_id=model, kind="cloud")
                # 评审 PR#17 N3：快照端点走 cand.route（_resolve_chat_target
                # 只读 route["openai_base"]；_openai_base 属性无人消费，
                # 违反池路径遵守的快照端点规则）
                dc.route = {"openai_base": provider_cfg.openai_base}
                res = attempt_chat_fallback(handler, pm, chat_data, dc,
                                            requested_model=model, pool=None,
                                            remaining=None, client_stream=client_stream)
            if not res.ok and not res.committed:
                # 评审 PR#17 B3：直连未交付失败必须发声封（对齐池路径
                # _finish_outcome_openai 协议）：status 非 None → 按上游
                # 状态明确错误；None（连接拒/gate 超时/引擎重启）→ 502。
                # 此前只写 RequestLog 就 return，客户端 curl (52) 空响应。
                if res.status is not None:
                    handler._send_json(
                        {"error": "upstream returned HTTP " + str(res.status),
                         "status": "upstream_error"}, res.status)
                else:
                    handler._send_json(
                        {"error": "upstream unavailable for " + model,
                         "status": "upstream_unavailable"}, 502)
            _logic_gate.release()
            usage = res.usage or {}
            pm.logger.log(RequestLog(
                model=model, status=res.status if res.ok else (res.status or 502),
                route="cloud:" + provider_name,
                key_name=key_name, req_id=req_id,
                cloud_provider=provider_name,
                agent=handler._agent_hit.agent if hasattr(handler, "_agent_hit") else "",
                ua=handler._agent_hit.ua if hasattr(handler, "_agent_hit") else "",
                x_app=getattr(handler, "_agent_hit", None) and handler._agent_hit.x_app or "",
                tokens_in=usage.get("prompt_tokens", 0),
                tokens_in_cached=usage.get("prompt_tokens_cached", 0),
                tokens_out=usage.get("completion_tokens", 0),
                ttft_ms=res.ttft_ms,
                error=res.error,
            ))
            return True

    # 本地直连
    service_name = pm.model_to_service(model)
    model_obj = pm.mgr.get_model(service_name) if service_name else None
    if model_obj and model_obj.name in pm.mgr.active_services:
        cand = _DirectCandidate(provider="", model_id=model_obj.served_name or model, kind="local")
        data["model"] = model_obj.served_name or model
        res = _probe_native(handler, pm, data, cand, pool=None,
                            remaining=None, client_stream=client_stream)
        if (not res.ok and not res.committed and res.status is not None
                and res.status in RESPONSES_UNSUPPORTED_STATUSES):
            chat_data = responses_to_chat(copy.deepcopy(data))
            chat_data["model"] = model_obj.served_name or model
            lc = _DirectCandidate(provider="", model_id=model_obj.served_name or model, kind="local")
            res = attempt_chat_fallback(handler, pm, chat_data, lc,
                                        requested_model=model, pool=None,
                                        remaining=None, client_stream=client_stream)
        if not res.ok and not res.committed:
            # 评审 PR#17 B3：同云直连——未交付失败发声封，不再静默 return。
            if res.status is not None:
                handler._send_json(
                    {"error": "upstream returned HTTP " + str(res.status),
                     "status": "upstream_error"}, res.status)
            else:
                handler._send_json(
                    {"error": "upstream unavailable for " + model,
                     "status": "upstream_unavailable"}, 502)
        _logic_gate.release()
        usage = res.usage or {}
        pm.logger.log(RequestLog(
            req_id=req_id, key_name=key_name, model=model,
            status=res.status if res.ok else (res.status or 502),
            route="local",
            agent=handler._agent_hit.agent if hasattr(handler, "_agent_hit") else "",
            ua=handler._agent_hit.ua if hasattr(handler, "_agent_hit") else "",
            x_app=getattr(handler, "_agent_hit", None) and handler._agent_hit.x_app or "",
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_in_cached=usage.get("prompt_tokens_cached", 0),
            tokens_out=usage.get("completion_tokens", 0),
            ttft_ms=res.ttft_ms,
            error=res.error,
        ))
        return True

    _logic_gate.release()
    elapsed = (time.monotonic() - handler._req_start) * 1000
    pm.logger.log(RequestLog(
        req_id=req_id, key_name=key_name, model=model,
        status=404, error="unknown_model",
        agent=handler._agent_hit.agent if hasattr(handler, "_agent_hit") else "",
        ua=handler._agent_hit.ua if hasattr(handler, "_agent_hit") else "",
        x_app=getattr(handler, "_agent_hit", None) and handler._agent_hit.x_app or "",
        duration_ms=elapsed,
    ))
    handler._send_json({"error": "Unknown model: " + model}, 404)
    return True


class _DirectCandidate:
    """直连路径的单目标候选适配（dispatch._attempt_cloud 同字段面）。"""
    def __init__(self, provider, model_id, kind):
        self.candidate = self
        self.provider = provider
        self.model_id = model_id
        self.kind = kind
        self.model_name = model_id if kind == "local" else ""
        self.route = None
        self.endpoint_fingerprint = ""
