"""model_pool.catalog — 候选目录：池可选集的权威视图（v7.1.2）。

目录是 model_pool 域的概念（「池候选可以引用什么」），由后端一次性
构建；前端不再刮 /models 与 /admin/cloud/providers 逆向猜 schema。
契约测试断言精确到键名 — 后端改字段名会红（上一版云目录就是因前端
猜错字段静默断掉的）。
"""

from types import SimpleNamespace

from inferfabric.model_pool.catalog import build_catalog


class CatalogMgr:
    """模拟 pm.mgr：list_models 返回与真实 manager 相同结构的 dict 列表。"""

    def __init__(self, model_dicts, served=None):
        self._dicts = model_dicts
        self._served = served or {}   # name → served_name

    def list_models(self):
        return self._dicts

    def get_model(self, name):
        served = self._served.get(name)
        if served is None and not any(d["name"] == name for d in self._dicts):
            return None
        return SimpleNamespace(name=name, served_name=served or name)


class CatalogPM:
    """模拟 pm：build_catalog 只依赖 mgr.list_models/get_model + cloud.cloud_models。"""

    def __init__(self, model_dicts=None, cloud_models=None, served=None):
        self.mgr = CatalogMgr(model_dicts or [], served=served)
        self.cloud = SimpleNamespace(cloud_models=cloud_models or {})


def _llm(name="qwen-small", type_="vllm", active=True, ctx=32768):
    return {"name": name, "model_type": "llm", "type": type_, "active": active,
            "modality": "text", "context_window": ctx}


def _vl(name="Qwen38-27B-VL", type_="ninfer", active=False, ctx=204800):
    return {"name": name, "model_type": "vl", "type": type_, "active": active,
            "modality": "text-vision", "context_window": ctx}


class TestLocalCatalog:
    def test_llm_only_non_chat_filtered(self):
        pm = CatalogPM(model_dicts=[
            _llm("qwen-small"),
            {"name": "bge-m3", "model_type": "embedding", "type": "ollama_cpp",
             "active": True, "modality": "text", "context_window": None},
            {"name": "cosyvoice", "model_type": "tts", "type": "tts",
             "active": False, "modality": "audio", "context_window": None},
        ])
        cat = build_catalog(pm)
        assert [e["name"] for e in cat["local"]] == ["qwen-small"]

    def test_local_entry_contract(self):
        cat = build_catalog(CatalogPM(model_dicts=[_llm()]))
        entry = cat["local"][0]
        # 契约精确到键名（前端与 Phase 2/3 扩展依赖这些字段）
        assert set(entry) == {"name", "active", "engine", "modality",
                              "context_window", "protocols", "features"}
        assert entry["name"] == "qwen-small"
        assert entry["active"] is True
        assert entry["engine"] == "vllm"
        assert entry["context_window"] == 32768

    def test_vl_models_in_catalog(self):
        # 模态路由端到端（本地侧）：vl 模型必须能进目录，否则面板下拉
        # 永远选不到 VL、池配置无法引用它做视觉候选
        pm = CatalogPM(model_dicts=[_llm("qwen-small"), _vl()])
        cat = build_catalog(pm)
        assert [e["name"] for e in cat["local"]] == ["qwen-small", "Qwen38-27B-VL"]

    def test_local_features_default_empty(self):
        # 7.8.0 语义反转：本地 features 一律空（= 无约束/全能力默认）。
        # modality 仍暴露供 UI 提示展示，但不再构成快照约束 —
        # 视觉请求到文本模型直通上游裁定，池 failover 兜底。
        omni = {"name": "omni-x", "model_type": "vl", "type": "vllm",
                "active": False, "modality": "multimodal", "context_window": 8192}
        pm = CatalogPM(model_dicts=[_llm("qwen-small"), _vl(), omni])
        by_name = {e["name"]: e for e in build_catalog(pm)["local"]}
        assert by_name["qwen-small"]["features"] == []
        assert by_name["Qwen38-27B-VL"]["features"] == []
        assert by_name["omni-x"]["features"] == []
        # modality 保留（编辑器提示用）
        assert by_name["Qwen38-27B-VL"]["modality"] == "text-vision"

    def test_local_uses_served_name(self):
        # 池候选引用 served_name（dispatch 评审 #15）— 目录必须给可引用名
        pm = CatalogPM(model_dicts=[_llm("qwen-internal")],
                       served={"qwen-internal": "qwen-small"})
        cat = build_catalog(pm)
        assert cat["local"][0]["name"] == "qwen-small"

    def test_local_protocols_by_engine(self):
        # vllm/ninfer/sglang 有本地 anthropic 路径（forward_anthropic_local
        # 直通引擎原生 /v1/messages，不翻译）→ 双协议声明。ninfer 有 7 天
        # 线上实证（6615 TXT + 3483 VL 次 /v1/messages → LOCAL，池 bug #1）；
        # sglang 双协议为用户确认（本地框架默认双协议，2026-10-28 顺带开启）。
        # 其余引擎保守只声明 openai（declared 态，证据系统可探实）
        pm = CatalogPM(model_dicts=[_llm("a", type_="vllm"),
                                    _llm("b", type_="ninfer"),
                                    _llm("c", type_="sglang"),
                                    _llm("d", type_="ollama")])
        cat = build_catalog(pm)
        by_name = {e["name"]: e for e in cat["local"]}
        assert by_name["a"]["protocols"] == ["openai", "anthropic"]
        assert by_name["b"]["protocols"] == ["openai", "anthropic"]
        assert by_name["c"]["protocols"] == ["openai", "anthropic"]
        assert by_name["d"]["protocols"] == ["openai"]

    def test_no_mgr_no_local(self):
        cat = build_catalog(SimpleNamespace(mgr=None, cloud=SimpleNamespace(
            cloud_models={})))
        assert cat["local"] == []


