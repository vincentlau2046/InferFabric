"""unit/proxy/test_cloud_alias_routing.py — PR2 Task 3 路由解析（别名优先、不回退）。

契约（spec §3 PR2 / plan Task 3）：
- 调用别名精确解析到目标 provider 的注册表条目；上游收到原始 model_id
  （forward_to_cloud 既有 override，零改动）；
- 目标未激活（停用/撤销/删除）→ 显式失败（404 + explain_unknown 上下文），
  绝不回退到本地/另一 provider/池；
- /v1/models 公布当前可调用的显式别名（owned_by: iff-alias，无重复 id）；
- 旧裸名 / provider 前缀 / 池行为不变。

harness：真 CloudDiscovery + 两个同 model_id 的本地 mock 上游 + 真
chat_handlers.handle_chat / ProxyHandler._handle_v1_models。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import yaml

from inferfabric.cloud_discovery import CloudDiscovery
from inferfabric.proxy import chat_handlers
from inferfabric.proxy.handler import ProxyHandler


# ── mock 上游：记录 POST path/headers/body ────────────────────────────

class _RecHandler(BaseHTTPRequestHandler):
    REQS = []  # 类级共享由测试自行隔离（每测试新建 server + 列表）

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).REQS.append({
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "body": body.decode("utf-8"),
        })
        resp = json.dumps({
            "id": "cmpl-x", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a):
        pass


def _mk_upstream(reqs):
    cls = type("_Rec", (_RecHandler,), {"REQS": reqs})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


# ── 组装：双 provider 同 model_id + 别名绑 B ──────────────────────────

class _WFile:
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b

    def flush(self):
        pass


class _FakeClient:
    """chat 直连的 handler 桩：_send_json 捕获 + wfile 供 forwarder 写流。"""

    def __init__(self):
        self.wfile = _WFile()
        self.status = None
        self.sent_headers = {}
        self.sent = []
        self.headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.sent_headers[k] = str(v)

    def end_headers(self):
        pass

    def _send_json(self, data, status=200, extra_headers=None):
        self.sent.append((data, status))


def _write_yaml(path, data):
    path.write_text(yaml.dump(data, allow_unicode=True))


def _mk_cd(tmp_path, base_a, base_b, *, aliases_b=None, enabled_b=True):
    p = tmp_path / "cloud_provider.yaml"
    _write_yaml(p, {"providers": {
        "prov-a": {"api_key": "sk-a", "openai_base": base_a,
                   "enabled_models": ["glm-5"]},
        "prov-b": {"api_key": "sk-b", "openai_base": base_b,
                   "enabled_models": ["glm-5"], "enabled": enabled_b,
                   "aliases": aliases_b or {}},
    }})
    return CloudDiscovery(p), p


def _mk_pm(cd, logged, captured):
    return SimpleNamespace(
        new_request_id=lambda: "rid",
        auth=SimpleNamespace(enabled=False),
        model_to_service=lambda m: None,
        get_target_port=lambda m: None,
        ensure_cloud_discovered=lambda: None,
        mgr=SimpleNamespace(_models={}, active_services=[]),
        cloud=cd,
        logger=SimpleNamespace(log=lambda e: logged.append(e)),
        anomalies=SimpleNamespace(record=lambda e: captured.append(e)),
        pool_registry=None,
        agent_registry=None,
        _runtime_config={},
    )


# ── 测试 ──────────────────────────────────────────────────────────────

def test_alias_resolves_to_target_provider(tmp_path):
    """别名 glm-5-b → B 收到请求且 body model=glm-5（原始 id，非别名）；A 零请求。"""
    srv_a, base_a = _mk_upstream([])
    reqs_b = []
    srv_b, base_b = _mk_upstream(reqs_b)
    try:
        cd, _p = _mk_cd(tmp_path, base_a, base_b, aliases_b={"glm-5-b": "glm-5"})
        logged, captured = [], []
        pm = _mk_pm(cd, logged, captured)
        h = _FakeClient()
        chat_handlers.handle_chat(h, pm, {
            "model": "glm-5-b", "temperature": 1,
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert not h.sent, h.sent            # 未走 404/错误分支
        assert h.status == 200, h.status
        assert reqs_b, "B 未收到请求"
        assert not srv_a.__dict__.get("_reqs_seen"), "A 不应收到任何请求"
        body = json.loads(reqs_b[-1]["body"])
        assert body["model"] == "glm-5", body["model"]      # 原始 id，非别名
        assert reqs_b[-1]["path"].endswith("/chat/completions")
        assert reqs_b[-1]["auth"] == "Bearer sk-b"
    finally:
        srv_a.shutdown()
        srv_b.shutdown()


def test_alias_inactive_no_fallback(tmp_path):
    """B 停用 → 404 且错误含别名上下文；A、B 均零请求（不回退不串路由）。"""
    reqs_a, reqs_b = [], []
    srv_a, base_a = _mk_upstream(reqs_a)
    srv_b, base_b = _mk_upstream(reqs_b)
    try:
        cd, p = _mk_cd(tmp_path, base_a, base_b, aliases_b={"glm-5-b": "glm-5"})
        # 停用 B（写盘 + reload 重建注册表 — 与生产 provider-update 停用同路径）
        cd.providers["prov-b"].enabled = False
        cd.save_config()
        cd.reload()
        logged, captured = [], []
        pm = _mk_pm(cd, logged, captured)
        h = _FakeClient()
        chat_handlers.handle_chat(h, pm, {
            "model": "glm-5-b", "temperature": 1,
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert h.sent and h.sent[-1][1] == 404, h.sent
        err = h.sent[-1][0]["error"]
        assert "glm-5-b" in err and "prov-b" in err, err     # 别名未激活上下文
        assert reqs_a == [] and reqs_b == [], (reqs_a, reqs_b)
    finally:
        srv_a.shutdown()
        srv_b.shutdown()


def test_v1_models_lists_active_alias_only(tmp_path):
    """/v1/models 公布激活别名（iff-alias、无重复）；目标停用后不再公布。"""
    srv_a, base_a = _mk_upstream([])
    srv_b, base_b = _mk_upstream([])
    try:
        cd, _p = _mk_cd(tmp_path, base_a, base_b,
                        aliases_b={"glm-5-b": "glm-5", "dup-name": "glm-5"})
        logged, captured = [], []
        pm = _mk_pm(cd, logged, captured)

        def _v1_models_client():
            h = ProxyHandler.__new__(ProxyHandler)
            h.sent = []
            h._send_json = lambda data, status=200, extra_headers=None: h.sent.append((data, status))
            return h

        h = _v1_models_client()
        h._handle_v1_models(pm)
        data = h.sent[0][0]["data"]
        alias_entries = [m for m in data if m["id"] in ("glm-5-b", "dup-name")]
        assert {(m["id"], m["owned_by"]) for m in alias_entries} == \
            {("glm-5-b", "iff-alias"), ("dup-name", "iff-alias")}
        assert len([m for m in data if m["id"] == "glm-5-b"]) == 1  # 无重复

        # B 停用 → 别名不再公布（不可调用的别名不宣称可用）
        cd.providers["prov-b"].enabled = False
        cd.save_config()
        cd.reload()
        h2 = _v1_models_client()
        h2._handle_v1_models(pm)
        data2 = h2.sent[0][0]["data"]
        assert not [m for m in data2 if m["id"] in ("glm-5-b", "dup-name")]
    finally:
        srv_a.shutdown()
        srv_b.shutdown()


def test_bare_name_and_pool_behavior_unchanged(tmp_path):
    """旧裸名（短名 first-wins）/ provider 前缀路由不受别名影响。"""
    reqs_a, reqs_b = [], []
    srv_a, base_a = _mk_upstream(reqs_a)
    srv_b, base_b = _mk_upstream(reqs_b)
    try:
        cd, _p = _mk_cd(tmp_path, base_a, base_b, aliases_b={"glm-5-b": "glm-5"})
        logged, captured = [], []
        pm = _mk_pm(cd, logged, captured)
        # 注册表层：短名 first-wins → prov-a；显式前缀 → prov-b
        assert cd.resolve_route("glm-5", local_models=set()) == "cloud:prov-a"
        assert cd.resolve_route("prov-b/glm-5", local_models=set()) == "cloud:prov-b"
        assert cd.resolve_route("totally-unknown", local_models=set()) is None
        # 裸名 chat → A 收到（B 零请求）— 别名不劫持既有调用名
        h = _FakeClient()
        chat_handlers.handle_chat(h, pm, {
            "model": "glm-5", "temperature": 1,
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert h.status == 200 and not h.sent
        assert len(reqs_a) == 1 and reqs_b == [], (reqs_a, reqs_b)
        assert json.loads(reqs_a[0]["body"])["model"] == "glm-5"
    finally:
        srv_a.shutdown()
        srv_b.shutdown()


def test_resolve_cloud_model_accepts_alias(tmp_path):
    """二段解析识别别名：prov-b + glm-5-b → 目标注册表条目（model_id=glm-5）。"""
    srv_a, base_a = _mk_upstream([])
    srv_b, base_b = _mk_upstream([])
    try:
        cd, _p = _mk_cd(tmp_path, base_a, base_b, aliases_b={"glm-5-b": "glm-5"})
        cm = cd.resolve_cloud_model("prov-b", "glm-5-b")
        assert cm is not None and cm.model_id == "glm-5" and cm.provider == "prov-b"
        # 别名绑定其他 provider 时不翻译（不串路由）
        assert cd.resolve_cloud_model("prov-a", "glm-5-b") is None
    finally:
        srv_a.shutdown()
        srv_b.shutdown()


def test_explain_unknown_contexts(tmp_path):
    """explain_unknown：停用 → 提供方停用上下文；撤销 → 模型不可路由上下文。"""
    srv_a, base_a = _mk_upstream([])
    srv_b, base_b = _mk_upstream([])
    try:
        cd, _p = _mk_cd(tmp_path, base_a, base_b, aliases_b={"glm-5-b": "glm-5"})
        assert cd.explain_unknown("no-such-alias") is None       # 非别名 → None
        assert cd.explain_unknown("glm-5-b") is None             # 激活别名 → None
        cd.providers["prov-b"].enabled = False
        cd.save_config()
        cd.reload()
        msg = cd.explain_unknown("glm-5-b")
        assert msg and "glm-5-b" in msg and "prov-b" in msg and "停用" in msg
    finally:
        srv_a.shutdown()
        srv_b.shutdown()
