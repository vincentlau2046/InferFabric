"""推理页切换反馈：运行真实 inference.js，不访问网络或 GPU。"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
INFERENCE_JS = ROOT / "inferfabric" / "dashboard" / "js" / "inference.js"

_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const toasts = [];
const requests = [];
let refreshes = 0;
let confirmOptions;
const window = {
  tabRenderers: {},
  UI: {
    toast: (message, kind) => toasts.push({message, kind}),
    adminHeaders: () => ({'Content-Type': 'application/json'}),
    confirm: options => { confirmOptions = options; },
  },
  store: {
    on: () => {},
    forceRefresh: () => { refreshes++; },
  },
};
const fetch = async (url, options) => {
  requests.push({url, method: options.method, body: JSON.parse(options.body)});
  return {
    ok: input.httpStatus >= 200 && input.httpStatus < 300,
    status: input.httpStatus,
    json: async () => {
      if (input.invalidJson) throw new SyntaxError('invalid JSON');
      return input.body;
    },
  };
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {
  window,
  document: {getElementById: () => null},
  fetch,
  console,
});
(async () => {
  const action = input.action;
  await window.doModelAction('RequestedModel', action);
  assert.ok(confirmOptions, 'model operation must still require confirmation');
  assert.equal(requests.length, 0, 'operation must not run before confirmation');
  await confirmOptions.onOk();
  assert.deepEqual(requests, [{
    url: '/' + action, method: 'POST', body: {model: 'RequestedModel'},
  }]);
  console.log(JSON.stringify({toasts, refreshes}));
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
"""


@pytest.fixture
def run_switch(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    harness = tmp_path / "inference_switch_harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")

    def run(body=None, *, http_status=200, invalid_json=False, action="switch"):
        proc = subprocess.run(
            [node, str(harness), str(INFERENCE_JS)],
            input=json.dumps({
                "body": body,
                "httpStatus": http_status,
                "invalidJson": invalid_json,
                "action": action,
            }),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, (
            f"node harness failed:\nstdout:{proc.stdout}\nstderr:{proc.stderr}"
        )
        return json.loads(proc.stdout)

    return run


@pytest.mark.parametrize("error_field", ["message", "error"])
def test_switch_business_failure_is_not_reported_as_success(run_switch, error_field):
    result = run_switch({"status": "error", error_field: "VRAM budget exceeded"})

    assert len(result["toasts"]) == 1
    assert result["toasts"][0]["kind"] == "error"
    assert "VRAM budget exceeded" in result["toasts"][0]["message"]


@pytest.mark.parametrize("status, message", [
    ("switched", "RequestedModel 已切换"),
    ("already_active", "RequestedModel 已在运行"),
])
def test_switch_success_refreshes_state(run_switch, status, message):
    result = run_switch({"status": status, "model": "RequestedModel"})

    assert result["toasts"] == [{"kind": "ok", "message": message}]
    assert result["refreshes"] == 1


def test_switch_rollback_does_not_claim_requested_model_started(run_switch):
    result = run_switch({
        "status": "switched", "model": "PreviousModel", "rollback": "succeeded",
    })

    assert len(result["toasts"]) == 1
    assert result["toasts"][0]["kind"] == "error"
    assert "PreviousModel" in result["toasts"][0]["message"]
    assert result["refreshes"] == 1


@pytest.mark.parametrize("body", [
    None,
    {},
    {"status": "unexpected"},
    {"status": "switched", "model": "OtherModel"},
])
def test_switch_unknown_result_is_not_reported_as_success(run_switch, body):
    result = run_switch(body)

    assert len(result["toasts"]) == 1
    assert result["toasts"][0]["kind"] == "error"


def test_switch_invalid_json_is_not_reported_as_success(run_switch):
    result = run_switch(invalid_json=True)

    assert len(result["toasts"]) == 1
    assert result["toasts"][0]["kind"] == "error"


@pytest.mark.parametrize("action", ["switch", "stop", "sleep", "wake"])
def test_do_model_action_checks_res_ok_not_just_body(run_switch, action):
    result = run_switch(
        {"message": "GPU switch in progress"}, http_status=409, action=action,
    )

    assert len(result["toasts"]) == 1
    assert result["toasts"][0]["kind"] == "error"
    assert "GPU switch in progress" in result["toasts"][0]["message"]


def test_switch_http_error_with_non_json_body_is_not_reported_as_success(run_switch):
    result = run_switch(http_status=502, invalid_json=True)

    assert len(result["toasts"]) == 1
    assert result["toasts"][0]["kind"] == "error"
    assert "502" in result["toasts"][0]["message"]


def test_switch_managed_admission_rejection_toasts_hint_and_action(run_switch):
    """issue #49：受管闸拒绝（resource_busy）→ toast 展示人话 hint 与
    下一步 action，不再复读英文原文 message；无 hint 的旧载荷仍回退。"""
    result = run_switch({
        "status": "error", "reason_code": "resource_busy",
        "message": "resource_busy: GPU busy — managed GPU pack admission is idle-only",
        "hint": "GPU 忙 — 托管能力包仅允许 GPU idle 时启动，不挤占现役模型",
        "action": "先把 GPU 切回 idle（控制台「回到 idle」或 ./iff reset），再安装/启动该包",
    })
    assert len(result["toasts"]) == 1
    toast = result["toasts"][0]
    assert toast["kind"] == "error"
    assert "GPU 忙" in toast["message"]
    assert "下一步" in toast["message"] and "idle" in toast["message"]
    assert "resource_busy: GPU busy" not in toast["message"]   # 原文不进 toast


def test_switch_legacy_error_payload_unchanged(run_switch):
    result = run_switch({"status": "error", "message": "VRAM budget exceeded"})
    assert "VRAM budget exceeded" in result["toasts"][0]["message"]
