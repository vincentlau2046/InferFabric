"""
inferfabric/forwarder.py — Forwarding logic extracted from ProxyHandler.

All functions accept a `handler` parameter (ProxyHandler instance) and
use its HTTP response methods (send_response, send_header, end_headers,
wfile.write, wfile.flush) to send data to the client.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from http.client import HTTPConnection
from urllib.request import Request, urlopen
from urllib.error import HTTPError as _HTTPError

from inferfabric.config import (
    UPSTREAM_LOCAL_RETRIES,
    exponential_backoff,
    should_retry_on_status,
)
from inferfabric.proxy.sse_buffer import SSELineBuffer, first_token_ttft_cb
from inferfabric.proxy.usage import normalize_usage


@dataclass
class CloudResult:
    """cloud 路由请求结果 — 供 RequestLog 补全"""
    status: int = 200
    # {prompt_tokens, prompt_tokens_cached, completion_tokens}（normalize_usage 口径）
    usage: dict = field(default_factory=dict)
    ttft_ms: float | None = None
    duration_ms: float = 0.0
    error: str | None = None

log = logging.getLogger("inferfabric.forwarder")


# ── Local model type filter ──

LOCAL_LLM_TYPES = {"llm", "vl", "omni"}


# ── Response helpers ──


def send_json(handler, body_d, status=200, extra_headers=None):
    """Send JSON response with CORS headers."""
    body = json.dumps(body_d, ensure_ascii=False).encode()
    try:
        handler.send_response(status)
        if extra_headers:
            for k, v in extra_headers.items():
                if v is None:
                    continue
                handler.send_header(k, str(v))
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type")
        handler.end_headers()
        handler.wfile.write(body)
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


def read_body(handler):
    """Read and parse JSON request body. Returns dict or None on error."""
    try:
        cl = handler.headers.get("Content-Length")
        if cl is None:
            # No Content-Length (e.g. chunked transfer encoding) — read all
            raw = handler.rfile.read()
            return json.loads(raw) if raw else {}
        size = int(cl)
        if size == 0:
            return {}
        if size > 100 * 1024 * 1024:  # 100MB limit (matches engine capability)
            send_json(handler, {"error": "payload too large (max 100MB)"}, 413)
            return None
        raw = handler.rfile.read(size)
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        send_json(handler, {"error": f"Invalid JSON: {e}"}, 400)
        return None


# ── Stream forwarding ──


def pipe_stream_response(handler, resp, sse_buf=None):
    """Pipe SSE stream response to client (CCR-style streaming).

    sse_buf (optional): SSELineBuffer 旁路观察器 — 每个 chunk 写入客户端后
    喂入 buffer 提取 usage；finally 里 flush。不传 sse_buf 时行为与原来完全一致
    （本地 Anthropic 路径不受影响）。
    """
    try:
        handler.send_response(resp.status)
        for h in ("content-type", "cache-control", "x-request-id"):
            val = resp.getheader(h)
            if val:
                handler.send_header(h, val)
        # CORS — required for browser-based clients
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, x-api-key, anthropic-version")
        # HTTP/1.1 keep-alive 下，响应体必须自带结束信号：发送
        # Transfer-Encoding: chunked 并按 chunked 分帧写出，否则客户端
        # (Claude/reqwest) 会一直等待 body 结束 → "wait api" 卡住
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
    except (BrokenPipeError, ConnectionResetError, OSError):
        # 客户端在 header 阶段就断开 — 关闭上游 resp 避免 socket 泄漏，直接返回
        log.info("Client disconnected before stream headers; closing upstream response")
        resp.close()
        return
    # PR-B: TTFT — 仅在 handler 携带 _req_start 时记录（本地路径）
    # v6.1: sse_buf 挂有首 token 回调时，ttft 以回调为准（首个内容 delta 时刻，
    # 精确到 token）；首 chunk 记录仅作无回调时的兜底（避免在 role / message_start
    # 前言 chunk 上提前记）
    use_token_ttft = (sse_buf is not None
                      and getattr(sse_buf, "_on_first_token", None) is not None)
    ttft_recorded = False
    try:
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            try:
                if not ttft_recorded and not use_token_ttft:
                    ttft_recorded = True
                    if hasattr(handler, '_req_start'):
                        handler._ttft_ms = (time.monotonic() - handler._req_start) * 1000
                # chunked 分帧：hex 尺寸前缀 + 数据 + CRLF
                handler.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
                handler.wfile.write(chunk)
                handler.wfile.write(b"\r\n")
                handler.wfile.flush()
                # G-1b: 旁路观察 — 零延迟透传不变，喂入 buffer 提取 usage
                if sse_buf is not None:
                    sse_buf.feed(chunk)
            except (BrokenPipeError, ConnectionResetError):
                log.info("Client disconnected during stream forwarding")
                break
        # 终止块 0 大小 chunk：HTTP/1.1 keep-alive 下通知客户端 body 结束
        try:
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
    finally:
        if sse_buf is not None:
            sse_buf.flush()
        resp.close()


# ── JSON response handling ──


def handle_json_response(handler, resp, model_obj, original_model, data, auth_header, response_cache=None):
    """Handle non-streaming JSON response; caches on success (R5)."""
    resp_status = resp.status
    resp_body = resp.read()
    if resp_status != 200:
        log.warning("Local %s returned %d (non-streaming)",
                    model_obj.name, resp_status)
        data["model"] = original_model
        resp.close()
        send_json(handler, {"error": f"Local model returned {resp_status}"}, 502)
        return
    try:
        result = json.loads(resp_body)
        usage = result.get("usage")
        if usage and isinstance(usage, dict):
            # 双协议归一化（含缓存命中拆分），口径统一见 proxy/usage.py
            handler._usage = normalize_usage(usage)
        send_json(handler, result)
        # R5: 缓存成功的非流式响应
        if response_cache is not None and resp_status == 200:
            try:
                cached_model = original_model or data.get("model", "")
                response_cache.put(cached_model, data, result, handler._usage or {})
            except Exception:
                log.debug("Cache put failed (non-critical)")
    except json.JSONDecodeError:
        send_json(handler, {"error": "invalid response from local model"}, 502)


# ── Cloud provider forwarding (PR-D) ──


def forward_to_cloud(handler, data, provider_cfg, cloud_model, protocol="openai", original_model=None):
    """双协议透传：OpenAI → cloud OpenAI endpoint, Anthropic → cloud Anthropic endpoint。

    IFF 不做协议转换，客户端用什么协议发，就往对应的云端端点转发。
    IFF 持有云端凭证，客户端只需 IFF key。

    Returns CloudResult for request logging (G-1a).
    """
    start = time.monotonic()

    # B1: 协议不匹配 501 附 hint —— 引导客户端改打该 provider 支持的协议端点
    # （只读错误路径，不改请求体/转发核心；单协议 provider 无法透传时给可执行指引）。
    if protocol == "anthropic":
        if not cloud_model.anthropic_available:
            err = f"Provider {provider_cfg.name} does not support Anthropic protocol"
            hint = "该 provider 仅支持 OpenAI 协议，请改用 POST /v1/chat/completions"
            send_json(handler, {"error": err, "hint": hint}, 501)
            return CloudResult(status=501, error=err)
        if not provider_cfg.anthropic_base:
            err = f"Provider {provider_cfg.name} has no Anthropic base configured"
            hint = "该 provider 未配置 Anthropic 端点（anthropic_base），无法以 Anthropic 协议访问"
            send_json(handler, {"error": err, "hint": hint}, 501)
            return CloudResult(status=501, error=err)
        url = f"{provider_cfg.anthropic_base.rstrip('/')}/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": provider_cfg.api_key,
        }
    else:  # openai
        if not cloud_model.openai_available:
            err = f"Provider {provider_cfg.name} does not support OpenAI protocol"
            hint = "该 provider 仅支持 Anthropic 协议，请改用 POST /v1/messages"
            send_json(handler, {"error": err, "hint": hint}, 501)
            return CloudResult(status=501, error=err)
        if not provider_cfg.openai_base:
            err = f"Provider {provider_cfg.name} has no OpenAI base configured"
            hint = "该 provider 未配置 OpenAI 端点（openai_base），无法以 OpenAI 协议访问"
            send_json(handler, {"error": err, "hint": hint}, 501)
            return CloudResult(status=501, error=err)
        url = f"{provider_cfg.openai_base.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {provider_cfg.api_key}",
        }

    # Override model with cloud model_id
    if original_model and data.get("model") != cloud_model.model_id:
        data["model"] = cloud_model.model_id

    was_stream = data.get("stream", False)
    body = json.dumps(data).encode("utf-8")

    # R2: 云端转发退避重试（3 次尝试，0.5s/1s/2s 指数退避）
    max_retries = 3
    last_error = None
    for attempt in range(max_retries):
        try:
            req = Request(url, data=body, headers=headers, method="POST")
            resp = urlopen(req, timeout=provider_cfg.timeout)

            # Retryable status (429/5xx) → close and backoff
            if should_retry_on_status(resp.status) and attempt < max_retries - 1:
                log.warning(
                    "Cloud %s returned HTTP %d (attempt %d/%d), retrying in %.1fs...",
                    provider_cfg.name, resp.status, attempt + 1, max_retries,
                    exponential_backoff(attempt),
                )
                resp.close()
                time.sleep(exponential_backoff(attempt))
                continue

            if was_stream:
                # v6.1: TTFT = 首个内容 token（回调记 handler._ttft_ms），
                # 不再用 HTTP header 到达时刻（header 早于 prefill 完成，语义错误）
                sse_buf = SSELineBuffer(first_token_ttft_cb(handler))
                pipe_stream_response(handler, resp, sse_buf)
                return CloudResult(
                    status=200,
                    usage=dict(sse_buf.usage),
                    ttft_ms=getattr(handler, "_ttft_ms", None),
                    duration_ms=(time.monotonic() - start) * 1000,
                )
            else:
                resp_body = resp.read()
                result = json.loads(resp_body)
                resp.close()
                send_json(handler, result)
                # 双协议归一化（含缓存命中拆分）— CloudResult.usage 携带
                # prompt_tokens / prompt_tokens_cached / completion_tokens
                usage = normalize_usage(result.get("usage", {}))
                # v6.1: 非流式无「首 token」语义（header 到达 ≈ 总耗时），
                # 保持 ttft None（→ 请求日志 ttft_ms NULL，与本地非流式路径一致）
                return CloudResult(
                    status=200,
                    usage=usage,
                    ttft_ms=None,
                    duration_ms=(time.monotonic() - start) * 1000,
                )
        except _HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            e.close()
            if should_retry_on_status(e.code) and attempt < max_retries - 1:
                log.warning(
                    "Cloud %s HTTP %d (attempt %d/%d), retrying in %.1fs...",
                    provider_cfg.name, e.code, attempt + 1, max_retries,
                    exponential_backoff(attempt),
                )
                time.sleep(exponential_backoff(attempt))
                continue
            # Non-retryable HTTP error → surface to client
            log.error("Cloud %s returned HTTP %d: %s", provider_cfg.name, e.code, error_body[:200])
            status = e.code if 400 <= e.code < 500 else 502
            err_msg = f"Cloud provider error ({e.code}): {error_body[:500]}"
            send_json(handler, {"error": err_msg}, status)
            return CloudResult(
                status=status,
                error=err_msg,
                duration_ms=(time.monotonic() - start) * 1000,
            )
        except Exception as e:
            if attempt < max_retries - 1:
                log.warning(
                    "Cloud %s request failed (attempt %d/%d), retrying in %.1fs...",
                    provider_cfg.name, attempt + 1, max_retries,
                    exponential_backoff(attempt),
                )
                time.sleep(exponential_backoff(attempt))
                continue
            last_error = e

    # All retries exhausted
    log.error("Cloud %s request failed after %d attempts: %s",
              provider_cfg.name, max_retries, last_error)
    err_msg = f"Cloud provider unreachable: {last_error}"
    send_json(handler, {"error": err_msg}, 503)
    return CloudResult(
        status=503,
        error=err_msg,
        duration_ms=(time.monotonic() - start) * 1000,
    )


# ── Local forward with retry chain ──


def forward_anthropic_local(handler, pm, data, auth_header, model_obj, original_model):
    """CCR-style retry chain: local vLLM + exponential backoff → error on exhaustion.

    Returns the upstream HTTP status code once the response is sent to the
    client (200 → success; non-retryable error is surfaced to the client as
    502 by handle_json_response). Returns None when all retries are
    exhausted. The caller (ProxyHandler._forward_local) uses the return
    value to write a RequestLog for the usage statistics.
    """
    was_stream = data.get("stream", False)
    data["model"] = model_obj.served_name or "vllm_qwen27b"
    body = json.dumps(data).encode("utf-8")

    last_error = None
    for attempt in range(UPSTREAM_LOCAL_RETRIES + 1):
        conn = None
        try:
            conn = HTTPConnection("127.0.0.1", model_obj.port, timeout=300)
            conn.request("POST", "/v1/messages", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()

            if should_retry_on_status(resp.status) and attempt < UPSTREAM_LOCAL_RETRIES:
                try:
                    resp.read()
                except Exception:
                    pass
                resp.close()
                delay_s = exponential_backoff(attempt)
                log.warning("Local %s returned %d, retry #%d in %.1fs",
                            model_obj.name, resp.status, attempt, delay_s)
                time.sleep(delay_s)
                continue

            if was_stream:
                # G-1b: 旁路观察 Anthropic SSE 事件提取 usage（message_start 的
                # message.usage.input_tokens + message_delta 的 usage.output_tokens）
                # v6.1: 首 token 回调 — TTFT 记首个 content_block_delta 时刻
                # （message_start 是 metadata 前言，不算 token）
                sse_buf = SSELineBuffer(first_token_ttft_cb(handler))
                pipe_stream_response(handler, resp, sse_buf)
                handler._usage = dict(sse_buf.usage)
            else:
                handle_json_response(handler, resp, model_obj, original_model, data, auth_header,
                                     response_cache=getattr(pm, 'response_cache', None))
            return resp.status

        except (ConnectionRefusedError, ConnectionResetError, OSError, BrokenPipeError) as e:
            last_error = e
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            if attempt < UPSTREAM_LOCAL_RETRIES:
                delay_s = exponential_backoff(attempt)
                log.warning("Local %s connection failed (attempt %d/%d): %s — retry in %.1fs",
                            model_obj.name, attempt + 1, UPSTREAM_LOCAL_RETRIES + 1, e, delay_s)
                time.sleep(delay_s)
                continue
            log.error("Local %s failed after %d attempts: %s",
                       model_obj.name, UPSTREAM_LOCAL_RETRIES + 1, e)

        except Exception as e:
            log.error("Local %s unexpected error: %s", model_obj.name, e)
            break
        finally:
            # M1: 统一在此关闭 conn — 成功返回、可重试状态分支、异常路径都经过
            # 此 finally（此前成功/重试分支不关 socket，近似每请求泄漏一个连接）
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    log.error("Local model failed after all retries: %s", last_error)
    send_json(handler, {"error": f"Local model unreachable: {last_error}"}, 503)
    return None
