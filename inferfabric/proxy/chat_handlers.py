"""
inferfabric/proxy/chat_handlers.py — Chat completion handlers.

Extracted from proxy.py (v4.1 P3 split).
"""

import json
import time
import uuid
import logging
from inferfabric import forwarder
from inferfabric.proxy.sse_buffer import SSELineBuffer, first_token_ttft_cb
from inferfabric.proxy.request_logger import RequestLog
from inferfabric.agent_registry import request_protocol
from inferfabric.proxy.usage import normalize_usage
from inferfabric.anomaly_collector import AnomalyEvent

log = logging.getLogger("inferfabric.proxy.chat")

# 引擎类型 → 上游聊天 API 路径（YAML model type 驱动，非硬编码）
# 所有非 Ollama LLM 引擎统一归一化到 OpenAI /v1/chat/completions
_ENGINE_API_PATHS = {
    "ninfer":    "/v1/chat/completions",
    "vllm":      "/v1/chat/completions",
    "sglang":    "/v1/chat/completions",
    "ollama_cpp":"/v1/chat/completions",
}


def _normalize_tools_for_openai(data):
    """Rewrite Anthropic-format tool definitions into OpenAI function format.

    Minimal conversion: tools with ``input_schema`` (and no ``function``) are
    wrapped in an OpenAI ``function`` envelope so they survive vLLM's Pydantic
    validation at ``/v1/chat/completions``.
    """
    tools = data.get("tools")
    if isinstance(tools, list):
        for i, t in enumerate(tools):
            if isinstance(t, dict) and "input_schema" in t and "function" not in t:
                fn = {"name": t.get("name", "")}
                if t.get("description"):
                    fn["description"] = t["description"]
                fn["parameters"] = t.get("input_schema") or {"type": "object", "properties": {}}
                tools[i] = {"type": "function", "function": fn}
    tc = data.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") in ("tool", "any", "auto", "none"):
        tc_type = tc["type"]
        if tc_type == "tool":
            data["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}
        elif tc_type == "any":
            data["tool_choice"] = "required"
        elif tc_type == "auto":
            data["tool_choice"] = "auto"
        else:
            data["tool_choice"] = "none"


def handle_ollama_native(handler, pm, data, target_port, model_obj):
    """Handle chat for Ollama backends using native /api/chat API.

    This replaces the old ProxyHandler._handle_chat_ollama_native method.
    Called directly by the handler; uses handler._send_json and handler._safe_write.
    """
    stream = data.get("stream", False)
    ollama_req = {
        "model": data["model"],
        "messages": data.get("messages", []),
        "stream": stream,
        "options": {},
    }
    if model_obj.ollama and model_obj.ollama.num_gpu >= 0:
        ollama_req["options"]["num_gpu"] = model_obj.ollama.num_gpu
    if data.get("max_tokens"):
        ollama_req["options"]["num_predict"] = data["max_tokens"]
    if model_obj.ollama and model_obj.ollama.keep_alive:
        ollama_req["keep_alive"] = model_obj.ollama.keep_alive
    if model_obj.ollama_cpp and model_obj.ollama_cpp.gpu_layers != 0:
        ollama_req["options"]["num_gpu"] = model_obj.ollama_cpp.gpu_layers

    body = json.dumps(ollama_req).encode("utf-8")

    conn = None
    try:
        conn = pm.make_conn(target_port)
        conn.request("POST", "/api/chat", body=body,
                      headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
    except Exception as e:
        log.error("Ollama native forward to :%d failed: %s", target_port, e)
        handler._send_json({"error": str(e)}, 502)
        if conn:
            try:
                conn.close()
            except Exception as e:
                log.warning("Chat handler error: %s", e)
        return

    try:
        resp_status = resp.status
        if resp_status != 200:
            err_body = resp.read().decode("utf-8", errors="replace")
            handler._send_json({"error": f"Ollama error: {err_body[:500]}", }, resp_status)
            return

        if stream:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.send_header("Cache-Control", "no-cache")
            handler.end_headers()
            chat_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
            full_content = ""
            try:
                buffer = b""
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            content = obj.get("message", {}).get("content", "")
                            if content:
                                full_content += content
                                sse_data = json.dumps({
                                    "id": chat_id,
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": data["model"],
                                    "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]
                                })
                                handler._safe_write(f"data: {sse_data}\n\n".encode())
                        except json.JSONDecodeError as e:
                            log.warning("JSON decode error: %s", e)
                sse_done = json.dumps({
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": data["model"],
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                })
                handler._safe_write(f"data: {sse_done}\n\n".encode())
                handler._safe_write(b"data: [DONE]\n\n")
            except Exception as e:
                log.error("Ollama native stream error: %s", e)
        else:
            resp_body = resp.read()
            try:
                obj = json.loads(resp_body)
                full_content = obj.get("message", {}).get("content", "")
                total_input = obj.get("prompt_eval_count", 0) or 0
                total_output = obj.get("eval_count", 0) or 0
            except json.JSONDecodeError:
                full_content = resp_body.decode("utf-8", errors="replace")
                total_input = 0
                total_output = 0
            handler._send_json({
                "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": data["model"],
                "system_fingerprint": "fp_ollama",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": full_content},
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": total_input,
                    "completion_tokens": total_output,
                    "total_tokens": total_input + total_output
                }
            })
    except Exception as e:
        log.error("Ollama native response error: %s", e)
        handler._send_json({"error": str(e)}, 500)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        try:
            resp.close()
        except Exception:
            pass


def handle_chat(handler, pm, data):
    """Handle OpenAI chat completions request.

    Replaces ProxyHandler._handle_chat. Dispatches to either
    handle_ollama_native or the vLLM forwarding path.
    """
    model = data.get("model")
    if not model:
        handler._send_json({"error": "model field is required", "status": "bad_request"}, 400)
        return
    log.debug("Incoming request: model=%s", model)

    # PR-B: Request context for logging
    req_id = pm.new_request_id()
    handler._req_start = time.monotonic()
    auth_header = handler.headers.get("Authorization", "") or handler.headers.get("x-api-key", "")
    key_name = pm.auth.key_name(auth_header) if pm.auth.enabled else "anonymous"
    # v6.5: 客户端 Agent 分类（每请求一次，缓存到 handler 供所有 RequestLog 站点取值）
    _reg = getattr(pm, "agent_registry", None)
    if _reg is not None:
        handler._agent_hit = _reg.classify(
            request_protocol(getattr(handler, "path", "")),
            getattr(handler, "headers", {}))

    # G-1b: Initialize usage (before any early return)
    # prompt_tokens = 总输入（含缓存命中）；prompt_tokens_cached = 缓存命中部分
    handler._usage = {"prompt_tokens": 0, "prompt_tokens_cached": 0,
                      "completion_tokens": 0}

    # 逻辑模型池（iff/*）：完整名鉴权 + 池选择，先于直连鉴权/缓存
    # （spec §13 顺序：鉴权→槽位识别→选择→传输；池请求跳过响应缓存）
    # 函数内 import 避免 chat_handlers ↔ model_pool.dispatch 循环依赖
    if model.startswith("iff/") and getattr(pm, "pool_registry", None) is not None:
        from inferfabric.model_pool.dispatch import handle_pool_chat
        if handle_pool_chat(handler, pm, data, model):
            return

    # PR-A: Auth check
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
                duration_ms=(time.monotonic()-handler._req_start)*1000,
            ))
            handler._send_json({"error": auth_reason, "status": "unauthorized"}, 401)
            return

    # PR-G2: 流式请求自动注入 include_usage=true — 确保 token 提取生效
    # vLLM 默认不在 SSE 流中返回 usage，需客户端传 stream_options.include_usage=true
    # IFF 代理层透明注入，对客户端无感
    if data.get("stream", False):
        if "stream_options" not in data:
            data["stream_options"] = {}
        data["stream_options"].setdefault("include_usage", True)

    stream = data.get("stream", False)

    # R5: 响应缓存查找
    cache_enabled = getattr(pm, '_runtime_config', {}).get("cache", {}).get("enabled", True)
    cache_body = None
    if cache_enabled and not stream and data.get("temperature", 0) in (0, None):
        cached = getattr(pm, 'response_cache', None)
        if cached is not None:
            cached_resp = cached.get(model, data)
            if cached_resp is not None:
                # 缓存命中不记 RequestLog（方案 A，与 /v1/messages 路径一致）：
                # 回放历史响应未发生推理，落库会重复计数 token/费用并产生
                # 「1ms 带数万 tokens」假完成行。LRU 生效见 snapshot
                # local_models.cache_stats（ResponseCache.stats()）。
                log.info("/v1/chat/completions → cache HIT for %s", model)
                handler._send_json(cached_resp["body"], 200)
                return
            # A3: 改写前快照 — 后续 model 改写（data["model"]=served_name）与
            # tools 归一化（_normalize_tools_for_openai）都会 mutate data，
            # 转发后 PUT 侧必须用此快照，保证与 GET 侧键一致。
            cache_body = json.loads(json.dumps(data))

    # Auto-switch（R-AS: 活读 pm.auto_switch，UI 开关可即时翻转，无需重启）
    service_name = pm.model_to_service(model)
    if service_name and pm.auto_switch:
        switched = pm.ensure_service(service_name)
        if switched is None:
            elapsed = (time.monotonic() - handler._req_start) * 1000
            pm.logger.log(RequestLog(
                req_id=req_id, key_name=key_name, model=model,
                status=409, error="switch_in_progress",
                agent=handler._agent_hit.agent if hasattr(handler, "_agent_hit") else "",
                ua=handler._agent_hit.ua if hasattr(handler, "_agent_hit") else "",
                    x_app=getattr(handler, "_agent_hit", None) and handler._agent_hit.x_app or "",
                duration_ms=elapsed,
            ))
            pm.anomalies.record(AnomalyEvent(
                category="routing", severity="info", model=model,
                status_code=409,
                message=f"Switch to {service_name} in progress",
                possible_cause="另一请求正在发起同模型切换，线程锁被占用。",
            ))
            handler._send_json({"error": "switch already in progress", "status": "conflict"}, 409)
            return
        if not switched and service_name not in pm.mgr.active_services:
            if pm.mgr.state.is_manually_stopped(service_name):
                reason = f"{service_name} was manually stopped — auto-switch blocked for {pm.mgr.state.MANUAL_STOP_TTL}s"
            else:
                reason = "tri-state rule violation or switch in progress"
            elapsed = (time.monotonic() - handler._req_start) * 1000
            pm.logger.log(RequestLog(
                req_id=req_id, key_name=key_name, model=model,
                status=503, error="cannot_switch",
                agent=handler._agent_hit.agent if hasattr(handler, "_agent_hit") else "",
                ua=handler._agent_hit.ua if hasattr(handler, "_agent_hit") else "",
                    x_app=getattr(handler, "_agent_hit", None) and handler._agent_hit.x_app or "",
                duration_ms=elapsed,
            ))
            pm.anomalies.record(AnomalyEvent(
                category="model", severity="warning", model=model,
                status_code=503,
                message=f"Cannot switch to {service_name}: {reason}",
                possible_cause="模型被手动停止 / 三态规则阻止 / 切换正在进行中",
            ))
            handler._send_json(
                {"error": f"Cannot switch to {reason}", "status": "switch_blocked", "retry_after": 10},
                503,
                extra_headers={"Retry-After": "10"},  # R0: Agent 可据此退避
            )
            return

    target_port = pm.get_target_port(model)
    if not target_port:
        # PR-D: Check cloud routing before returning 404
        pm.ensure_cloud_discovered()
        local_model_names = {m.served_name for m in pm.mgr._models.values() if m.served_name}
        route = pm.cloud.resolve_route(model, local_model_names)
        if route and route.startswith("cloud:"):
            provider_name = route.split(":", 1)[1]
            provider_cfg = pm.cloud.get_provider_config(provider_name)
            short_name = model.split("/")[-1] if "/" in model else model
            cloud_model = (pm.cloud.cloud_models.get(f"{provider_name}/{short_name}")
                          or pm.cloud.cloud_models.get(short_name))
            if provider_cfg and cloud_model:
                log.info("/v1/chat/completions → CLOUD %s [model=%s]", provider_name, model)
                result = forwarder.forward_to_cloud(
                    handler, data, provider_cfg, cloud_model,
                    protocol="openai", original_model=model,
                )
                pm.logger.log(RequestLog(
                    model=model, status=result.status, route=f"cloud:{provider_name}",
                    key_name=key_name, req_id=req_id,
                    cloud_provider=provider_name,
                    agent=handler._agent_hit.agent if hasattr(handler, "_agent_hit") else "",
                    ua=handler._agent_hit.ua if hasattr(handler, "_agent_hit") else "",
                    x_app=getattr(handler, "_agent_hit", None) and handler._agent_hit.x_app or "",
                    tokens_in=result.usage.get("prompt_tokens", 0),
                    tokens_in_cached=result.usage.get("prompt_tokens_cached", 0),
                    tokens_out=result.usage.get("completion_tokens", 0),
                    ttft_ms=result.ttft_ms,
                    duration_ms=result.duration_ms,
                    error=result.error,
                ))
                return
        elapsed = (time.monotonic() - handler._req_start) * 1000
        pm.logger.log(RequestLog(
            req_id=req_id, key_name=key_name, model=model,
            status=404, error="unknown_model",
            agent=handler._agent_hit.agent if hasattr(handler, "_agent_hit") else "",
            ua=handler._agent_hit.ua if hasattr(handler, "_agent_hit") else "",
                    x_app=getattr(handler, "_agent_hit", None) and handler._agent_hit.x_app or "",
            duration_ms=elapsed,
        ))
        pm.anomalies.record(AnomalyEvent(
            category="routing", severity="warning", model=model,
            status_code=404,
            message=f"Unknown model '{model}' — no local served_name or cloud match",
            possible_cause="1. 客户端传了不存在的模型名；2. cloud_provider.yaml 缺少对应 model_id；"
                           "3. models.d/*.ymal 的 served_name 配置未覆盖此模型名",
        ))
        handler._send_json({"error": f"Unknown model: {model}"}, 404)
        return

    # Rewrite model name to upstream's served_name
    ollama_model_obj = None
    if service_name:
        model_obj = pm.mgr.get_model(service_name)
        if model_obj and model_obj.served_name:
            data["model"] = model_obj.served_name
        if model_obj and (model_obj.is_ollama or model_obj.is_ollama_cpp):
            ollama_model_obj = model_obj

    if ollama_model_obj and (ollama_model_obj.is_ollama or ollama_model_obj.is_ollama_cpp):
        handle_ollama_native(handler, pm, data, target_port, ollama_model_obj)
        return

    # vLLM/NInfer path — apply dynamic rate limiter
    # Normalize Anthropic-style tools (input_schema) to OpenAI function format
    # before forwarding to vLLM's /v1/chat/completions (prevents per-tool
    # "Field required: function" 400s that Claude Code does not retry).
    _normalize_tools_for_openai(data)
    body = json.dumps(data).encode("utf-8")

    # 路径归一化：将 incoming 路径（/v1/completions, /api/chat, /api/generate）
    # 统一映射到引擎支持的聊天端点。由 model YAML type 字段驱动，
    # 不硬编码模型名或端口（PR-19 预存死路由修复）。
    upstream_path = _ENGINE_API_PATHS.get(model_obj.type, "/v1/chat/completions")
    log.debug("%s → upstream %s (engine=%s)", handler.path, upstream_path, model_obj.type)

    # v4.6.3: 使用配置的 timeout (observe 模式下不会 429)
    gate = pm.dual_gate.acquire(model)
    if not gate.ok:
        pm.logger.log(RequestLog(
            req_id=req_id, key_name=key_name, model=model,
            status=429, error=gate.reason, route="local",
            agent=handler._agent_hit.agent if hasattr(handler, "_agent_hit") else "",
            ua=handler._agent_hit.ua if hasattr(handler, "_agent_hit") else "",
                    x_app=getattr(handler, "_agent_hit", None) and handler._agent_hit.x_app or "",
            duration_ms=(time.monotonic()-handler._req_start)*1000,
        ))
        handler._send_json(
            {"error": f"Rate limited: {gate.reason}", "status": "rate_limit"},
            429,
        )
        return
    try:
        for attempt in range(3):
            if _forward_request(handler, pm, target_port, body, stream, model_name=model, upstream_path=upstream_path, cache_body=cache_body):
                # PR-B: Log successful request with TTFT + usage
                ttft = getattr(handler, '_ttft_ms', None)
                usage = getattr(handler, '_usage', {})
                pm.logger.log(RequestLog(
                    req_id=req_id, key_name=key_name, model=model,
                    status=200, ttft_ms=ttft, route="local",
                    agent=handler._agent_hit.agent if hasattr(handler, "_agent_hit") else "",
                    ua=handler._agent_hit.ua if hasattr(handler, "_agent_hit") else "",
                    x_app=getattr(handler, "_agent_hit", None) and handler._agent_hit.x_app or "",
                    tokens_in=usage.get("prompt_tokens", 0),
                    tokens_in_cached=usage.get("prompt_tokens_cached", 0),
                    tokens_out=usage.get("completion_tokens", 0),
                    duration_ms=(time.monotonic()-handler._req_start)*1000,
                ))
                return
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))  # 0.5s, 1s, 2s
        pm.logger.log(RequestLog(
            req_id=req_id, key_name=key_name, model=model,
            status=502, error="upstream_unavailable", route="local",
            agent=handler._agent_hit.agent if hasattr(handler, "_agent_hit") else "",
            ua=handler._agent_hit.ua if hasattr(handler, "_agent_hit") else "",
                    x_app=getattr(handler, "_agent_hit", None) and handler._agent_hit.x_app or "",
            duration_ms=(time.monotonic()-handler._req_start)*1000,
        ))
        handler._send_json({"error": "Upstream unavailable after retry"}, 502)
    finally:
        gate.release()


