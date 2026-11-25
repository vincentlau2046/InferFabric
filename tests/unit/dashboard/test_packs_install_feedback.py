"""能力包安装反馈自解释（issue #49）：运行真实 packs.js，不访问网络。

- 安装 422（admission 拒绝，reason/kind/message/action 载荷）→ toast 展示
  人话 + 下一步；未登记 kind（409 等）保持原文 toast 形态。
- 装机状态 failed 行（kind=download）→ 行内渲染「原因/下一步」+ 重试按钮。
- failed 行（kind=runtime）→ 有原因与下一步，无重试按钮。
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
PACKS_JS = ROOT / "inferfabric" / "dashboard" / "js" / "packs.js"

_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

const toasts = [];
const elements = {};
for (const id of ['pkStatus', 'pkCatalog', 'pkPolicyMaxB', 'pkPolicyReserve']) {
  elements[id] = {id, innerHTML: '', value: ''};
}
const clickListeners = [];
const tabCallbacks = {};
let fetchResponses = {};

const window = {
  tabRenderers: {},
  UI: {
    toast: (message, kind) => toasts.push({message, kind}),
    adminHeaders: () => ({'Content-Type': 'application/json'}),
    confirm: (options) => { confirmOptions = options; },
    icon: () => '<i>icon</i>',
  },
  store: {
    on: (ev, fn) => { if (ev === 'tab_active') tabCallbacks[ev] = fn; },
  },
};
let confirmOptions;
const fetch = async (url, options) => {
  const spec = fetchResponses[url] || {status: 200, body: {}};
  return {
    ok: spec.status >= 200 && spec.status < 300,
    status: spec.status,
    json: async () => spec.body,
  };
};
const document = {
  getElementById: (id) => elements[id] || null,
  addEventListener: (type, fn) => { if (type === 'click') clickListeners.push(fn); },
  activeElement: null,
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {
  window, document, fetch, console, navigator: {},
  setInterval: () => 0, clearInterval: () => {},   // 轮询桩（不吊住进程）
});

async function drain() {   // 微任务排空（fetch promise 链）
  for (let i = 0; i < 500; i++) await Promise.resolve();
}

function clickInstall(packId) {
  const btn = {
    disabled: false,
    getAttribute: (k) => (k === 'data-action' ? 'install' : packId),
  };
  const ev = {
    target: {
      closest: (sel) => (sel === '[data-action]' ? btn : {id: 'tab-packs'}),
    },
  };
  for (const fn of clickListeners) fn(ev);
}

(async () => {
  fetchResponses = {
    '/admin/packs/status': {status: 200, body: {packs: input.packs || []}},
    '/admin/packs/catalog': {status: 200, body: {packs: []}},
    '/admin/packs/policy': {status: 200, body: {max_model_parameters_b: 8,
                                                system_reserved_memory_mb: 2048}},
  };
  window.tabRenderers['tab-packs']();
  await drain();

  if (input.expect === 'status-row') {
    const html = elements.pkStatus.innerHTML;
    assert.ok(html.includes('原因：'), 'failed row must render 原因: ' + html);
    assert.ok(html.includes(input.messageFragment), 'human message in row');
    assert.ok(html.includes('下一步：'), 'action in row');
    assert.equal(html.includes('data-action="install"'),
                 input.expectRetry, 'retry button presence');
    console.log(JSON.stringify({html}));
    process.exit(0);
  }

  // 安装 422 → 分类 toast
  fetchResponses['/admin/packs/install'] = {status: input.httpStatus,
                                            body: input.body};
  clickInstall('llm.x');
  await drain();
  assert.ok(toasts.length >= 1, 'install failure must toast');
  console.log(JSON.stringify({toasts}));
  process.exit(0);
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
"""


@pytest.fixture
def run_packs(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    harness = tmp_path / "packs_feedback_harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")

    def run(input_obj):
        proc = subprocess.run([node, str(harness), str(PACKS_JS)],
                              input=json.dumps(input_obj),
                              capture_output=True, text=True, timeout=15)
        assert proc.returncode == 0, (
            f"node harness failed:\nstdout:{proc.stdout}\nstderr:{proc.stderr}")
        return json.loads(proc.stdout)

    return run


