"""tests/interactive/test_cloud_caps_browser.py — 云面板能力 chip 三态浏览器门禁

7.8.0 cross-review I-1 的真实浏览器钉子：云面板能力 chips 与池候选约束
共享 supports_* 字段，而 7.8.0 起显式 false = 活跃约束（catalog 排除
词表）。必须钉住：

1. 三态渲染可区分：true=确认（.on）/ null=未声明（灰，title 注明默认
   全能力）/ false=显式排除（.x 警示态，title 说明约束后果）。
2. 点灭不造约束：点击亮 chip → 服务端解除打标（键移除回 None），YAML
   不落 false —— 「点一下就造出池候选拒 tools 且无回路」的单向陷阱
   必须不存在。
3. YAML 显式 false（约束）可被一次点击解除（→ true）。
4. .x 警示态双主题对比度 ≥ 4.5:1（新 UI 态进扫描面，防「面板可用但
   不可读」翻车）。

fixture 复用 test_model_pools_browser 的 scratch 仓库构建（符号链接
inferfabric/_deps + 独立 models.d + HOME 覆盖空数据目录）；云 provider
三模型静态注册（零探测零出网，base_url 指向不存在的本机端口，api_key
为自造占位值，绝非真实凭据）。云面板不依赖任何本地模型在跑 — 无桩引擎。
"""

import importlib.util
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

try:
    from playwright.sync_api import sync_playwright
    _PW_IMPORT_ERROR = None
except Exception as _e:  # noqa: BLE001
    sync_playwright = None
    _PW_IMPORT_ERROR = _e

# 复用模型池门禁的 scratch 构建工具（同目录模块按路径加载 — tests/ 无
# __init__.py，包导入不可用；被加载模块 import 期无副作用）
_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "_mpb_helpers", _HERE / "test_model_pools_browser.py")
_mpb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mpb)

PROVIDER = "iff-caps"
# 三模型对应三态：untagged（无键）/ confirmed（true）/ excluded（false 约束）
UNTAGGED, CONFIRMED, EXCLUDED = "untagged-m", "confirmed-m", "excluded-m"

# .x/.on/灰 三态 chip 的对比度扫描（色值取 computed style，元素透明度并入）
_CHIP_CONTRAST_JS = """
el => {
  const parse = (s) => {
    const m = s.match(/rgba?\\(([\\d.]+), ?([\\d.]+), ?([\\d.]+)(?:, ?([\\d.]+))?\\)/);
    return m ? [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]] : null;
  };
  const lum = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92
                        : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
  };
  const blend = (top, under) => top.map((v, i) =>
    i < 3 ? Math.round(top[3] * v + (1 - top[3]) * under[i]) : 1);
  const resolveBg = (start) => {
    let node = start, stack = [];
    while (node && node !== document.documentElement) {
      const c = parse(getComputedStyle(node).backgroundColor);
      if (c && c[3] >= 1) { stack.push(c); break; }
      if (c && c[3] > 0) stack.push(c);
      node = node.parentElement;
    }
    let bg = parse(getComputedStyle(document.documentElement).backgroundColor)
             || [255, 255, 255, 1];
    for (let i = stack.length - 1; i >= 0; i--) bg = blend(stack[i], bg);
    return bg;
  };
  const out = [];
  for (const n of el.querySelectorAll('.cpm-cap-chip')) {
    const st = getComputedStyle(n);
    const fg = parse(st.color);
    if (!fg) continue;
    const bg = resolveBg(n);
    const op = parseFloat(st.opacity);
    const fgEff = [fg[0], fg[1], fg[2], fg[3] * (isNaN(op) ? 1 : op)];
    const l1 = lum(blend(fgEff, bg)), l2 = lum(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    out.push({cls: n.className, text: n.textContent.trim(),
              ratio: Math.round(ratio * 100) / 100});
  }
  return out;
}
"""


def _chip(model, cap="supports_tools"):
    return (f'button[data-action="cpm-tag"][data-provider="{PROVIDER}"]'
            f'[data-model="{model}"][data-cap="{cap}"]')


def _chip_class(page, model):
    return page.eval_on_selector(_chip(model), "e => e.className")


def _chip_title(page, model):
    return page.eval_on_selector(_chip(model), "e => e.title")


def _wait_class(page, model, expect, timeout_s=15):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = _chip_class(page, model)
        if last == expect:
            return last
        time.sleep(0.2)
    pytest.fail(f"chip {model} 未翻到 {expect!r}: last={last!r}")


def _read_caps_yaml(home: Path) -> dict:
    import yaml
    with open(home / ".inferfabric" / "cloud_provider.yaml") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="session")
