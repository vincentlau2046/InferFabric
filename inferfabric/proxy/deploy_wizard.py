"""部署向导后端 — 能力×引擎×平台收窄（GET /admin/deploy/options）。

收窄在 Python 侧完成且不可绕过：向导内不出被收窄掉的选项。
- darwin/arm64 → llama_cpp（Metal 全卸载，2026-11 裁决）
- linux + nvidia-smi → vllm/sglang/llama_cpp
- 其余（含 nvidia-smi 探测失败）→ llama_cpp（Review Focus 2：探测失真
  按「无 NVIDIA」收窄，绝不出 vllm 选项）

引擎可用性仅探测不安装（packs 体系只作探测与 deep-link，不扩范围）：
llama_cpp 查 packs 可复用引擎或 PATH 上的 llama-server；vllm/sglang 查
PATH 二进制。
"""
import json
import platform
import re
import shutil
import subprocess
import urllib.request

ENGINE_LABELS = {
    "llama_cpp": "llama.cpp",
    "vllm": "vLLM",
    "sglang": "SGLang",
}
CAPABILITIES = ["llm", "embedding", "tts", "asr", "ocr"]
# 部署方式沿用引擎既有的装载通道：llama_cpp 走本地 GGUF 或 packs 目录，
# vllm/sglang 只支持本地目录（权重经下载器落盘后由 local-dir 装载）
DEPLOYMENT_MODES = {
    "llama_cpp": ["local-gguf", "packs-catalog"],
    "vllm": ["local-dir"],
    "sglang": ["local-dir"],
}


def _default_engine_available(engine_id: str) -> bool:
    """真实探测（生产默认；测试经 engine_available 注入）。"""
    if engine_id == "llama_cpp":
        try:
            from inferfabric.packs.custom import find_reusable_engine
            from inferfabric.proxy.pack_handlers import get_pack_manager
            if find_reusable_engine(get_pack_manager().root) is not None:
                return True
        except Exception:
            pass
        return shutil.which("llama-server") is not None
    return shutil.which(engine_id) is not None


def narrowed_engines(os_name: str, has_nvidia: bool | None,
                     engine_available=None) -> list[dict]:
    """按平台收窄引擎清单；has_nvidia=None（探测失败）按无 NVIDIA。"""
    if engine_available is None:
        engine_available = _default_engine_available
    if os_name == "darwin":
        ids = ["llama_cpp"]
    elif has_nvidia is True:
        ids = ["vllm", "sglang", "llama_cpp"]
    else:
        ids = ["llama_cpp"]
    return [{"id": eid, "label": ENGINE_LABELS[eid],
             "available": bool(engine_available(eid))} for eid in ids]


def _probe_nvidia() -> bool:
    """评审 F7：nvidia-smi 在 PATH ≠ 有可用 GPU（容器常见）。

    以有界 `nvidia-smi -L` 探测设备清单：二进制缺失/超时/非零退出/
    空清单一律按无 NVIDIA 收窄（绝不出 vllm 选项）。
    """
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True,
                           text=True, timeout=3)
    except Exception:
        return False
    if r.returncode != 0:
        return False
    return any(ln.strip().startswith("GPU")
               for ln in (r.stdout or "").splitlines())


def deploy_options(os_name: str | None = None, arch: str | None = None,
                   has_nvidia: bool | None = None,
                   engine_available=None) -> dict:
    """向导首屏选项（纯函数；默认从真实平台探测，参数注入供测试）。"""
    if os_name is None:
        os_name = platform.system().lower()
    if arch is None:
        arch = platform.machine().lower()
    if has_nvidia is None:
        has_nvidia = _probe_nvidia()
    return {
        "capabilities": list(CAPABILITIES),
        "engines": narrowed_engines(os_name, has_nvidia, engine_available),
        "platform": {"os": os_name, "arch": arch, "nvidia": has_nvidia},
        "deployment_modes": {k: list(v) for k, v in DEPLOYMENT_MODES.items()},
    }


