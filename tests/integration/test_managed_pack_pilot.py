#!/usr/bin/env python3
"""integration/test_managed_pack_pilot.py — 受管能力包事务与共享启动决策（阶段 1 Task 3）。

契约（specs/packs-platform-adaptation §4 + 计划 Task 3）：
- resolve()：legacy 模型返回 None；坏引用/缺失记录/身份不匹配抛
  PackRuntimeError（带 reason_code），绝不默认降级 legacy。
- check_start()：解析后、任何 stop/switch 副作用前完成准入；GPU 包
  idle-only（占用 → resource_busy）；锁复用，不重入。
- 失败回滚只清本次拥有的状态；外部产物与既有 YAML/路由不动。
- 端口/名字冲突只拒绝，绝不收养或停止外来实例。
- 调度边界（_start_model）复核受管身份，失效绝不落入通用启动。
"""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from inferfabric.packs import admission
from inferfabric.packs.admission import RuntimeTemplate
from inferfabric.packs.managed_runtime import (
    ManagedRuntime,
    PackRuntimeError,
)
from inferfabric.packs.store import PackStore
from tests.unit.tests_fixtures_packs import A64 as _A64

TEMPLATE_NAME = "ninfer-nvfp4-linux-v1"
PACK_ID = "llm.pilot-ninfer-27b"


# ── 夹具 ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _registry():
    admission.reset_registry()
    admission.register_template(RuntimeTemplate(
        name=TEMPLATE_NAME, platforms=("linux-nvidia",),
        isolation="verified", allow_degraded=False,
        resource_profile="legacy_default", expected_artifacts=()))
    yield
    admission.reset_registry()


def _marker(**over):
    d = {"marker_version": 1, "pack_id": PACK_ID, "pack_version": "1.0.0",
         "runtime_template": TEMPLATE_NAME,
         "artifact_identities": [_A64], "role": "owned"}
    d.update(over)
    return d


def _manifest_yaml(pack_id=PACK_ID, version="1.0.0", template=TEMPLATE_NAME):
    return f"""
pack_id: {pack_id}
pack_version: {version}
capability: llm
model: {{id: qwen3-27b, parameter_count_b: 27, quantization: nvfp4, license: apache-2.0}}
runtime: {{engine: ninfer, runtime_template: {template}}}
resources: {{download_mb: 30000, disk_mb: 32000, peak_memory_mb: 24000, gpu_role: exclusive}}
lifecycle: {{mode: on_demand}}
network: {{listen: loopback, allowed_upstream: []}}
artifacts:
  - {{id: model, kind: model, source: "https://example.com/m.nf4", sha256: "{_A64}"}}
execution: {{model_config_template: ninfer_chat_v1, healthcheck: openai_chat_ready_v1}}
sandbox: {{policy: verified-local-pack-v1, degraded_install_allowed: false}}
"""


def _install_pack(tmp_path, marker=None):
    """在 store + pack_root 落一个已装包（含 manifest.yaml + 产物占位）。"""
    marker = marker or _marker()
    root = tmp_path / "packs_root"
    pack_dir = root / marker["pack_id"] / marker["pack_version"]
    pack_dir.mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(_manifest_yaml(), encoding="utf-8")
    (pack_dir / "models" / "model").parent.mkdir(parents=True, exist_ok=True)
    (pack_dir / "models" / "model").write_text("weights", encoding="utf-8")
    store = PackStore(tmp_path / "packs" / "state.json")
    from inferfabric.packs.store import PackRecord
    store.set(PackRecord(
        pack_id=marker["pack_id"], pack_version=marker["pack_version"],
        status="installed", stage="installed", error=None,
        sandbox_degraded=False, installed_at=0))
    return store, root


def _model_yaml(tmp_path, name, *, marker=None, port=8107):
    models_d = tmp_path / "models.d"
    models_d.mkdir(exist_ok=True)
    doc = {"name": name, "type": "ninfer", "gpu_role": "exclusive",
           "model_type": "llm", "peak_vram_mb": 24000,
           "ninfer": {"port": port, "docker_image": "ninfer:test",
                      "weight_path": str(tmp_path / "weights")}}
    if marker is not None:
        doc["managed_by_pack"] = marker
    path = models_d / f"{name}.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _load_one(tmp_path, name):
    from inferfabric.config import load_models
    return load_models(tmp_path / "models.d")[name]


