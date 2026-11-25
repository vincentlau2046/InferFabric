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
