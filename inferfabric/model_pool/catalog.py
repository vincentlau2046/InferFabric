"""inferfabric/model_pool/catalog.py — 池候选目录（v7.1.2）。

目录回答的域问题是「池候选可以引用什么」。此前前端自行刮 /models 与
/admin/cloud/providers 逆向猜 schema，字段一变即静默断（云目录曾因此
恒空）；现由后端一次性构建权威视图，前端只消费。

语义：
  - local  = models.d 中 model_type=='llm' 的模型（chat 池；TTS/ASR/
    embedding 不进 chat 池 — 选了也永远不可用）。条目名 = served_name
    （池候选引用 served_name，dispatch 评审 #15）。
  - cloud  = cloud_models（发现后可路由集合）— 与 runtime_view 同源，
    「目录里出现的 ⇔ dispatch 能路由到的」按构造一致。provider.candidates
    里的非 routable 别名（如 glm-5-failover）按构造不进目录。
  - protocols：云来自发现结果（openai/anthropic available），本地按引擎
    映射声明（declared 态 — 证据系统区分 declared/probed，语义不变）。

可扩展性：本响应是运营视图唯一的「候选元数据总线」。Phase 2/3 只做
加法 — 条目可追加 latency/degraded/evidence 等字段，前端选择键编码
（'local:<name>' / 'cloud:<provider>/<model_id>'）不变。多模态池未来
经 ?kind= 参数支持。
"""

import logging

log = logging.getLogger("inferfabric.model_pool.catalog")

# 本地引擎 → 声明协议。vllm/ninfer 有本地 anthropic 路径（forward_anthropic_local
# 直通引擎原生 /v1/messages，不翻译）→ 双协议声明；ninfer 有 7 天线上实证
# （6615 TXT + 3483 VL 次 /v1/messages → LOCAL，2026-10-28 池 bug #1）。
# 其余引擎保守只声明 openai — 声明可被证据系统探测推翻/证实，宁可少声明不虚报。
_LOCAL_PROTOCOLS: dict[str, list[str]] = {
    "vllm": ["openai", "anthropic"],
    "ninfer": ["openai", "anthropic"],
}


def local_protocols_for_engine(engine: str) -> list[str]:
    """引擎的本地协议声明（目录与 registry 存量自愈共用同一映射源）。"""
    return list(_LOCAL_PROTOCOLS.get(engine, ["openai"]))


def build_catalog(pm) -> dict:
    """构建候选目录（纯读，零副作用，不出网）。"""
    return {"local": _local_entries(pm), "cloud": _cloud_entries(pm)}


def _local_entries(pm) -> list[dict]:
    mgr = getattr(pm, "mgr", None)
    list_models = getattr(mgr, "list_models", None)
    if list_models is None:
        return []
    try:
        models = list_models() or []
    except Exception as e:   # 目录降级 — 不阻塞页面
        log.warning("catalog: list_models failed: %s", e)
        return []
    get_model = getattr(mgr, "get_model", None)
    out = []
    for m in models:
        if not isinstance(m, dict) or m.get("model_type") != "llm":
            continue
        name = m.get("name")
        if not name:
            continue
        served = name
        if get_model is not None:
            try:
                mc = get_model(name)
                served = (getattr(mc, "served_name", None) or name) \
                    if mc is not None else name
            except Exception:
                served = name
        engine = m.get("type") or ""
        out.append({
            "name": served,
            "active": bool(m.get("active")),
            "engine": engine,
            "modality": m.get("modality"),
            "context_window": m.get("context_window"),
            "protocols": local_protocols_for_engine(engine),
        })
    return out


def _cloud_entries(pm) -> list[dict]:
    cloud = getattr(pm, "cloud", None)
    models = getattr(cloud, "cloud_models", None) or {}
    out = []
    for key in sorted(models):
        provider, _, model_id = key.partition("/")
        if not provider or not model_id:
            continue
        cm = models[key]
        protocols = [p for p, ok in (
            ("openai", getattr(cm, "openai_available", True)),
            ("anthropic", getattr(cm, "anthropic_available", False)),
        ) if ok]
        if not protocols:
            protocols = ["openai"]   # 至少声明 openai（无可用标志 = 老条目）
        features = []
        if getattr(cm, "supports_tools", False):
            features.append("tools")
        if getattr(cm, "supports_vision", False):
            features.append("vision")
        out.append({
            "provider": provider,
            "model_id": model_id,
            "protocols": protocols,
            "features": features,
            "active": True,   # 在 cloud_models = 已发现即可路由
            "context_window": (getattr(cm, "context_window", None)
                               or getattr(cm, "contextWindow", None)),
        })
    return out