def _runtime(tmp_path, store, *, probe_gpu_busy=False, models=None):
    return ManagedRuntime(
        store=store, pack_root=tmp_path / "packs_root",
        registry_lookup=admission.get_template,
        probe_gpu_busy=lambda: probe_gpu_busy,
        models_lookup=models or {})


# ── 1. GPU 占用：准入拒绝，零启动/停止/复位 ─────────────────────

def test_busy_after_preflight_never_launches_or_stops_existing(tmp_path):
    store, _ = _install_pack(tmp_path)
    _model_yaml(tmp_path, "pilot", marker=_marker())
    model = _load_one(tmp_path, "pilot")
    rec = {"launch": [], "stop": [], "reset": []}
    proc = MagicMock()
    proc.start_ninfer.side_effect = \
        lambda *a, **k: rec["launch"].append(a)
    proc.stop_service.side_effect = lambda *a, **k: rec["stop"].append(a)
    proc.clear_gpu_cuda_state.side_effect = \
        lambda *a, **k: rec["reset"].append(a)
    rt = _runtime(tmp_path, store, probe_gpu_busy=True)
    with pytest.raises(PackRuntimeError) as ei:
        rt.check_start(model, proc=proc)
    assert ei.value.reason_code == "resource_busy"
    assert rec == {"launch": [], "stop": [], "reset": []}


# ── 2. 坏引用：先拒绝后切换；force 不可越过 ─────────────────────

def test_managed_reference_missing_denies_before_switch(tmp_path):
    store, _ = _install_pack(tmp_path)
    _model_yaml(tmp_path, "pilot",
                marker=_marker(pack_id="llm.missing-pack"))
    model = _load_one(tmp_path, "pilot")
    rt = _runtime(tmp_path, store)
    with pytest.raises(PackRuntimeError) as ei:
        rt.resolve(model)
    assert ei.value.reason_code == "ownership_conflict"

    # manager 级：switch 在任何 stop 副作用前返回错误；force 不能越过
    from inferfabric.manager import ModelManager
    from inferfabric.packs.managed_runtime import set_managed_runtime
    proc = MagicMock()
    mgr = ModelManager(models_dir=tmp_path / "models.d",
                       state_db_path=tmp_path / "state.db", proc=proc)
    set_managed_runtime(rt)
    try:
        for force in (False, True):
            r = mgr.switch("pilot", force=force)
            assert r.get("reason_code") == "ownership_conflict", r
            assert r["status"] == "error"
    finally:
        set_managed_runtime(None)
    proc.stop_service.assert_not_called()
    proc.clear_gpu_cuda_state.assert_not_called()


# ── 3. 事务失败：只回滚本次拥有状态 ─────────────────────────────

