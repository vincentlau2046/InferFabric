"""推理页主色纪律 + 停止卡塌缩（2026-11-20 impeccable 评审 P2 修复）。

运行真实 inference.js（node vm，无网络/GPU）：
  - GPU=idle 时整页至多一张「建议启动」卡（独占组首个停止模型，btn-pri）；
    GPU 非 idle 时零推荐。
  - 停止卡卡体 = 2 槽摘要（模式/精度），无 端口/uptime 死列；
    运行卡保留 4 槽（端口/uptime/精度/场景）。
  - 侧栏 nav 4 组标题 + 9 项 + aria-current（IA v3.1，2026-11-24；base.html / store.js）。
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
INFERENCE_JS = ROOT / "inferfabric" / "dashboard" / "js" / "inference.js"
BASE_HTML = ROOT / "inferfabric" / "dashboard" / "base.html"
STORE_JS = ROOT / "inferfabric" / "dashboard" / "js" / "store.js"

_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

function el() {
  return {
    textContent: '',
    innerHTML: '',
    style: {},
    disabled: false,
    className: '',
    setAttribute: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    scrollIntoView: () => {},
    classList: { toggle: () => {}, add: () => {}, remove: () => {} },
  };
}
const els = {};
const window = {
  tabRenderers: {},
  UI: {
    toast: () => {},
    adminHeaders: () => ({}),
    confirm: () => {},
    icon: (name) => '[icon:' + name + ']',
    fmtDur: (s) => s + 's',
    fmtNum: (n) => String(n),
  },
  store: {
    on: () => {},
    forceRefresh: () => {},
    get: (key) => input.snapshot[key],
    set: () => {},
  },
};
const document = {
  getElementById: (id) => (els[id] = els[id] || el()),
  querySelectorAll: () => [],
  querySelector: () => null,
  addEventListener: () => {},
  removeEventListener: () => {},
  createElement: () => el(),
  body: { appendChild: () => {} },
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {
  window, document, fetch: async () => ({ ok: true, json: async () => ({}) }), console,
});
window.tabRenderers['tab-inference']();
console.log(JSON.stringify({
  excl: els['infExclList'].innerHTML,
  shrd: els['infShrdList'].innerHTML,
  free: els['infFreeList'].innerHTML,
}));
"""


