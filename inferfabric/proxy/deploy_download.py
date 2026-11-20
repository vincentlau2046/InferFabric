"""ModelScope GGUF 下载器 — 部署向导 Task 3b（2026-11-21 用户裁决 B）。

install 语义不含权重下载（与 fork 一致），一键流必须先落盘 GGUF：
校验 → files API 清单（只留 .gguf，多量化变体须选 path）→ daemon 线程
逐文件 HttpArtifactFetcher fetch（Range 断点续传）→ sha256 校验 → done。

落盘目录 <ws_root>/models/<model_name>/：install_yaml（macOS llama_cpp
分支）的第二个 glob `**/{name}/*.gguf` 命中该目录。

线上校准（2026-11-21，真实 curl）：
- 文件列表 GET https://modelscope.cn/api/v1/models/{org}/{name}/repo/files
  ?Revision=master&PageSize=200 → Data.Files[] blob{Path, Size, Sha256}
- 下载 GET https://modelscope.cn/models/{org}/{name}/resolve/master/{Path}
  → 302 至 CDN，支持 Range（fetcher 的 _NoDowngradeRedirectHandler 兜底）
"""
import hashlib
import json
import re
import threading
import time
import urllib.request
import uuid
from collections import namedtuple
from pathlib import Path

FILES_TIMEOUT = 15           # 列表走主 API（非 CDN），给足重分页延迟
FILES_PAGE_SIZE = 200
# ModelScope id 恒为 org/name 两段；组织/模型名不含 /、空白（Review Focus 4 同源）
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
# split 分片名：xxx-00001-of-00004.gguf（llama.cpp 需整套，按家族归并）
_SPLIT_RE = re.compile(r"^(?P<base>.+)-(?P<idx>\d{5})-of-(?P<total>\d{5})\.gguf$")

_Status = namedtuple("_Status", "stage model_id files_total files_done "
                                "current_file error dest_dir")

# 状态注册表（_PACK_DOWNLOADS 同款：模块级 dict + 锁；daemon 线程写）
_DOWNLOADS: dict[str, dict] = {}
_LOCK = threading.Lock()


def normalize_model_id(raw: str) -> str:
    """org/name 白名单校验；违规抛 ValueError（消息面向前端可直接展示）。"""
    raw = str(raw or "").strip()
    if ".." in raw or not _MODEL_ID_RE.match(raw):
        raise ValueError(f"invalid model_id: {raw!r} — 需为 org/name 形态")
    return raw


def _default_files_transport(url: str) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=FILES_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_gguf_files(transport, model_id: str) -> list[dict]:
    """files API 归一化：只留 .gguf blob（拒子目录/穿越条目），畸形响应抛
    ValueError（上层归一为 400/500 可见错误，不静默空清单）。"""
    url = (f"https://modelscope.cn/api/v1/models/{model_id}/repo/files"
           f"?Revision=master&PageSize={FILES_PAGE_SIZE}")
    try:
        raw = transport(url)
        files = raw["Data"]["Files"]
        if not isinstance(files, list):
            raise ValueError("Files is not a list")
    except Exception as e:
        raise ValueError(f"listing_failed: {e}") from e
    out = []
    for f in files:
        if not isinstance(f, dict) or f.get("Type") != "blob":
            continue
        path = str(f.get("Path") or "")
        if not path.lower().endswith(".gguf"):
            continue
        if "/" in path or ".." in path:  # 平坦落盘；拒穿越/子目录
            continue
        out.append({"path": path, "size": int(f.get("Size") or 0),
                    "sha256": str(f.get("Sha256") or "")})
    return out


def select_files(files: list[dict], chosen_path: str | None) -> list[str]:
    """多量化变体 repo 必须显式选文件；选中 split 第一分片 → 同家族全部分片。

    只下单个变体而非全 repo（Qwen 系 repo 全部 .gguf 可达数十 GB）。
    """
    paths = [f["path"] for f in files]
    if chosen_path is None:
        if len(paths) == 1:
            return paths
        raise ValueError("multiple gguf variants — 需指定 path "
                         f"（可选: {', '.join(paths)}）")
    if chosen_path not in paths:
        raise ValueError(f"unknown gguf path: {chosen_path!r}")
    m = _SPLIT_RE.match(chosen_path)
    if not m:
        return [chosen_path]
    prefix = m.group("base")
    family = [p for p in paths
              if p == chosen_path or _SPLIT_RE.match(p)
              and _SPLIT_RE.match(p).group("base") == prefix]
    # 分片序保证（-NNNNN-of- 决定字典序即序）
    return sorted(family)


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(download_id: str, model_id: str, sources: list[dict],
         dest_dir: Path, fetcher) -> None:
    """daemon 线程体：逐文件 .part 落盘 → rename → sha256 校验。"""
    done = 0
    try:
        for item in sources:
            with _LOCK:
                _DOWNLOADS[download_id].update(stage="downloading",
                                               current_file=item["path"])
            dest = dest_dir / item["path"]
            part = dest.with_suffix(dest.suffix + ".part")
            url = (f"https://modelscope.cn/models/{model_id}"
                   f"/resolve/master/{item['path']}")
            fetcher.fetch(namedtuple("_A", "source")(url), part)
            if item["sha256"]:
                got = _sha256_of(part)
                if got != item["sha256"].lower():
                    part.unlink(missing_ok=True)
                    raise ValueError(
                        f"sha256 mismatch for {item['path']}: "
                        f"expected {item['sha256']}, got {got}")
            part.replace(dest)
            done += 1
            with _LOCK:
                _DOWNLOADS[download_id]["files_done"] = done
        with _LOCK:
            _DOWNLOADS[download_id].update(stage="done", current_file=None)
    except Exception as e:
        with _LOCK:
            _DOWNLOADS[download_id].update(stage="error", error=str(e))


