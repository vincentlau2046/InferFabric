"""云端路由精确解析 — 斜杠 model_id / 全名优先修复的验收测试。

契约：
- resolve_route：provider 前缀全名优先于短名（短名是多 provider 同名时的
  first-wins 歧义键，显式前缀请求不得被劫持）。
- resolve_cloud_model：短名 / 斜杠 model_id 本体 / provider 前缀全名 三形态
  精确解析；跨 provider 不误命中。
- _merge_model_spec：spec_key 缺省时按 model_id 查不到则兜底短名键
  （重启后斜杠模型打标属性不漏合并）。

全部纯逻辑（mock 注册表），无网络/GPU 依赖。
"""

import pytest

from inferfabric.cloud_discovery import CloudDiscovery, CloudModel, ProviderConfig


def make_cd() -> CloudDiscovery:
    cd = CloudDiscovery(config_path=None)
    return cd


def cm(model_id: str, provider: str, **kw) -> CloudModel:
    return CloudModel(model_id=model_id, provider=provider, **kw)


class TestResolveRouteFullNamePriority:
    """全名（含 provider 前缀）优先于短名 — 显式前缀请求不被同名短名劫持。"""

    def test_prefixed_request_hits_own_provider(self):
        cd = make_cd()
        cd._cloud_models = {
            "m": cm("m", "alpha"),           # alpha 先发现，占住短名
            "alpha/m": cm("m", "alpha"),
            "beta/m": cm("m", "beta"),
        }
        assert cd.resolve_route("beta/m", set()) == "cloud:beta"

    def test_short_name_still_works(self):
        cd = make_cd()
        cd._cloud_models = {"m": cm("m", "alpha")}
        assert cd.resolve_route("m", set()) == "cloud:alpha"

    def test_unknown_returns_none(self):
        cd = make_cd()
        cd._cloud_models = {"m": cm("m", "alpha")}
        assert cd.resolve_route("nope", set()) is None


class TestResolveCloudModel:
    """resolve_cloud_model 三形态精确解析（二段查找）。"""

    def _seed_slash_model(self, cd):
        # spec 落盘形态：key=短名 / model_id=上游全名（含斜杠）
        cd._cloud_models = {
            "k3.1-pro": cm("kimi/k3.1-pro", "atlaswork"),
            "atlaswork/k3.1-pro": cm("kimi/k3.1-pro", "atlaswork"),
        }

    def test_short_name(self):
        cd = make_cd()
        self._seed_slash_model(cd)
        m = cd.resolve_cloud_model("atlaswork", "k3.1-pro")
        assert m is not None and m.provider == "atlaswork"

    def test_slash_model_id_body(self):
        cd = make_cd()
        self._seed_slash_model(cd)
        m = cd.resolve_cloud_model("atlaswork", "kimi/k3.1-pro")
        assert m is not None and m.model_id == "kimi/k3.1-pro"

    def test_provider_prefixed_full_name(self):
        cd = make_cd()
        self._seed_slash_model(cd)
        m = cd.resolve_cloud_model("atlaswork", "atlaswork/kimi/k3.1-pro")
        assert m is not None and m.model_id == "kimi/k3.1-pro"

    def test_wrong_provider_does_not_match(self):
        cd = make_cd()
        self._seed_slash_model(cd)
        assert cd.resolve_cloud_model("other", "kimi/k3.1-pro") is None

    def test_unknown_model_returns_none(self):
        cd = make_cd()
        self._seed_slash_model(cd)
        assert cd.resolve_cloud_model("atlaswork", "ghost") is None


class TestMergeModelSpecShortKeyFallback:
    """spec_key 缺省 + model_id 查不到 → 兜底短名键（斜杠模型重启不漏合并）。"""

    def test_falls_back_to_short_key(self):
        cd = make_cd()
        cfg = ProviderConfig(name="p", model_specs={"k3": {"supports_vision": True}})
        model = cm("kimi/k3", "p")
        merged = cd._merge_model_spec(model, cfg)    # 不传 spec_key
        assert merged.supports_vision is True

    def test_explicit_key_still_wins(self):
        cd = make_cd()
        cfg = ProviderConfig(name="p", model_specs={
            "kimi/k3": {"supports_vision": True},
            "k3": {"supports_vision": False},
        })
        model = cm("kimi/k3", "p")
        merged = cd._merge_model_spec(model, cfg)    # 全名键存在 → 直接命中
        assert merged.supports_vision is True

    def test_no_spec_untouched(self):
        cd = make_cd()
        cfg = ProviderConfig(name="p", model_specs={})
        model = cm("kimi/k3", "p")
        assert cd._merge_model_spec(model, cfg).supports_vision is None