def test_install_422_admission_toasts_human_message_and_action(run_packs):
    result = run_packs({
        "httpStatus": 422,
        "body": {"error": "admission rejected (install)",
                 "reason": "resource_busy", "kind": "admission",
                 "message": "GPU 忙 — 托管能力包仅允许 GPU idle 时启动，不挤占现役模型",
                 "action": "先把 GPU 切回 idle，再安装/启动该包",
                 "decision": {}},
    })
    toast = result["toasts"][0]
    assert toast["kind"] == "error"
    assert "GPU 忙" in toast["message"]
    assert "下一步" in toast["message"] and "idle" in toast["message"]
    assert "admission rejected" not in toast["message"]   # 机器短语不进 toast


def test_install_404_unknown_pack_toasts_human_message(run_packs):
    result = run_packs({
        "httpStatus": 404,
        "body": {"error": "unknown pack_id 'llm.nope'",
                 "reason": "unknown_pack_id", "kind": "not_found",
                 "message": "未知能力包 — 目录中没有该 pack_id",
                 "action": "在能力包目录中确认名称（注意大小写与拼写）"},
    })
    toast = result["toasts"][0]
    assert "未知能力包" in toast["message"]
    assert "下一步" in toast["message"]


def test_install_409_without_kind_keeps_legacy_toast(run_packs):
    result = run_packs({
        "httpStatus": 409,
        "body": {"error": "pack 'llm.x' already installed; uninstall first"},
    })
    toast = result["toasts"][0]
    assert "already installed" in toast["message"]   # 原文形态不变


def test_failed_row_download_kind_renders_hint_and_retry(run_packs):
    result = run_packs({
        "expect": "status-row", "expectRetry": True,
        "messageFragment": "下载失败",
        "packs": [{"pack_id": "llm.a", "pack_version": "1.0.0",
                   "status": "failed", "stage": "downloading",
                   "error": "download_error:URLError",
                   "reason": "download_error", "kind": "download",
                   "message": "下载失败 — 网络/下载源不可达",
                   "action": "点击重试重新发起安装",
                   "sandbox_degraded": False, "installed_at": None,
                   "install_path": None, "affected_pools": []}],
    })
    assert "download_error:URLError" not in result["html"]   # 机器错误码不进行内文案
    assert "重试" in result["html"]


def test_failed_row_runtime_kind_no_retry_button(run_packs):
    result = run_packs({
        "expect": "status-row", "expectRetry": False,
        "messageFragment": "隔离健康检查",
        "packs": [{"pack_id": "llm.b", "pack_version": "1.0.0",
                   "status": "failed", "stage": "isolated_healthcheck",
                   "error": "healthcheck_error:Timeout",
                   "reason": "healthcheck_error", "kind": "runtime",
                   "message": "隔离健康检查未通过 — 包在本机自检失败",
                   "action": "查看网关日志定位",
                   "sandbox_degraded": False, "installed_at": None,
                   "install_path": None, "affected_pools": []}],
    })
    assert "下一步：查看网关日志定位" in result["html"]
    assert "重试" not in result["html"]


def test_failed_row_unregistered_error_keeps_original_text(run_packs):
    result = run_packs({
        "expect": "status-row", "expectRetry": False,
        "messageFragment": "parameter_count_b 40",
        "packs": [{"pack_id": "llm.c", "pack_version": "1.0.0",
                   "status": "failed", "stage": "preflight",
                   "error": "parameter_count_b 40 > max 8",
                   "reason": None, "kind": "unknown",
                   "message": "未登记的失败原因：parameter_count_b 40 > max 8",
                   "action": "查看网关日志（~/.inferfabric/logs/proxy.log）定位，"
                             "修复后重试；或在本机用 CLI 重试",
                   "sandbox_degraded": False, "installed_at": None,
                   "install_path": None, "affected_pools": []}],
    })
    assert "parameter_count_b 40" in result["html"]   # 原文不丢
    assert 'data-action="install"' not in result["html"]   # 无重试按钮