@pytest.fixture
def render_inference(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    harness = tmp_path / "inference_discipline_harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")

    def run(snapshot):
        proc = subprocess.run(
            [node, str(harness), str(INFERENCE_JS)],
            input=json.dumps({"snapshot": snapshot}),
            capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode == 0, (
            f"node harness failed:\nstdout:{proc.stdout}\nstderr:{proc.stderr}"
        )
        return json.loads(proc.stdout)

    return run


def _snapshot(gpu_mode):
    return {
        "models": [
            {"name": "model-a", "mode": "exclusive", "active": True,
             "type": "vllm", "quantization": "NVFP4"},
            {"name": "model-b", "mode": "exclusive", "active": False,
             "type": "vllm", "quantization": "NVFP4"},
            {"name": "model-c", "mode": "exclusive", "active": False,
             "quantization": None},
            {"name": "shared-1", "mode": "shared", "active": False, "type": "ollama"},
        ],
        "sleep_states": {},
        "services_info": {"model-a": {"mode": "exclusive", "type": "vllm", "port": 8001}},
        "status": {"gpu_mode": gpu_mode, "scenario_active": {}},
        "local_models": {},
    }


def test_gpu_idle_recommends_exactly_one_start(render_inference):
    out = render_inference(_snapshot("idle"))
    # 整页唯一 btn-pri，落在独占组；建议徽标唯一
    assert out["excl"].count("btn btn-pri") == 1
    assert out["shrd"].count("btn btn-pri") == 0
    assert out["free"].count("btn btn-pri") == 0
    assert out["excl"].count(">建议</span>") == 1
    # 推荐落在独占组第一个停止模型（model-a active，model-b 首个停止）
    b_pos = out["excl"].find("model-b")
    assert b_pos != -1
    assert out["excl"].find("btn btn-pri") > b_pos


def test_gpu_busy_no_recommendation(render_inference):
    out = render_inference(_snapshot("exclusive"))
    assert out["excl"].count("btn btn-pri") == 0
    assert "建议" not in out["excl"]
    assert out["shrd"].count("btn btn-pri") == 0


def test_sleeping_exclusive_excluded_from_recommendation(render_inference):
    # model-b 停止但处于 L2 sleep → 推荐跳到 model-c（非 sleeping 的首个停止卡）
    snap = _snapshot("idle")
    snap["sleep_states"] = {"model-b": {"state": "sleeping"}}
    out = render_inference(snap)
    assert out["excl"].count("btn btn-pri") == 1
    assert out["excl"].count(">建议</span>") == 1
    c_pos = out["excl"].find("model-c")
    assert c_pos != -1
    assert out["excl"].find("btn btn-pri") > c_pos


def test_missing_status_never_recommends(render_inference):
    # cross-review nit4：status 缺失/异常（无 gpu_mode）按非空闲处理，不推荐
    snap = _snapshot("exclusive")
    snap["status"] = {}
    out = render_inference(snap)
    assert out["excl"].count("btn btn-pri") == 0
    assert "建议" not in out["excl"]


def test_stopped_card_collapses_to_two_slots(render_inference):
    out = render_inference(_snapshot("idle"))
    # 运行卡（model-a）保留 4 槽；停止卡不再有 端口/uptime 死列
    assert out["excl"].count("<dt>端口</dt>") == 1
    assert out["excl"].count("<dt>uptime</dt>") == 1
    assert out["excl"].count("<dt>模式</dt>") == 2  # model-b + model-c
    assert out["excl"].count("<dt>精度</dt>") == 3
    # 空闲/共享组的停止卡同样塌缩
    assert out["shrd"].count("<dt>模式</dt>") == 1
    assert out["shrd"].count("<dt>端口</dt>") == 0
    # 精度缺失的停止卡显式占位 —（不回退裸空）
    assert "<dd class=\"mono\">—</dd>" in out["excl"]


def test_nav_grouped_ia_and_aria_current():
    html = BASE_HTML.read_text(encoding="utf-8")
    import re
    caps = [s.strip() for s in re.findall(
        r'<span class="nav-cap"[^>]*>([^<]+)</span>', html)]
    # IA v3.1（2026-11-24 用户修订，specs/console-ux-batch WS4）：监控为最常用入口
    # 并入状态组、异常随迁，可观测组取消（4 组 10 项 — PR3 WS4 增设设置页）
    assert caps == ["状态", "推理与池", "网关策略", "接入"]
    assert html.count('class="if-nav-item') == 10  # 9 + tab-settings（PR3 WS4）
    # 组顺序：推理在模型池前；监控/异常紧跟总览（状态组内）
    assert html.find("tab-inference") < html.find("tab-model-pools")
    assert 0 < html.find("tab-overview") < html.find("tab-monitor") < html.find("tab-anomaly")
    # 设置页位于网关策略组（鉴权之后、云端之前）
    assert 0 < html.find("tab-security") < html.find("tab-settings") < html.find("tab-cloud")
    # deploy 不再是独立 nav tab（并入能力包页 deploySection）
    assert 'data-tab="tab-deploy"' not in html
    # fallback switchTab 维护 aria-current
    assert 'setAttribute(\'aria-current\', \'page\')' in html

    packs = (ROOT / "inferfabric" / "dashboard" / "fragments" / "packs.html").read_text(encoding="utf-8")
    assert 'id="deploySection"' in packs
    # 旧 localStorage 'tab-deploy' 迁移到 'tab-packs'（否则全 tab 失活）
    store = STORE_JS.read_text(encoding="utf-8")
    assert "aria-current" in store
    assert "removeAttribute('aria-current')" in store
    assert "'tab-deploy'" in store and "'tab-packs'" in store
