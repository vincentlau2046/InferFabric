"""
unit/proxy/test_r5_response_cache.py — R5: 精确匹配响应缓存

测试对象:
  - inferfabric.proxy.response_cache.ResponseCache
  - cachetools.LRUCache 集成

覆盖范围:
  - 缓存键一致：相同 model + body → 命中
  - 缓存键区分：不同 model → 不命中
  - 缓存键区分：不同 body → 不命中
  - stream=true → 不缓存
  - temperature=0.7 → 不缓存（allow_nonzero_temp=False）
  - 单条超标 → 不缓存
  - clear 清空
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# 添加 _deps 路径（cachetools 等额外依赖）
# 4 级 parent = 仓库根（tests/unit/proxy/ → 根）；旧代码多走一级指向
# 仓库父目录 → is_dir() 恒 False，_deps 永不生效（靠别的测试文件
# 先加 sys.path 才碰巧能跑，单独运行此文件即 ModuleNotFoundError）。
_deps = Path(__file__).parent.parent.parent.parent / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import pytest
from inferfabric.proxy.response_cache import ResponseCache


def _openai_resp(content: str, tool_calls: list = None) -> dict:
    """OpenAI chat completion 形响应体（R5 实际缓存的两种形状之一）。"""
    msg = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {"id": "cmpl-test", "choices": [{"message": msg}]}


# ═══════════════════════════════════════════════════════════════
# 1. 缓存键 & 命中
# ═══════════════════════════════════════════════════════════════


def test_cache_hit():
    """相同 model + body → 命中。"""
    cache = ResponseCache(maxsize=100)
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    cache.put("m", body, _openai_resp("hello"), {"prompt_tokens": 5, "completion_tokens": 3})
    hit = cache.get("m", body)
    assert hit is not None
    assert hit["body"]["choices"][0]["message"]["content"] == "hello"
    assert hit["usage"]["prompt_tokens"] == 5


def test_cache_miss_different_model():
    """不同 model → 不命中。"""
    cache = ResponseCache()
    body = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    cache.put("model-a", body, _openai_resp("a"), {})
    hit = cache.get("model-b", body)
    assert hit is None


def test_cache_miss_different_body():
    """不同 body → 不命中。"""
    cache = ResponseCache()
    cache.put("m", {"messages": [{"role": "user", "content": "hello"}], "temperature": 0},
              _openai_resp("hello"), {})
    hit = cache.get("m", {"messages": [{"role": "user", "content": "world"}], "temperature": 0})
    assert hit is None


def test_cache_stream_excluded_from_key():
    """stream 字段不影响缓存键：stream=true 和 stream=false 应命中同一缓存。"""
    cache = ResponseCache()
    # 存入 stream=false
    body_a = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0, "stream": False}
    cache.put("m", body_a, {"r": "same"}, {})
    # 用 stream=true 查询
    body_b = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0, "stream": True}
    # 但 stream=true 不在准入条件内，通过手动 get 测试键一致性
    key_a = cache._make_key("m", body_a)
    key_b = cache._make_key("m", body_b)
    assert key_a == key_b


# ═══════════════════════════════════════════════════════════════
# 2. 准入条件
# ═══════════════════════════════════════════════════════════════


def test_stream_not_cached():
    """stream=true → should_cache=False。"""
    assert not ResponseCache._should_cache({"stream": True, "temperature": 0}, 200)


def test_temperature_nonzero_not_cached():
    """temperature=0.7 → should_cache=False。"""
    assert not ResponseCache._should_cache({"temperature": 0.7}, 200)


def test_temperature_zero_cached():
    """temperature=0 → should_cache=True。"""
    assert ResponseCache._should_cache({"temperature": 0}, 200)


def test_temperature_none_cached():
    """temperature=None → should_cache=True（默认 0）。"""
    assert ResponseCache._should_cache({"temperature": None}, 200)


def test_non_200_not_cached():
    """status=500 → should_cache=False。"""
    assert not ResponseCache._should_cache({"temperature": 0}, 500)


# ═══════════════════════════════════════════════════════════════
# 3. 字节上限 & LRU 淘汰
# ═══════════════════════════════════════════════════════════════


def test_oversized_not_cached():
    """单条超过 max_body_bytes → 不缓存。"""
    cache = ResponseCache(maxsize=10, max_body_bytes=50)
    body = {"messages": [{"role": "user", "content": "x"}], "temperature": 0}
    huge = _openai_resp("x" * 500)
    cache.put("m", body, huge, {})
    assert cache.size == 0


def test_lru_eviction():
    """超过 maxsize 时淘汰最旧条目。"""
    cache = ResponseCache(maxsize=3)
    for i in range(4):
        body = {"messages": [{"role": "user", "content": str(i)}], "temperature": 0}
        cache.put("m", body, _openai_resp(str(i)), {})
    assert cache.size == 3
    # 第一条应该被淘汰
    hit = cache.get("m", {"messages": [{"role": "user", "content": "0"}], "temperature": 0})
    assert hit is None, "oldest entry should be evicted"


def test_lru_access_refreshes():
    """被访问的条目移到末尾，不被 LRU 淘汰。"""
    cache = ResponseCache(maxsize=3)
    for i in range(3):
        body = {"messages": [{"role": "user", "content": str(i)}], "temperature": 0}
        cache.put("m", body, _openai_resp(str(i)), {})

    # cache = [0, 1, 2]
    # 访问 0 → 移到末尾 → [1, 2, 0]
    body0 = {"messages": [{"role": "user", "content": "0"}], "temperature": 0}
    cache.get("m", body0)

    # 再 put 一条 → 淘汰最旧的 1 → [2, 0, 3]
    body3 = {"messages": [{"role": "user", "content": "3"}], "temperature": 0}
    cache.put("m", body3, _openai_resp("3"), {})

    assert cache.get("m", body0) is not None, "accessed entry 0 should survive"
    assert cache.get("m", {"messages": [{"role": "user", "content": "1"}], "temperature": 0}) is None, "oldest 1 evicted"
    assert cache.get("m", {"messages": [{"role": "user", "content": "2"}], "temperature": 0}) is not None


# ═══════════════════════════════════════════════════════════════
# 4. 管理
# ═══════════════════════════════════════════════════════════════


def test_clear():
    """clear 清空所有条目。"""
    cache = ResponseCache()
    body = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    cache.put("m", body, _openai_resp("x"), {})
    assert cache.size == 1
    cache.clear()
    assert cache.size == 0
    assert cache.get("m", body) is None


# ═══════════════════════════════════════════════════════════════
# 5. stats() — 命中计数（网关控制卡 LRU 生效可见性，方案 A）
# ═══════════════════════════════════════════════════════════════


def test_stats_initial():
    """新缓存：hits=0 / size=0 / max=构造上限。"""
    cache = ResponseCache(maxsize=100)
    assert cache.stats() == {"hits": 0, "size": 0, "max": 100}


def test_stats_counts_hits_only():
    """get 命中才累计 hits；miss / put 不累计；size 随条目数变化。"""
    cache = ResponseCache(maxsize=50)
    body = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    cache.put("m", body, _openai_resp("1"), {})
    assert cache.get("m", body) is not None          # hit 1
    assert cache.get("m", {"messages": []}) is None  # miss（键不同）
    assert cache.stats() == {"hits": 1, "size": 1, "max": 50}


def test_stats_clear_keeps_cumulative_hits():
    """clear 清条目但 hits 是累计计数器（与 uptime 同类），不归零。"""
    cache = ResponseCache(maxsize=50)
    body = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    cache.put("m", body, _openai_resp("1"), {})
    cache.get("m", body)
    cache.clear()
    s = cache.stats()
    assert s["size"] == 0
    assert s["hits"] == 1, "hits 是累计值，clear 不应重置"


# ═══════════════════════════════════════════════════════════════
# 6. 空内容排除（PUT 侧判据 + HIT 加固 replayed-empty）
# ═══════════════════════════════════════════════════════════════

_REQ = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0}


class TestEmptyContentExclusion:
    def test_empty_content_not_cached(self):
        """content="" → 不写缓存。"""
        cache = ResponseCache()
        cache.put("m", _REQ, _openai_resp(""), {})
        assert cache.size == 0

    def test_whitespace_content_not_cached(self):
        """content 纯空白 → 不写缓存。"""
        cache = ResponseCache()
        cache.put("m", _REQ, _openai_resp("   \n"), {})
        assert cache.size == 0

    def test_pure_thinking_not_cached(self):
        """content 空而 reasoning_content 有内容（纯 thinking）→ 不写缓存。"""
        cache = ResponseCache()
        msg = {"role": "assistant", "content": "", "reasoning_content": "让我想想…"}
        cache.put("m", _REQ, {"choices": [{"message": msg}]}, {})
        assert cache.size == 0

    def test_tool_calls_cached_despite_empty_content(self):
        """content 空但 tool_calls 在场 → 有实质内容，写缓存。"""
        cache = ResponseCache()
        tc = [{"id": "call_1", "type": "function",
              "function": {"name": "get_weather", "arguments": "{}"}}]
        cache.put("m", _REQ, _openai_resp("", tool_calls=tc), {})
        assert cache.size == 1
        hit = cache.get("m", _REQ)
        assert hit is not None
        assert hit["body"]["choices"][0]["message"]["tool_calls"] == tc

    def test_nonempty_content_still_cached(self):
        """回归：正常 content 仍写缓存（排除判据不误伤）。"""
        cache = ResponseCache()
        cache.put("m", _REQ, _openai_resp("hello"), {})
        assert cache.size == 1

    def test_unknown_shape_not_cached(self):
        """无法识别的响应形状（非 OpenAI/Anthropic）→ 保守不缓存。"""
        cache = ResponseCache()
        cache.put("m", _REQ, {"foo": "bar"}, {})
        assert cache.size == 0

    # ── Anthropic 形 content blocks ──

    def test_anthropic_thinking_only_not_cached(self):
        """仅 thinking block → 不写缓存。"""
        cache = ResponseCache()
        resp = {"content": [{"type": "thinking", "thinking": "…"}]}
        cache.put("m", _REQ, resp, {})
        assert cache.size == 0

    def test_anthropic_text_cached(self):
        """text block（非空）→ 写缓存。"""
        cache = ResponseCache()
        resp = {"content": [{"type": "text", "text": "hi"}]}
        cache.put("m", _REQ, resp, {})
        assert cache.size == 1

    def test_anthropic_thinking_plus_text_cached(self):
        """thinking + text → 有实质内容，写缓存。"""
        cache = ResponseCache()
        resp = {"content": [{"type": "thinking", "thinking": "…"},
                            {"type": "text", "text": "hi"}]}
        cache.put("m", _REQ, resp, {})
        assert cache.size == 1

    def test_anthropic_tool_use_cached(self):
        """tool_use block → 写缓存。"""
        cache = ResponseCache()
        resp = {"content": [{"type": "tool_use", "id": "t1",
                             "name": "f", "input": {}}]}
        cache.put("m", _REQ, resp, {})
        assert cache.size == 1

    def test_anthropic_empty_text_not_cached(self):
        """text block 为空白串 → 不写缓存。"""
        cache = ResponseCache()
        resp = {"content": [{"type": "text", "text": "  "},
                            {"type": "thinking", "thinking": "…"}]}
        cache.put("m", _REQ, resp, {})
        assert cache.size == 0

    # ── HIT 加固：陈旧空条目按 MISS ──

    def test_stale_empty_entry_treated_as_miss(self):
        """旧版本写入的空内容条目：get 按 MISS 处理并移除条目（replayed-empty）。"""
        cache = ResponseCache()
        stale = {"body": _openai_resp(""), "usage": {}, "cached_at": 0}
        key = cache._make_key("m", _REQ)
        with cache._lock:
            cache._cache[key] = stale  # 模拟旧版本遗留条目
        assert cache.get("m", _REQ) is None
        with cache._lock:
            assert key not in cache._cache, "stale empty entry must be evicted"
        assert cache.stats()["hits"] == 0, "按 MISS 处理不计入 hits"

    def test_stale_empty_entry_replayed_after_removal(self):
        """移除陈旧条目后，同一请求可正常命中新写入的有效条目。"""
        cache = ResponseCache()
        stale = {"body": _openai_resp(""), "usage": {}, "cached_at": 0}
        key = cache._make_key("m", _REQ)
        with cache._lock:
            cache._cache[key] = stale
        assert cache.get("m", _REQ) is None
        cache.put("m", _REQ, _openai_resp("real answer"), {})
        hit = cache.get("m", _REQ)
        assert hit is not None
        assert hit["body"]["choices"][0]["message"]["content"] == "real answer"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))