def _forward_request(handler, pm, target_port, body, stream, model_name="", upstream_path=None, cache_body=None, timeout=None):
    """Forward a request to an upstream service.

    Args:
        upstream_path: 上游 API 路径（由模型引擎 YAML type 字段驱动）。
                       为 None 时回退 handler.path（兼容旧调用方）。
        cache_body: A3 (R5) — 转发前请求 body 快照（model 为客户端原名、
                    tools 未归一化）。非流式 200 且 JSON 可解析时用它写
                    响应缓存，保证与 GET 侧缓存键一致；None 则不写。
        timeout: 可选连接/读超时秒数（池路径按剩余预算传入，终审 #7）。
                 None = 引擎默认（既有行为逐字节不变）。

    Returns True if the response was fully sent to the client.
    Returns False if the caller should retry (headers not yet sent).
    """
    if upstream_path is None:
        upstream_path = handler.path
    headers_sent = False
    conn = None
    resp = None
    try:
        conn = pm.make_conn(target_port)
        if timeout is not None:
            conn.settimeout(timeout)
        conn.request("POST", upstream_path, body=body,
                      headers={"Content-Type": "application/json"})
        resp = conn.getresponse()

        resp_status = resp.status
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        resp_ct = resp_headers.get("content-type", "application/json")

        if stream:
            headers_sent = True
            handler.send_response(resp_status)
            handler.send_header("Content-Type", resp_ct)
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.send_header("Cache-Control", "no-cache")
            handler.end_headers()
            # PR-B: TTFT tracking — v6.1 语义 = 首个内容 delta（回调记录，精确到
            # token）；首 chunk 记录仅作无回调时的兜底（避免在 role-only 前言
            # chunk 上提前记）
            ttft_recorded = False
            sse_buf = SSELineBuffer(first_token_ttft_cb(handler))  # G-1b + v6.1
            use_token_ttft = getattr(sse_buf, "_on_first_token", None) is not None
            try:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    # PR-B: Record TTFT on first data chunk（无回调时兜底）
                    if not ttft_recorded and not use_token_ttft:
                        ttft_recorded = True
                        if hasattr(handler, '_req_start'):
                            handler._ttft_ms = (time.monotonic() - handler._req_start) * 1000
                    size = f"{len(chunk):x}\r\n".encode()
                    handler._safe_write(size)
                    handler._safe_write(chunk)
                    handler._safe_write(b"\r\n")
                    # G-1b: 旁路观察提取 usage
                    sse_buf.feed(chunk)
                handler._safe_write(b"0\r\n\r\n")
            except Exception as e:
                log.debug("Stream forwarding interrupted: %s", e)
            finally:
                # G-1b: flush 残余 + 保存 usage（即使异常也保留已提取的部分）
                sse_buf.flush()
                handler._usage = dict(sse_buf.usage)
                resp.close()
        else:
            # 非流式不记 TTFT：引擎算完整响应才回 header，"收到 header"≈总耗时，
            # 无"首 token"语义。若记为 ttft，聚合器派生 TPOT
            # (duration-ttft)/(tokens_out-1) ≈ 0.001 → 舍入 0.0 污染延迟趋势。
            # 保持 _ttft_ms=None（→ 请求日志 ttft_ms NULL，与 Anthropic 非流式路径一致）。
            body_obj = None
            try:
                resp_body = resp.read()
                # G-1b: 非流式 — 解析 JSON body 提取 usage
                try:
                    body_obj = json.loads(resp_body)
                    usage = body_obj.get("usage", {})
                    if usage:
                        # 双协议归一化（OpenAI/Anthropic/百度），含缓存命中拆分
                        handler._usage = normalize_usage(usage)
                except (json.JSONDecodeError, AttributeError):
                    body_obj = None
                # A3 (R5): 非流式 200 响应写缓存 — 修复前 OpenAI 路径只有
                # GET 没有 PUT，缓存永久冷。键用改写前快照 cache_body
                # （model=客户端原名 + 未被转发 mutate 的 body）。
                if resp_status == 200 and cache_body is not None and isinstance(body_obj, dict):
                    rc = getattr(pm, 'response_cache', None)
                    if rc is not None:
                        try:
                            rc.put(model_name, cache_body, body_obj, handler._usage or {})
                        except Exception:
                            log.debug("R5 cache put failed (non-critical)")
            finally:
                resp.close()
            headers_sent = True
            handler.send_response(resp_status)
            handler.send_header("Content-Type", resp_ct)
            handler.send_header("Content-Length", str(len(resp_body)))
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.end_headers()
            handler._safe_write(resp_body)
        return True
    except Exception as e:
        log.error("Forward to :%d failed: %s", target_port, e)
        try:
            if resp:
                resp.close()
        except Exception:
            pass
        try:
            if conn:
                conn.close()
        except Exception:
            pass
        if not headers_sent:
            return False
        return True
    finally:
        if model_name:
            pm.release_port(model_name, target_port)
