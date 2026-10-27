"""tests/interactive/test_model_pools_browser.py — 模型池浏览器交互硬门禁（大手术 T6）

背景（2026-10-26 大手术，v7.2.0–7.4.0 翻车教训）：
  - 面板类 UI 门禁必须含真实浏览器交互 — 文本断言 + Node VM 全绿 ≠ 可用
  - 用户实测坐实两主题 select 近乎不可读（对比度地基缺口）

本文件 = 真实 chromium + 真实 scratch 代理（自有 HOME/models.d，零池起步）
跑通主路径与负路径：开面板四未配置卡 → 选模型建池 → 卡+树翻转 →
选未就绪模型被三段门禁拦住 → 换模树更新 → 高级模态 Esc →
双主题 computed contrast ≥4.5:1 → console 零 error。

隔离（零后端改动）：config.py 硬编码 IFF_DATA_DIR=Path.home()/".inferfabric"
（环境变量不被读取），故用 HOME 覆盖实现空数据目录；BASE_DIR/models.d
经「inferfabric+_deps 符号链接 + 独立 models.d」的 scratch 仓库隔离，
并注入第二个本地模型（目录隔离自证 + 负路径靶 — 它未在跑，选它必须被
三段门禁拦住）。

换模靶 = 假云 provider（同在 scratch HOME）：cloud_provider.yaml 的静态
models: 段启动即注册、零探测零出网（_register_spec_only_models）；dry-run
为纯内存选择模拟（selector.select），云候选 ready 只查注册表不看本地
active_services —— 故不启动任何真模型就能得到第二个 ready 目标。
base_url 指向不存在的本机端口（dry-run 不转发，永不命中）；api_key 为
自造占位值，绝非真实凭据。

验证测试有效性（计划 Task 6）：对未重构树（HEAD~ 大手术前）以
IFF_BROWSER_TEST_BASE 指向旧 scratch 实例跑本文件 → 必须 FAIL（向导布局/
坏对比度抓得住）；对当前树 PASS。
"""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

# 硬门禁：playwright 不可用必须 fail，不许 skip（面板 UI 门禁教训）
try:
    from playwright.sync_api import sync_playwright
    _PW_IMPORT_ERROR = None
except Exception as _e:  # noqa: BLE001
    sync_playwright = None
    _PW_IMPORT_ERROR = _e

REPO_ROOT = Path(__file__).resolve().parents[2]   # sandbox 仓库根
TXT_YAML = REPO_ROOT / "models.d" / "Qwen38-27B-TXT.yaml"
TXT_MODEL = "Qwen38-27B-TXT"                       # 唯一在跑的本地模型（独占 GPU）
SWAP_MODEL = "SwapTarget-TXT"                      # 负路径靶（scratch 注入，未在跑）
CLOUD_PROVIDER = "iff-gate"                        # 假云 provider（静态注册零出网）
CLOUD_SWAP = f"cloud:{CLOUD_PROVIDER}/swap-target"  # 换模靶（dry-run 纯内存）
# 终审 I3：401 云靶 — 指向测试内 127.0.0.1 常回 401 的服务器，为
# 树行冷却态/时间线提供真实 dispatch 失败（零外网；api_key 为自造占位值）
CLOUD_401_PROVIDER = "iff-gate-401"
CLOUD_401_SWAP = f"cloud:{CLOUD_401_PROVIDER}/unauthorized-model"

# 外部目标模式：IFF_BROWSER_TEST_BASE 指向已启动的（如未重构树的）scratch
# 代理 → 不自建实例，直接测（负向验证测试有效性用）
EXTERNAL_BASE = os.environ.get("IFF_BROWSER_TEST_BASE") or None

HEALTH_TIMEOUT_S = 30
POLL_MS = 200


