"""MacFabric P1 单测 — Anthropic↔OpenAI 翻译层 + golden replay。

golden 文件来自真实 llama-server b12361（Metal）双协议录制：
- anthropic_plain.json / anthropic_stream.sse：native /v1/messages 透传
- openai_plain.json / openai_stream.sse：/v1/chat/completions
测试断言翻译层对同一轮对话产出与 native 语义一致的结构。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inferfabric.anthropic_translate import (
    SseToAnthropic,
    anthropic_to_openai_request,
    openai_to_anthropic_response,
    sse_event_bytes,
)

GOLDEN = Path(__file__).resolve().parents[1] / "golden"


def load_golden(name):
    return (GOLDEN / name).read_text()


# ── 请求翻译 ─────────────────────────────────────────────────────

def test_request_system_string():
    body = {"model": "m", "max_tokens": 100, "system": "你是一个路由器",
            "messages": [{"role": "user", "content": "hi"}]}
    out = anthropic_to_openai_request(body)
    assert out["messages"][0] == {"role": "system", "content": "你是一个路由器"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}
    assert out["max_tokens"] == 100


def test_request_system_blocks():
    body = {"model": "m", "max_tokens": 10,
            "system": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}],
            "messages": []}
    out = anthropic_to_openai_request(body)
    assert out["messages"][0]["content"] == "A\nB"


def test_request_claude_code_style_tools():
    """Claude Code 主路径：system + tools + 多轮含 tool_use/tool_result。"""
    body = {
        "model": "m", "max_tokens": 1024,
        "system": "You are Claude Code",
        "tools": [{"name": "read_file", "description": "读文件",
                   "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                                    "required": ["path"]}}],
        "tool_choice": {"type": "auto"},
        "messages": [
            {"role": "user", "content": "读一下 /etc/hosts"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "read_file",
                 "input": {"path": "/etc/hosts"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1",
                 "content": "127.0.0.1 localhost"}]},
        ],
    }
    out = anthropic_to_openai_request(body)
    assert out["tools"][0]["function"]["name"] == "read_file"
    assert out["tool_choice"] == "auto"
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert out["messages"][2]["tool_calls"][0]["id"] == "toolu_1"
    assert json.loads(out["messages"][2]["tool_calls"][0]["function"]["arguments"]) == {"path": "/etc/hosts"}
    assert out["messages"][3]["tool_call_id"] == "toolu_1"
    assert "localhost" in out["messages"][3]["content"]


def test_request_image_base64():
    body = {"model": "m", "max_tokens": 10, "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png", "data": "aGk="}}]}]}
    out = anthropic_to_openai_request(body)
    msg = out["messages"][0]
    assert msg["content"][0]["type"] == "text"
    assert msg["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_request_stop_sequences_and_stream():
    body = {"model": "m", "max_tokens": 10, "stream": True,
            "stop_sequences": ["END"], "temperature": 0.5,
            "messages": [{"role": "user", "content": "x"}]}
    out = anthropic_to_openai_request(body)
    assert out["stop"] == ["END"]
    assert out["stream"] is True
    assert out["stream_options"] == {"include_usage": True}
    assert out["temperature"] == 0.5


# ── 响应翻译（非流式）────────────────────────────────────────────

def test_response_text_mapping():
    oai = {"id": "c1", "model": "m", "choices": [{"finish_reason": "stop",
        "message": {"role": "assistant", "content": "秋 高 爽"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7}}
    out = openai_to_anthropic_response(oai, model="qwen3-4b")
    assert out["type"] == "message" and out["role"] == "assistant"
    assert out["content"] == [{"type": "text", "text": "秋 高 爽"}]
    assert out["stop_reason"] == "end_turn"
    assert out["usage"] == {"input_tokens": 5, "output_tokens": 7}
    assert out["model"] == "qwen3-4b"


def test_response_tool_calls_mapping():
    oai = {"id": "c2", "model": "m", "choices": [{"finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "/etc/hosts"}'}}]}},
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 4}}
    out = openai_to_anthropic_response(oai)
    assert out["stop_reason"] == "tool_use"
    assert out["content"][0] == {"type": "tool_use", "id": "call_1",
                                 "name": "read_file", "input": {"path": "/etc/hosts"}}


def test_response_reasoning_to_thinking():
    oai = {"id": "c3", "model": "m", "choices": [{"finish_reason": "stop",
        "message": {"role": "assistant", "content": "答案",
                    "reasoning_content": "思考过程"}}],
        "usage": {}}
    out = openai_to_anthropic_response(oai)
    assert out["content"][0] == {"type": "thinking", "thinking": "思考过程"}
    assert out["content"][1] == {"type": "text", "text": "答案"}


# ── 流式翻译 ─────────────────────────────────────────────────────

def _parse_sse(text):
    """SSE 文本 → [(event, data_dict)]。"""
    out = []
    for block in text.split("\n\n"):
        ev, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if ev and data:
            out.append((ev, json.loads(data)))
    return out


def _parse_openai_sse(text):
    chunks = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                payload = line[6:].strip()
                if payload and payload != "[DONE]":
                    chunks.append(json.loads(payload))
    return chunks


def test_stream_event_sequence_shape():
    """OpenAI 流 → Anthropic 事件序列的骨架正确（start→delta→stop）。"""
    chunks = _parse_openai_sse(load_golden("openai_stream.sse"))
    tr = SseToAnthropic(model="qwen3-4b")
    events = []
    for c in chunks:
        events += tr.feed(c)
    events += tr.finish()
    names = [e for e, _ in events]
    assert names[0] == "message_start"
    assert names[-1] == "message_stop"
    assert "message_delta" in names
    assert names.count("content_block_start") == names.count("content_block_stop")
    # 有实际文本增量
    deltas = [d for n, d in events if n == "content_block_delta"
              and d["delta"]["type"] == "thinking_delta"]
    assert deltas, "thinking 模型应有 thinking 增量"
    # SSE 序列化往返
    frame = sse_event_bytes(events[0])
    assert frame.startswith(b"event: message_start\ndata: ")


def test_stream_golden_event_names_match_native():
    """Golden replay：翻译层事件序列与 native /v1/messages 骨架一致。"""
    native = [n for n, _ in _parse_sse(load_golden("anthropic_stream.sse"))]
    chunks = _parse_openai_sse(load_golden("openai_stream.sse"))
    tr = SseToAnthropic(model="qwen3-4b")
    translated = []
    for c in chunks:
        translated += tr.feed(c)
    translated += tr.finish()
    t_names = [n for n, _ in translated]
    # 骨架：start 在头，stop 在尾，start/stop 成对
    assert t_names[0] == native[0] == "message_start"
    assert t_names[-1] == native[-1] == "message_stop"
    assert t_names.count("content_block_start") == native.count("content_block_start")


def test_stream_multi_block_interleaving():
    """thinking → text 切换时正确闭合/开块。"""
    tr = SseToAnthropic(model="m")
    evs = []
    evs += tr.feed({"choices": [{"delta": {"reasoning_content": "想"}}]})
    evs += tr.feed({"choices": [{"delta": {"content": "答"}}]})
    evs += tr.feed({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    evs += tr.finish()
    names = [n for n, _ in evs]
    # thinking 块开闭 + text 块开闭 = 2 对
    assert names.count("content_block_start") == 2
    assert names.count("content_block_stop") == 2
    # thinking_delta 在 text_delta 之前
    kinds = [d["delta"]["type"] for n, d in evs if n == "content_block_delta"]
    assert kinds.index("thinking_delta") < kinds.index("text_delta")


def test_stream_tool_call_accumulation():
    """工具调用参数分片到达时聚合为一个 tool_use 块 + input_json_delta。"""
    tr = SseToAnthropic(model="m")
    evs = []
    evs += tr.feed({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call_1", "type": "function",
         "function": {"name": "read_file", "arguments": ""}}]}}]})
    evs += tr.feed({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": '{"path"'}}]}}]})
    evs += tr.feed({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": ': "/x"}'}}]}}]})
    evs += tr.feed({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
    evs += tr.finish()
    starts = [d for n, d in evs if n == "content_block_start"
              and d["content_block"]["type"] == "tool_use"]
    assert len(starts) == 1 and starts[0]["content_block"]["name"] == "read_file"
    json_parts = "".join(d["delta"]["partial_json"] for n, d in evs
                         if n == "content_block_delta"
                         and d["delta"]["type"] == "input_json_delta")
    assert json.loads(json_parts) == {"path": "/x"}
    stop = [d for n, d in evs if n == "message_delta"][0]
    assert stop["delta"]["stop_reason"] == "tool_use"

# ── 评审 PR#17 review fixes：H1/H2 ──

def test_tool_result_precedes_text_translated_in_contract_order():
    """H2：[tool_result, text]（Claude Code 主线流）必须译成
    assistant(tool_calls) → tool → user(text)，不得 user(text) 先行。"""
    body = {"model": "m", "max_tokens": 16, "messages": [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "get_weather",
             "input": {"city": "SF"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "sunny"},
            {"type": "text", "text": "summarize please"}]},
    ]}
    out = anthropic_to_openai_request(body)["messages"]
    roles = [m["role"] for m in out]
    assert roles == ["assistant", "tool", "user"], roles
    assert out[1]["tool_call_id"] == "t1"
    assert out[2]["content"] == "summarize please"


def test_tool_result_is_error_and_image_passthrough():
    """H2：is_error 映射进 content（ERROR: 前缀）；tool_result 内 image
    block 并入 content 数组，不静默丢。"""
    body = {"model": "m", "max_tokens": 16, "messages": [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": [{"type": "image", "source": {
                 "type": "base64", "media_type": "image/png", "data": "QUJD"}}]}]},
    ]}
    out = anthropic_to_openai_request(body)["messages"]
    tool = out[0]
    assert tool["role"] == "tool"
    assert isinstance(tool["content"], list)
    kinds = [p["type"] for p in tool["content"]]
    assert "image_url" in kinds, "image block must survive"
    text_part = next(p for p in tool["content"] if p["type"] == "text")
    assert text_part["text"].startswith("ERROR:")


def test_stream_usage_max_not_sum():
    """H1：重复/分片 usage 块 max-not-sum（gateway 双 usage chunk 的坑）。"""
    tr = SseToAnthropic(model="m")
    tr.feed({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    tr.feed({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 9}})
    assert tr._usage["prompt_tokens"] == 10
    assert tr._usage["completion_tokens"] == 9


def test_stream_finish_carries_input_tokens():
    """H1：桥接 Anthropic 流的 message_delta 必须带 input_tokens（此前
    永无 input tokens，成本归因全错）。"""
    tr = SseToAnthropic(model="m")
    tr.feed({"choices": [{"delta": {"role": "assistant", "content": "hi"}}]})
    evs = tr.finish()
    delta = next(d for n, d in evs if n == "message_delta")
    # usage 块在流尾（finish 前 feed）：finish() 前已合并进 _usage
    assert delta["usage"]["output_tokens"] >= 0
    # usage 在最后一条 chat chunk 里到达的真实时序
    tr2 = SseToAnthropic(model="m")
    tr2.feed({"choices": [{"delta": {"role": "assistant", "content": "hi"}}]})
    tr2.feed({"choices": [], "usage": {"prompt_tokens": 42, "completion_tokens": 3}})
    delta2 = next(d for n, d in tr2.finish() if n == "message_delta")
    assert delta2["usage"]["input_tokens"] == 42
    assert delta2["usage"]["output_tokens"] == 3
