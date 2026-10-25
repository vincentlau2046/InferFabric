# UI/UX P1：动效令牌 + 候选体检卡 + 错误即导航 + 局部渲染（v7.2.0）

> **For agentic workers:** 沿用本仓库既定流程：沙箱 TDD（node VM 契约测试先行）→ 全量门禁 → 干净 worktree 复验 → 合并生产 → 重启冒烟。用户已批准实施（2026-10-24，四项拍板见 steering 会话记录）。

**Goal:** 模型池运营视图的视觉与交互底盘升级——状态一眼可读、轮询零闪烁、错误带行动入口。

**Spec:** 会话内已审方案（A1/A3/B1 + Motion/Depth 令牌）；本文档即实施计划。

## Global Constraints

- PR-14：不动转发核心；改动限 `dashboard/`（CSS/JS）+ 测试。
- 池调度绝不自动启动本地模型；本 P1 纯前端展示层，零后端改动。
- `prefers-reduced-motion: reduce` 全局降级（动效一刀切关闭）。
- 既有语义文本令牌不变（服务中/已发现/未启动/未发现/冷却/隔离/探测中、mp-lamp-ok/warn/err、no_evidence 等）——既有测试是契约。
- harness 扩展只做加法（className/style/writeCounts 输出），不改既有断言含义。

## Task 1：Motion/Depth 令牌（style.css）

**Files:** Modify `inferfabric/dashboard/fragments/style.css`；Test: `tests/unit/dashboard/test_model_pools_panel.py`（新类 `TestMotionTokens`）

- `:root` 加 `--dur-fast: 120ms; --dur: 200ms; --dur-slow: 400ms; --ease: cubic-bezier(0.2, 0, 0, 1);`
- `@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; } }`
- 测试：读 style.css 断言四令牌 + 降级块存在。RED → 实现 → GREEN。

## Task 2：A1 候选体检卡（表格 → 横向卡片）

**Files:** Modify `model-pools.js` `poolCard()`/新增 `candCard()`、`style.css`；Test: 新类 `TestP1CandidateCards`

候选行改 `<div class="mp-cands">` 内的 `mp-cand-card`（`data-mp-cand-card="<id>"`）：

- **状态灯** `mp-dot`（`id="mp-dot-<pool>-<candId>"`），类按优先级：
  `probing`（cooling.probing，快脉冲）> `cool`（cooling，暗态+环形倒计时）> `flow`（last_attempt.ok && age_s<30 —— 真实数据交换，呼吸流动）> `ok`（active 空闲，routable 静默）> `off`（未启动/未发现，unroutable）。
  （用户拍板 #3 语义：流动=真实有数据交换；routable/unroutable 视觉区分。）
- 中部：目标（mono）+ 本地/云徽章 + 优先级；**协议徽章**（declared_protocols ‖ evidence 键）；**能力点**（features：tools/vision）。
- 右部：运行态文本（沿用 runtimeBadge 语义）`id="mp-rt-<pool>-<candId>"`；冷却/隔离带环形倒计时 `data-mp-ring` + `--p` 百分比（首见锚定总时长，客户端递减）。
- CSS：卡片三级景深第一步（页面卡沿用 if-card），呼吸/脉冲动画用 Task 1 令牌。
- 既有 TestOpsRender 文本断言必须原样通过（语义令牌保留）。

## Task 3：A3 错误即导航

**Files:** Modify `model-pools.js`；Test: 新类 `TestP1ErrorBanner`

- 模块态 `_actionError = {message, hint, action}`；`renderModelPools` 渲染池列表前输出 `mp-action-banner`（原因 + 行动按钮 `data-mp-banner-action` + 关闭 `data-mp-banner-dismiss`）。
- 接线（P1 范围）：`doMpToggle` 409 与非 409 失败、`doMpWizardSubmit` 失败、`doMpUndo` 失败 → 置 banner（行动=重试原操作/刷新），成功清除。
- 新 window 导出：`doMpBannerAction` / `doMpBannerDismiss`；`onBoxClick` 委托两个 data 属性。
- toast 保留（即时反馈），banner 承担"常驻 + 去哪修"。

## Task 4：B1 局部渲染 + 本地倒计时

**Files:** Modify `model-pools.js`；Test: 新类 `TestP1PartialRender`；harness 加法扩展

- **结构签名** `_sig`（池名序列 + 每池候选 id 序列 + enabled + issues 数）：签名不变 → 不重写 `mpPools.innerHTML`，只走 `applyDynamic()`；签名变 → 整块重建。
- **applyDynamic()** 按 id 定点更新：`mp-lamp-<pool>-<proto>`（className）、`mp-dot-*`、`mp-rt-*`（textContent）、`mp-chosen-<pool>-<proto>`、revision。
- **本地倒计时**：独立 1s interval（懒启动，tab 可见且无向导时走）；以 `_rtAt`（runtime 拉取时刻）为锚本地递减 remaining_s 更新 `mp-rt-*` 与环形 `--p`，不发出任何 fetch。
- harness 加法：fake el 加 `className`/`style.setProperty`；innerHTML 写计数 `writeCounts`；输出 `dyn`（各元素 textContent/className）。既有测试零改动通过。

## 验收（P1 整体）

- 新测试类全绿 + 既有 335 例面板/池测试全绿 + 全量 unit+integration。
- 干净 worktree 复验（committed-tree verification）。
- 版本 7.2.0（phase 授权内 minor）+ README 历史行。
- 部署：合并生产 main → push（BatchMode 兜底）→ 重启服务 → 冒烟（dashboard 200 + 面板渲染 + 无 JS 错误）。
