# 模型池面板重构（大手术）设计 — 2026-10-26

> 用户 2026-10-25 判定 v7.2.0–7.4.0 为"完完全全的失败产品"，2026-10-26 授权大手术：
> "你排个详细计划，我需要全部修改，做大手术，你自己完成，我走开一下。（重启 proxy 的动作先不要做。）"

## Intent & Why

P1–P3 的核心错误：把"首次初始化"（一次性向导）当成了"配置界面"。四个坐实缺口：

1. 四组配置窗口（向导）只在"从未配置"空态出现，配完永远消失；
2. 正常视图对缺失组（fast/premium）**无任何创建入口**——用户"无法进行配置和修改"属实；
3. 若未点"完成"，面板锁死在成果卡（`_wizardDone` 挡住一切渲染）；
4. 控件对比度：全页未声明 `color-scheme`（实测 `normal`）→ 原生 select 弹层按浅色平台渲染，而 option 继承 `#eee` → **两主题下拉列表均白底白字（≈1.0:1）**；且 `--if-text/--if-border/--if-text-dim` 幽灵 token 从未定义、回退值按深色硬编码 → 浅色主题下 mp-* 全族白字白底（实测 1.04:1）。

测试为何全绿未拦：门禁全是源码文本断言 + Node VM 行为测试，无一次真实浏览器交互，无对比度检查。

## Goals

1. **四组常驻**：fast / premium / small / classifier 四张配置卡永远在屏、永远可配——面板只有一种形态，不存在会消失的窗口。
2. **顶部调用树**：`:8999 → iff/<角色> → 模型` 四行；链路**绿色+数据流动 = 正在被调用**（复用 C3 已批语义：runtime `last_attempt.ok && age_s < 阈值`，沿用 5s 轮询不加频）；**断链/红 = 故障**；**虚线暗 = 隔离/冷却**；**灰 + 「未配置」= 缺失组（不消失）**。
3. **对比度地基**：双主题下全文本控件 computed contrast ≥ 4.5:1（含原生 select 弹层，经 color-scheme 跟随主题）。
4. **交互门禁**：python-playwright 真实浏览器测试成为硬门禁（主路径 + 双主题对比度）。
5. **零后端改动**：不新增端点（复用 /admin/model-pools 定义/runtime/latency/events/catalog + 编辑器 apply/CAS）；PR-14 转发核心不碰。

## Non-goals

- 不改 model_pool/ 后端调度与转发逻辑；
- 不重建三段式编辑器（复用其预览/CAS/回滚）；
- 不做配置历史/回滚（原二期项）；
- 历史递延 minor 不在本次清偿。

## Anti-goals（做成这样即失败）

- 任何"空态专属"UI / 一次性引导流；
- 新增装饰性动效（本轮只删不加）;
- 配置入口藏在点击背后不可发现。

## Layout

### 上：调用树（#mpTree）

四行，每行 `:8999 ── iff/<角色> ── <模型>`：

- 链路段状态（纯函数 `linkState(def, rt)`，复用 C3 beamClass 语义）：
  `flowing`（近窗有真实调用，绿 + 流动光点）/ `idle`（可路由，实线）/ `cooldown`（虚线暗）/ `fail`（红断链，issues 非空或 last_attempt 失败）/ `unconfigured`（灰虚线 + 徽标）；
- 模型叶：模型名 + C1 迷你图（现有渲染复用）；点击 `fail`/劣化链 → 展开 C2 失败时间线（现有）；
- C4 劣化提示：首候选降级时树行内引导条（现有语义）。

### 下：四组常驻配置卡（#mpCards）

2×2 grid（窄屏堆叠），每卡：

- 卡头：`iff/<角色>` + 角色描述 + 启用 toggle（现有）；
- 候选链行（优先级序 + 状态灯）；
- 「替换为…」行内换模下拉（A2 机制 + 目录预热复用，对比度修复后）；
- 「高级配置」→ 现有三段式编辑器模态（#mpModal 原样）;
- **未配置卡**：同形态 + 「未配置」徽标；下拉预填目录启发式（原 _prefill 逻辑保留为卡片默认值，不再是向导）；首次 apply 即建池（openEditor 已有骨架路径）；
- 四角色之外的额外池（YAML 手工添加的）：同卡片组件**追加渲染**在四角色之后，不隐藏。

### 删除清单

向导全套：`renderWizard / renderWizardHtml / _wizard / _wizardDone / renderWizardDone / doMpWizardDone / doMpWizardDoneCopy / _prefill`、三态空态分支（"拉取失败 / 暂无逻辑模型池"普通空态改为同一布局的降级渲染）、58 处向导相关测试迁移。

## Contrast foundation（全局）

1. `:root { color-scheme: dark; }` + `[data-theme="light"] { color-scheme: light; }`；
2. select/option 显式背景 `var(--surface-2)`（不透明回退平台色）；
3. 幽灵 token 全灭：`--if-text → var(--ink)`、`--if-border → var(--border)`、`--if-text-dim → var(--ink-2)`，grep 零残留为验收；
4. `:focus-visible` 键盘焦点环统一（B4 键盘流保留）。

## Gates（硬门禁，全过才合并）

1. pytest 全量 unit+integration 绿；
2. VM 测试迁移完成：向导用例删除、新布局用例 RED→GREEN（每任务）；
3. **交互门禁**：`tests/interactive/test_model_pools_browser.py`（python-playwright 1.61 + chromium）驱动沙箱代理（scratch 端口 + 临时 IFF_DATA_DIR，不碰生产 :8999）：开面板 → 四组常驻可见 → 未配置组行内建池 → 换模 → 高级 apply → 树状态更新；**双主题**全文本控件 computed contrast ≥ 4.5:1（含 mp-inline-sel 的 option 继承色）;
4. `node --check` 全部面板 JS；
5. 干净 worktree 复验（沙箱绿 ≠ 提交树绿）；
6. cross-review：一次 subagent 评审（fresh context，不绑定模型）→ Critical/Important 一轮修复 pass（RED→GREEN + 全量绿）；
7. v7.5.0（任务族 minor 已授权）+ README 版本历史表。

## Deployment

合并生产 main（no-ff "Merge sandbox/main: …"）→ push（`GIT_SSH_COMMAND='ssh -o BatchMode=yes'`）→ **停：不执行 `systemctl --user restart inferfabric-proxy`（用户明确指令）**。零 Python 改动 ⇒ 合并后静态文件按请求读盘即生效，无需重启亦安全（新面板仅依赖既有 API）。

## Open rulings（用户离场，我的裁量 — 成本记入报告）

- **R1** 四组 = fast/premium/small/classifier（原向导四角色；用户"四个模型组"所指）。
- **R2** 流动判定沿用 C3 已批语义（last_attempt.ok && age<60s，5s 轮询不加频）。
- **R3** 额外池不隐藏，追加渲染。
- **R4** 三段式编辑器复用不重建。
- **R5** C1/C2/C4 降级为树/卡从属诊断保留（不另占版面）。
- **R6** v7.5.0。
- **R7** 不预建 fast/premium 池——新 UI 即创建路径，用户回来自己配。
- **R8** builder-check 的"theirs"问题由我裁决定案，ledger 记录。
