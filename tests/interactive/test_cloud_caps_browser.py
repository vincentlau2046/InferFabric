"""tests/interactive/test_cloud_caps_browser.py — 云面板视觉 chip 浏览器门禁

7.9.0「能力归模型 / 池归优先级」的真实浏览器钉子：云模型的视觉声明
入口 = 云面板能力 chip（supports_vision），这是 catalog vision=true 与
带图请求可路由的唯一云侧来源。必须钉住：

1. 单 chip 两态渲染：每模型行只有「视觉」一个能力 chip（supports_tools
   chip 已退役，面板无入口）；点亮 = 已声明（.on，title 注明进池自动带
   视觉）；未声明 = 灰。7.8.0 遗留的显式 false 与 null 同态渲染
   （.x 排除态随约束语义退役）。
2. 点灭不落 false：点击亮 chip → 服务端解除声明（键移除回 None），
   YAML 不落 false —— 「点一下就造出约束且无回路」的单向陷阱必须
   不存在（7.8.0 cross-review I-1 语义沿承）。
3. 遗留 false / 未声明均可一次点击声明（→ true）。
4. 添加模型表单只有「视觉」声明复选框（无 tools 声明入口）。
5. 两态 chip 双主题对比度 ≥ 4.5:1（防「面板可用但不可读」）。

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
# 三模型对应三种 YAML 态：untagged（无键）/ declared（true 声明）/
# legacy-false（7.8.0 遗留显式 false — 渲染须与未声明同态）
UNTAGGED, DECLARED, LEGACY_FALSE = "untagged-m", "declared-m", "legacy-false-m"

# .on/灰 两态 chip 的对比度扫描（色值取 computed style，元素透明度并入）
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


def _chip(model, cap="supports_vision"):
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
        f"      {DECLARED}:\n        model_id: {DECLARED}\n"
        "        supports_vision: true\n"
        f"      {LEGACY_FALSE}:\n        model_id: {LEGACY_FALSE}\n"
        "        supports_vision: false\n",
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
    page.wait_for_selector(_chip(LEGACY_FALSE))


# ── 1. 单 chip 两态渲染 + 点击往返（点灭不落 false）+ 添加表单 + 对比度 ──

def test_01_vision_chip_two_state_roundtrip_and_contrast(page, caps_base):
    base, home = caps_base
    _open_cpm_panel(page, base)
    # 单 chip 契约：全面板无 supports_tools chip（退役无入口）；每模型
    # 行恰一个 chip
    n_tools = page.eval_on_selector_all(
        'button[data-cap="supports_tools"]', "els => els.length")
    assert n_tools == 0, "supports_tools chip 已退役，面板不得再有入口"
    for m in (UNTAGGED, DECLARED, LEGACY_FALSE):
        n = page.eval_on_selector_all(
            f'button[data-action="cpm-tag"][data-model="{m}"]', "els => els.length")
        assert n == 1, f"{m}: 期望恰一个能力 chip，实得 {n}"
    # 两态渲染：declared 点亮（.on，title 注明进池自动带视觉）；untagged
    # 灰（title 指路声明）；legacy-false 与未声明同态（.x 随 7.8.0 约束
    # 语义退役）
    assert _chip_class(page, DECLARED) == "cpm-cap-chip on", \
        _chip_class(page, DECLARED)
    assert "已声明" in _chip_title(page, DECLARED) \
        and "进池候选自动带视觉" in _chip_title(page, DECLARED), \
        _chip_title(page, DECLARED)
    assert _chip_class(page, UNTAGGED) == "cpm-cap-chip", \
        _chip_class(page, UNTAGGED)
    assert "未声明视觉" in _chip_title(page, UNTAGGED), \
        _chip_title(page, UNTAGGED)
    assert _chip_class(page, LEGACY_FALSE) == "cpm-cap-chip", \
        f"遗留 false 必须与未声明同态渲染: {_chip_class(page, LEGACY_FALSE)}"
    # 添加模型表单：只有「视觉」声明复选框（无 tools 声明入口）
    assert page.eval_on_selector_all(
        ".cpm-cap-vision", "els => els.length") >= 1, "添加表单缺视觉复选框"
    assert page.eval_on_selector_all(
        ".cpm-cap-tools", "els => els.length") == 0, \
        "添加表单不得再有 tools 声明入口"
    # .on + 灰 两态 chip 双主题对比度 ≥ 4.5:1
    for theme in ("dark", "light"):
        if theme == "light":
            page.eval_on_selector(
                "html", "el => el.setAttribute('data-theme', 'light')")
        else:
            page.eval_on_selector(
                "html", "el => el.removeAttribute('data-theme')")
        page.wait_for_timeout(150)
        results = page.eval_on_selector("#tab-cloud", _CHIP_CONTRAST_JS)
        assert len(results) >= 3, f"{theme}: 三模型 chip 未全进扫描面: {results}"
        assert any(r["cls"] == "cpm-cap-chip on" for r in results), \
            f"{theme}: 点亮态未进对比度扫描面: {results}"
        offenders = [r for r in results if r["ratio"] < 4.5]
        assert not offenders, (
            f"{theme}: chip 对比度 <4.5:1: "
            f"{json.dumps(offenders, ensure_ascii=False)}")
    # 未声明 → 点击声明（→ true）+ YAML 落 true
    page.click(_chip(UNTAGGED))
    _wait_class(page, UNTAGGED, "cpm-cap-chip on")
    models = _read_caps_yaml(home)["providers"][PROVIDER]["models"]
    assert models[UNTAGGED]["supports_vision"] is True, models[UNTAGGED]
    # 遗留 false → 点击同样一次到位（→ true）
    page.click(_chip(LEGACY_FALSE))
    _wait_class(page, LEGACY_FALSE, "cpm-cap-chip on")
    models = _read_caps_yaml(home)["providers"][PROVIDER]["models"]
    assert models[LEGACY_FALSE]["supports_vision"] is True, models[LEGACY_FALSE]
    # 点击亮 chip → 解除声明（键移除回 None）— 不得落 false（单向陷阱）
    page.click(_chip(DECLARED))
    _wait_class(page, DECLARED, "cpm-cap-chip")
    models = _read_caps_yaml(home)["providers"][PROVIDER]["models"]
    assert "supports_vision" not in models[DECLARED], models[DECLARED]
    # 刚声明的同样可解除（往返回路闭合）
    page.click(_chip(UNTAGGED))
    _wait_class(page, UNTAGGED, "cpm-cap-chip")
    models = _read_caps_yaml(home)["providers"][PROVIDER]["models"]
    assert "supports_vision" not in models[UNTAGGED], models[UNTAGGED]


# ── 2. console 零 error + 零未捕获异常 ─────────────────────────────

def test_02_console_zero_errors(console_log):
    favicon = [c for c in console_log["console"]
               if c["type"] == "error" and "favicon" not in c["text"]
               and "favicon" not in c["url"]]
    assert not favicon, f"console error: {json.dumps(favicon, ensure_ascii=False)}"
    assert not console_log["pageerror"], \
        f"未捕获 JS 异常: {console_log['pageerror']}"
