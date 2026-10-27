# 模型池面板重构（大手术）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 模型池面板单形态重构——顶部调用树 + 四组常驻配置卡，删一次性向导，对比度地基修复，浏览器交互硬门禁。

**Architecture:** 纯 dashboard 层（JS/CSS）手术，零后端端点改动。渲染从"三态空态分支+卡片列表"改为"树+四固定卡"单一形态；A2 行内换模/三段式编辑器/C1 迷你图/C2 时间线/C3 光带语义全部复用；对比度经 color-scheme + 主题 token 接线修复。

**Tech Stack:** 原生 JS（无构建）、pytest + Node VM harness（现有）、python-playwright 1.61（交互门禁，本机 chromium-1228）。

**Spec:** `docs/superpowers/specs/2026-10-26-model-pool-panel-redesign-design.md`（含用户授权原话与 R1–R8 裁量）

## Global Constraints

- 工作区：`/home/vince/inferfabric/sandbox`（自有 git，main 分支直接提交，commit 尾行 `Co-Authored-By: Claude Code <noreply@anthropic.com>`）
- 零后端：只许动 `inferfabric/dashboard/**`、`tests/**`、`docs/**`、`README.md`、`inferfabric/__init__.py`（版本）；**PR-14：转发核心（forwarder.py 等）禁碰**
- 生产 :8999 不作测试目标；交互门禁用 scratch 端口 8998 + 临时 IFF_DATA_DIR
- **不重启生产 proxy**（用户指令）；合并+push 后停在重启前
- 常量：四角色固定序 `fast, premium, small, classifier`（`_ROLE_ORDER` + `_ROLE_DEFS` 描述表）；流动窗 `FLOW_WINDOW_S = 60`；对比度阈值 `4.5`
- 数据形状（已钉）：`GET /admin/model-pools/runtime` → `{pools:{<name>:{candidates:[{id,kind,target,active,cooling,last_attempt}]}}}`；`/latency`、`/events` → `{pools:{}}`
- 版本 7.5.0：`inferfabric/__init__.py` 唯一源 + README 版本历史表

## Review Focus（测试未覆盖、最可能咬人的五类输入）

1. **目录不可用/为空时的未配置卡**：下拉降级路径（编辑器手填降级已有——未配置卡复用），不能白屏或死下拉。
2. **额外池**（四角色之外的 YAML 池）：渲染、编辑、树行追加都不丢。
3. **双主题新增类的对比度**：`mp-tree*`/`mp-link-*`/未配置徽标在 light/dark 都 ≥4.5:1（含 option 继承色）。
4. **既有配置无损**：small/classifier 现有 YAML 在新布局 raw 基底读写不变形。
5. **B1 by-id 局部渲染**：轮询时树区与卡区互不冲掉（by-id 更新不得整块 innerHTML 重建邻区）。

对应钉位：1→Task 3/5 VM 用例；2→Task 2/3 VM 用例；3→Task 6 浏览器断言；4→Task 4 VM 用例 + Task 6 实测；5→Task 4/5 VM 用例。

---

### Task 1: CSS 对比度地基

**Files:**
- Modify: `inferfabric/dashboard/fragments/style.css`
- Test: `tests/unit/dashboard/test_dashboard_contrast.py`（新建）

**Interfaces:**
- Produces: `:root { color-scheme: dark; }`、`[data-theme="light"] { color-scheme: light; }`、`.mp-inline-sel { background: var(--surface-2); … }`；`--if-text/--if-border/--if-text-dim` 全文件零命中；mp 段改用 `var(--ink)/var(--border)/var(--ink-2)`。
- Consumes: 现有主题 token（`--ink/--border/--ink-2/--surface-2`）。

- [ ] **Step 1: RED** — 新建 `test_dashboard_contrast.py`（`_css()` 助手读 style.css，范式照 test_cloud_panel_css.py）：

