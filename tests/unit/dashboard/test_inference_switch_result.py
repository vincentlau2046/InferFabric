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