def _free_port(prefer=8998):
    """优先用计划钉的 8998；被占则回退内核分配（生产 :8999 绝不碰）。"""
    for port in (prefer, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port")


def _build_scratch_repo(base: Path) -> Path:
    """scratch 仓库：inferfabric/_deps 符号链接（BASE_DIR=scratch，代码即当前
    树）+ 独立 models.d（TXT + 换模靶 SwapTarget — 目录第二选项）。"""
    repo = base / "scratch-repo"
    repo.mkdir()
    os.symlink(REPO_ROOT / "inferfabric", repo / "inferfabric")
    os.symlink(REPO_ROOT / "_deps", repo / "_deps")
    md = repo / "models.d"
    md.mkdir()
    txt = TXT_YAML.read_text(encoding="utf-8")
    (md / "Qwen38-27B-TXT.yaml").write_text(txt, encoding="utf-8")
    # 换模靶：同引擎同权重（目录只列不发请求），名/端口/容器错开
    swap = (txt
            .replace("name: Qwen38-27B-TXT", f"name: {SWAP_MODEL}")
            .replace("served_name: Qwen38-27B-TXT",
                     f"served_name: {SWAP_MODEL}")
            .replace("model_id: Qwen38-27B-TXT", f"model_id: {SWAP_MODEL}")
            .replace("container_name: iff-ninfer-qwen38",
                     "container_name: iff-ninfer-swaptarget")
            .replace("port: 8007", "port: 18907"))
    (md / f"{SWAP_MODEL}.yaml").write_text(swap, encoding="utf-8")
    return repo


class _Always401(BaseHTTPRequestHandler):
    """终审 I3：常回 401 的本机服务器 — 给云候选提供真实「可连但拒绝」
    的上游，让 dispatch 失败走 cooldown_401（连接拒绝只记 last_attempt
    不进冷却，事件环为空，树行没有可点的时间线）。"""

    def _reject(self):
        body = b'{"error": {"message": "Unauthorized"}}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 — http.server 约定
        self._reject()

    def do_GET(self):  # noqa: N802
        self._reject()

    def log_message(self, *args):   # 静默 — 不污染 pytest 输出
        pass


def _write_fake_cloud_provider(home: Path, port_401: int) -> None:
    """假云 provider → scratch HOME/.inferfabric/cloud_provider.yaml。

    静态 models: 段启动即注册（_register_spec_only_models，零探测零出网），
    为换模测试提供第二个 ready 目标（云候选 ready 只查注册表；本地候选
    必须真在跑，而 scratch 不启动任何模型）。dry-run 纯内存不转发，故
    base_url 指向不存在的端口也永不命中；api_key 为自造占位值。
    iff-gate-401（终审 I3）指向测试内常回 401 的本机服务器 — 真实
    dispatch 失败 → cooldown_401 事件 → 树行冷却态 + 可点时间线。
    """
    data_dir = home / ".inferfabric"
    data_dir.mkdir()
    (data_dir / "cloud_provider.yaml").write_text(
        "providers:\n"
        f"  {CLOUD_PROVIDER}:\n"
        '    openai_base: "http://127.0.0.1:9/v1"\n'
        '    anthropic_base: "http://127.0.0.1:9/v1"\n'
        '    api_key: "iff-browser-gate-fake-key"\n'
        "    models:\n"
        "      swap-target:\n"
        "        model_id: swap-target\n"
        f"  {CLOUD_401_PROVIDER}:\n"
        f'    openai_base: "http://127.0.0.1:{port_401}/v1"\n'
        f'    anthropic_base: "http://127.0.0.1:{port_401}/v1"\n'
        '    api_key: "iff-browser-gate-fake-key"\n'
        "    models:\n"
        "      unauthorized-model:\n"
        "        model_id: unauthorized-model\n",
        encoding="utf-8")


def _wait_health(base: str):
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    last = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
        time.sleep(0.3)
    raise RuntimeError(f"scratch proxy /health timeout: {last}")


def _get_json(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


@pytest.fixture(scope="session")
def scratch_base(tmp_path_factory):
    """启动 scratch 代理（session 级）。EXTERNAL_BASE 模式下直接用外部目标。"""
    if EXTERNAL_BASE:
        yield EXTERNAL_BASE
        return
    if _PW_IMPORT_ERROR:
        pytest.fail(f"playwright 不可用 — 硬门禁必须 fail 不 skip: "
                    f"{_PW_IMPORT_ERROR}")
    base_dir = tmp_path_factory.mktemp("iff-browser-gate")
    repo = _build_scratch_repo(base_dir)
    home = base_dir / "scratch-home"       # HOME 覆盖 → 空数据目录（零池）
    home.mkdir()
    # 终审 I3：先起 401 服务器（端口进 yaml），再写 provider 配置
    httpd401 = HTTPServer(("127.0.0.1", 0), _Always401)
    threading.Thread(target=httpd401.serve_forever, daemon=True).start()
    _write_fake_cloud_provider(home, httpd401.server_address[1])
    port = _free_port()
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
        health = _wait_health(base)
        assert health.get("status") == "ok", health
        # 隔离自证：零池起步 + 两个本地目录选项（符号链接 BASE_DIR 生效）
        pools = _get_json(base, "/admin/model-pools")
        assert pools.get("pools") == {} and pools.get("configured") is False, \
            f"scratch 未隔离（读到既有配置）: {pools}"
        catalog = _get_json(base, "/admin/model-pools/catalog")
        names = [m["name"] for m in catalog["local"]]
        assert SWAP_MODEL in names, \
            f"scratch models.d 未生效（负路径靶缺失）: {names}"
        # 假云换模靶已静态注册（ready 只查注册表 — dry-run 纯内存可选它）
        cloud_names = [f'{m["provider"]}/{m["model_id"]}'
                       for m in catalog["cloud"]]
        assert f"{CLOUD_PROVIDER}/swap-target" in cloud_names, \
            f"假云 provider 未注册: {cloud_names}"
        assert f"{CLOUD_401_PROVIDER}/unauthorized-model" in cloud_names, \
            f"401 云靶未注册: {cloud_names}"
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        httpd401.shutdown()


@pytest.fixture(scope="session")
def console_log():
    """跨用例收集 console error 与 pageerror（favicon 404 除外）。"""
    return {"console": [], "pageerror": []}


@pytest.fixture
def page(scratch_base, console_log):
    if _PW_IMPORT_ERROR:
        pytest.fail(f"playwright 不可用 — 硬门禁必须 fail 不 skip: "
                    f"{_PW_IMPORT_ERROR}")
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


def _open_pools_tab(page, base):
    """打开面板 → 切到模型池 tab → 等四卡渲染。"""
    page.goto(base + "/", wait_until="domcontentloaded")
    page.click('button[data-tab="tab-model-pools"]')
    page.wait_for_selector("#mpPools .mp-unconfigured")


def _prime_catalog(page):
    """手势预热目录（box mouseover → primeInline）→ 等本地选项出现。"""
    page.hover("#mpPools")
    # CSP 无 unsafe-eval：等待一律用 CSS selector（wait_for_function 被挡）
    # option 在闭合下拉内永远非 visible — attached 语义
    page.wait_for_selector('select[data-mp-config-pool] option[value^="local:"]',
                           state="attached")
    return page.eval_on_selector_all(
        "select[data-mp-config-pool]",
        "els => els.length")


def _poll(page, selector, expr, expect, timeout_s=15, msg=""):
    """元素级 eval 轮询（eval_on_selector 不受 CSP 限制）到期望值。"""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = page.eval_on_selector(selector, expr)
        if last == expect:
            return last
        time.sleep(0.2)
    pytest.fail(f"{msg}: last={last!r}, expect={expect!r}")


def _apply_inline_diff(page):
    """等 diff 条预演通过 → 点应用 → 等横幅/toast 出现（配置已生效）。"""
    page.wait_for_selector('[data-mp-inline-apply]:not([disabled])')
    page.click('[data-mp-inline-apply]')


# ── 1. 零池起步：四张未配置卡 + 树四行，无向导残留 ──────────────────

def test_01_zero_pools_four_unconfigured_cards_and_tree(page, scratch_base):
    _open_pools_tab(page, scratch_base)
    roles = page.eval_on_selector_all(
        "select[data-mp-config-pool]", "els => els.map(e => e.dataset.mpConfigPool)")
    assert sorted(roles) == ["classifier", "fast", "premium", "small"], roles
    # 树：四行，全未配置态；行结构 = 端口→角色→模型叶
    rows = page.eval_on_selector_all(
        ".mp-tree-row",
        "els => els.map(e => ({pool: e.dataset.mpTreePool,"
        "                      state: e.querySelector('.mp-tree-state').textContent,"
        "                      leaf: e.querySelector('.mp-tree-leaf').textContent}))")
    assert [r["pool"] for r in rows] == ["fast", "premium", "small", "classifier"]
    assert all(r["state"] == "未配置" for r in rows), rows
    assert all(r["leaf"] == "—" for r in rows), rows
    assert page.locator("#mpPools").inner_text().count("未配置") >= 4
    # 大手术契约：向导全删（旧设计 = 配置窗口用后即消失）
    body = page.locator("#tab-model-pools").inner_text()
    assert "引导创建" not in body and "向导" not in body


# ── 2. 主路径：未配置卡选模型 → apply → 卡转已配置 + 树行翻转 ────────

def test_02_select_apply_creates_pool_flips_card_and_tree(page, scratch_base):
    _open_pools_tab(page, scratch_base)
    _prime_catalog(page)
    # fast 卡选「在跑」的 TXT（真实主路径 — ready 要求模型实际运行）。
    # ninfer 引擎只声明 openai → anthropic 预演无候选；门禁语义 =
    # 两协议「均」无可用候选才拦（previewWork noneChosen === outs.length）
    sel = page.locator('select[data-mp-config-pool="fast"]')
    sel.select_option(value=f"local:{TXT_MODEL}")
    _apply_inline_diff(page)
    # 卡转已配置（类失未配置）+ 树行翻转（态徽标 + 链路段 + 模型叶）
    _poll(page, "#mp-card-fast", "e => e.className", "if-card mp-pool",
          msg="fast 卡未转已配置")
    state = page.eval_on_selector("#mp-tstate-fast", "e => e.textContent")
    assert state != "未配置", state
    leaf = page.eval_on_selector(
        '.mp-tree-row[data-mp-tree-pool="fast"] .mp-tree-leaf',
        "e => e.textContent")
    assert leaf == TXT_MODEL, leaf
    # 已配置卡动作俱在（编辑/接入片段/启用开关 — 配置界面不再消失）
    card_html = page.eval_on_selector("#mp-card-fast", "e => e.innerHTML")
    for frag in ('data-mp-edit="fast"', 'data-mp-snippet="fast"',
                 'data-mp-pool="fast"'):
        assert frag in card_html, frag


# ── 3. 负路径：选「未在跑」的本地模型 → 三段门禁拦住应用 ─────────────

def test_03_gate_blocks_not_ready_local_target(page, scratch_base):
    _open_pools_tab(page, scratch_base)
    _prime_catalog(page)
    # premium 卡选未在跑的 SwapTarget：openai no_ready_target +
    # anthropic no_compatible_target → 两协议均无可用候选 — 已阻止应用
    page.locator('select[data-mp-config-pool="premium"]') \
        .select_option(value=f"local:{SWAP_MODEL}")
    # 会话首渲染前 diff 条尚不存在（validate + 两协议 dry-run 在途）；
    # eval_on_selector 不等待元素 — 轮询一律用 eval_on_selector_all
    txt = ""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        txt = page.eval_on_selector_all(
            '[data-mp-inline-bar="premium|premium-1"]',
            "els => els.length ? els[0].innerText : ''")
        if txt and "已阻止应用" in txt:
            break
        time.sleep(0.2)
    else:
        pytest.fail(f"三段门禁未拦截未就绪目标（或预演未完成）: {txt!r}")
    disabled = page.eval_on_selector_all(
        "[data-mp-inline-apply]", "els => els.length && els[0].disabled")
    assert disabled, "未就绪目标：应用按钮必须保持禁用"
    # 卡保持未配置 — 被拦的会话没有生效
    cls = page.eval_on_selector("#mp-card-premium", "e => e.className")
    assert "mp-unconfigured" in cls, cls


# ── 4. 换模：行内替换为假云靶 → apply → 树模型叶更新 ────────────────

def test_04_swap_model_updates_tree_leaf(page, scratch_base):
    _open_pools_tab(page, scratch_base)
    _prime_catalog(page)
    before = page.eval_on_selector(
        '.mp-tree-row[data-mp-tree-pool="fast"] .mp-tree-leaf',
        "e => e.textContent")
    assert before == TXT_MODEL, before
    # 行内「替换为…」换成假云靶（ready 只查注册表，双协议声明 → 双预演
    # 均有选择 → 应用可点；dry-run 纯内存，不出网）
    page.locator('select[data-mp-inline-replace="fast|fast-1"]') \
        .select_option(value=CLOUD_SWAP)
    _apply_inline_diff(page)
    _poll(page,
          '.mp-tree-row[data-mp-tree-pool="fast"] .mp-tree-leaf',
          "e => e.textContent", f"{CLOUD_PROVIDER}/swap-target",
          msg="树模型叶未更新")
    # 会话收尾：应用成功 → diff 条消失（_inline 置空）
    deadline = time.monotonic() + 15
    n = -1
    while time.monotonic() < deadline:
        n = page.eval_on_selector_all("[data-mp-inline-bar]",
                                      "els => els.length")
        if n == 0:
            break
        time.sleep(0.2)
    assert n == 0, "应用后 diff 条未消失（会话未收尾）"
    # 卡上双协议选择视图：云候选双协议 → 两行均为「当前选择」
    chosen = page.locator("#mp-card-fast .mp-chosen").all_inner_texts()
    assert chosen and all("fast-1" in c for c in chosen), chosen


# ── 5. 高级配置 → 三段式编辑器模态 → Esc 关闭（B4 键盘流）──────────

def test_05_advanced_modal_esc(page, scratch_base):
    _open_pools_tab(page, scratch_base)
    page.click('#mp-card-fast [data-mp-edit="fast"]')
    page.wait_for_selector('#mpModal [data-mp-editor="fast"]')
    editor_text = page.locator("#mpModal").inner_text()
    assert "max_attempts" in editor_text and "deadline_ms" in editor_text
    page.keyboard.press("Escape")
    deadline = time.monotonic() + 10
    n = -1
    while time.monotonic() < deadline:
        n = page.eval_on_selector_all("#mpModal [data-mp-editor]",
                                      "els => els.length")
        if n == 0:
            break
        time.sleep(0.2)
    assert n == 0, "Esc 未关闭编辑器模态"
    assert page.locator("#mpModal").inner_text().strip() == ""


def _post_chat(base: str, model: str):
    """POST /v1/chat/completions（无 api_keys.yaml → 鉴权不启用）。"""
    payload = json.dumps({"model": model,
                          "messages": [{"role": "user", "content": "ping"}],
                          "max_tokens": 1}).encode("utf-8")
    req = urllib.request.Request(
        base + "/v1/chat/completions", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


# ── 5b. 真实 dispatch 失败 → 树行冷却态 + 点击开时间线（终审 I3）──

def test_05b_tree_cooldown_click_opens_timeline(page, scratch_base):
    """终审 I3：树行可点性必须有真实浏览器交互钉住。

    真实 POST 到 iff/fast（唯一候选 = 401 云靶）→ cooldown_401 事件入环
    → 树行转「冷却中」（mp-tree-row-diag 可点）→ 点击展开事件轴。
    冷却记录是选择器层的传输尝试回写（pool_forward 的 record_attempt/
    add），与 watchdog 的引擎进程自愈互不相干 — 后者重启进程，前者只
    影响候选准入。
    """
    _open_pools_tab(page, scratch_base)
    _prime_catalog(page)
    # fast 行内换到 401 云靶（假 provider 静态注册；dry-run 纯内存通过）
    page.locator('select[data-mp-inline-replace="fast|fast-1"]') \
        .select_option(value=CLOUD_401_SWAP)
    _apply_inline_diff(page)
    _poll(page, '.mp-tree-row[data-mp-tree-pool="fast"] .mp-tree-leaf',
          "e => e.textContent", f"{CLOUD_401_PROVIDER}/unauthorized-model",
          msg="401 云靶未换上")
    # 真实 dispatch：唯一候选上游 401 → 冷却 + 队列耗尽 503 pool_unavailable
    code, body = _post_chat(scratch_base, "iff/fast")
    assert code == 503, (code, body)
    # 树行翻转「冷却中」（面板 5s 轮询拿运行态；冷却 TTL 60s 足够宽）
    _poll(page, "#mp-tstate-fast", "e => e.textContent", "冷却中",
          msg="冷却态未上树")
    # 冷却徽标对比度（终审 I5：亮主题 --warn 加深后的真实渲染进扫描面）
    for theme in ("dark", "light"):
        if theme == "light":
            page.eval_on_selector(
                "html", "el => el.setAttribute('data-theme', 'light')")
        else:
            page.eval_on_selector(
                "html", "el => el.removeAttribute('data-theme')")
        page.wait_for_timeout(150)
        results = page.eval_on_selector("#mpPools", _CONTRAST_JS)
        badges = [r for r in results
                  if r["el"].startswith(".mp-tree-state-cooldown")]
        assert badges, f"{theme}: 冷却徽标不在扫描面"
        assert all(r["ratio"] >= 4.5 for r in badges), (
            f"{theme}: 冷却徽标对比度 <4.5:1: "
            f"{json.dumps(badges, ensure_ascii=False)}")
    # 点击树行 → 行内时间线展开（cooldown_401 事件点在轴上）
    page.click('.mp-tree-row[data-mp-tree-pool="fast"]')
    page.wait_for_selector(
        '.mp-tree-row[data-mp-tree-pool="fast"] + .mp-tree-tl'
        ' .mp-tl-dot.mp-tl-cooldown', state="attached")


# ── 6. 双主题对比度：面板全文本控件 computed contrast ≥ 4.5:1 ───────

_CONTRAST_JS = """
el => {
  // 终审 I5 门禁升级：.mp-card-title 是死选择器（实为 .if-card-title）；
  // .mp-diff-arrow / .mp-tree-state-cooldown 纳入扫描面；元素 opacity
  // 并入前景有效色（.85 透明度曾让 3.99:1 骗过整段门禁）；
  // 禁用控件跳过（WCAG 1.4.3 inactive 组件豁免 — .mp-inline-sel:disabled
  // 的 opacity .55 是刻意的弱化态）
  const SEL = ['.mp-tree-row', '.mp-tree-state', '.mp-unconfigured-badge',
               '.mp-role-desc', '.mp-config-line', '.if-card-title',
               '.mp-edit-btn', 'select.mp-inline-sel', 'option',
               '.mp-tree-port', '.mp-tree-role', '.mp-tree-leaf',
               '.mp-diff-old', '.mp-diff-arrow', '.mp-diff-new',
               '.mp-inline-preview', '.mp-tree-state-cooldown',
               '.mp-inline-apply:not([disabled])'];
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
  for (const s of SEL) {
    for (const n of el.querySelectorAll(s)) {
      if (n.matches('[disabled]')) continue;   // WCAG inactive 豁免
      const st = getComputedStyle(n);
      const fg = parse(st.color);
      if (!fg) continue;
      const bg = resolveBg(n);
      // 元素透明度并入前景 alpha（先与 bg 合成再算亮度比）
      const op = parseFloat(st.opacity);
      const fgEff = [fg[0], fg[1], fg[2],
                     fg[3] * (isNaN(op) ? 1 : op)];
      const l1 = lum(blend(fgEff, bg)), l2 = lum(bg);
      const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
      const label = s + ' «' + (n.textContent || '').trim().slice(0, 24) + '»';
      out.push({el: label, ratio: Math.round(ratio * 100) / 100});
    }
  }
  return out;
}
"""


def test_06_dual_theme_contrast_ge_45(page, scratch_base):
    _open_pools_tab(page, scratch_base)
    _prime_catalog(page)
    # 先开一个已验证的行内会话（small 卡选在跑 TXT → openai 有选择）—
    # diff 条/预演文本/启用的应用按钮全部进入扫描面（不留死区）
    page.locator('select[data-mp-config-pool="small"]') \
        .select_option(value=f"local:{TXT_MODEL}")
    page.wait_for_selector('[data-mp-inline-apply]:not([disabled])')
    for theme, expect_scheme in (("dark", "dark"), ("light", "light")):
        if theme == "light":
            page.eval_on_selector(
                "html", "el => el.setAttribute('data-theme', 'light')")
        else:
            page.eval_on_selector(
                "html", "el => el.removeAttribute('data-theme')")
        page.wait_for_timeout(150)
        # color-scheme 跟随主题（原生 select 弹层配色 — T1 地基）
        scheme = page.eval_on_selector(
            "html", "el => getComputedStyle(el).colorScheme")
        assert scheme == expect_scheme, f"{theme}: colorScheme={scheme}"
        results = page.eval_on_selector("#mpPools", _CONTRAST_JS)
        assert results, "对比度扫描零元素 — 选择器失效"
        offenders = [r for r in results if r["ratio"] < 4.5]
        assert not offenders, (
            f"{theme} 主题对比度 <4.5:1 的文本控件: "
            f"{json.dumps(offenders, ensure_ascii=False, indent=1)}")


# ── 7. console 零 error（favicon 404 除外）+ 零未捕获异常 ─────────────

def test_07_console_zero_errors(console_log):
    favicon = [c for c in console_log["console"]
               if c["type"] == "error" and "favicon" not in c["text"]
               and "favicon" not in c["url"]]
    assert not favicon, f"console error: {json.dumps(favicon, ensure_ascii=False)}"
    assert not console_log["pageerror"], \
        f"未捕获 JS 异常: {console_log['pageerror']}"
