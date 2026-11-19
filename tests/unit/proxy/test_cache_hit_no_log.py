"""
unit/proxy/test_cache_hit_no_log.py — 方案 A：LRU 缓存命中不落 RequestLog

根因：缓存命中回放的是历史响应（未发生推理），旧代码在命中路径落库
tokens_in/out（取自被缓存请求的 usage）→ 请求日志出现「1ms 却带数万
tokens」的假完成行，且 token 统计 / 24h 费用被重复计数。

契约（锁死）：
  * /v1/messages 与 /v1/chat/completions 缓存命中 → 返回缓存体、
    **不**调用 pm.logger.log（journal 保留 log.info 一行）
  * LRU 生效可见性改由 ResponseCache.stats() 承担（snapshot
    local_models.cache_stats；网关控制卡展示）
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
_deps = Path(__file__).parent.parent.parent.parent / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

from inferfabric.proxy import chat_handlers


# ── Fakes ────────────────────────────────────────────────────────

def _entry():
    return {
        "body": {"id": "msg-x", "content": [{"type": "text", "text": "hi"}]},
        "usage": {"prompt_tokens": 41960, "prompt_tokens_cached": 0,
                  "completion_tokens": 2112},
        "cached_at": 0,
    }


class _FakeCache:
    """最小 ResponseCache 替身：get 恒命中 + stats（不依赖 cachetools）。"""

    def __init__(self):
        self.get_calls = 0

    def get(self, model, body):
        self.get_calls += 1
        return _entry()

    def stats(self):
        return {"hits": 7, "size": 3, "max": 500}


def _fake_pm():
    pm = MagicMock()
    pm.new_request_id.return_value = "iff-test"
    pm.auth.enabled = False
    pm.pack_manager = None   # F4 gate: MagicMock auto-attrs would trip the pack-uninstall 503
    pm._runtime_config = {"cache": {"enabled": True}}
    pm.response_cache = _FakeCache()
    return pm


# ═══════════════════════════════════════════════════════════════
# /v1/messages（Anthropic 路径）
# ═══════════════════════════════════════════════════════════════

def test_messages_cache_hit_no_request_log(monkeypatch):
    """缓存命中：返回缓存体 200，pm.logger.log 一次都不调。"""
    import importlib
    import inferfabric.proxy.handler as handler_module
    importlib.reload(handler_module)
    ProxyHandler = handler_module.ProxyHandler

    h = ProxyHandler.__new__(ProxyHandler)
    h.headers = {}
    h._read_body = MagicMock(
        return_value={"model": "qwen", "temperature": 0})
    h._send_json = MagicMock()

    pm = _fake_pm()
    h._handle_messages(pm)

    # 命中 → 返回缓存体（200）
    h._send_json.assert_called_once()
    body, status = h._send_json.call_args[0][:2]
    assert status == 200
    assert body == _entry()["body"]
    # 方案 A 核心断言：不落 RequestLog
    pm.logger.log.assert_not_called()
    # 确实走了缓存查询
    assert pm.response_cache.get_calls == 1


# ═══════════════════════════════════════════════════════════════
# /v1/chat/completions（OpenAI 路径）
# ═══════════════════════════════════════════════════════════════

def test_chat_cache_hit_no_request_log():
    """缓存命中：返回缓存体 200，pm.logger.log 一次都不调。"""
    h = MagicMock()
    h.headers = {}
    data = {"model": "qwen", "temperature": 0,
            "messages": [{"role": "user", "content": "hi"}]}
    pm = _fake_pm()

    chat_handlers.handle_chat(h, pm, data)

    h._send_json.assert_called_once()
    body, status = h._send_json.call_args[0][:2]
    assert status == 200
    assert body == _entry()["body"]
    pm.logger.log.assert_not_called()
    assert pm.response_cache.get_calls == 1