def start_download(model_id: str, ws_root, fetcher=None, transport=None,
                   path: str | None = None) -> dict:
    """启动 ModelScope GGUF 下载（同步返回清单 + download_id，线程落盘）。

    参数校验/清单/选择错误抛 ValueError（端点归一 400）；网络内错误在线程
    状态里可见（stage=error，fail-visible）。
    """
    model_id = normalize_model_id(model_id)
    if transport is None:
        transport = _default_files_transport
    files = list_gguf_files(transport, model_id)
    if not files:
        raise ValueError(f"no_gguf_files: {model_id} 仓库无 .gguf 文件")
    chosen = select_files(files, path)
    name = model_id.split("/")[-1]
    dest_dir = Path(ws_root) / "models" / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    by_path = {f["path"]: f for f in files}
    sources = [by_path[p] for p in chosen]
    total_bytes = sum(f["size"] for f in sources)
    download_id = uuid.uuid4().hex[:12]
    with _LOCK:
        _DOWNLOADS[download_id] = {
            "stage": "listing", "model_id": model_id,
            "files_total": len(sources), "files_done": 0,
            "current_file": None, "error": None,
            "dest_dir": str(dest_dir),
        }
    if fetcher is None:
        from inferfabric.proxy.pack_handlers import HttpArtifactFetcher
        fetcher = HttpArtifactFetcher()
    threading.Thread(target=_run, args=(download_id, model_id, sources,
                                        dest_dir, fetcher),
                     daemon=True, name=f"deploy-dl-{download_id}").start()
    return {"download_id": download_id, "model_id": model_id,
            "dest_dir": str(dest_dir), "total_bytes": total_bytes,
            "files": [{"path": f["path"], "size": f["size"],
                       "source": f"https://modelscope.cn/models/{model_id}"
                                 f"/resolve/master/{f['path']}"} for f in sources]}


def download_status(download_id: str) -> dict | None:
    with _LOCK:
        rec = _DOWNLOADS.get(str(download_id or ""))
        return dict(rec) if rec else None


def handle_deploy_download(handler, pm) -> None:
    """POST /admin/deploy/download {model_id, path?} — _admin 门内调用。

    path 缺省且 repo 有多个量化变体 → 400（error 内含可选清单，前端据此
    渲染变体选择）；listing 网络失败 → 502；其余 ValueError → 400。
    """
    body = handler._read_body()
    if not isinstance(body, dict):
        handler._send_json({"error": "body must be a JSON object"}, 400,
                           extra_headers={"Cache-Control": "no-store"})
        return
    try:
        # 落盘根 = <workspace>（models_dir=<ws>/models.d，与 install_yaml 的
        # parents[1] 语义一致）→ <ws>/models/<name>/*.gguf 命中其 glob
        ws_root = Path(pm.mgr.models_dir).parents[1]
        result = start_download(body.get("model_id", ""), ws_root,
                                path=body.get("path"))
    except ValueError as e:
        code = 502 if str(e).startswith("listing_failed") else 400
        handler._send_json({"error": str(e)}, code,
                           extra_headers={"Cache-Control": "no-store"})
        return
    handler._send_json(result, 200,
                       extra_headers={"Cache-Control": "no-store"})


def handle_deploy_download_status(handler, pm) -> None:
    """GET /admin/deploy/download/status?download_id= — _admin 门内调用。"""
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(handler.path).query)
    download_id = qs.get("download_id", [""])[0]
    st = download_status(download_id)
    if st is None:
        handler._send_json({"error": f"unknown download_id: {download_id!r}"},
                           404, extra_headers={"Cache-Control": "no-store"})
        return
    handler._send_json(st, 200,
                       extra_headers={"Cache-Control": "no-store"})
