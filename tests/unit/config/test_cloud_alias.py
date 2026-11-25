"""PR2 Task 1 — 调用别名存储与加载（cloud_provider.yaml `aliases` + 顶层 `retired_aliases`）。

契约（spec §3 PR2 / plan Task 1）：
- provider 段 `aliases: {调用别名: 上游 model_id}`，save/load 往返一致；
- 非字符串/空值条目 → 解析抛错走既有 corrupt 保注册表路径（不部分应用）；
- `alias_target(alias)` 从内存 providers 查表 → (provider, model_id) | None；
- 删除 provider → 别名移入顶层 `retired_aliases`（未激活、不可路由、不列出）；
  重建同名 provider 不自动复活；显式重设后激活且 retired 清空。
"""

import yaml

from inferfabric.cloud_discovery import CloudDiscovery, CloudModel


def _write_yaml(path, data):
    path.write_text(yaml.dump(data, allow_unicode=True))


def _provider(base="https://a.example/v1", aliases=None, models=("glm-5",)):
    pcfg = {
        "api_key": "sk-test",
        "openai_base": base,
        "enabled_models": list(models),
    }
    if aliases is not None:
        pcfg["aliases"] = aliases
    return pcfg


def _mk_cd(tmp_path, providers, retired=None):
    p = tmp_path / "cloud_provider.yaml"
    data = {"providers": providers}
    if retired is not None:
        data["retired_aliases"] = retired
    _write_yaml(p, data)
    return CloudDiscovery(p), p


# ── YAML 往返 ─────────────────────────────────────────────────────────

def test_alias_roundtrip_yaml(tmp_path):
    cd, p = _mk_cd(tmp_path, {"prov-a": _provider(aliases={"glm-5-b": "glm-5"})})
    assert cd.providers["prov-a"].aliases == {"glm-5-b": "glm-5"}
    cd.save_config()
    cd2 = CloudDiscovery(p)
    assert cd2.providers["prov-a"].aliases == {"glm-5-b": "glm-5"}


def test_alias_absent_defaults_empty(tmp_path):
    cd, _ = _mk_cd(tmp_path, {"prov-a": _provider()})
    assert cd.providers["prov-a"].aliases == {}


# ── 坏条目 → corrupt 保注册表路径 ────────────────────────────────────

def test_alias_invalid_entry_rejected_preserves_registry(tmp_path):
    cd, p = _mk_cd(tmp_path, {"prov-a": _provider()})
    cd._cloud_models = {"glm-5": CloudModel("glm-5", "prov-a")}
    assert cd.resolve_route("glm-5", local_models=set()) == "cloud:prov-a"

    for bad in ({"glm-5-b": 123}, {"glm-5-b": ""}, {"": "glm-5"}):
        _write_yaml(p, {"providers": {"prov-a": _provider(aliases=bad)}})
        cd.reload()
        assert cd._config_corrupt is True, f"bad={bad!r} 未判 corrupt"
        # 注册表保留：存量模型仍可路由（corrupt SIGHUP 不打 404）
        assert cd.resolve_route("glm-5", local_models=set()) == "cloud:prov-a"
        # 坏别名不激活、不可调用
        assert cd.alias_target("glm-5-b") is None


# ── alias_target 查表 ─────────────────────────────────────────────────

def test_alias_target_lookup(tmp_path):
    cd, _ = _mk_cd(tmp_path, {
        "prov-a": _provider(aliases={"glm-5-b": "glm-5", "k2-long": "kimi/k2.5"}),
        "prov-b": _provider(base="https://b.example/v1", aliases={"glm-5-c": "glm-5"}),
    })
    assert cd.alias_target("glm-5-b") == ("prov-a", "glm-5")
    assert cd.alias_target("k2-long") == ("prov-a", "kimi/k2.5")  # 含 / 的上游 id
    assert cd.alias_target("glm-5-c") == ("prov-b", "glm-5")
    assert cd.alias_target("nope") is None


# ── 删除退役 / 重建不复活 / 显式重设 ─────────────────────────────────

def test_provider_delete_retires_aliases_no_auto_reactivate(tmp_path):
    cd, p = _mk_cd(tmp_path, {"prov-a": _provider(aliases={"glm-5-b": "glm-5"})})

    # DELETE（retire → drop）→ 别名退役：不可调用、不列出
    cd.retire_provider_aliases("prov-a")
    cd.drop_provider("prov-a")
    cd.save_config()
    assert cd.alias_target("glm-5-b") is None
    on_disk = yaml.safe_load(p.read_text())
    assert on_disk["retired_aliases"] == {"prov-a": {"glm-5-b": "glm-5"}}

    # 重载读回：仍不激活
    cd2 = CloudDiscovery(p)
    assert cd2.alias_target("glm-5-b") is None

    # 重建同名 provider（无别名声明）→ 不自动复活，retired 原样保留
    _write_yaml(p, {"providers": {"prov-a": _provider()},
                    "retired_aliases": {"prov-a": {"glm-5-b": "glm-5"}}})
    cd3 = CloudDiscovery(p)
    assert "prov-a" in cd3.providers
    assert cd3.alias_target("glm-5-b") is None
    assert cd3._retired_aliases == {"prov-a": {"glm-5-b": "glm-5"}}

    # 显式重设同名别名 → 激活，retired 清空（YAML 不再写 retired_aliases）
    cd3.providers["prov-a"].aliases["glm-5-b"] = "glm-5"
    cd3.unretire_alias("prov-a", "glm-5-b")
    cd3.save_config()
    assert cd3.alias_target("glm-5-b") == ("prov-a", "glm-5")
    assert cd3._retired_aliases == {}
    on_disk2 = yaml.safe_load(p.read_text())
    assert "retired_aliases" not in on_disk2