```python
class TestContrastFoundation:
    def test_color_scheme_declared_per_theme(self):
        # :root 块含 color-scheme: dark；[data-theme="light"] 块含 color-scheme: light
    def test_phantom_tokens_eliminated(self):
        # '--if-text' / '--if-border' / '--if-text-dim' 均不在 css 全文
    def test_selects_have_explicit_background(self):
        # .mp-inline-sel 规则含 background: var(--surface-2)
    def test_mp_controls_use_theme_tokens(self):
        # mp 段出现 var(--ink) 与 var(--border)
```

- [ ] **Step 2:** `python3 -m pytest tests/unit/dashboard/test_dashboard_contrast.py -v` → 4 failed（断言缺失项）
- [ ] **Step 3: GREEN** — style.css：`color-scheme` 两行；`.mp-inline-sel` 背景改 `var(--surface-2)`（去 `background: transparent`）；全文替换 `var(--if-text, #eee)`→`var(--ink)`、`var(--if-border, #444)`→`var(--border)`、`var(--if-text-dim, #888|#999)`→`var(--ink-2)`（`.mp-dot` 1444 行的 background 同理改 token）；编辑器/向导段 select 同步。检查 `test_model_pools_panel.py` 是否有钉旧字符串的 CSS 用例，有则本任务内迁移。
- [ ] **Step 4:** 新文件 4 passed + `tests/unit/dashboard/test_model_pools_panel.py` 全绿
- [ ] **Step 5: Commit** `fix(dashboard): 对比度地基 — color-scheme 双主题 + 幽灵 token 全灭 + select 显式背景`

### Task 2: linkState 纯函数 + 调用树渲染

**Files:**
- Modify: `inferfabric/dashboard/js/model-pools.js`
- Test: `tests/unit/dashboard/test_model_pools_panel.py` 追加 `TestCallTree`

**Interfaces:**
- Produces: `linkState(def, rtPool) -> {cls, label}`（cls ∈ `flowing|idle|cooldown|fail|unconfigured`）；`treeHtml(defs, rt) -> string`（gateway 根 `:8999` + 每池一行 `iff/<name>` → 模型叶，行根 `data-mp-tree-pool="<name>"`，链路段 `mp-link mp-link-<cls>`）；`_ROLE_ORDER`、`_ROLE_DEFS`（名+描述，从向导段上移）。
- Consumes: runtime 候选 `last_attempt{ok,age_s}/cooling/active`；defs.pools；C3 beamClass 既有判定语义（`last_attempt.ok && age_s < FLOW_WINDOW_S` → flowing）。

- [ ] **Step 1: RED** — TestCallTree（VM harness 跑真实 JS，stub fetch 预置 defs/runtime）：

```python
def test_link_state_unconfigured():          # linkState(null, null) → unconfigured
def test_link_state_flowing():               # last_attempt {ok:true, age_s:10} → flowing
def test_link_state_idle():                  # active、无 cooling、无近期 attempt → idle
def test_link_state_cooldown():              # cooling 非 null → cooldown
def test_link_state_fail():                  # def.issues 非空 → fail
def test_tree_four_rows_fixed_order():       # defs 仅 small → 四行 fast,premium,small,classifier，缺行含「未配置」
def test_tree_extra_pool_appended():         # defs 含 custom-x → 第五行在四角色后
def test_tree_row_structure():               # 行含 :8999 / iff/small / Qwen38-27B-TXT / data-mp-tree-pool
```

- [ ] **Step 2:** 跑 → 新用例 fail（函数不存在）
- [ ] **Step 3: GREEN** — model-pools.js 顶部加 `_ROLE_ORDER/_ROLE_DEFS/FLOW_WINDOW_S`、`linkState`、`treeHtml`（**不接线 renderModelPools**——Task 4 接）
- [ ] **Step 4:** 全文件绿；`node --check model-pools.js`
- [ ] **Step 5: Commit** `feat(dashboard): 调用树 — linkState 纯函数 + 四行树渲染（端口→角色→模型）`

### Task 3: 四组常驻配置卡

**Files:**
- Modify: `inferfabric/dashboard/js/model-pools.js`
- Test: `test_model_pools_panel.py` 追加 `TestPersistentCards`

