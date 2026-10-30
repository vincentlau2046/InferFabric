# PR-24 模型池能力默认语义反转 — spec

**日期：** 2026-10-31　**版本：** 7.8.0　**状态：** 已实现，待质量门 + 评审

## 背景与用户裁决

deepseek harness 带 `tools` 请求调 `iff/premium`，4 个云候选全被选择器以
`feature_unsupported` 拒 → 400 `no_compatible_target`。根因：建库快照时
`cloud_provider.yaml` 无 `supports_tools` 标志 → 候选 `features` 存空 →
旧语义把「没声明」解释为「仅纯文本」，拦死 agent 调用基础场景（纯 `stream`
请求同被拦，属同根隐藏 bug）。

用户裁决（方案 B，产品级修正，2026-10-31）：

> 应该默认所有模型都有 tool 功能，除非主动去勾选，这是 agent 调用的基础。
> 因此修改逻辑和整体的用户面的方案出现这种情况是灾难性的。

## 统一新语义（单一规则，无例外）

- `allowed_features` 为空（候选 `features` 缺省/`[]`，或证据 features 为空）
  → **无约束，放行一切需求**（tools/stream/vision/structured）。
- `allowed_features` 非空 → **严格约束**：`required ⊆ allowed`，否则
  `feature_unsupported`。
- 显式约束的两条通道：① 面板编辑器逐候选「能力」控件（本次新增）；
  ② `cloud_provider.yaml` 显式 `false` 能力标签（三态：None=True 不约束、
  False 才约束）。
- 空声明**不可表达**「完全不支持」——4 项全不支持的候选无池内用途（YAGNI）；
  编辑器全取消时拒绝保存并提示。

## 实现

| 层 | 改动 |
|---|---|
| `model_pool/selector.py` | `_static_reason` 语义反转（空声明不拦）；`CandidateDecision.detail` 新增字段：`feature_unsupported` 携带 `missing=<f,...>; declared=[<f,...>]`（排序稳定）；`select()` 返回二元组解包 |
| `model_pool/dispatch.py` | `_no_eligible_message`：逐候选 id+原因+明细；含 `feature_unsupported` 时追加解除指引（面板路径）；openai/anthropic 两处 400 共用；请求日志 `error` 仍为固定码 `no_compatible_target` |
| `model_pool/catalog.py` | 本地 features 一律 `[]`（删 modality→vision 推导，modality 保留展示）；云三态推导：`supports_tools/vision is False` 进排除集，空排除集 → `[]`，否则剩余词表（规范序 `_FEATURE_ORDER`） |
| `cloud_discovery.py` | `CloudModel.supports_vision/supports_tools: bool | None = None`（三态）；`_merge_model_spec` 既有 `is not None` 守卫天然兼容 |
| `model_pool/admin.py` | `_selection_view` 决策透传 `detail`（dry-run/状态视图自解释） |
| `dashboard/js/model-pools.js` | 编辑器「能力」列：默认态 `全能力` 徽章 / 约束态 chips + 解除按钮；编辑态 4 复选框草稿（change 不重绘防打断连续勾选），保存写 work（全勾 → `[]` 规范式，空草稿拒绝）；卡片 chips + 树叶 title |
| `dashboard/fragments/style.css` | `.mp-feat-all` / `.mp-feat-opts` / `.mp-feat-opt` 样式 |

## 测试

- 单测翻转/新增：selector（空声明放行全需求、非空约束仍拒、detail 明细、
  PROBED 空证据不约束）、dispatch openai/anthropic（显式约束 400 + 空声明
  放行 + 自解释消息断言）、catalog（本地恒空 + 云三态）、admin（detail
  透传 + dry-run）、core_comprehensive（三态默认 None，3 断言翻转属契约变更）。
- 面板 Node VM：`TestCapabilityDefaults` + `TestCapabilityEditor`（7 测）。
- 浏览器硬门禁 `test_08_capability_constraint_and_self_explaining_400`：
  编辑器取消 tools → 预览+应用 → 卡片 chips 落盘（YAML 复核）→ 带 tools
  真实请求 400（id + missing + declared + 指引 + 固定码）→ 解除约束 →
  同请求 200（桩引擎直通）。`_StubEngine` 补 `do_POST`（最小 OpenAI 200）。

## 门禁捞出的顺带修复（非本 PR 主题，同批合入）

**本地池候选转发必败的潜在生产 bug**：`chat_handlers._forward_request`
池路径以 `timeout=剩余预算` 调用（终审 #7），原实现 `conn.settimeout(timeout)`
在 `http.client.HTTPConnection` 上不存在（socket 才有）→ AttributeError
→ 本地候选必 "unreachable" → 503 pool_unavailable。非池调用方不传
timeout 从不触发 → 单测全绿、生产只炸「池选本地候选」这一条路径
（premium 全云候选，故未暴露）。浏览器门禁 test_08 首跑实锤
（`Forward to :45959 failed: 'HTTPConnection' object has no attribute 'settimeout'`）。
修复 = timeout 经 `pm.make_conn(port, timeout=…)` 构造参数传入
（连接期消费 `self.timeout`）；`timeout=None` 保持缺省调用形状，
非池路径字节不变（PR-14 冻结面不动）。单测
`tests/unit/proxy/test_forward_request_timeout.py`（RED 实锤后 GREEN）。

## 明确不做（避免重蹈「做过头」）

- **无存量数据迁移**：空声明经语义反转自动解封；4 个遗留 `[vision]` 显式
  约束候选（small-vl / small·cand-1a18c915094s / premium·cand-1a18c915094p /
  fast·cand-mvs52l4h）保留为合法显式约束，用户可在新 UI 自行解除。
- **无本地 modality 硬拦截**：视觉请求 → 文本本地模型直通上游裁定，
  池 failover 兜底（「放行，交给上游裁定」原则）。
- **无 PROBED 自动探测**：显式管理动作 + 出网授权仍是唯一来源。
- **无 structured 专门推导通道**：词表在、默认放行、显式约束可用。
- 历史文档中旧规则「未知能力不默认当作支持」不重写，本 spec 声明取代。
