"""
unit/proxy/test_cloud_provider_update.py — PR1 云提供方原地编辑

Task 1: GET /admin/cloud/providers 每项新增 display_name / config_fingerprint / key_source
        （managed = secrets.env 有该 var ｜ external = 仅 os.environ 有 ｜ unset）
Task 2+: POST /admin/cloud/provider-update（非凭证字段 + 配置指纹 CAS）。
        凭证/端点变更的服务端真实探针门禁（key_action）。

harness 照 test_cloud_probe.py：ProxyHandler.__new__ + stub _read_body/_send_json
+ 真 CloudDiscovery(tmp_path)；secrets 用 tmp secrets.env 隔离（cd._secrets_mgr）。
"""
import json
from types import SimpleNamespace

import yaml

from inferfabric.cloud_discovery import CloudDiscovery, SecretsManager
from inferfabric.proxy.handler import ProxyHandler


def _write_yaml(path, data):
    with open(path, "w") as f:
        yaml.dump(data, f)


def _mk_handler(cd, command="GET", body=b""):
    """ProxyHandler.__new__ + stub，返回 (handler, sent)。"""
    h = ProxyHandler.__new__(ProxyHandler)
    h.command = command
    sent = []
    h._send_json = lambda data, status=200, extra_headers=None: sent.append((data, status))
    h._read_body = lambda: json.loads(body.decode("utf-8")) if body else {}
    return h, sent


def _mk_cd(tmp_path, providers_data, secrets_vars=(), env_vars=(), monkeypatch=None):
    """真 CloudDiscovery 指向 tmp YAML + tmp secrets.env（隔离注入源）。"""
    p = tmp_path / "cloud_provider.yaml"
    _write_yaml(p, {"providers": providers_data})
    cd = CloudDiscovery(p)
    sm = SecretsManager(tmp_path / "secrets.env")
    for v in secrets_vars:
        sm.write(v, "sk-managed-test-value")
    cd._secrets_mgr = sm
    if monkeypatch is not None:
        for v in env_vars:
            monkeypatch.setenv(v, "sk-external-test-value")
        # 未显式注入的测试 var 一律清出 os.environ（防止外部环境残留干扰三态判定）
        for v in ("IFF_TEST_GS_A", "IFF_TEST_GS_B", "IFF_TEST_GS_C"):
            if v not in env_vars:
                monkeypatch.delenv(v, raising=False)
    return cd, p


# ── Task 1: GET providers 新增编辑三字段 ─────────────────────────

class TestGetProvidersEditFields:
    def test_get_exposes_display_name_fingerprint_key_source(self, tmp_path, monkeypatch):
        cd, _p = _mk_cd(tmp_path, {
            "prov-a": {"openai_base": "http://a", "display_name": "Alpha",
                       "key_env_var": "IFF_TEST_GS_A"},
            "prov-b": {"openai_base": "http://b", "key_env_var": "IFF_TEST_GS_B"},
            "prov-c": {"openai_base": "http://c", "key_env_var": "IFF_TEST_GS_C"},
        }, secrets_vars=["IFF_TEST_GS_A"], env_vars=["IFF_TEST_GS_B"],
            monkeypatch=monkeypatch)
        h, sent = _mk_handler(cd)
        h._handle_cloud_providers(SimpleNamespace(cloud=cd))
        assert sent and sent[0][1] == 200
        by_name = {pr["name"]: pr for pr in sent[0][0]["providers"]}
        # display_name：设置与默认
        assert by_name["prov-a"]["display_name"] == "Alpha"
        assert by_name["prov-b"]["display_name"] == ""
        # config_fingerprint：非 none、16 位
        fp = by_name["prov-a"]["config_fingerprint"]
        assert fp != "none" and len(fp) == 16
        # key_source 三态
        assert by_name["prov-a"]["key_source"] == "managed"
        assert by_name["prov-b"]["key_source"] == "external"
        assert by_name["prov-c"]["key_source"] == "unset"


# ── Task 2: POST /admin/cloud/provider-update（非凭证字段 + 指纹 CAS）──

