"""Legacy 卸载 handler 的池清理失败上报（7.11.3 最终评审 Important-1 收尾）。

包级 uninstaller 已把校验闸前移（拒绝 → 零删除副作用 + 400）。legacy 路径
（用户导入模型 {name} 流程）按设计先删文件后 purge —— purge 失败不能静默
成功：响应必须带 pool_purge_error，操作者可见池内陈旧引用需要清理。
"""

import types

from inferfabric.proxy.handler import ProxyHandler


class _FakeHandler:
    """duck-type：只供 _handle_pack_uninstall_legacy 用到的两个方法。"""

    def __init__(self, body):
        self._body = body
        self.sent = []

    def _read_body(self):
        return self._body

    def _send_json(self, data, status=200, extra_headers=None):
        self.sent.append((status, data))


def test_legacy_uninstall_surfaces_pool_purge_error(tmp_path):
    y = tmp_path / "mymodel.yaml"
    y.write_text("name: mymodel\ntype: llama_cpp\n")
    model = types.SimpleNamespace(yaml_path=str(y))

    class _Mgr:
        def __init__(self):
            self._models = {"mymodel": model}
            self.active_services = set()
            self.reloaded = 0

        def reload_models(self):
            self.reloaded += 1

        def stop_service(self, name):
            return None

    class _Registry:
        def purge_candidates(self, predicate):
            raise ValueError("pool 'legacy': max_attempts (2) is less than "
                             "candidate count (3) — later candidates unreachable")

    pm = types.SimpleNamespace(mgr=_Mgr(), pool_registry=_Registry())
    h = _FakeHandler({"name": "mymodel"})
    ProxyHandler._handle_pack_uninstall_legacy(h, pm)

    status, body = h.sent[-1]
    assert status == 200                        # 卸载本身完成
    assert body["status"] == "uninstalled"
    assert body["pool_candidates_purged"] == 0
    assert "max_attempts" in body["pool_purge_error"]   # 失败不再静默
    assert not y.exists()                       # legacy 语义：文件已删