def test_prep_or_reload_failure_rolls_back_owned_files_only(tmp_path):
    from inferfabric.proxy.pack_handlers import PackManager

    def _cat(pack_id):
        cat = tmp_path / f"catalog-{pack_id}.yaml"
        cat.write_text(yaml.safe_dump({"packs": [{
            "pack_id": pack_id, "display_name": "Rollback", "summary": "s",
            "tags": ["llm"], "risk_level": "verified", "builtin": True,
            "manifest": yaml.safe_load(_rollback_manifest(pack_id))}]}),
            encoding="utf-8")
        return cat

    other_yaml = tmp_path / "models.d" / "Other.yaml"
    other_yaml.parent.mkdir(parents=True, exist_ok=True)
    other_yaml.write_text("name: Other\ntype: ollama\ngpu_role: none\n",
                          encoding="utf-8")
    external_weights = tmp_path / "external" / "weights.gguf"
    external_weights.parent.mkdir(parents=True)
    external_weights.write_text("borrowed", encoding="utf-8")

    class _Boom:
        def fetch(self, *a, **k):
            raise AssertionError("builtin pack must not download")

    def _pm(pack_id, reload_ok):
        return PackManager(tmp_path / f"pk-{pack_id}", tmp_path / "models.d",
                           catalog_path=_cat(pack_id), fetcher=_Boom(),
                           reload_gateway=(None if reload_ok else _raise_reload),
                           prepare_runtime=lambda *a, **k: None,
                           run_healthcheck=lambda *a, **k: None,
                           memory_available_mb=lambda: 100_000)

    # 轮 A：成功基线 — pack-a 装入并发布受控 YAML
    pm_a = _pm("llm.rollback-a", reload_ok=True)
    res_a = pm_a.installer.install(pm_a.catalog.find("llm.rollback-a"))
    assert res_a.ok and res_a.stage == "installed", res_a.error
    yaml_a = tmp_path / "models.d" / "pack-rollback-a-1-0-0.yaml"
    assert yaml_a.exists()

    # 轮 B：reload 失败 — 只清理本次拥有状态，不碰他者
    pm_b = _pm("llm.rollback-b", reload_ok=False)
    res_b = pm_b.installer.install(pm_b.catalog.find("llm.rollback-b"))
    assert not res_b.ok and res_b.stage == "gateway_reload"
    yaml_b = tmp_path / "models.d" / "pack-rollback-b-1-0-0.yaml"
    assert not yaml_b.exists()          # 本次发布的受控 YAML 已回滚
    assert yaml_a.exists()              # 已提交的旧拥有状态保留
    assert other_yaml.read_text(encoding="utf-8").startswith("name: Other")
    assert external_weights.read_text(encoding="utf-8") == "borrowed"


def _rollback_manifest(pack_id):
    return f"""
pack_id: {pack_id}
pack_version: 1.0.0
capability: llm
model: {{id: mini-test, parameter_count_b: 1, quantization: q4_k_m, license: apache-2.0}}
runtime: {{engine: llama_cpp, runtime_template: llama-cpp-macos-arm64-v1}}
resources: {{download_mb: 10, disk_mb: 20, peak_memory_mb: 500, gpu_role: none}}
lifecycle: {{mode: on_demand}}
network: {{listen: loopback, allowed_upstream: []}}
artifacts:
  - {{id: model, kind: model, source: "https://example.com/m.gguf", sha256: "{_A64}"}}
execution: {{model_config_template: llama_cpp_chat_v1, healthcheck: openai_chat_ready_v1}}
sandbox: {{policy: verified-local-pack-v1, degraded_install_allowed: true}}
"""


def _raise_reload():
    raise RuntimeError("injected reload failure")


# ── 4. 冲突：拒绝，绝不收养/停止外来实例 ────────────────────────

def test_port_or_name_collision_never_adopts_or_stops_foreign_instance(
        tmp_path):
    store, _ = _install_pack(tmp_path)
    _model_yaml(tmp_path, "pilot", marker=_marker(), port=8007)
    # 外来现役模型占用同一端口（legacy，无 managed 标记）
    _model_yaml(tmp_path, "Qwen38-27B-TXT", port=8007)
    models = {}
    from inferfabric.config import load_models
    models = load_models(tmp_path / "models.d")
    model = models["pilot"]
    foreign = models["Qwen38-27B-TXT"]
    rt = _runtime(tmp_path, store, models=models)
    stops = []
    with pytest.raises(PackRuntimeError) as ei:
        rt.check_start(model, proc=MagicMock(),
                       on_conflict_stop=lambda m: stops.append(m))
    assert ei.value.reason_code == "ownership_conflict"
    assert stops == []            # 从未停止外来实例


# ── 5. 调度边界复核：身份失效不落通用启动 ────────────────────────

def test_policy_rechecked_at_dispatch_for_managed_model(tmp_path):
    store, root = _install_pack(tmp_path)
    _model_yaml(tmp_path, "pilot", marker=_marker())
    model = _load_one(tmp_path, "pilot")
    rt = _runtime(tmp_path, store)
    # 初始解析通过
    inst = rt.resolve(model)
    assert inst is not None and inst.pack_id == PACK_ID
    # 包被卸载（记录清除）后，调度边界必须拒绝，绝不走通用启动
    store.remove(PACK_ID)
    with pytest.raises(PackRuntimeError) as ei:
        rt.verify(inst)
    assert ei.value.reason_code == "ownership_conflict"

    from inferfabric.model_lifecycle import ModelLifecycle
    from inferfabric.packs.managed_runtime import set_managed_runtime
    set_managed_runtime(rt)
    try:
        lc = ModelLifecycle(state=MagicMock(), proc=MagicMock(),
                            health=MagicMock(), lock=MagicMock(),
                            gpu_state=MagicMock(), models={model.name: model},
                            models_dir=str(tmp_path / "models.d"))
        import inferfabric.model_lifecycle as ml_mod
        r = lc._start_model(model)
        assert r["status"] == "error" and \
            r.get("reason_code") == "ownership_conflict"
    finally:
        set_managed_runtime(None)


