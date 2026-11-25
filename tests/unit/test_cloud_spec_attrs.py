"""M2-T4 backend regression — cloud spec attribute keys (six-key editing).

Pins two bugs the attrs modal exposed:
- _serialize_providers truthy gates dropped price 0.0 (a legal value), and
  context_window/max_output_tokens sat in the `known` exclusion set without
  ever being written — attributes silently evaporated on save_config.
- update_model_spec only accepted _CAPABILITY_KEYS; int/float attribute
  keys (context_window/max_output_tokens/price_input/price_output) are now
  accepted, coerced, and clearable via None/"".
"""

import threading

from inferfabric.cloud_discovery import CloudDiscovery, ProviderConfig


def _mgr_with_spec(spec: dict) -> CloudDiscovery:
    """Minimal instance: bypass __init__ (no config/secrets needed for
    _serialize_providers with an empty api_key; _env_key_for_provider and
    the registry rebuild are stubbed)."""
    mgr = object.__new__(CloudDiscovery)
    mgr._providers = {"prov": ProviderConfig(name="prov", model_specs={"m1": dict(spec)})}
    mgr._env_key_for_provider = lambda name: f"PROV_{name.upper()}_API_KEY"
    mgr._rebuild_provider_registry = lambda *a, **k: None
    mgr._cloud_models = {}
    mgr._models_lock = threading.Lock()
    return mgr


# ── _serialize_providers: attribute persistence ───────────────────────

def test_serialize_keeps_zero_price_and_int_attrs():
    mgr = _mgr_with_spec({
        "price_input": 0.0, "price_output": 0.0,
        "context_window": 128000, "max_output_tokens": 8192,
        "supports_vision": True,
    })
    md = mgr._serialize_providers()["providers"]["prov"]["models"]["m1"]
    assert md["price_input"] == 0.0
    assert md["price_output"] == 0.0
    assert md["context_window"] == 128000
    assert md["max_output_tokens"] == 8192
    assert md["supports_vision"] is True


def test_serialize_omits_unset_attrs():
    mgr = _mgr_with_spec({"supports_vision": True})
    md = mgr._serialize_providers()["providers"]["prov"]["models"]["m1"]
    for k in ("price_input", "price_output", "context_window", "max_output_tokens"):
        assert k not in md


# ── update_model_spec: int/float attribute keys ──────────────────────

def test_update_spec_accepts_int_and_float_keys():
    mgr = _mgr_with_spec({})
    count = mgr.update_model_spec("prov", "m1", {
        "context_window": 64000, "max_output_tokens": 4096,
        "price_input": 2.5, "price_output": 10,
    })
    spec = mgr._providers["prov"].model_specs["m1"]
    assert spec["context_window"] == 64000
    assert spec["max_output_tokens"] == 4096
    assert spec["price_input"] == 2.5
    assert spec["price_output"] == 10.0   # int coerced to float
    assert count == mgr._routable_count("prov")


def test_update_spec_none_or_empty_clears_attr():
    mgr = _mgr_with_spec({"context_window": 64000, "price_input": 2.5})
    mgr.update_model_spec("prov", "m1", {"context_window": None, "price_input": ""})
    spec = mgr._providers["prov"].model_specs["m1"]
    assert "context_window" not in spec
    assert "price_input" not in spec


def test_update_spec_capability_false_still_unsets():
    mgr = _mgr_with_spec({"supports_vision": True})
    mgr.update_model_spec("prov", "m1", {"supports_vision": False})
    assert "supports_vision" not in mgr._providers["prov"].model_specs["m1"]


def test_update_spec_rejects_unknown_keys():
    mgr = _mgr_with_spec({})
    mgr.update_model_spec("prov", "m1", {"bogus_key": 1})
    assert "bogus_key" not in mgr._providers["prov"].model_specs["m1"]


# ── GET /admin/cloud/providers: attribute backfill (review B1) ────────

class _FakeGetHandler:
    """GET-only fake: command + _send_json capture (no socket)."""

    command = "GET"

    def __init__(self):
        self.sent = []

    def _send_json(self, data, status=200, extra_headers=None):
        self.sent.append(data)


def _pm_with(mgr):
    import types
    return types.SimpleNamespace(
        cloud=types.SimpleNamespace(
            providers=mgr._providers,
            cloud_models={},
            get_candidates=lambda name: [],
            _last_discovery=0.0,
            # PR1 编辑契约成员（GET providers 现依赖指纹缓存与 secrets 判定）
            config_fingerprint=lambda: "none",
            secrets=types.SimpleNamespace(has=lambda var: False),
        ),
    )


def _providers_payload(mgr):
    from inferfabric.proxy.handler import ProxyHandler
    h = _FakeGetHandler()
    ProxyHandler._handle_cloud_providers(h, _pm_with(mgr))
    assert h.sent, "handler did not reply"
    return h.sent[0]


def test_get_providers_backfills_saved_attrs_roundtrip():
    """保存 → GET 重开 → 回填值相等（评审 B1 行为断言一）。

    模拟属性浮窗完整回路：POST tag 语义写 spec（update_model_spec），
    GET providers 序列化必须透传同值 —— 缺键即「存得进、读不回」。"""
    saved = {"context_window": 128000, "max_output_tokens": 8192,
             "price_input": 2.5, "price_output": 0.0, "supports_vision": True}
    mgr = _mgr_with_spec({})
    mgr.update_model_spec("prov", "m1", dict(saved))
    specs = _providers_payload(mgr)["providers"][0]["model_specs"]
    entry = next(s for s in specs if s["id"] == "m1")
    for k, v in saved.items():
        assert entry[k] == v, f"backfill lost '{k}': {entry.get(k)!r} != {v!r}"


def test_get_providers_backfill_none_tri_state_allows_clear():
    """清空 → 置 null（评审 B1 行为断言二）。

    清空路径依赖回填源给出 None（而非缺键/undefined）：update_model_spec
    清除属性后 spec 无键，GET 必须以 None 三态透传，openAttrsModal 才能
    看到「已清空」并把下次提交发成 null。"""
    mgr = _mgr_with_spec({})
    mgr.update_model_spec("prov", "m1", {"context_window": 128000})
    mgr.update_model_spec("prov", "m1", {"context_window": None})
    entry = next(s for s in _providers_payload(mgr)["providers"][0]["model_specs"]
                 if s["id"] == "m1")
    assert entry["context_window"] is None
    for k in ("max_output_tokens", "price_input", "price_output"):
        assert entry[k] is None
