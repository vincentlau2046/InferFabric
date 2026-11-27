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


# ── Task 11 验收暴露：spec-only 短名碰撞丢前缀键 ─────────────────────

def test_spec_only_model_colliding_short_name_still_routable_prefixed(tmp_path):
    """跨 provider 短名碰撞时 spec-only 手填模型仍须注册前缀键。

    验收 step 10 暴露（v6.6 遗留）：手动重建的 provider 无白名单
    （enabled_models=[]），手填模型只进 model_specs；短键已被先注册
    provider 占用 → spec-only 路径整体跳过（含前缀键）→ 手填模型不可
    路由 → 指向它的别名也 404。对齐 _register_whitelist_models 的碰撞
    契约：短键 first-wins 保持，前缀键必须注册（spec 恒可路由）。
    """
    cd, p = _mk_cd(tmp_path, {"prov-a": _provider()})
    # 短键 glm-5 first-wins → prov-a
    assert cd._cloud_models["glm-5"].provider == "prov-a"

    # 重建第二个 provider：无白名单（手动 re-add 不带 enabled_models）
    _write_yaml(p, {"providers": {
        "prov-a": _provider(),
        "prov-b": _provider(base="https://b.example/v1", models=()),
    }})
    cd.reload()
    # 手填 glm-5 → 纯 spec-only（无候选快照、无白名单），短键被 prov-a 占用
    cd.add_manual_model("prov-b", "glm-5")
    assert cd._cloud_models["prov-b/glm-5"].provider == "prov-b"
    assert cd.resolve_route("prov-b/glm-5", set()) == "cloud:prov-b"

    # 验收 step 10 场景：别名指向该手填模型 → 精确路由（不串 prov-a）
    cd.providers["prov-b"].aliases["glm-5-b"] = "glm-5"
    assert cd.resolve_route("glm-5-b", set()) == "cloud:prov-b"
    # 短键 first-wins 契约不变
    assert cd.resolve_route("glm-5", set()) == "cloud:prov-a"


# ── 终审 Important：别名翻译后 stripped 残留致字面同名条目劫持 ────────

def test_resolve_cloud_model_alias_not_hijacked_by_later_same_named_model(tmp_path):
    """别名翻译后不得回落匹配本 provider 内字面同名的注册表条目。

    终审发现（Important）：resolve_cloud_model 的 stripped 在别名翻译**前**
    计算、翻译后仍参与匹配 —— 同 provider 后续出现与别名同名的模型
    （发现/勾选/手填，validate_alias 只守设置时刻）时，第二段解析返回
    字面同名条目，转发 model=字面名而非别名绑定的上游 id（依赖字典插入
    序，静默错路由 — 违反 spec §3 精确选定上游）。
    """
    cd, _p = _mk_cd(tmp_path, {"prov-a": _provider(models=("glm-5", "bar"))})
    cd.providers["prov-a"].aliases["foo"] = "bar"
    # 后续该 provider 内出现字面同名模型 foo（手填 spec 路径，注册表序在 bar 之后）
    cd.add_manual_model("prov-a", "foo")
    # 一段路由仍走别名（别名先于短名查表）
    assert cd.resolve_route("foo", set()) == "cloud:prov-a"
    # 二段解析（chat 处理器以「请求名」调用）必须解析到别名绑定的上游 id
    m = cd.resolve_cloud_model("prov-a", "foo")
    assert m is not None and m.model_id == "bar", m
    # 字面同名模型自身的直呼不受影响
    assert cd.resolve_cloud_model("prov-a", "prov-a/foo").model_id == "foo"


# ── Task 2: validate_alias + POST /admin/cloud/provider-alias ─────────

import json
from types import SimpleNamespace

from inferfabric.cloud_discovery import validate_alias
from inferfabric.proxy.handler import ProxyHandler