def _make_cm(model_id="glm-5", provider="tathr-glm",
             anthropic=True, tools=None, vision=None):
    # 7.8.0：能力标志三态 — None=未设置（默认全能力）/ True=确认 / False=约束
    return SimpleNamespace(
        model_id=model_id, provider=provider,
        openai_available=True, anthropic_available=anthropic,
        supports_tools=tools, supports_vision=vision,
        context_window=None, contextWindow=None)


class TestCloudCatalog:

    def test_cloud_entries_from_routable_models(self):
        # 目录 = cloud_models（发现后可路由集合，与 runtime_view 同源）—
        # provider.candidates 里的非 routable 别名（如 glm-5-failover）
        # 不在 cloud_models，按构造不进目录
        pm = CatalogPM(cloud_models={
            "tathr-glm/glm-5": _make_cm(),
            "Qianfan-Token/deepseek-v4.1-flash": _make_cm(
                model_id="deepseek-v4.1-flash", provider="Qianfan-Token"),
        })
        cat = build_catalog(pm)
        assert [(e["provider"], e["model_id"]) for e in cat["cloud"]] == [
            ("Qianfan-Token", "deepseek-v4.1-flash"),
            ("tathr-glm", "glm-5"),
        ]   # 按 key 排序 — 输出稳定可测

    def test_cloud_entry_contract(self):
        pm = CatalogPM(cloud_models={
            "tathr-glm/glm-5": _make_cm()})
        entry = build_catalog(pm)["cloud"][0]
        assert set(entry) == {"provider", "model_id", "protocols", "features",
                              "active", "context_window"}
        assert entry["protocols"] == ["openai", "anthropic"]  # 来自发现结果
        assert entry["features"] == []
        assert entry["active"] is True   # 在 cloud_models = 已发现即可路由

    def test_cloud_capability_tri_state(self):
        # 7.8.0 三态推导：未设置/True → 无约束（[]）；显式 False → 排除该
        # 词，features 写剩余词表（规范序 tools,stream,vision,structured）
        pm = CatalogPM(cloud_models={
            "p/unset": _make_cm(model_id="unset", provider="p"),          # None/None
            "p/true": _make_cm(model_id="true", provider="p",
                               tools=True, vision=True),                   # True/True
            "p/no-tools": _make_cm(model_id="no-tools", provider="p",
                                   tools=False),                           # False/None
            "p/none": _make_cm(model_id="none", provider="p",
                               tools=False, vision=False),                 # False/False
        })
        by_id = {e["model_id"]: e for e in build_catalog(pm)["cloud"]}
        assert by_id["unset"]["features"] == []
        assert by_id["true"]["features"] == []
        assert by_id["no-tools"]["features"] == ["stream", "vision", "structured"]
        assert by_id["none"]["features"] == ["stream", "structured"]

    def test_cloud_anthropic_unavailable_openai_only(self):
        pm = CatalogPM(cloud_models={
            "prov/m": _make_cm(model_id="m", provider="prov", anthropic=False)})
        entry = build_catalog(pm)["cloud"][0]
        assert entry["protocols"] == ["openai"]

    def test_cloud_no_models_empty(self):
        cat = build_catalog(CatalogPM())
        assert cat == {"local": [], "cloud": []}


class TestCatalogRoute:
    def test_route_registered_in_get_routes(self):
        from inferfabric.proxy.handler import _GET_ROUTES
        assert "/admin/model-pools/catalog" in _GET_ROUTES

    def test_handler_sends_catalog(self):
        from inferfabric.model_pool import admin as mp_admin
        from tests.unit.model_pool.test_admin import AdminHandler
        pm = CatalogPM(model_dicts=[_llm()],
                       cloud_models={"prov/m": _make_cm(model_id="m",
                                                        provider="prov")})
        h = AdminHandler()
        mp_admin.handle_get_model_pools_catalog(h, pm)
        status, body = h.sent[-1]
        assert status == 200
        assert set(body) == {"local", "cloud"}
        assert body["local"][0]["name"] == "qwen-small"
        assert body["cloud"][0]["provider"] == "prov"

