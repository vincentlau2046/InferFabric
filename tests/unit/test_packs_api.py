"""admin API — catalog/status/install/policy — PK1（Task 8）。

契约（计划 Interfaces）：
- PackManager 门面（pack_handlers.py）：catalog_payload/status_payload/
  start_install/policy_get/policy_put。
- HTTP 契约（handler 薄委托）：
  GET  /admin/packs/catalog → {"packs":[...]}
  GET  /admin/packs/status  → {"packs":[PackRecord...]}（含进行中事务）
  POST /admin/packs/install {"pack_id"} → 202 进行中 / 409 互斥 / 404 未知
  GET/PUT /admin/packs/policy（越界 400：参数 <1，保留内存出 [1024,65536]）
- 旧 /admin/packs/download 原样保留（用户导入模型路径）。
- repo 无 aiohttp 测试夹具（Ruling）：门面层函数级测试 + 路由表注册断言。
"""

import threading
import time
from pathlib import Path

import pytest

from inferfabric.packs.catalog import load_catalog
from inferfabric.proxy import pack_handlers

REPO = Path(__file__).resolve().parents[2]


class SlowFetcher:
    """慢速 fetcher：制造进行中事务窗口。"""

    def __init__(self, files, delay=0.3):
        self.files = files
        self.delay = delay

    def fetch(self, artifact, dest):
        time.sleep(self.delay)
        dest.write_bytes(self.files[artifact.id])


@pytest.fixture()
def mgr(tmp_path):
    from tests.unit.tests_fixtures_packs import MINIMAL_MANIFEST_YAML
    from inferfabric.packs.schema import parse_manifest
    import hashlib
    m = parse_manifest(MINIMAL_MANIFEST_YAML).to_dict()
    m["resources"]["download_mb"] = 1
    m["resources"]["disk_mb"] = 1
    files = {"model": b"M" * 128, "engine": b"E" * 64}
    arts = []
    for i, aid in enumerate(files):
        a = parse_manifest(MINIMAL_MANIFEST_YAML).artifacts[i]
        arts.append({"id": aid, "kind": a.kind, "source": f"https://x/{aid}",
                     "sha256": hashlib.sha256(files[aid]).hexdigest()})
    m["artifacts"] = arts
    from inferfabric.packs.catalog import Catalog, CatalogEntry
    manifest = parse_manifest(m)
    cat_path = tmp_path / "catalog.yaml"
    import yaml
    cat_path.write_text(yaml.safe_dump({"packs": [{
        "pack_id": manifest.pack_id, "display_name": "T", "summary": "s",
        "tags": [], "risk_level": "verified", "builtin": False,
        "manifest": m}]}, sort_keys=False))
    pmgr = pack_handlers.PackManager(
        root=tmp_path / "packs", models_d=tmp_path / "models.d",
        catalog_path=cat_path, fetcher=SlowFetcher(files),
        reload_gateway=lambda: None,
        prepare_runtime=lambda pd, m: None,       # 单测不发真进程
        run_healthcheck=lambda pd, m: None,
        # 评审 PR#16 B2：探针密闭化——内存门禁吃固定值，不依赖宿主机
        # 运行时内存（Mac 空闲内存大全绿、Linux 缓存占用高 3 连挂）。
        memory_available_mb=lambda: 65536)
    return pmgr


def test_catalog_payload_shape(mgr):
    p = mgr.catalog_payload()
    assert p["packs"][0]["pack_id"] == "llm.qwen3-4b-instruct-2507"
    assert {"pack_id", "display_name", "summary", "tags", "capability",
            "builtin", "risk_level", "manifest"} <= set(p["packs"][0])


def test_install_unknown_pack_404(mgr):
    code, payload = mgr.start_install("nope")
    assert code == 404