class TestProviderUpdate:
    def _post(self, cd, body):
        h, sent = _mk_handler(cd, command="POST", body=json.dumps(body).encode())
        h._handle_cloud_provider_update(SimpleNamespace(cloud=cd))
        return sent

    def test_update_display_name_only_succeeds(self, tmp_path):
        cd, p = _mk_cd(tmp_path, {"prov-a": {"openai_base": "http://a"}})
        fp = cd.config_fingerprint()
        sent = self._post(cd, {"name": "prov-a", "expected_config_fingerprint": fp,
                               "display_name": "新显示名"})
        assert sent and sent[0][1] == 200
        assert sent[0][0]["status"] == "updated"
        assert sent[0][0]["config_fingerprint"] != fp  # 保存后指纹已刷新
        # 内存可见 + 磁盘持久（重载读回）
        assert cd.providers["prov-a"].display_name == "新显示名"
        cd2 = CloudDiscovery(p)
        assert cd2.providers["prov-a"].display_name == "新显示名"
        # 缺省 = 不改：未提及的字段原样
        assert cd2.providers["prov-a"].timeout == 600

    def test_update_fingerprint_conflict_409(self, tmp_path):
        cd, _p = _mk_cd(tmp_path, {"prov-a": {"openai_base": "http://a"}})
        sent = self._post(cd, {"name": "prov-a",
                               "expected_config_fingerprint": "stale000000000000",
                               "display_name": "X"})
        assert sent[0][1] == 409
        assert sent[0][0]["error"] == "config_conflict"
        # 响应带当前指纹，客户端可据此刷新重试
        assert sent[0][0]["config_fingerprint"] == cd.config_fingerprint()
        assert cd.providers["prov-a"].display_name == ""  # 冲突不写入

    def test_update_save_failure_restores_memory(self, tmp_path, monkeypatch):
        cd, p = _mk_cd(tmp_path, {"prov-a": {"openai_base": "http://a"}})
        fp = cd.config_fingerprint()

        def boom():
            raise OSError("disk full")

        monkeypatch.setattr(cd, "save_config", boom)
        sent = self._post(cd, {"name": "prov-a", "expected_config_fingerprint": fp,
                               "display_name": "X", "timeout": 120})
        assert sent[0][1] == 500
        assert sent[0][0]["error"] == "save_failed"
        # 内存回滚：改过的字段全部还原
        assert cd.providers["prov-a"].display_name == ""
        assert cd.providers["prov-a"].timeout == 600
        # 磁盘未动：重载 YAML 后旧值不变、指纹不变
        cd2 = CloudDiscovery(p)
        assert cd2.providers["prov-a"].display_name == ""
        assert cd2.config_fingerprint() == fp

    def test_update_unknown_provider_404(self, tmp_path):
        cd, _p = _mk_cd(tmp_path, {"prov-a": {"openai_base": "http://a"}})
        sent = self._post(cd, {"name": "ghost",
                               "expected_config_fingerprint": cd.config_fingerprint()})
        assert sent[0][1] == 404
        assert sent[0][0]["error"] == "provider_not_found"


# ── Task 3: 凭证/端点变更门禁（key_action + 服务端真实探针）────────
# 真实本地 HTTP mock 上游，记录收到的 path/headers/body — 断言出站凭证值。

import os as _os
from http.server import HTTPServer, BaseHTTPRequestHandler as _BaseH
from threading import Thread as _Thread


class _RecordingUpstream(_BaseH):
    def do_POST(self):
        cls = type(self)
        length = int(self.headers.get("Content-Length", 0) or 0)
        cls.REQS.append({
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": self.rfile.read(length).decode("utf-8", "replace"),
        })
        payload = cls.POST_BODY.encode()
        self.send_response(cls.POST_STATUS)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        payload = b'{"data":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def _mk_upstream(status=200, body='{"choices":[{"message":{"content":"ok"}}]}'):
    """每测试新建子类（REQS/behavior 隔离），起在 127.0.0.1 随机端口。"""
    cls = type("Upstream", (_RecordingUpstream,),
               {"REQS": [], "POST_STATUS": status, "POST_BODY": body})
    srv = HTTPServer(("127.0.0.1", 0), cls)
    t = _Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return cls, f"http://127.0.0.1:{srv.server_address[1]}", srv


def _stop_upstream(srv):
    srv.shutdown()
    srv.server_close()


