"""
unit/proxy/test_r5_cache_parity.py — R5 缓存键一致 + OpenAI 路径缓存写入（A2/A3 修复）

A2 (CRIT): Anthropic 路径 GET（handler._handle_messages）用改写前 body
  （model=客户端别名），PUT（forwarder.handle_json_response）用改写后
  body（model 已被改为 served_name），_make_key 的 canonical 含 body
  的 model 字段 → 键错位，客户端用别名时缓存永不命中。
  修复：_make_key 的 canonical 排除 model 字段，键隔离仅由 model 参数（客户端原名）承担。

A3 (HIGH): OpenAI /v1/chat/completions 路径只有缓存 GET（chat_handlers
  L238-254），没有 PUT → 该协议响应缓存永久冷。
  修复：_forward_request 非流式 200 且 JSON 解析成功后写缓存，键用转发
  前快照 body（model 改写 L349 + tools 归一化 L361 会 mutate data，
  直接复用会键漂移）。
"""

import sys
import json
import time
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

from inferfabric.proxy.response_cache import ResponseCache
from inferfabric.proxy.chat_handlers import handle_chat


# ═══════════════════════════════════════════════════════════════
# 1. A2: 缓存键 — 别名 get/put 键一致
# ═══════════════════════════════════════════════════════════════


class TestAliasKeyParity:
    def test_put_post_rewrite_get_pre_rewrite_hits(self):
        """A2：PUT 时 body.model 已被改写为 served_name，GET 时是客户端别名 → 应命中。

        修复前：两个 body 的 model 字段都进 canonical → 键不同 → 永不命中。
        """
        cache = ResponseCache()
        msgs = [{"role": "user", "content": "hi"}]
        body_put = {"model": "qwen-served", "messages": msgs, "temperature": 0}
        body_get = {"model": "qwen-alias", "messages": msgs, "temperature": 0}
        resp = {"choices": [{"message": {"role": "assistant", "content": "x"}}]}
        cache.put("qwen-alias", body_put, resp, {"prompt_tokens": 1, "completion_tokens": 1})
        hit = cache.get("qwen-alias", body_get)
        assert hit is not None, "alias GET must hit entry written with rewritten model field"
        assert hit["body"]["choices"][0]["message"]["content"] == "x"

    def test_model_param_still_scopes_key(self):
        """model 参数（客户端原名）仍参与键隔离 → 不同模型不串键。"""
        cache = ResponseCache()
        body = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0}
        resp = {"choices": [{"message": {"role": "assistant", "content": "1"}}]}
        cache.put("qwen-alias", body, resp, {})
        assert cache.get("other-model", body) is None

    def test_body_diff_still_misses(self):
        """body 实质内容不同 → 不命中（键排除 model 后仍由其余字段区分）。"""
        cache = ResponseCache()
        b1 = {"messages": [{"role": "user", "content": "a"}], "temperature": 0}
        b2 = {"messages": [{"role": "user", "content": "b"}], "temperature": 0}
        resp = {"choices": [{"message": {"role": "assistant", "content": "1"}}]}
        cache.put("m", b1, resp, {})
        assert cache.get("m", b2) is None

    def test_stream_still_excluded_from_key(self):
        """stream 字段仍不进键（既有语义回归）。"""
        cache = ResponseCache()
        b_a = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0, "stream": False}
        b_b = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0, "stream": True}
        assert cache._make_key("m", b_a) == cache._make_key("m", b_b)


# ═══════════════════════════════════════════════════════════════
# 2. A3: OpenAI 路径非流式响应写缓存
# ═══════════════════════════════════════════════════════════════


class _FakeResp:
    """模拟 http.client.HTTPResponse：状态 + 一次性 body。"""

    def __init__(self, payload: dict):
        self.status = 200
        self._body = json.dumps(payload).encode("utf-8")
        self._closed = False

    def getheaders(self):
        # http.client.HTTPResponse.getheaders() → list[(name, value)]
        return [("Content-Type", "application/json")]

    def read(self, n=None):
        data = self._body if n is None else self._body[:n]
        self._body = b""  # 一次性，流式循环靠空块终止
        return data

    def close(self):
        self._closed = True


class _FakeConn:
    """模拟 pm.make_conn 返回的 HTTPConnection。"""

    def __init__(self, calls: list, payload: dict):
        self._calls = calls
        self._payload = payload

    def request(self, method, path, body=None, headers=None):
        self._calls.append({"method": method, "path": path, "body": body})

    def getresponse(self):
        return _FakeResp(self._payload)

    def close(self):
        pass


