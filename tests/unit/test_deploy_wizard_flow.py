"""一键链路集成 — 部署向导 Task 5（download → status → install → autostart）。

后端序列：POST /admin/deploy/download → GET status 轮询至 done →
POST /admin/packs/install（install_model 语义：装载不启动，Mac 裁决；
不走 /deploy——auto_deploy 装载即 switch）→ set-autostart。
already_configured 200 可继续；下载 error 中止序列不触发 install。
"""
import hashlib
import sys
import time
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import pytest

from inferfabric.proxy import deploy_download as dd
from inferfabric.proxy.handler import ProxyHandler

_PAYLOAD = b"GGUF-flow"
_SHA = hashlib.sha256(_PAYLOAD).hexdigest()


def _files_transport(files):
    def transport(url):
        return {"Code": 200, "Data": {"Files": files}}
    return transport


class FakeFetcher:
    def fetch(self, artifact, dest):
        dest.write_bytes(_PAYLOAD)


class BoomFetcher:
    def fetch(self, artifact, dest):
        raise TimeoutError("cdn cut mid-stream")


def _make_handler(command, path, body=None):
    h = ProxyHandler.__new__(ProxyHandler)
    h.command = command
    h.path = path
    h.headers = {}
    h._read_body = lambda: body
    h._sent = []

    def _send_json(data, status=200, extra_headers=None):
        h._sent.append((data, status))
    h._send_json = _send_json
    return h


def _last(h):
    assert h._sent, "no response sent"
    return h._sent[-1]


def _make_pm(tmp_path, monkeypatch, fetcher):
    """真实 ModelManager（Task 1 钉死的两层布局）+ 下载器 fake 注入。

    install_yaml 的 GGUF 查找目录 = Path(models_dir).parents[1]/"models"
    （Task 1 校准：models_dir=<tmp>/ws/models.d → 查找 <tmp>/models）。
    下载器落盘根 ws_root = parents[1] = <tmp> → <tmp>/models/<name>/，
    两个 glob 形态均命中。
    """
    models_d = tmp_path / "ws" / "models.d"
    models_d.mkdir(parents=True, exist_ok=True)
    ws_models = tmp_path / "models"
    ws_models.mkdir(parents=True, exist_ok=True)
    (ws_models / "m1.gguf").write_bytes(b"GGUF")   # install_yaml glob 命中

    from inferfabric.manager import ModelManager
    mgr = ModelManager(models_dir=str(models_d))
    reloads = []

    def _reload():
        # 热重载边界替身（沿 Task 1）：刷新 _models；set-autostart 契约
        # 需要 model.yaml_path（handler 读该字段做 YAML 行级编辑）
        reloads.append(1)
        mgr._models.update({"m1": SimpleNamespace(
            name="m1", yaml_path=str(models_d / "m1.yaml"))})
    monkeypatch.setattr(mgr, "reload_models", _reload)
    pm = SimpleNamespace(mgr=mgr)

    files = [{"Type": "blob", "Path": "m1-q4_k_m.gguf",
              "Size": len(_PAYLOAD), "Sha256": _SHA}]
    monkeypatch.setattr(dd, "_default_files_transport", _files_transport(files))
    import inferfabric.proxy.pack_handlers as ph
    monkeypatch.setattr(ph, "HttpArtifactFetcher", fetcher)
    return pm, reloads


def _wait_done(download_id, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        st = dd.download_status(download_id)
        if st["stage"] in ("done", "error"):
            return st
        time.sleep(0.01)
    raise AssertionError("download did not finish")


class TestOneClickFlow:
    def test_full_sequence_install_then_autostart(self, tmp_path, monkeypatch):
        pm, reloads = _make_pm(tmp_path, monkeypatch, FakeFetcher)
        # ① POST /admin/deploy/download（单变体免选 path）
        h = _make_handler("POST", "/admin/deploy/download",
                          {"model_id": "org/m1"})
        dd.handle_deploy_download(h, pm)
        data, status = _last(h)
        assert status == 200, data
        # ② status 轮询至 done
        st = _wait_done(data["download_id"])
        assert st["stage"] == "done"
        dest = tmp_path / "models" / "m1" / "m1-q4_k_m.gguf"
        assert dest.read_bytes() == _PAYLOAD
        # ③ POST /admin/packs/install（install_model：装载不启动）
        h = _make_handler("POST", "/admin/packs/install",
                          {"name": "m1", "type": "llama_cpp"})
        h._handle_pack_install(pm)
        data, status = _last(h)
        assert status == 200, data
        assert data["status"] == "installed"
        assert (Path(pm.mgr.models_dir) / "m1.yaml").exists()
        assert reloads == [1], "installed 必须热重载一次"
        # ④ set-autostart {name, enabled:true}
        h = _make_handler("POST", "/admin/models/set-autostart",
                          {"name": "m1", "enabled": True})
        h._handle_set_autostart(pm)
        data, status = _last(h)
        assert status == 200, data

    def test_second_install_is_already_configured_200(self, tmp_path, monkeypatch):
        pm, _ = _make_pm(tmp_path, monkeypatch, FakeFetcher)
        h = _make_handler("POST", "/admin/packs/install",
                          {"name": "m1", "type": "llama_cpp"})
        h._handle_pack_install(pm)
        assert _last(h)[1] == 200
        h = _make_handler("POST", "/admin/packs/install",
                          {"name": "m1", "type": "llama_cpp"})
        h._handle_pack_install(pm)
        data, status = _last(h)
        assert status == 200
        assert data["status"] == "already_configured"

    def test_download_error_aborts_before_install(self, tmp_path, monkeypatch):
        pm, reloads = _make_pm(tmp_path, monkeypatch, BoomFetcher)
        h = _make_handler("POST", "/admin/deploy/download",
                          {"model_id": "org/m1"})
        dd.handle_deploy_download(h, pm)
        data, status = _last(h)
        assert status == 200
        st = _wait_done(data["download_id"])
        assert st["stage"] == "error"
        assert "cdn cut" in st["error"]
        # 前端契约：仅 stage===done 触发装载（error 先行 return）
        js = (_ROOT / "inferfabric" / "dashboard" / "js" / "wizard.js"
              ).read_text(encoding="utf-8")
        assert "if (j.stage === 'done') { _installModel(); return; }" in js
        assert "if (j.stage === 'error') { _finishOneClick(false" in js
        assert reloads == [], "下载失败不得触发装载/重载"