**Interfaces:**
- Produces: `cardsHtml(defs, rt, catalog) -> string`：`_ROLE_ORDER` 四卡固定序 + 额外池追加；`cardDefault(roleName, catalog)`（原 `_prefill` 启发式，作未配置卡下拉默认值）；未配置卡：`mp-unconfigured` 徽标 + 替换为… select（预填）+ `data-mp-config-pool="<name>"`；已配置卡：现 poolCard 全部动作保留（toggle/候选链灯/替换为…/高级配置/接入片段）。
- Consumes: `fetchCatalog/primeInline`（A2）、`newPoolSkeleton`/`openEditor`（骨架路径，Task 5 接线点击）。

- [ ] **Step 1: RED**：

```python
def test_cards_fixed_four_plus_extras():     # 四固定序 + custom 追加
def test_unconfigured_card_badge_and_prefill():  # 徽标「未配置」+ select 预填 cardDefault 值
def test_configured_card_keeps_actions():    # 编辑/接入片段/启用/替换为… 俱在
def test_unconfigured_config_select_present():   # data-mp-config-pool 属性 + 选项来自目录
```

- [ ] **Step 2:** fail → **Step 3: GREEN** 实现（重构 poolCard → cardInner；未配置 = skeleton 卡）→ **Step 4:** 绿 + node --check
- [ ] **Step 5: Commit** `feat(dashboard): 四组常驻配置卡 — 未配置即待建，配置界面不再消失`

### Task 4: renderModelPools 切换 + 向导全套删除

**Files:**
- Modify: `inferfabric/dashboard/js/model-pools.js`（删 ~1044–1260 向导段 + 守卫 582–583/1895/1934 + 640 `_wizardDone` 分支）
- Test: 迁移 `test_model_pools_panel.py`（删向导用例类；新增主路径类）

**Interfaces:**
- renderModelPools 新体：fetch 5 端点（POOLS/STATUS/RUNTIME/LATENCY/EVENTS）→ `box.innerHTML = (banner?) + treeHtml(defs, rt) + cardsHtml(defs, rt, catalog)`；**不再有三态空态**——零池 = 四张未配置卡；拉取失败仍走 A3 横幅。
- 删除：`renderWizard/renderWizardHtml/renderWizardDone/doMpWizardDone/doMpWizardDoneCopy/_wizard/_wizardDone/_prefill`。

- [ ] **Step 1: RED**（先删旧向导用例，再写新）：

```python
def test_render_unconfigured_four_cards_no_wizard():  # 全新 harness：零池 defs → 四 data-mp-config-pool；全文无「引导创建」
def test_render_configured_tree_and_cards():          # small/classifier defs → 树含两配置行 + 四卡俱在
def test_source_has_no_wizard_symbols():              # 源文本契约：renderWizard/_wizardDone/_prefill 零命中
def test_existing_yaml_rendered_lossless():           # 现有 small/classifier 卡内容与旧 poolCard 断言等价（迁移保留）
```

- [ ] **Step 2:** fail → **Step 3: GREEN** 切换渲染 + 删向导 + 清守卫
- [ ] **Step 4:** 全文件绿（Review Focus #2/#4/#5 的 VM 用例在此任务一并落）
- [ ] **Step 5: Commit** `refactor(dashboard): 模型池单形态 — 树+四组常驻卡，一次性向导全套删除`

### Task 5: 交互接线

**Files:**
- Modify: `inferfabric/dashboard/js/model-pools.js`
- Test: `TestPersistentCards`/`TestTreeInteractions` 追加

**Interfaces:**
- 点击 `data-mp-tree-pool`（fail/cooldown 行）→ 展开 C2 时间线（复用现有 events 渲染，行内展开不跳转）；
- 未配置卡 select change → A2 preview→apply 链路（首次 apply 建池，POST 现有 config 端点，CAS 语义同编辑器）；apply 成功 → 该卡转已配置 + 树行 `unconfigured` 消失；
- 「高级配置」→ `openEditor(name)`（不存在 → `newPoolSkeleton`）；
- B4 键盘流（Esc 关编辑器、Tab 序）现有用例保持绿。

- [ ] **Step 1: RED**：