def test_install_happy_path_reaches_installed(mgr):
    code, payload = mgr.start_install("llm.qwen3-4b-instruct-2507")
    assert code == 202
    deadline = time.time() + 5
    while time.time() < deadline:
        st = mgr.status_payload()
        rec = next(r for r in st["packs"]
                   if r["pack_id"] == "llm.qwen3-4b-instruct-2507")
        if rec["status"] == "installed":
            break
        time.sleep(0.05)
    assert rec["status"] == "installed"
    # 安装路径可见性（2026-11-16 用户裁决：能力包页展示预设安装位置）
    assert rec["install_path"] == str(
        mgr.root / "llm.qwen3-4b-instruct-2507" / rec["pack_version"])
    assert Path(rec["install_path"]).is_dir()
    # 受控 YAML 已生成
    y = (mgr.models_d / "pack-qwen3-4b-instruct-2507-1-0-0.yaml")
    assert y.exists()


def test_reinstall_of_installed_pack_409(mgr):
    # 终审 F1：已安装包再次 install → 409（失败的重装不得摧毁已装工作包）
    code, _ = mgr.start_install("llm.qwen3-4b-instruct-2507")
    assert code == 202
    deadline = time.time() + 5
    rec = None
    while time.time() < deadline:
        rec = mgr.store.get("llm.qwen3-4b-instruct-2507")
        if rec and rec.status == "installed":
            break
        time.sleep(0.05)
    assert rec and rec.status == "installed"
    code2, _ = mgr.start_install("llm.qwen3-4b-instruct-2507")
    assert code2 == 409
    # 已装状态未被二次请求破坏
    assert mgr.store.get("llm.qwen3-4b-instruct-2507").status == "installed"


def test_bookkeeping_failure_releases_in_progress(mgr, monkeypatch):
    # 终审 F4：store.set 崩溃不得让 pack_id 永久 409
    def broken_set(rec):
        raise OSError("disk io error")

    monkeypatch.setattr(mgr.store, "set", broken_set)
    code, _ = mgr.start_install("llm.qwen3-4b-instruct-2507")
    assert code == 202
    deadline = time.time() + 5
    while time.time() < deadline:
        if "llm.qwen3-4b-instruct-2507" not in mgr._in_progress:
            break
        time.sleep(0.05)
    assert "llm.qwen3-4b-instruct-2507" not in mgr._in_progress
    code2, _ = mgr.start_install("llm.qwen3-4b-instruct-2507")
    assert code2 == 202


def test_concurrent_install_same_pack_409(mgr):
    # Review Focus 4：同 pack_id 事务互斥，后到者 409
    results = []
    mgr._fetcher = SlowFetcher(mgr._fetcher.files, delay=0.4)
    t = threading.Thread(
        target=lambda: results.append(
            mgr.start_install("llm.qwen3-4b-instruct-2507")))
    t.start()
    time.sleep(0.1)   # 让第一个事务进入 downloading
    code, _ = mgr.start_install("llm.qwen3-4b-instruct-2507")
    t.join()
    assert code == 409
    assert results[0][0] == 202


def test_policy_get_put_and_bounds(mgr):
    code, p = mgr.policy_get()
    assert code == 200 and p["max_model_parameters_b"] == 4
    assert p["system_reserved_memory_mb"] == 8192
    code, _ = mgr.policy_put({"max_model_parameters_b": 0})
    assert code == 400
    code, _ = mgr.policy_put({"system_reserved_memory_mb": 100})
    assert code == 400
    code, p = mgr.policy_put({"max_model_parameters_b": 8})
    assert code == 200 and p["max_model_parameters_b"] == 8


def test_policy_bounds_range():
    # 保留内存合法域 [1024, 65536] MiB
    from inferfabric.proxy.pack_handlers import POLICY_MEM_RANGE
    assert POLICY_MEM_RANGE == (1024, 65536)