# ─── ModelScope 搜索代理（GET /admin/deploy/search）──────────────────────
# 线上校准（2026-11-21，真实 curl）：搜索为 PUT（POST 404）
#   PUT https://modelscope.cn/api/v1/dolphin/models
#   {"Name": q, "PageSize": N, "PageNumber": P}
#   → Data.Model.Models[]{Path, Name, Downloads, Pipelines, ...}
#     + Data.Model.TotalCount
MODELSCOPE_SEARCH_URL = "https://modelscope.cn/api/v1/dolphin/models"
SEARCH_TIMEOUT = 5            # 网络对面可能变动/被限流 → 超时兜底
SEARCH_PAGE_SIZE = 20
SEARCH_MAX_PAGE = 100
# pull_model 白名单正则同形态（Review Focus 4）：id 允许字母数字._-:/，
# 拒绝路径穿越（..）——搜索归一化层据此跳过畸形/恶意条目
_SEARCH_ID_RE = re.compile(r'^(?!.*\.\.)[\w.\-:/]+$')


def _default_search_transport(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="PUT",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def search_models(transport=None, q: str = "", capability: str | None = None,
                  page: int = 1) -> dict:
    """ModelScope 搜索代理：归一化 + 错误归一，前端不因半截 JSON 崩。

    transport 注入 `(url, payload) -> dict`（测试密闭化；默认 urllib PUT）。
    错误形态 {"error", "code"}：invalid_request（本地参数）|
    search_failed（网络/响应畸形）。
    """
    if transport is None:
        transport = _default_search_transport
    q = (q or "").strip()
    if not q or ".." in q or len(q) > 200:
        return {"error": "invalid query", "code": "invalid_request"}
    if not isinstance(page, int) or page < 1 or page > SEARCH_MAX_PAGE:
        return {"error": "invalid page", "code": "invalid_request"}
    payload = {"Name": q, "PageSize": SEARCH_PAGE_SIZE, "PageNumber": page}
    try:
        raw = transport(MODELSCOPE_SEARCH_URL, payload)
        if not isinstance(raw, dict):
            raise ValueError("response is not an object")
        data = raw.get("Data")
        if not isinstance(data, dict):
            raise ValueError("Data missing")
        bucket = data.get("Model")
        if not isinstance(bucket, dict):
            raise ValueError("Model bucket missing")
        models_raw = bucket.get("Models")
        if not isinstance(models_raw, list):
            raise ValueError("Models is not a list")
        total = int(bucket.get("TotalCount") or 0)
    except Exception as e:  # 网络/超时/畸形响应统一归一（不崩、不透传半截 JSON）
        return {"error": f"search_failed: {e}", "code": "search_failed"}
    models = []
    for m in models_raw:
        try:
            models.append(_normalize_row(m))
        except Exception:
            continue      # 评审 F6：单行 schema 垃圾跳过，整页不得崩
    return {"models": models, "total": total}


def _normalize_row(m) -> dict:
    if not isinstance(m, dict):
        raise ValueError("row is not an object")
    path, name = m.get("Path"), m.get("Name")
    if not path or not name:
        raise ValueError("row missing Path/Name")
    mid = f"{path}/{name}"
    # ModelScope id 形态恒为 org/name 两段：Path 段含 / 或整体不过
    # pull_model 白名单形态的条目跳过（Review Focus 4）
    if "/" in str(path) or not _SEARCH_ID_RE.match(mid):
        raise ValueError("row id rejected")
    pipelines = m.get("Pipelines") or []
    blob = f"{name} {' '.join(map(str, pipelines))}".lower()
    return {
        "id": mid,
        "name": name,
        "downloads": int(m.get("Downloads") or 0),
        # 能力猜测：仅 UI 提示，最终能力由用户在步骤①声明
        "capability_guess": "embedding" if "embed" in blob else "llm",
        "pipeline": pipelines[0] if pipelines else None,
    }


def handle_deploy_search(handler, pm) -> None:
    """GET /admin/deploy/search?q=&page= — _admin 门内调用（handler.py 路由）。"""
    from urllib.parse import urlparse, parse_qs
    try:
        qs = parse_qs(urlparse(handler.path).query)
        q = qs.get("q", [""])[0]
        page = int(qs.get("page", ["1"])[0])
    except Exception:
        handler._send_json({"error": "invalid request",
                            "code": "invalid_request"}, 400,
                           extra_headers={"Cache-Control": "no-store"})
        return
    result = search_models(q=q, page=page)
    # 评审 F6：错误必须走非 2xx 契约 —— 200+{error} 会被前端当「无结果」
    if result.get("code") == "invalid_request":
        status = 400
    elif result.get("code") == "search_failed":
        status = 502
    else:
        status = 200
    handler._send_json(result, status,
                       extra_headers={"Cache-Control": "no-store"})