```python
def test_tree_fail_click_expands_timeline():      # harness 触发点击 → 展开块含事件渲染节点
def test_unconfigured_select_applies_creates_pool():  # stub fetch：change → POST 断言 + 卡转已配置
def test_unconfigured_advanced_opens_skeleton_editor():  # openEditor('fast') → mpModal 含骨架字段
```

- [ ] **Step 2:** fail → **Step 3: GREEN** 事件委托接线（现有 data-mp-* 委托处加分支）→ **Step 4:** 绿 + node --check
- [ ] **Step 5: Commit** `feat(dashboard): 树交互 + 未配置组建池链路 — 点击诊断/行内建池/高级直通`

### Task 6: 浏览器交互硬门禁

**Files:**
- Create: `tests/interactive/test_model_pools_browser.py`（+ `tests/interactive/__init__.py`）

**Interfaces:**
- fixture（session 级）：`cd /home/vince/inferfabric/sandbox && EDGE_PROXY_HOST=127.0.0.1 EDGE_PROXY_PORT=8998 IFF_DATA_DIR=<tmp> python3 -m inferfabric.proxy.handler --async` 子进程 + 等 `/health` 200；teardown terminate。
- python-playwright sync API + chromium headless；断言集：
  1. 开面板 → 四张 `data-mp-config-pool` 卡 + 树四行（两配置两未配置，scratch 实例从空数据目录起步 → 全部未配置起步更简：**用空数据目录**，四卡全未配置）；
  2. **主路径**：未配置卡选模型 → apply → 卡转已配置 + 树行翻转；
  3. 换模 → apply → 树模型叶更新；
  4. 高级配置 → 模态出现 → Esc 关闭；
  5. **双主题对比度**：`document.documentElement.dataset.theme` 置 dark/light，全文本控件（select/button/hint/树行/徽标）computed contrast ≥ 4.5（fg/bg 沿祖先解析，算法同本次诊断脚本）；
  6. console 零 error（favicon 404 除外）。
- **验证测试有效性**：先对**未重构的 HEAD~（Task 1 前）代码**跑一次确认 FAIL（抓得住向导布局/坏对比度），再对当前树跑 PASS。

- [ ] **Step 1:** 写文件（含空跑保护：playwright import 失败 → fail 不 skip——硬门禁）
- [ ] **Step 2:** 对当前树跑 → PASS；抽查反向：注释掉 color-scheme 行跑一次确认对比度断言 FAIL → 恢复
- [ ] **Step 3: Commit** `test(interactive): 模型池浏览器交互硬门禁 — 主路径 + 双主题对比度 ≥4.5:1`

### Task 7: v7.5.0 + 发布门禁

**Files:**
- Modify: `inferfabric/__init__.py`（7.5.0）、`README.md`（版本历史行）

- [ ] **Step 1:** 版本 + README（"7.5.0 — 模型池面板重构：调用树 + 四组常驻配置 + 对比度地基 + 交互门禁"）
- [ ] **Step 2:** 全量 `python3 -m pytest tests/unit/ tests/integration/ tests/interactive/ -q` 绿；`node --check` 全面板 JS
- [ ] **Step 3:** 干净 worktree 复验：`git -C sandbox worktree add $CLAUDE_JOB_DIR/tmp/wt-verify HEAD` → 全量跑 → 删 worktree（沙箱绿 ≠ 提交树绿）
- [ ] **Step 4: Commit** `chore(version): 7.5.0 — 模型池面板重构（大手术）`

### 收尾（executing-plans 流程）

review-package → 一次 subagent 终审（fresh context，不绑定模型）→ re-grade → Critical/Important 一轮修复（RED→GREEN + 全量绿）→ Minor 入 ledger → 生产合并（no-ff `Merge sandbox/main: 模型池面板重构 — 调用树 + 四组常驻配置 + 对比度地基（v7.5.0）`）→ `GIT_SSH_COMMAND='ssh -o BatchMode=yes' git push origin main` → **停：不重启 proxy** → 最终报告（Rulings + Deferred minors + 重启待用户指令）。