def _mk_handler(cd, command="GET", body=b""):
    """ProxyHandler.__new__ + stub（照 test_cloud_provider_update.py 同款）。"""
    h = ProxyHandler.__new__(ProxyHandler)
    h.command = command
    sent = []
    h._send_json = lambda data, status=200, extra_headers=None: sent.append((data, status))
    h._read_body = lambda: json.loads(body.decode("utf-8")) if body else {}
    return h, sent


class TestValidateAlias:
    def _va(self, alias, *, local=(), short=(), others=()):
        return validate_alias(alias, "prov-a", "glm-5",
                              local_names=set(local),
                              registry_short_names=set(short),
                              other_aliases=set(others))

    def test_ok_shapes(self):
        for a in ("glm-5-b", "My_Model:2", "k3@beta", "a.b-c_d", "v1.2"):
            ok, why = self._va(a)
            assert ok, f"{a!r} 应合法: {why}"

    def test_alias_conflict_local_name(self):
        ok, why = self._va("glm-5", local={"glm-5"})
        assert not ok and "glm-5" in why

    def test_alias_conflict_reserved_fabric_prefix(self):
        ok, why = self._va("fabric-fast")
        assert not ok and "fabric-" in why

    def test_alias_conflict_existing_short_name(self):
        ok, why = self._va("glm-5", short={"glm-5"})
        assert not ok and "glm-5" in why

    def test_alias_conflict_other_alias(self):
        ok, why = self._va("glm-5-b", others={"glm-5-b"})
        assert not ok and "glm-5-b" in why

    def test_alias_slash_rejected(self):
        for bad in ("a/b", "a b", "", "  ", "别名", "a!b"):
            ok, why = self._va(bad)
            assert not ok, f"{bad!r} 应拒绝"


def _mk_pm(cd):
    return SimpleNamespace(cloud=cd,
                           mgr=SimpleNamespace(_models={}),
                           pool_registry=None)


