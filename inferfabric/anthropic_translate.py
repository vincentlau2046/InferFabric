"""
inferfabric/anthropic_translate.py — Anthropic ↔ OpenAI 单向翻译层（PLAN.md P1）。

方向：Anthropic 请求 → OpenAI 上游；OpenAI 响应/SSE → Anthropic 客户端。
用途：为没有原生 /v1/messages 的引擎（vLLM/MLX/…）提供 Anthropic 协议入口
（llama-server b12361 原生支持，走透传不经此层 —— anthropic_native: true）。

覆盖面（P1 范围，Claude Code 主路径优先）：
- messages：system 顶层块、text/image(base64) 内容块、tool_use / tool_result 往返
- tools：input_schema ↔ function.parameters，tool_choice 三态映射
- 流式：OpenAI chunk → Anthropic SSE 事件机（message_start / content_block_*
  / message_delta / message_stop），reasoning_content → thinking 块
- 不映射：prompt caching（上游 OpenAI 协议无对应语义）、metadata、服务端 tool
"""

from __future__ import annotations

import json
from typing import Any, Optional


# ─── 请求翻译：Anthropic → OpenAI ────────────────────────────────

def _system_to_message(system: Any) -> Optional[dict]:
    if not system:
        return None
    if isinstance(system, str):
        return {"role": "system", "content": system}
    texts = [b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"]
    return {"role": "system", "content": "\n".join(t for t in texts if t)} if texts else None


def _convert_messages(messages: list[dict]) -> list[dict]:
    """Anthropic messages → OpenAI messages。

    - text/image(base64) 块合并为同一条多模态消息（user）
    - tool_use 块 → assistant 消息的 tool_calls
    - tool_result 块 → 独立 role:tool 消息
    """
    out: list[dict] = []
    for m in messages or []:
        role, content = m.get("role", "user"), m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        texts: list[str] = []
        images: list[dict] = []
        tool_calls: list[dict] = []
        tool_results: list[dict] = []
        for b in content or []:
            t = b.get("type")
            if t == "text":
                texts.append(b.get("text", ""))
            elif t == "image":
                src = b.get("source", {})
                if src.get("type") == "base64":
                    images.append({"type": "image_url", "image_url": {
                        "url": f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"}})
            elif t == "tool_use":
                tool_calls.append({
                    "id": b.get("id", ""), "type": "function",
                    "function": {"name": b.get("name", ""),
                                 "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)}})
            elif t == "tool_result":
                tool_results.append(b)

        # 评审 PR#17 H2：输出序 = assistant(tool_calls) → tool 结果 →
        # user 文本。OpenAI/vLLM 契约要求 tool 紧跟 tool_calls；Anthropic
        # 源方向 tool_result 也在消息首（Claude Code 主线流常发
        # [tool_result, text]）——原先 text 先行会译出 user(text)→
        # tool(result) 的非法序列（上游 400）。
        if tool_calls:
            out.append({"role": "assistant", "content": None, "tool_calls": tool_calls})
        for tr in tool_results:
            inner = tr.get("content")
            if isinstance(inner, list):
                parts = []
                for b in inner:
                    if isinstance(b, dict) and b.get("type") == "text":
                        parts.append({"type": "text", "text": b.get("text", "")})
                    elif isinstance(b, dict) and b.get("type") == "image":
                        src = b.get("source", {})
                        if src.get("type") == "base64":
                            # image block 并入 tool content 数组，不静默丢
                            parts.append({"type": "image_url", "image_url": {
                                "url": f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"}})
                content = parts[0]["text"] if (
                    len(parts) == 1 and parts[0]["type"] == "text") else (parts or "")
            else:
                content = inner or ""
            if tr.get("is_error"):
                # is_error 透传：模型必须知道工具出错（原先静默丢弃）
                content = ("ERROR: " + content) if isinstance(content, str)                     else [{"type": "text", "text": "ERROR: "}] + content
            out.append({"role": "tool", "tool_call_id": tr.get("tool_use_id", ""),
                        "content": content})
        if texts or images:
            if images:
                parts = ([{"type": "text", "text": "\n".join(x for x in texts if x)}]
                         if texts else []) + images
                out.append({"role": role, "content": parts})
            else:
                out.append({"role": role, "content": "\n".join(x for x in texts if x)})
    return out


def _convert_tools(tools: list[dict]) -> list[dict]:
    out = []
    for t in tools or []:
        fn = {"name": t.get("name", ""), "parameters": t.get("input_schema", {"type": "object"})}
        if t.get("description"):
            fn["description"] = t["description"]
        out.append({"type": "function", "function": fn})
    return out


def _convert_tool_choice(tc: Any) -> Any:
    if not tc:
        return None
    if isinstance(tc, dict):
        t = tc.get("type")
        if t == "auto":
            return "auto"
        if t == "any":
            return "required"
        if t == "tool":
            return {"type": "function", "function": {"name": tc.get("name", "")}}
    return None


def anthropic_to_openai_request(body: dict) -> dict:
    """Anthropic /v1/messages 请求体 → OpenAI /v1/chat/completions 请求体。"""
    out: dict[str, Any] = {
        "model": body.get("model", ""),
        "max_tokens": body.get("max_tokens", 4096),
        "messages": [],
    }
    sysmsg = _system_to_message(body.get("system"))
    if sysmsg:
        out["messages"].append(sysmsg)
    out["messages"].extend(_convert_messages(body.get("messages", [])))
    if body.get("tools"):
        out["tools"] = _convert_tools(body["tools"])
    tchoice = _convert_tool_choice(body.get("tool_choice"))
    if tchoice:
        out["tool_choice"] = tchoice
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p")):
        if body.get(src) is not None:
            out[dst] = body[src]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    if body.get("stream"):
        out["stream"] = True
        out["stream_options"] = {"include_usage": True}
    return out


# ─── 响应翻译：OpenAI → Anthropic（非流式）──────────────────────

_STOP_REASON = {"stop": "end_turn", "length": "max_tokens",
                "tool_calls": "tool_use", "function_call": "tool_use"}


def openai_to_anthropic_response(resp: dict, model: str = "") -> dict:
    """OpenAI chat.completion 响应 → Anthropic message 响应。"""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content: list[dict] = []
    if msg.get("reasoning_content"):
        content.append({"type": "thinking", "thinking": msg["reasoning_content"]})
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": fn.get("arguments", "")}
        content.append({"type": "tool_use", "id": tc.get("id", ""),
                        "name": fn.get("name", ""), "input": args})
    usage = resp.get("usage") or {}
    return {
        "id": resp.get("id", "msg_unknown"),
        "type": "message",
        "role": "assistant",
        "model": model or resp.get("model", ""),
        "content": content,
        "stop_reason": _STOP_REASON.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)},
    }


# ─── 流式翻译：OpenAI chunk 流 → Anthropic SSE 事件流 ───────────

class SseToAnthropic:
    """有状态翻译器：逐个喂入 OpenAI chunk（dict），吐出 Anthropic SSE 事件。

    事件以 (event_name, data_dict) 元组列表返回；调用方序列化为
    `event: <name>\\ndata: <json>\\n\\n`。
    """

    def __init__(self, model: str = ""):
        self.model = model
        self._started = False
        self._block_index = 0
        self._open_block: Optional[tuple[int, str]] = None   # (index, kind)
        self._tool_blocks: dict[int, int] = {}               # upstream tool index → block index
        self._tool_names: dict[int, str] = {}
        self._finish_reason: Optional[str] = None
        self._usage: dict = {}
        self._finished = False

    # -- helpers --
    def _emit(self, name: str, data: dict):
        return (name, data)

    def _start_message(self):
        self._started = True
        return [self._emit("message_start", {"type": "message_start", "message": {
            "id": "msg_translated", "type": "message", "role": "assistant",
            "model": self.model, "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}})]

    def _open_content_block(self, kind: str, extra: dict | None = None):
        idx = self._block_index
        self._block_index += 1
        self._open_block = (idx, kind)
        body = {"type": kind, **(extra or {})}
        if kind == "text":
            body["text"] = ""
        elif kind == "thinking":
            body["thinking"] = ""
        return idx, self._emit("content_block_start", {
            "type": "content_block_start", "index": idx,
            "content_block": body})

    def _close_open_block(self):
        if self._open_block is None:
            return []
        idx, kind = self._open_block
        self._open_block = None
        return [self._emit("content_block_stop", {
            "type": "content_block_stop", "index": idx})]

    # -- main entry --
    def feed(self, chunk: dict) -> list[tuple[str, dict]]:
        if self._finished:
            return []
        events: list[tuple[str, dict]] = []
        if not self._started:
            events += self._start_message()
        if chunk.get("usage"):
            # 评审 PR#17 H1：max-not-sum（gateway 双 usage chunk 的坑）——
            # 重复/分片 usage 块按字段取最大值，绝不累加。
            for _k, _v in chunk["usage"].items():
                if isinstance(_v, (int, float)):
                    self._usage[_k] = max(self._usage.get(_k, 0), _v)
        choices = chunk.get("choices") or []
        delta = (choices[0].get("delta") or {}) if choices else {}

        # reasoning_content → thinking 块
        if delta.get("reasoning_content"):
            if not (self._open_block and self._open_block[1] == "thinking"):
                events += self._close_open_block()
                idx, ev = self._open_content_block("thinking")
                events.append(ev)
            events.append(self._emit("content_block_delta", {
                "type": "content_block_delta", "index": self._open_block[0],
                "delta": {"type": "thinking_delta", "thinking": delta["reasoning_content"]}}))

        # text 增量
        if delta.get("content"):
            if not (self._open_block and self._open_block[1] == "text"):
                events += self._close_open_block()
                idx, ev = self._open_content_block("text")
                events.append(ev)
            events.append(self._emit("content_block_delta", {
                "type": "content_block_delta", "index": self._open_block[0],
                "delta": {"type": "text_delta", "text": delta["content"]}}))

        # tool_calls 增量（按上游 index 聚合）
        for tc in delta.get("tool_calls") or []:
            up_idx = tc.get("index", 0)
            if up_idx not in self._tool_blocks:
                events += self._close_open_block()
                idx, ev = self._open_content_block(
                    "tool_use", {"id": tc.get("id", ""), "name": tc.get("function", {}).get("name", ""),
                                 "input": {}})
                events.append(ev)
                self._tool_blocks[up_idx] = idx
                self._tool_names[up_idx] = tc.get("function", {}).get("name", "")
            args = tc.get("function", {}).get("arguments")
            if args:
                events.append(self._emit("content_block_delta", {
                    "type": "content_block_delta", "index": self._tool_blocks[up_idx],
                    "delta": {"type": "input_json_delta", "partial_json": args}}))

        if choices and choices[0].get("finish_reason"):
            self._finish_reason = choices[0]["finish_reason"]
        return events

    def finish(self) -> list[tuple[str, dict]]:
        """上游流结束后调用：闭合块 + message_delta + message_stop。"""
        if self._finished:
            return []
        self._finished = True
        events = self._close_open_block()
        out_usage = {"input_tokens": self._usage.get("prompt_tokens", 0),
                     "output_tokens": self._usage.get("completion_tokens", 0)}
        events.append(self._emit("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": _STOP_REASON.get(self._finish_reason, "end_turn"),
                      "stop_sequence": None},
            # 评审 PR#17 H1：input_tokens 一并带出——上游 chat 流的 usage
            # 块到流尾才出现，message_start 时恒不可知；message_delta 是
            # 唯一能携带真实 input_tokens 的 Anthropic 事件。
            "usage": {"input_tokens": out_usage["input_tokens"],
                      "output_tokens": out_usage["output_tokens"]}}))
        events.append(self._emit("message_stop", {"type": "message_stop"}))
        return events


def sse_event_bytes(event: tuple[str, dict]) -> bytes:
    """(event_name, data) → SSE 帧字节。"""
    name, data = event
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
