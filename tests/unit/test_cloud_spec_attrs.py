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