class TestProviderAliasEndpoint:
    def _post(self, cd, body, pool_registry=None, models=None):
        pm = _mk_pm(cd)
        pm.pool_registry = pool_registry
        if models:
            pm.mgr._models = models
        h, sent = _mk_handler(cd, command="POST", body=json.dumps(body).encode())
        h._handle_cloud_provider_alias(pm)
        return sent

    def _cd(self, tmp_path, aliases=None):
        return _mk_cd(tmp_path, {
            "prov-a": {"openai_base": "http://a", "enabled_models": ["glm-5"],
                       "aliases": aliases or {}},
        })

    def test_alias_set_and_persist(self, tmp_path):
        cd, p = self._cd(tmp_path)
        fp = cd.disk_fingerprint()
        sent = self._post(cd, {"provider": "prov-a", "model_id": "glm-5",
                               "alias": "glm-5-b",
                               "expected_config_fingerprint": fp})
        assert sent[0][1] == 200, sent
        assert cd.alias_target("glm-5-b") == ("prov-a", "glm-5")
        assert yaml.safe_load(p.read_text())["providers"]["prov-a"]["aliases"] \
            == {"glm-5-b": "glm-5"}

    def test_alias_remove_via_empty(self, tmp_path):
        cd, p = self._cd(tmp_path, aliases={"glm-5-b": "glm-5"})
        fp = cd.disk_fingerprint()
        sent = self._post(cd, {"provider": "prov-a", "model_id": "glm-5",
                               "alias": "",
                               "expected_config_fingerprint": fp})
        assert sent[0][1] == 200, sent
        assert cd.alias_target("glm-5-b") is None
        # 序列化契约：空 aliases 不落盘（非空才写）
        assert yaml.safe_load(p.read_text())["providers"]["prov-a"].get("aliases", {}) == {}

    def test_alias_fingerprint_conflict_409(self, tmp_path):
        cd, _p = self._cd(tmp_path)
        sent = self._post(cd, {"provider": "prov-a", "model_id": "glm-5",
                               "alias": "glm-5-b",
                               "expected_config_fingerprint": "stale-stale-stale"})
        assert sent[0][1] == 409, sent
        assert sent[0][0]["error"] == "config_conflict"
        assert len(sent[0][0]["config_fingerprint"]) == 16

    def test_alias_model_not_routable_400(self, tmp_path):
        cd, _p = self._cd(tmp_path)
        fp = cd.disk_fingerprint()
        sent = self._post(cd, {"provider": "prov-a", "model_id": "no-such",
                               "alias": "x1",
                               "expected_config_fingerprint": fp})
        assert sent[0][1] == 400, sent
        assert sent[0][0]["error"] == "model_not_routable"

    def test_alias_conflict_via_endpoint_local_and_pool(self, tmp_path):
        """端点把本地名与池逻辑名并入 local_names — 撞名拒绝且不写入。"""
        cd, _p = self._cd(tmp_path)

        class _M:
            served_name = "glm-5"
        sent = self._post(cd, {"provider": "prov-a", "model_id": "glm-5",
                               "alias": "glm-5",
                               "expected_config_fingerprint": cd.disk_fingerprint()},
                          models={"m": _M()})
        assert sent[0][1] == 400 and sent[0][0]["error"] == "alias_conflict", sent

        class _Pool:
            def snapshot(self):
                return SimpleNamespace(pools={"fast-pool": SimpleNamespace()})
        sent = self._post(cd, {"provider": "prov-a", "model_id": "glm-5",
                               "alias": "fast-pool",
                               "expected_config_fingerprint": cd.disk_fingerprint()},
                          pool_registry=_Pool())
        assert sent[0][1] == 400 and sent[0][0]["error"] == "alias_conflict", sent
        assert cd.alias_target("fast-pool") is None

    def test_alias_set_unretires_deleted_pair(self, tmp_path):
        """重建同名 provider 后端点显式重设 → 激活且 retired 清空。"""
        cd, p = self._cd(tmp_path)
        # 退役：删除 provider（retire → drop → save）
        cd.retire_provider_aliases("prov-a")
        cd.drop_provider("prov-a")
        cd.save_config()
        # 重建同名 provider（无别名）：写回 YAML 后 reload（POST add 的磁盘等价态）
        _write_yaml(p, {"providers": {"prov-a": {
                            "openai_base": "http://a",
                            "enabled_models": ["glm-5"]}},
                        "retired_aliases": {"prov-a": {"glm-5-b": "glm-5"}}})
        cd.reload()
        assert cd.alias_target("glm-5-b") is None
        fp = cd.disk_fingerprint()
        sent = self._post(cd, {"provider": "prov-a", "model_id": "glm-5",
                               "alias": "glm-5-b",
                               "expected_config_fingerprint": fp})
        assert sent[0][1] == 200, sent
        assert cd.alias_target("glm-5-b") == ("prov-a", "glm-5")
        assert cd._retired_aliases == {}

    def test_provider_delete_endpoint_retires_aliases(self, tmp_path):
        """DELETE /admin/cloud/providers 调 retire_provider_aliases（Task 1 接线）。"""
        cd, p = self._cd(tmp_path, aliases={"glm-5-b": "glm-5"})
        h, sent = _mk_handler(cd, command="DELETE",
                              body=json.dumps({"name": "prov-a"}).encode())
        h._handle_cloud_providers(_mk_pm(cd))
        assert sent[0][1] == 200, sent
        on_disk = yaml.safe_load(p.read_text())
        assert on_disk["retired_aliases"] == {"prov-a": {"glm-5-b": "glm-5"}}


def test_get_providers_exposes_aliases(tmp_path):
    """GET providers 每 provider 带 aliases（仪表盘徽标/复制数据源）。"""
    cd, _p = _mk_cd(tmp_path, {"prov-a": _provider(aliases={"glm-5-b": "glm-5"})})
    h, sent = _mk_handler(cd)
    h._handle_cloud_providers(_mk_pm(cd))
    by_name = {pr["name"]: pr for pr in sent[0][0]["providers"]}
    assert by_name["prov-a"]["aliases"] == {"glm-5-b": "glm-5"}


