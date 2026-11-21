"""unit/proxy/test_llama_cpp_chat_route.py — llama_cpp 聊天路由回归（Task 3 真机首发现）。

契约（锁死）：
  * llama_cpp（llama-server）的 /v1/chat/completions 走标准上游转发
    （upstream_path=/v1/chat/completions），绝不进 Ollama 原生 /api/chat
    通道（llama-server 无该路由 → 必 404）。
  * 来源：MacFabric P0 PATCH（fork chat_handlers.py:392）——上游 main
    把 is_llama_cpp 一并送进 handle_ollama_native；Mac 上 llama_cpp =
    llama-server 本体（chat 包），Linux 上 llama_cpp 仅 embedding/rerank、
    聊天请求原行为也是必 404，本修复对两平台均为严格改进。
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
_deps = Path(__file__).parent.parent.parent.parent / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

from inferfabric.proxy import chat_handlers


def _llama_cpp_model_obj():
    return SimpleNamespace(served_name="qwen3-4b-instruct-2507",
                           is_ollama=False, is_llama_cpp=True,
                           type="llama_cpp")


def _fake_pm(model_obj):
    pm = MagicMock()
    pm.new_request_id.return_value = "iff-test"
    pm.auth.enabled = False
    pm.pack_manager = None      # MagicMock auto-attrs would trip the 503 gate
    pm._runtime_config = {"cache": {"enabled": False}}
    pm.response_cache = None
    pm.auto_switch = False
    pm.model_to_service.return_value = "pack-qwen3-4b-instruct-2507-1-0-0"
    pm.get_target_port.return_value = 58411
    pm.mgr.get_model.return_value = model_obj
    gate = MagicMock()
    gate.ok = True
    pm.dual_gate.acquire.return_value = gate
    return pm


def _fake_handler(data):
    import inferfabric.proxy.handler as handler_module
    h = handler_module.ProxyHandler.__new__(handler_module.ProxyHandler)
    h.headers = {}
    h.path = "/v1/chat/completions"
    h._read_body = MagicMock(return_value=data)
    h._send_json = MagicMock()
    return h


def test_llama_cpp_chat_routes_to_openai_upstream_not_ollama(monkeypatch):
    """活跃 llama_cpp 模型的聊天请求：标准上游转发（/v1/chat/completions），
    绝不调 handle_ollama_native（/api/chat → llama-server 404）。"""
    model_obj = _llama_cpp_model_obj()
    pm = _fake_pm(model_obj)
    h = _fake_handler({"model": "qwen3-4b-instruct-2507",
                       "messages": [{"role": "user", "content": "ping"}],
                       "max_tokens": 8})

    def _boom(*a, **k):
        raise AssertionError(
            "llama_cpp chat must not dispatch to the Ollama native channel")

    monkeypatch.setattr(chat_handlers, "handle_ollama_native", _boom)

    forwards = []

    def fake_forward(handler, pm_, port, body, stream, model_name="",
                     upstream_path="", cache_body=None):
        forwards.append({"port": port, "upstream_path": upstream_path})
        return True

    monkeypatch.setattr(chat_handlers, "_forward_request", fake_forward)

    chat_handlers.handle_chat(h, pm, h._read_body.return_value)

    assert len(forwards) == 1, "必须走标准上游转发路径"
    assert forwards[0]["upstream_path"] == "/v1/chat/completions"
    assert forwards[0]["port"] == 58411


def test_ollama_model_still_routes_native(monkeypatch):
    """真 Ollama 后端行为不变：仍走 /api/chat 原生通道。"""
    model_obj = SimpleNamespace(served_name="qwen2.5:7b", is_ollama=True,
                                is_llama_cpp=False, type="ollama")
    pm = _fake_pm(model_obj)
    pm.get_target_port.return_value = 11434
    h = _fake_handler({"model": "qwen2.5:7b",
                       "messages": [{"role": "user", "content": "ping"}]})

    calls = []
    monkeypatch.setattr(chat_handlers, "handle_ollama_native",
                        lambda *a, **k: calls.append(1))
    monkeypatch.setattr(chat_handlers, "_forward_request",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not forward openai path")))

    chat_handlers.handle_chat(h, pm, h._read_body.return_value)
    assert calls == [1]
