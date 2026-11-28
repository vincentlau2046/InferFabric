"""Exercise shipped monitor renderers with a controlled response order, without GPU."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

MONITOR_JS = Path(__file__).resolve().parents[3] / "inferfabric/dashboard/js/monitor.js"
HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const scenario = process.argv[3];
const callbacks = {};
const requests = [];
const drawings = {};
let powerDraws = 0;
let active = null;
let models = [];
const els = Object.fromEntries(['monPowerCost', 'monPowerMeta', 'monPowerEmpty',
  'monKpis', 'monKpiModel'].map(id => [id, {innerHTML: '', textContent: '', style: {}}]));
const document = {
  getElementById: id => els[id] || null,
  querySelectorAll: () => [],
  addEventListener: () => {},
};
const window = {
  UI: { skeleton: el => { el.innerHTML = 'loading'; }, fmtNum: String },
  IFCharts: {
    create: id => id,
    update: (id, opts) => {
      if (id === 'monPowerChart') powerDraws += 1;
      drawings[id] = opts.series?.[0]?.data || [];
    },
  },
  tabRenderers: {},
  store: {
    get: field => field === 'active_services' ? models : undefined,
    on: (event, cb) => { callbacks[event] = cb; },
    isGroupActive: key => key === active,
  },
};
const fetch = url => new Promise((resolve, reject) => requests.push({url, resolve, reject}));
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {window, document, fetch, console});
const tick = () => new Promise(resolve => setImmediate(resolve));
async function run() {
  if (scenario === 'power') {
    active = 'tab-monitor.usage';
    callbacks.tab_active();
    const pending = requests.find(r => r.url === '/api/device_power?gran=hour');
    active = 'tab-monitor.kpis';
    callbacks.tab_active();
    pending.resolve({ok: true, json: async () => ({available: true,
      buckets: [{t: 1, avg_w: 42, cum_kwh: 0.6}], totals: {kwh: 0.6, yuan: 0.6}})});
    await tick();
    const hidden = drawings.monPowerChart;
    active = 'tab-monitor.usage';
    callbacks.tab_active();
    callbacks.sync_meta();
    callbacks.sync_meta();
    return {hidden, shown: drawings.monPowerChart, cost: els.monPowerCost.textContent,
      powerDraws, powerCalls: requests.filter(r => r.url.startsWith('/api/device_power?')).length};
  }
  active = 'tab-monitor.kpis';
  models = ['model-a']; callbacks.tab_active();
  models = ['model-b']; callbacks.sync_meta();
  const a = requests[0], b = requests[1];
  if (scenario === 'stale-kpi-error') a.reject(new Error('synthetic failure'));
  else a.resolve({ok: true, json: async () => ({kv_cache_usage_perc: 17})});
  await tick();
  const bPending = els.monKpis.innerHTML;
  callbacks.sync_meta();
  b.resolve({ok: true, json: async () => ({kv_cache_usage_perc: 42})});
  await tick();
  return {urls: requests.map(r => r.url), bPending, kpis: els.monKpis.innerHTML,
    label: els.monKpiModel.textContent};
}
run().then(out => console.log(JSON.stringify(out))).catch(e => {
  console.error(e); process.exitCode = 1;
});
"""


@pytest.fixture
def monitor(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    harness = tmp_path / "monitor-activation.js"
    harness.write_text(HARNESS, encoding="utf-8")

    def run(scenario):
        p = subprocess.run([node, str(harness), str(MONITOR_JS), scenario],
                           capture_output=True, text=True, timeout=10)
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout)

    return run


def test_power_sample_arriving_off_page_draws_on_return(monitor):
    out = monitor("power")
    assert out == {"hidden": [], "shown": [42], "cost": "¥0.60",
                   "powerDraws": 2, "powerCalls": 1}


@pytest.mark.parametrize("scenario", ["stale-kpi-error", "stale-kpi-success"])
def test_stale_engine_response_does_not_end_new_models_request(monitor, scenario):
    out = monitor(scenario)
    assert out["bPending"] == "loading"
    assert out["urls"] == ["/api/engine_metrics?model=model-a", "/api/engine_metrics?model=model-b"]
    assert out["label"] == "model-b" and "42.0%" in out["kpis"]
