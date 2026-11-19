"""
inferfabric/proxy/anthropic_bridge.py — Anthropic 入口 → OpenAI 上游桥接。

供 handler._forward_local 在 model_obj.anthropic_native=False 时调用：
请求体经 anthropic_translate 转为 OpenAI 格式打向上游 /v1/chat/completions，
响应（JSON 或 SSE）转回 Anthropic 协议写回客户端。
不改动 forwarder 转发核心；本模块自成一条旁路。
"""

from __future__ import annotations

import json
import logging
import time
from http.client import HTTPConnection

from inferfabric.anthropic_translate import (
    SseToAnthropic,
    anthropic_to_openai_request,
    openai_to_anthropic_response,
    sse_event_bytes,
)

log = logging.getLogger("inferfabric")


def _post_openai(port: int, body: dict, timeout: int = 600) -> HTTPConnection:
    conn = HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("POST", "/v1/chat/completions",
                 body=json.dumps(body).encode("utf-8"),
                 headers={"Content-Type": "application/json",
                          "Content-Length": str(len(json.dumps(body)))})
    return conn


def forward_anthropic_via_openai(handler, pm, data, auth_header,
                                 model_obj, original_model) -> int:
    """返回 HTTP 状态码（供 RequestLog），响应已写回 handler。"""
    port = model_obj.port
    openai_body = anthropic_to_openai_request(data)
    streaming = bool(openai_body.get("stream"))
    try:
        conn = _post_openai(port, openai_body)
        resp = conn.getresponse()
    except Exception as e:
        log.warning("anthropic_bridge upstream connect failed (port %s): %s", port, e)
        handler._send_json({"type": "error",
                            "error": {"type": "api_error",
                                      "message": f"upstream unreachable: {e}"}}, 502)
        return 502

    if resp.status != 200:
        err_body = resp.read()[:2000]
        log.warning("anthropic_bridge upstream %s: %s", resp.status, err_body[:200])
        handler._send_json({"type": "error",
                            "error": {"type": "api_error",
                                      "message": f"upstream {resp.status}: {err_body[:500]!r}"}},
                           resp.status)
        resp.close()
        return resp.status

    if not streaming:
        raw = resp.read()
        resp.close()
        try:
            oai = json.loads(raw)
        except json.JSONDecodeError:
            handler._send_json({"type": "error",
                                "error": {"type": "api_error", "message": "bad upstream JSON"}}, 502)
            return 502
        anth = openai_to_anthropic_response(oai, model=original_model or data.get("model", ""))
        handler._send_json(anth, 200)
        return 200

    # 流式：OpenAI SSE → Anthropic SSE，chunked 写回
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()

    def write_frame(b: bytes):
        handler.wfile.write(f"{len(b):x}\r\n".encode("ascii"))
        handler.wfile.write(b)
        handler.wfile.write(b"\r\n")
        handler.wfile.flush()

    translator = SseToAnthropic(model=original_model or data.get("model", ""))
    buf = b""
    try:
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                for line in frame.split(b"\n"):
                    if not line.startswith(b"data: "):
                        continue
                    payload = line[len(b"data: "):].strip()
                    if payload in (b"", b"[DONE]"):
                        continue
                    try:
                        oai_chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    for ev in translator.feed(oai_chunk):
                        write_frame(sse_event_bytes(ev))
        for ev in translator.finish():
            write_frame(sse_event_bytes(ev))
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()
        return 200
    except (BrokenPipeError, ConnectionResetError):
        log.info("anthropic_bridge: client disconnected during stream")
        return 200
    finally:
        resp.close()
