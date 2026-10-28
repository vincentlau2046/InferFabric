# PR-21 模型池双修复 — spec

**日期：** 2026-10-28　**版本：** 7.6.1　**状态：** 已实现，评审通过，待用户放行合并

用户报告的两个缺陷（原文）：

> 1/本地模型的协议支持，当前是没有有效识别，（本地框架是默认支持双框架的）
> 2/预览（编辑态，未应用）：openai → 选择 small-1 / anthropic → 选择 small-1 /
> 编辑候选池增加候选模型，预览完成后无法应用，应用是灰色的。

## Bug #1 — 本地模型双协议识别失效

**根因链：** `catalog._LOCAL_PROTOCOLS` 只有 vllm 声明双协议 → ninfer 候选
存盘 `declared_protocols=["openai"]` → registry `_evidence_for` 按存盘声明
铸 DECLARED 证据 → anthropic 证据缺失（UNKNOWN）→ selector `_static_reason`
返回 `no_evidence` → anthropic 池永不选本地候选（dry-run 报
`no_compatible_target`），目录/徽标也只显示 openai。

**实证：** 本地 anthropic 路由是零翻译直通（`forward_anthropic_local` POST
引擎原生 `/v1/messages`），NInfer 原生服务该端点 — 7 天线上日志 6615 TXT +
3483 VL 次 `/v1/messages → LOCAL`，零失败归因于协议不支持。

**修复（两层，同一映射源）：**
1. `catalog._LOCAL_PROTOCOLS["ninfer"] = ["openai", "anthropic"]`（新池从目录
   拿到正确声明）；sglang 保守维持 openai-only（muse-glimmer-vl 无本地
   anthropic 实证 — 宁可少声明不虚报，用户确认后一行可加）。
2. `PoolRegistry(local_protocols=...)` 回调：local 候选的 DECLARED 基线按
   引擎实时映射推导，**存量配置免迁移自愈**（建池时存进的旧
   `declared_protocols` 不再钉死证据）。proxy_manager 接线
   `served_name → ModelConfig.type → 映射`。

**语义边界（有意为之）：**
- 映射是 DECLARED 基线的唯一权威源 — 收窄方向同样生效（引擎换型后存盘的
  多余声明不再铸 DECLARED 证据）。注意（评审 M-3）：本地端点指纹不含引擎
  类型，引擎换型后 store 里指纹匹配的旧 PROBED 条目仍会注入 — PROBED 覆盖
  声明本就是证据分层的设计意图，只是「收窄」一词只对 DECLARED 基线成立。
- store 实测条目（PROBED/FAILED/STALE，指纹匹配）仍覆盖声明 — 自愈不会
  翻案实测失败。
- 回调缺省/返回 None 退回存盘声明（向后兼容，纯单测用法不受影响）。

## Bug #2 — 模态预览通过后应用钮灰死

**根因：** `renderEditor()` 把 `disabled` 属性按 `_edit.validated` 烘进 DOM；
`mpEditPreview` 成功路径只改 JS 状态（`_edit.validated = true`）不重渲染 →
按钮保持灰。整模态重渲染会抹掉 `#mpPreview`（用户必须阅读的预演结果），
不可取。

**修复：** 外科手术式 — 应用钮加 `id="mpEditApply"`，预览通过后
`$('mpEditApply').removeAttribute('disabled'/'title')`。附带一致性修复：
`mpEditPoolParam` 失效后补 `renderEditor()`（此前是唯一不重烘按钮态的编辑
操作 — 预览通过后改池参数，按钮会亮着过期状态；`mpEditApply` 的
`!validated` 守卫一直在，无绕过风险，纯 UI 态不一致）。

**评审 I-1 追加修复（2026-10-28）：** 五个编辑操作都就地改 `_edit`（身份
不变），`mpEditPreview` 的会话令牌只挡关闭/重开 — 预演在途时改动，
迟到的预演完成会把 validated 重新置真并点亮应用钮（用户没见过结果的配置
被放行 = 三段门禁绕过；bug #2 修复前按钮永不亮，该竞态潜伏不活跃，点亮
路径使它变活）。修复 = 行内编辑器同款纪律：会话对象加 `rev` 代次，
`mpEditInvalidated()` 统一失效拍子（validated=false + rev++，五 mutator
全走），`mpEditPreview` 起拍自增并捕获代次，完成时身份或代次对不上即
放弃（pv 改 await 后重查）。VM 测试以 noAwait + yieldTicks + _drain
确定性复现在途窗口，先 RED 后 GREEN。

## 测试

- 单测：catalog ninfer 双协议 + sglang 保守钉住；registry 自愈三向
  （补宽 / store 覆盖仍赢 / 收窄）；VM 逻辑层钉预览点亮（attrOps）。
- 浏览器硬门禁 `test_05c`（真实 chromium + 真实 scratch 代理全链路）：
  premium 建池（在跑 TXT）→ 开编辑器 → 预览 → **双协议均「选择」+ 应用钮
  点亮** → 应用 → 编辑器收尾 + revision+1 + raw 候选复核。RED→GREEN 实证
  （修复前 anthropic 行 = `no_compatible_target`，应用钮灰）。

## 部署注意

- 后端（catalog/registry/proxy_manager）生效需 proxy 重启（用户授权操作）；
  面板 JS 可落盘 + SIGHUP 零重启（PR-20 文档）。
- 用户存量池配置（models.d 修正前建的所有本地候选）在重启后自动自愈，
  无需手工改 YAML。