# ── 6. 私有试点：未审核不可入目录/安装 ──────────────────────────

def test_unverified_private_pilot_is_not_catalog_installable(tmp_path):
    from inferfabric.proxy.pack_handlers import PackManager

    class _Boom:
        def fetch(self, *a, **k):
            raise AssertionError("must not download")

    cat = tmp_path / "catalog.yaml"
    cat.write_text(yaml.safe_dump({"packs": [{
        "pack_id": PACK_ID, "display_name": "Pilot", "summary": "s",
        "tags": ["llm"], "risk_level": "verified", "builtin": True,
        "manifest": yaml.safe_load(_manifest_yaml())}]}), encoding="utf-8")
    pm = PackManager(tmp_path / "pk", tmp_path / "models.d",
                     catalog_path=cat, fetcher=_Boom())
    entry = pm.catalog.find(PACK_ID)
    res = pm.installer.install(entry)
    # 未审核试点：preflight 即拒（模板未实现 / 超出 legacy 门禁均在
    # preflight 拦截），绝不进入下载或发布
    assert not res.ok and res.stage == "preflight"

    # 私有 verify：仅精确审核身份可过；失败时不发布任何受控 YAML
    store, root = _install_pack(tmp_path)
    rt = _runtime(tmp_path, store)
    _model_yaml(tmp_path, "pilot", marker=_marker())
    model = _load_one(tmp_path, "pilot")
    inst = rt.resolve(model)
    admission.reset_registry()          # 审核档/模板被撤走
    before = sorted(p.name for p in (tmp_path / "models.d").glob("*.yaml"))
    with pytest.raises(PackRuntimeError):
        rt.verify(inst)
    # 复核失败不发布任何受控 YAML（除测试输入 pilot.yaml 外无新增）
    after = sorted(p.name for p in (tmp_path / "models.d").glob("*.yaml"))
    assert before == after == ["pilot.yaml"]


# ── 7. 受管启动链路：ResolvedRuntime 贯通到 launcher ─────────────

def test_managed_start_threads_resolved_runtime_to_launcher(tmp_path):
    """调度边界解析出的 ResolvedRuntime 必须贯通到 PM.start_ninfer(managed=…)
    （Task 4）；legacy 路径不得带 managed。"""
    store, _ = _install_pack(tmp_path)
    _model_yaml(tmp_path, "pilot", marker=_marker())
    model = _load_one(tmp_path, "pilot")
    rt = _runtime(tmp_path, store)
    inst = rt.resolve(model)

    from inferfabric.engine_adapter.ninfer import NInferAdapter
    proc = MagicMock()
    adapter = NInferAdapter(process_manager=proc)
    r = adapter.start(model, managed=inst)
    assert r["status"] == "healthy" or proc.start_ninfer.called
    proc.start_ninfer.assert_called_once_with(model.ninfer,
                                              model.container_name,
                                              managed=inst)

    # legacy：无 managed → 不带 managed kwarg
    proc2 = MagicMock()
    adapter2 = NInferAdapter(process_manager=proc2)
    adapter2.start(model)
    assert proc2.start_ninfer.call_args.kwargs.get("managed", None) is None

    # _start_model 全链路：set_managed_runtime 注入后，真 adapter 把
    # ResolvedRuntime 传给 proc.start_ninfer
    from inferfabric.model_lifecycle import ModelLifecycle
    from inferfabric.packs.managed_runtime import set_managed_runtime
    set_managed_runtime(rt)
    try:
        lc = ModelLifecycle(state=MagicMock(), proc=proc, health=MagicMock(),
                            lock=MagicMock(), gpu_state=MagicMock(),
                            models={model.name: model},
                            models_dir=str(tmp_path / "models.d"))
        lc._start_model(model)
        kwargs = proc.start_ninfer.call_args.kwargs
        assert kwargs.get("managed") == inst
    finally:
        set_managed_runtime(None)