def caps_base(tmp_path_factory):
    """scratch 代理（session 级）：iff-caps provider 三模型静态注册。"""
    if _PW_IMPORT_ERROR:
        pytest.fail(f"playwright 不可用 — 硬门禁必须 fail 不 skip: "
                    f"{_PW_IMPORT_ERROR}")
    base_dir = tmp_path_factory.mktemp("iff-caps-gate")
    repo = _mpb._build_scratch_repo(base_dir)
    home = base_dir / "scratch-home"
    data_dir = home / ".inferfabric"
    data_dir.mkdir(parents=True)
    (data_dir / "cloud_provider.yaml").write_text(
        "providers:\n"
        f"  {PROVIDER}:\n"
        '    openai_base: "http://127.0.0.1:9/v1"\n'
        '    anthropic_base: "http://127.0.0.1:9/v1"\n'
        '    api_key: "iff-caps-gate-fake-key"\n'
        "    models:\n"
        f"      {UNTAGGED}:\n        model_id: {UNTAGGED}\n"
        f"      {CONFIRMED}:\n        model_id: {CONFIRMED}\n"
        "        supports_tools: true\n"
        f"      {EXCLUDED}:\n        model_id: {EXCLUDED}\n"
        "        supports_tools: false\n",
        encoding="utf-8")
    port = _mpb._free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ,
               HOME=str(home),
               EDGE_PROXY_HOST="127.0.0.1",
               EDGE_PROXY_PORT=str(port))
    proc = subprocess.Popen(
        ["python3", "-m", "inferfabric.proxy.handler", "--async"],
        cwd=str(repo), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        health = _mpb._wait_health(base)
        assert health.get("status") == "ok", health
        yield base, home
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture(scope="session")
def console_log():
    return {"console": [], "pageerror": []}


@pytest.fixture
def page(caps_base, console_log):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 960})
        pg = context.new_page()
        pg.on("console", lambda m: console_log["console"].append(
            {"type": m.type, "text": m.text,
             "url": (m.location or {}).get("url", "")}))
        pg.on("pageerror", lambda e: console_log["pageerror"].append(str(e)))
        pg.set_default_timeout(10_000)
        try:
            yield pg
        finally:
            context.close()
            browser.close()


def _open_cpm_panel(page, base):
    page.goto(base + "/", wait_until="domcontentloaded")
    page.click('button[data-tab="tab-cloud"]')
    page.wait_for_selector(f'[data-action="provider-models"][data-provider="{PROVIDER}"]')
    page.click(f'[data-action="provider-models"][data-provider="{PROVIDER}"]')
    page.wait_for_selector(_chip(EXCLUDED))


# ── 1. 三态渲染 + 点击往返（点灭不落 false / 约束可解除）+ 对比度 ────

def test_01_tri_state_render_click_roundtrip_and_contrast(page, caps_base):
    base, home = caps_base
    _open_cpm_panel(page, base)
    # 三态渲染：untagged 灰（title 注明默认全能力）/ confirmed .on /
    # excluded .x（title 说明约束后果）
    assert _chip_class(page, UNTAGGED) == "cpm-cap-chip", _chip_class(page, UNTAGGED)
    assert "默认全能力" in _chip_title(page, UNTAGGED), _chip_title(page, UNTAGGED)
    assert _chip_class(page, CONFIRMED) == "cpm-cap-chip on"
    assert _chip_class(page, EXCLUDED) == "cpm-cap-chip x"
    assert "显式排除" in _chip_title(page, EXCLUDED) \
        and "拒绝" in _chip_title(page, EXCLUDED), _chip_title(page, EXCLUDED)
    # .x 警示态 + .on + 灰 三态 chip 双主题对比度 ≥ 4.5:1
    for theme in ("dark", "light"):
        if theme == "light":
            page.eval_on_selector(
                "html", "el => el.setAttribute('data-theme', 'light')")
        else:
            page.eval_on_selector(
                "html", "el => el.removeAttribute('data-theme')")
        page.wait_for_timeout(150)
        results = page.eval_on_selector("#tab-cloud", _CHIP_CONTRAST_JS)
        assert len(results) >= 6, f"{theme}: 三态 chip 未全进扫描面: {results}"
        offenders = [r for r in results if r["ratio"] < 4.5]
        assert not offenders, (
            f"{theme}: chip 对比度 <4.5:1: "
            f"{json.dumps(offenders, ensure_ascii=False)}")
    # 点击 excluded chip → true（约束解除 → 确认态）+ YAML 落 true
    page.click(_chip(EXCLUDED))
    _wait_class(page, EXCLUDED, "cpm-cap-chip on")
    models = _read_caps_yaml(home)["providers"][PROVIDER]["models"]
    assert models[EXCLUDED]["supports_tools"] is True, models[EXCLUDED]
    # 点击亮 chip → 解除打标（键移除回 None）— 不得落 false（I-1 单向陷阱）
    page.click(_chip(EXCLUDED))
    _wait_class(page, EXCLUDED, "cpm-cap-chip")
    models = _read_caps_yaml(home)["providers"][PROVIDER]["models"]
    assert "supports_tools" not in models[EXCLUDED], models[EXCLUDED]
    assert "supports_tools" not in models[UNTAGGED], models[UNTAGGED]
    # confirmed 同样点灭 = 解除（回灰），键移除
    page.click(_chip(CONFIRMED))
    _wait_class(page, CONFIRMED, "cpm-cap-chip")
    models = _read_caps_yaml(home)["providers"][PROVIDER]["models"]
    assert "supports_tools" not in models[CONFIRMED], models[CONFIRMED]


# ── 2. console 零 error + 零未捕获异常 ─────────────────────────────

def test_02_console_zero_errors(console_log):
    favicon = [c for c in console_log["console"]
               if c["type"] == "error" and "favicon" not in c["text"]
               and "favicon" not in c["url"]]
    assert not favicon, f"console error: {json.dumps(favicon, ensure_ascii=False)}"
    assert not console_log["pageerror"], \
        f"未捕获 JS 异常: {console_log['pageerror']}"