def test_route_registration_contains_pack_endpoints():
    from inferfabric.proxy.handler import _GET_ROUTES, _POST_ROUTES
    for path in ("/admin/packs/catalog", "/admin/packs/status",
                 "/admin/packs/policy"):
        assert path in _GET_ROUTES, path
    for path in ("/admin/packs/install", "/admin/packs/policy"):
        assert path in _POST_ROUTES, path
    # 旧端点保留（用户导入模型路径）
    assert "/admin/packs/download" in _POST_ROUTES
    assert "/admin/packs/uninstall" in _POST_ROUTES


def test_singleton_defaults_from_env(tmp_path, monkeypatch):
    # 生产迁移前置 PR：config 是唯一事实源 —— env 覆盖在 config import 期
    # 解析，pack_handlers 直读 config 常量（不再自读 env）
    import importlib
    import inferfabric.config as _cfg
    monkeypatch.setenv("MACFABRIC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MACFABRIC_MODELS_DIR", str(tmp_path / "models.d"))
    importlib.reload(_cfg)
    try:
        pmgr = pack_handlers.get_pack_manager(
            catalog_path=REPO / "packs_catalog.yaml")
        assert pmgr.root == tmp_path / "state" / "packs"
        assert pmgr.models_d == tmp_path / "models.d"
    finally:
        for var in ("MACFABRIC_STATE_DIR", "MACFABRIC_MODELS_DIR"):
            monkeypatch.delenv(var, raising=False)
        importlib.reload(_cfg)
        pack_handlers.reset_pack_manager()


def test_is_model_disabled_delegates_to_uninstaller(mgr):
    # 终审 deferred #2b：禁用查询统一委托 uninstaller（单一事实源）
    class FakeUni:
        def __init__(self):
            self.disabled = set()

        def is_route_disabled(self, name):
            return name in self.disabled

    fake = FakeUni()
    mgr._uninstaller = fake
    fake.disabled.add("m1")
    assert mgr.is_model_disabled("m1") is True
    assert mgr.is_model_disabled("m2") is False


def test_fetcher_enforces_size_limit(tmp_path):
    # 终审 deferred #6a：下载超尺寸上限 → 拒绝（防绕过 disk_mb 预检）
    from types import SimpleNamespace
    from inferfabric.proxy.pack_handlers import HttpArtifactFetcher

    class FakeResp:
        def __init__(self):
            self.left = [b"x" * 5, b"x" * 5]

        def read(self, n):
            return self.left.pop(0) if self.left else b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    f = HttpArtifactFetcher(max_bytes=8)
    f._opener = SimpleNamespace(open=lambda req, timeout=None: FakeResp())
    with pytest.raises(ValueError):
        f.fetch(SimpleNamespace(source="https://x/m.gguf"), tmp_path / "m.part")


def test_fetcher_redirect_https_downgrade_refused():
    # 终审 deferred #6b：https→http 降级重定向拒绝
    import urllib.error
    import urllib.request
    from inferfabric.proxy.pack_handlers import _NoDowngradeRedirectHandler

    h = _NoDowngradeRedirectHandler()
    req = urllib.request.Request("https://a/x")
    with pytest.raises(urllib.error.URLError):
        h.redirect_request(req, None, 302, "found", {}, "http://b/x")
    assert h.redirect_request(req, None, 302, "found", {}, "https://b/x") \
        is not None


def test_bundled_catalog_default_path_exists():
    # 生产冒烟回归：默认捆绑目录路径必须指向真实文件
    # （pack_handlers.py 位于 inferfabric/proxy/，需上溯三级到 macfabric_gateway/）
    from inferfabric.proxy.pack_handlers import BUNDLED_CATALOG
    assert BUNDLED_CATALOG.name == "packs_catalog.yaml"
    assert BUNDLED_CATALOG.exists(), BUNDLED_CATALOG