# ── 8. BGE CPU embedding 试点（Task 5）──────────────────────────

def _bge_pack(tmp_path, marker=None):
    """落一个已装的 BGE CPU embedding 包：包内 runtime/engine + models/model。"""
    marker = marker or _marker(pack_id="embedding.bge-cpu",
                               runtime_template="llama-cpp-linux-cpu-v1")
    pack_dir = tmp_path / "packs_root" / marker["pack_id"] / marker["pack_version"]
    (pack_dir / "runtime").mkdir(parents=True, exist_ok=True)
    (pack_dir / "runtime" / "engine").write_text("binary", encoding="utf-8")
    (pack_dir / "models").mkdir(parents=True, exist_ok=True)
    (pack_dir / "models" / "model").write_text("gguf", encoding="utf-8")
    store = PackStore(tmp_path / "packs" / "state.json")
    from inferfabric.packs.store import PackRecord
    store.set(PackRecord(
        pack_id=marker["pack_id"], pack_version=marker["pack_version"],
        status="installed", stage="installed", error=None,
        sandbox_degraded=True, installed_at=0))
    return store, pack_dir


def test_bge_cpu_uses_approved_binary_and_explicit_degraded_mode(
        tmp_path, monkeypatch):
    """受管 embedding：核准包内二进制、强制 CPU、loopback 绑定、
    /v1/embeddings 就绪探测（超越端口健康）；进程组收割。"""
    import io as _io
    import json as _json

    store, pack_dir = _bge_pack(tmp_path)
    marker = _marker(pack_id="embedding.bge-cpu",
                     runtime_template="llama-cpp-linux-cpu-v1")
    from inferfabric.packs.managed_runtime import ResolvedRuntime
    inst = ResolvedRuntime(
        pack_id="embedding.bge-cpu", pack_version="1.0.0",
        template_name="llama-cpp-linux-cpu-v1",
        artifact_identities=(_A64,), role="owned",
        model_name="pack-bge-cpu-1-0-0", marker=dict(marker),
        pack_dir=str(pack_dir))

    from inferfabric.config import LlamaCppConfig
    cfg = LlamaCppConfig(model_path=str(pack_dir / "models" / "model"),
                         binary_path=str(pack_dir / "runtime" / "engine"),
                         served_name="bge-m3", port=8123, threads=4,
                         context_size=8192,
                         gpu_layers=99,   # 即使 YAML 误配 GPU，受管也强制 CPU
                         extra_flags="--alias bge-m3")

    popens, probes, killpgs = [], [], []

    class _FakeProc:
        pid = 4321
        def poll(self):
            return None

    def fake_popen(cmd, **kw):
        popens.append(cmd)
        assert kw.get("start_new_session") is True, "进程组隔离（start_new_session）"
        return _FakeProc()

    monkeypatch.setattr(
        "inferfabric.process_manager.llama_cpp.subprocess.Popen", fake_popen)
    monkeypatch.setattr("inferfabric.process_manager.llama_cpp.time.sleep",
                        lambda s: None)
    monkeypatch.setattr("inferfabric.process_manager.llama_cpp.wait_http",
                        lambda url, timeout=120: True)

    class _FakeResp:
        status = 200
        def read(self):
            return _json.dumps({"data": [{"embedding": [0.0]}]}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=10):
        probes.append(req)
        return _FakeResp()

    import urllib.request as _urlreq
    monkeypatch.setattr(_urlreq, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        "inferfabric.process_manager.llama_cpp.os.killpg",
        lambda pgid, sig: killpgs.append((pgid, sig)))

    from inferfabric.process_manager.llama_cpp import LlamaCppProcessManager
    from inferfabric.state import StateDB
    pm = LlamaCppProcessManager(StateDB(tmp_path / "s.db"), tmp_path)
    r = pm.start_llama_cpp(cfg, "embedding", managed=inst)
    assert r["status"] == "healthy", r

    cmd = popens[0]
    assert cmd[0] == str(pack_dir / "runtime" / "engine"), \
        "必须用包内核准二进制（approved runtime path）"
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1", "受管绑定 loopback"
    assert "-ngl" not in cmd, "受管 embedding 强制 CPU（gpu_layers 不透传）"
    assert "--embeddings" in cmd
    # 就绪探测超越 /health：真实 /v1/embeddings 机制探测
    assert len(probes) == 1 and probes[0].full_url.endswith("/v1/embeddings")
    assert killpgs == []    # 成功路径不收割

    # 探测失败 → 进程组收割（degraded 隔离的清理手段；不碰 GPU 锁/复位）
    probes.clear()
    popens.clear()
    killpgs.clear()

    class _BadResp:
        status = 500
        def read(self):
            return b"boom"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(_urlreq, "urlopen", lambda req, timeout=10: _BadResp())
    r2 = pm.start_llama_cpp(cfg, "embedding", managed=inst)
    assert r2["status"] == "error" and "embeddings probe failed" in r2["message"]
    assert killpgs and killpgs[0][0] == 4321, "失败路径必须收割进程组"