class TestCredentialEditGate:
    def _post(self, cd, body):
        h, sent = _mk_handler(cd, command="POST", body=json.dumps(body).encode())
        h._handle_cloud_provider_update(SimpleNamespace(cloud=cd))
        return sent

    def test_replace_key_probe_pass_persists(self, tmp_path, monkeypatch):
        monkeypatch.delenv("IFF_TEST_UPD_KEY", raising=False)
        up, base, srv = _mk_upstream()
        try:
            cd, p = _mk_cd(tmp_path, {"prov-a": {"openai_base": base,
                                                 "key_env_var": "IFF_TEST_UPD_KEY"}})
            cd.secrets.write("IFF_TEST_UPD_KEY", "sk-old-value")
            fp = cd.config_fingerprint()
            sent = self._post(cd, {"name": "prov-a", "expected_config_fingerprint": fp,
                                   "api_key": "sk-new-value", "key_action": "replace",
                                   "probe_model": "test-model"})
            assert sent[0][1] == 200, sent
            # 上游收到新 Bearer（真实 1-token 探针的出站值）
            assert up.REQS and up.REQS[0]["headers"].get("authorization") == "Bearer sk-new-value"
            assert up.REQS[0]["body"] and json.loads(up.REQS[0]["body"])["model"] == "test-model"
            # secrets.env 换新；env 注入残留被覆盖（受管优先 — 出站值真实变化）
            assert cd.secrets.load()["IFF_TEST_UPD_KEY"] == "sk-new-value"
            assert _os.environ["IFF_TEST_UPD_KEY"] == "sk-new-value"
            # YAML 存引用，明文不落盘
            import yaml as _yaml
            data = _yaml.safe_load(p.read_text())
            assert data["providers"]["prov-a"]["api_key"] == "${IFF_TEST_UPD_KEY}"
        finally:
            _os.environ.pop("IFF_TEST_UPD_KEY", None)
            _stop_upstream(srv)

    def test_wrong_key_probe_fail_no_write(self, tmp_path, monkeypatch):
        monkeypatch.delenv("IFF_TEST_UPD_KEY", raising=False)
        up, base, srv = _mk_upstream(status=401, body='{"error":{"message":"bad key"}}')
        try:
            cd, p = _mk_cd(tmp_path, {"prov-a": {"openai_base": base,
                                                 "key_env_var": "IFF_TEST_UPD_KEY"}})
            cd.secrets.write("IFF_TEST_UPD_KEY", "sk-old-value")
            fp = cd.config_fingerprint()
            sent = self._post(cd, {"name": "prov-a", "expected_config_fingerprint": fp,
                                   "api_key": "sk-wrong-value", "key_action": "replace",
                                   "probe_model": "test-model"})
            assert sent[0][1] == 400
            assert sent[0][0]["error"] == "probe_failed"
            assert sent[0][0]["protocol"] == "openai"
            assert "bad key" in sent[0][0]["detail"]
            # 零写入：磁盘与内存均旧值
            cd2 = CloudDiscovery(p)
            assert cd2.config_fingerprint() == fp
            assert cd.secrets.load()["IFF_TEST_UPD_KEY"] == "sk-old-value"
            assert "IFF_TEST_UPD_KEY" not in _os.environ
            import yaml as _yaml
            data = _yaml.safe_load(p.read_text())
            assert "api_key" not in data["providers"]["prov-a"]  # YAML 未被写入
        finally:
            _os.environ.pop("IFF_TEST_UPD_KEY", None)
            _stop_upstream(srv)

    def test_host_change_keep_key_rejected_before_network(self, tmp_path, monkeypatch):
        monkeypatch.delenv("IFF_TEST_UPD_KEY", raising=False)
        up_a, base_a, srv_a = _mk_upstream()
        up_b, base_b, srv_b = _mk_upstream()
        try:
            cd, _p = _mk_cd(tmp_path, {"prov-a": {"openai_base": base_a,
                                                  "key_env_var": "IFF_TEST_UPD_KEY"}})
            cd.secrets.write("IFF_TEST_UPD_KEY", "sk-old-value")
            fp = cd.config_fingerprint()
            sent = self._post(cd, {"name": "prov-a", "expected_config_fingerprint": fp,
                                   "openai_base": base_b, "key_action": "keep"})
            assert sent[0][1] == 400
            assert sent[0][0]["error"] == "destination_key_unconfirmed"
            # 旧 Key 绝不自动发往新域 — 网络零请求
            assert up_a.REQS == [] and up_b.REQS == []
            cd2 = CloudDiscovery(tmp_path / "cloud_provider.yaml")
            assert cd2.providers["prov-a"].openai_base == base_a
            assert cd2.config_fingerprint() == fp
        finally:
            _stop_upstream(srv_a)
            _stop_upstream(srv_b)

    def test_host_change_replace_sends_new_key_to_new_host_only(self, tmp_path, monkeypatch):
        monkeypatch.delenv("IFF_TEST_UPD_KEY", raising=False)
        up_a, base_a, srv_a = _mk_upstream()
        up_b, base_b, srv_b = _mk_upstream()
        try:
            cd, p = _mk_cd(tmp_path, {"prov-a": {"openai_base": base_a,
                                                 "key_env_var": "IFF_TEST_UPD_KEY"}})
            cd.secrets.write("IFF_TEST_UPD_KEY", "sk-old-value")
            sent = self._post(cd, {"name": "prov-a",
                                   "expected_config_fingerprint": cd.config_fingerprint(),
                                   "openai_base": base_b, "api_key": "sk-new-value",
                                   "key_action": "replace", "probe_model": "test-model"})
            assert sent[0][1] == 200, sent
            # 新 Key 只到新 host；旧 host 零请求
            assert up_a.REQS == []
            assert up_b.REQS and up_b.REQS[0]["headers"].get("authorization") == "Bearer sk-new-value"
            cd2 = CloudDiscovery(tmp_path / "cloud_provider.yaml")
            assert cd2.providers["prov-a"].openai_base == base_b
        finally:
            _os.environ.pop("IFF_TEST_UPD_KEY", None)
            _stop_upstream(srv_a)
            _stop_upstream(srv_b)

    def test_anthropic_probe_uses_x_api_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("IFF_TEST_UPD_KEY", raising=False)
        up, base, srv = _mk_upstream(body='{"content":[{"text":"ok"}]}')
        try:
            cd, _p = _mk_cd(tmp_path, {"prov-a": {"anthropic_base": base,
                                                  "key_env_var": "IFF_TEST_UPD_KEY"}})
            cd.secrets.write("IFF_TEST_UPD_KEY", "sk-old-value")
            sent = self._post(cd, {"name": "prov-a",
                                   "expected_config_fingerprint": cd.config_fingerprint(),
                                   "api_key": "sk-new-value", "key_action": "replace",
                                   "probe_model": "test-model"})
            assert sent[0][1] == 200, sent
            req = up.REQS[0]
            assert req["path"].endswith("/messages")
            # anthropic 口径：x-api-key，无 Authorization Bearer
            assert req["headers"].get("x-api-key") == "sk-new-value"
            assert "authorization" not in req["headers"]
        finally:
            _os.environ.pop("IFF_TEST_UPD_KEY", None)
            _stop_upstream(srv)

    def test_replace_external_key_rejected(self, tmp_path, monkeypatch):
        """spec §2：外部环境托管的凭证不可在 UI/管理面覆盖。"""
        up, base, srv = _mk_upstream()
        try:
            cd, _p = _mk_cd(tmp_path, {"prov-a": {"openai_base": base,
                                                  "key_env_var": "IFF_TEST_UPD_KEY"}},
                            env_vars=["IFF_TEST_UPD_KEY"], monkeypatch=monkeypatch)
            fp = cd.config_fingerprint()
            sent = self._post(cd, {"name": "prov-a", "expected_config_fingerprint": fp,
                                   "api_key": "sk-new-value", "key_action": "replace",
                                   "probe_model": "test-model"})
            assert sent[0][1] == 400
            assert sent[0][0]["error"] == "external_key_managed"
            # 拒绝在联网前 — 上游零请求，磁盘未动
            assert up.REQS == []
            assert cd.config_fingerprint() == fp
        finally:
            _stop_upstream(srv)

    def test_replace_requires_key(self, tmp_path, monkeypatch):
        up, base, srv = _mk_upstream()
        try:
            cd, _p = _mk_cd(tmp_path, {"prov-a": {"openai_base": base}})
            sent = self._post(cd, {"name": "prov-a",
                                   "expected_config_fingerprint": cd.config_fingerprint(),
                                   "key_action": "replace"})
            assert sent[0][1] == 400
            assert sent[0][0]["error"] == "api_key_required"
            assert up.REQS == []
        finally:
            _stop_upstream(srv)