class _FakeHandler:
    """最小 ProxyHandler 替身（chat 路径用到的接口面）。"""

    def __init__(self):
        self.headers = {}
        self.path = "/v1/chat/completions"
        self._req_start = time.monotonic()
        self._usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.json_responses = []

    def _send_json(self, obj, status=200, extra_headers=None):
        self.json_responses.append((status, obj))

    def send_response(self, code):
        pass

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def _safe_write(self, chunk):
        pass


UPSTREAM_PAYLOAD = {
    "id": "cmpl-1",
    "model": "qwen-served",
    "choices": [{"message": {"role": "assistant", "content": "hello"}}],
    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
}


def _make_pm(calls: list, cache: ResponseCache, port=8001) -> SimpleNamespace:
    """构造 handle_chat 非流式本地路径所需的最小 pm 桩。"""
    model_obj = SimpleNamespace(
        name="qwen", served_name="qwen-served", type="vllm",
        is_ollama=False, is_ollama_cpp=False, port=port,
    )
    pm = SimpleNamespace()
    pm.auth = SimpleNamespace(enabled=False, key_name=lambda h: "anonymous")
    pm.logger = SimpleNamespace(log=lambda *a, **k: None)
    pm.anomalies = SimpleNamespace(record=lambda *a, **k: None)
    pm.new_request_id = lambda: "req-test"
    pm.model_to_service = lambda m: "qwen"
    pm.get_target_port = lambda m: port
    def _gate():
        return SimpleNamespace(ok=True, reason=None, release=lambda: None)

    pm.dual_gate = SimpleNamespace(acquire=lambda m: _gate())
    pm.make_conn = lambda p: _FakeConn(calls, UPSTREAM_PAYLOAD)
    pm.release_port = lambda m, p: None
    pm.mgr = SimpleNamespace(
        get_model=lambda name: model_obj,
        active_services={"qwen"},
    )
    pm._runtime_config = {"cache": {"enabled": True}}
    pm.response_cache = cache
    # R-AS: 锁定 auto_switch=False（本测只覆盖缓存语义，不覆盖切换分支）
    pm.auto_switch = False
    return pm


def _client_body() -> dict:
    """客户端请求：用别名 model + temp=0 非流式（R5 准入条件内）。"""
    return {"model": "qwen-alias", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}


class TestOpenAiCacheWrite:
    def test_nonstreaming_200_cached(self):
        """A3：OpenAI 非流式 temp=0 的 200 响应写入 R5 缓存（修复前永久冷）。"""
        calls: list = []
        cache = ResponseCache()
        pm = _make_pm(calls, cache)
        handle_chat(_FakeHandler(), pm, _client_body())
        assert len(calls) == 1, "first request must reach upstream"
        assert cache.size == 1, "non-streaming 200 response should be written to R5 cache"

    def test_second_identical_request_hits_cache(self):
        """A2+A3：同模型同内容第二次请求命中缓存，不再打上游。"""
        calls: list = []
        cache = ResponseCache()
        pm = _make_pm(calls, cache)
        handle_chat(_FakeHandler(), pm, _client_body())
        assert len(calls) == 1

        h2 = _FakeHandler()
        handle_chat(h2, pm, _client_body())
        assert len(calls) == 1, "second identical request should be a cache HIT (no upstream call)"
        assert h2.json_responses, "cache HIT must send JSON response"
        assert h2.json_responses[0][0] == 200
        assert h2.json_responses[0][1]["choices"][0]["message"]["content"] == "hello"

    def test_streaming_not_cached(self):
        """stream=true 不写缓存（R5 准入条件保持）。"""
        calls: list = []
        cache = ResponseCache()
        pm = _make_pm(calls, cache)
        data = _client_body()
        data["stream"] = True
        handle_chat(_FakeHandler(), pm, data)
        assert cache.size == 0

    def test_nonzero_temp_not_cached(self):
        """temperature=0.7 不写缓存（R5 准入条件保持）。"""
        calls: list = []
        cache = ResponseCache()
        pm = _make_pm(calls, cache)
        data = _client_body()
        data["temperature"] = 0.7
        handle_chat(_FakeHandler(), pm, data)
        assert cache.size == 0