def test_uninstall_managed_pack_never_deletes_borrowed_weights(tmp_path):
    """role=external（借用既有产物）：卸载只删包自有目录与受控 YAML，
    绝不触碰借用权重（spec §7.2「只删包自有」）。"""
    store, pack_dir = _bge_pack(tmp_path, marker=_marker(
        pack_id="embedding.bge-cpu", runtime_template="llama-cpp-linux-cpu-v1",
        role="external"))
    borrowed = tmp_path / "borrowed" / "bge-m3.gguf"
    borrowed.parent.mkdir(parents=True, exist_ok=True)
    borrowed.write_text("borrowed-weights", encoding="utf-8")

    # 受控 YAML 的 model_path 指向包外借用权重
    models_d = tmp_path / "models.d"
    models_d.mkdir(exist_ok=True)
    (models_d / "pack-bge-cpu-1-0-0.yaml").write_text(yaml.safe_dump({
        "name": "pack-bge-cpu-1-0-0", "type": "llama_cpp",
        "gpu_role": "none", "model_type": "embedding",
        "managed_by_pack": _marker(pack_id="embedding.bge-cpu",
                                   runtime_template="llama-cpp-linux-cpu-v1",
                                   role="external"),
        "llama_cpp": {"model_path": str(borrowed), "binary_path":
                      str(pack_dir / "runtime" / "engine"),
                      "served_name": "bge-m3", "port": 8123,
                      "threads": 4, "context_size": 8192,
                      "gpu_layers": 0},
    }), encoding="utf-8")

    from inferfabric.packs.schema import parse_manifest
    manifest = parse_manifest(f"""
pack_id: embedding.bge-cpu
pack_version: 1.0.0
capability: embedding
model: {{id: bge-m3, parameter_count_b: 1, quantization: q8_0, license: mit}}
runtime: {{engine: llama_cpp, runtime_template: llama-cpp-linux-cpu-v1}}
resources: {{download_mb: 1200, disk_mb: 1500, peak_memory_mb: 1500, gpu_role: none}}
lifecycle: {{mode: on_demand}}
network: {{listen: loopback, allowed_upstream: []}}
artifacts:
  - {{id: model, kind: model, source: "https://example.com/bge.gguf", sha256: "{_A64}"}}
execution: {{model_config_template: llama_cpp_embedding_v1, healthcheck: openai_chat_ready_v1}}
sandbox: {{policy: verified-local-pack-v1, degraded_install_allowed: true}}
""")
    from inferfabric.packs.uninstaller import PackUninstaller
    un = PackUninstaller(root=tmp_path / "packs_root", store=store,
                         models_d=models_d, supervisor=MagicMock(),
                         manifest_for=lambda pid, *a, **k: manifest,
                         is_alive_fn=lambda names: False)
    code, payload = un.uninstall("embedding.bge-cpu", confirmed=True)
    assert code == 200, payload
    assert borrowed.read_text(encoding="utf-8") == "borrowed-weights", \
        "借用权重绝不删除"
    assert not (tmp_path / "packs_root" / "embedding.bge-cpu").exists()
    assert not (models_d / "pack-bge-cpu-1-0-0.yaml").exists()