class TestRetiredAliasSemantics:
    """#67 — 已退役别名绝不回退（先于 local/短名判定）、可解释、撞名集合含退役名。"""

    def _cd_two(self, tmp_path, extra_models_a=None):
        a = {"openai_base": "http://a", "enabled_models": ["glm-5"]}
        if extra_models_a:
            a["models"] = extra_models_a
        cd, _p = _mk_cd(tmp_path, {
            "prov-a": a,
            "prov-b": {"openai_base": "http://b", "enabled_models": ["glm-5"],
                       "aliases": {"shared-alias": "glm-5"}},
        })
        return cd

    def test_retired_alias_never_falls_back_to_local(self, tmp_path):
        """删除承诺字面语义：退役名命中 → None，先于本地模型判定。"""
        cd = self._cd_two(tmp_path)
        cd.retire_provider_aliases("prov-b")
        assert cd.resolve_route("shared-alias", {"shared-alias"}) is None

    def test_retired_alias_never_falls_back_to_other_provider(self, tmp_path):
        """他 provider 之后注册同名短名 —— 退役名不回退到他 provider。"""
        cd = self._cd_two(tmp_path, extra_models_a={"shared-alias": {}})
        cd.retire_provider_aliases("prov-b")
        assert cd.resolve_route("shared-alias", set()) is None

    def test_explain_unknown_reports_deleted_provider(self, tmp_path):
        """既有「目标提供方已删除」分支由此可达（此前 alias_target 未命中恒 None）。"""
        cd = self._cd_two(tmp_path)
        cd.retire_provider_aliases("prov-b")
        msg = cd.explain_unknown("shared-alias")
        assert msg is not None and "已删除" in msg and "prov-b" in msg

    def test_retired_alias_owner_helper(self, tmp_path):
        cd = self._cd_two(tmp_path)
        cd.retire_provider_aliases("prov-b")
        assert cd.retired_alias_owner("shared-alias") == "prov-b"
        assert cd.retired_alias_owner("nope") is None


class TestRetiredAliasEndpointConflicts:
    """#67 — validate_alias 调用点并入退役名（同 provider 显式重设除外）。"""

    def _cd_two(self, tmp_path):
        cd, _p = _mk_cd(tmp_path, {
            "prov-a": {"openai_base": "http://a", "enabled_models": ["glm-5"]},
            "prov-b": {"openai_base": "http://b", "enabled_models": ["glm-5"],
                       "aliases": {"shared-alias": "glm-5"}},
        })
        return cd

    def test_cross_provider_grab_of_retired_name_rejected(self, tmp_path):
        cd = self._cd_two(tmp_path)
        cd.retire_provider_aliases("prov-b")
        sent = TestProviderAliasEndpoint()._post(
            cd, {"provider": "prov-a", "model_id": "glm-5",
                 "alias": "shared-alias",
                 "expected_config_fingerprint": cd.disk_fingerprint()})
        assert sent[0][1] == 400 and sent[0][0]["error"] == "alias_conflict", sent
        assert cd.alias_target("shared-alias") is None

    def test_same_provider_reset_of_retired_allowed(self, tmp_path):
        """同 provider 显式重设退役名放行（unretire 语义），retired 清空。"""
        cd = self._cd_two(tmp_path)
        cd.retire_provider_aliases("prov-b")
        sent = TestProviderAliasEndpoint()._post(
            cd, {"provider": "prov-b", "model_id": "glm-5",
                 "alias": "shared-alias",
                 "expected_config_fingerprint": cd.disk_fingerprint()})
        assert sent[0][1] == 200, sent
        assert cd.alias_target("shared-alias") == ("prov-b", "glm-5")
        assert cd._retired_aliases.get("prov-b", {}) == {}