def test_fetcher_resumes_after_midstream_timeout(tmp_path):
    # 生产真装两次断连取证（1.5G / 2.3G 处 TimeoutError，2026-11-16）—
    # ModelScope CDN 掐长连接：fetcher 必须带 Range 断点续传重试，
    # 而不是从头再来或直接失败
    import socket
    from types import SimpleNamespace
    from inferfabric.proxy.pack_handlers import HttpArtifactFetcher

    class FlakyResp:
        def __init__(self, chunks, status=206):
            self.chunks = list(chunks)
            self.status = status

        def read(self, n):
            if not self.chunks:
                return b""
            c = self.chunks.pop(0)
            if isinstance(c, Exception):
                raise c
            return c

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeOpener:
        def __init__(self, resps):
            self.calls = []
            self.resps = list(resps)

        def open(self, req, timeout=None):
            self.calls.append(req)
            return self.resps.pop(0)

    # 首次下载中断流（socket.timeout），第二次带 Range 续上剩余
    f = HttpArtifactFetcher()
    op = FakeOpener([
        FlakyResp([b"AAAA", socket.timeout("read timed out")], status=200),
        FlakyResp([b"BBBB", b"CCCC"]),
    ])
    f._opener = op
    dest = tmp_path / "m.part"
    f.fetch(SimpleNamespace(source="https://x/m.gguf"), dest)
    assert dest.read_bytes() == b"AAAABBBBCCCC"
    assert len(op.calls) == 2
    assert op.calls[1].get_header("Range") == "bytes=4-"


def test_fetcher_resume_gives_up_after_limit(tmp_path):
    # 服务器持续断连 → 重试上限后诚实抛错（不无限循环）
    import socket
    from types import SimpleNamespace
    from inferfabric.proxy.pack_handlers import HttpArtifactFetcher

    class DeadResp:
        status = 200

        def read(self, n):
            raise socket.timeout("read timed out")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class AlwaysDeadOpener:
        def __init__(self):
            self.calls = 0

        def open(self, req, timeout=None):
            self.calls += 1
            return DeadResp()

    f = HttpArtifactFetcher()
    op = AlwaysDeadOpener()
    f._opener = op
    with pytest.raises(socket.timeout):
        f.fetch(SimpleNamespace(source="https://x/m.gguf"), tmp_path / "m.part")
    assert op.calls == 6   # 1 首发 + 5 次续传重试


def test_fetcher_no_resume_on_http_error(tmp_path):
    # 404 等协议级错误不重试（重试只救网络中断，不救坏 URL）
    import urllib.error
    from types import SimpleNamespace
    from inferfabric.proxy.pack_handlers import HttpArtifactFetcher

    class Always404Opener:
        def __init__(self):
            self.calls = 0

        def open(self, req, timeout=None):
            self.calls += 1
            raise urllib.error.HTTPError(
                "https://x/m.gguf", 404, "nope", {}, None)

    f = HttpArtifactFetcher()
    op = Always404Opener()
    f._opener = op
    with pytest.raises(urllib.error.HTTPError):
        f.fetch(SimpleNamespace(source="https://x/m.gguf"), tmp_path / "m.part")
    assert op.calls == 1


def test_install_preflight_rejects_when_memory_budget_exhausted(mgr):
    """评审 PR#16 B2：预算不足 → 安装记录落 failed，error=内存预算
    （门禁行为钉死在测试里，而非宿主机内存水位）。"""
    mgr.installer._memory_available_mb = lambda: 0   # 注入耗尽探针
    code, payload = mgr.start_install("llm.qwen3-4b-instruct-2507")
    assert code == 202
    deadline = time.time() + 5
    rec = None
    while time.time() < deadline:
        st = mgr.status_payload()
        rec = next(r for r in st["packs"]
                   if r["pack_id"] == "llm.qwen3-4b-instruct-2507")
        if rec["status"] == "failed":
            break
        time.sleep(0.05)
    assert rec["status"] == "failed"
    assert "memory budget" in str(rec.get("error", "")), rec


def test_platform_probe_mem_available_mb_shape():
    """评审 PR#16 B1：平台层探针契约——可探或 None，绝不抛。"""
    from inferfabric.platform.probe import mem_available_mb
    v = mem_available_mb()
    assert v is None or (isinstance(v, int) and v >= 0)
