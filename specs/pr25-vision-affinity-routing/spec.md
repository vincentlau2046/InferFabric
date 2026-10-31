# PR-25 能力归模型 / 池归优先级（vision 亲和路由）— spec

**日期：** 2026-10-31　**版本：** 7.9.0（minor，用户 2026-10-31 批准）
**前置：** PR-24（7.8.0 语义反转）已合 main **但未部署** — 本 PR 与其合并为一次上线。

## 背景与用户裁决

7.8.0 把「空声明=全能力、非空=排除约束」做进了池候选，用户判定方向错误
（"这些方案都不合理，看着就别扭"），连续三轮纠偏后收敛为业界实践
（LiteLLM Router / OpenRouter 模式）：

> 能力是**模型元数据**（声明一次，跟模型走）；路由表（池）只存
> 模型引用+优先级；路由器按**请求内容过滤**。没有任何一家让用户在
> 路由表里逐条勾能力。

用户关键裁决（2026-10-31，逐条）：

1. "我不希望池子和模型的能力特性挂钩，是池内模型的路由和模型能力
   挂钩，而且只与 VL 挂钩" — 池是服务分级，不是能力分组。
2. "VL 模型排在最高优先级，是可以正常响应所有 agent 服务的，不应该
   被跳过" — 无图请求纯优先级，VL 候选全参与，**无跳过规则**。
3. "vision 这项 feature 需要声明，其他的都是默认 all" — 词表收敛为
   vision 一项；tools/stream/structured 不建模（准入原则：无 tools
   不进池 → 恒真，不建模不变量）。
4. 最终："OK，是这个意思"（批准本 spec 的方案）。

## 统一语义

| 请求 | vision-capable 候选 | 其他候选 |
|---|---|---|
| 带图（含 agent tools+图） | 可接，按优先级 | 不可接（模型本身无视觉） |
| 无图（纯文本/纯 agent） | 可接——排最前就全接 | 可接，按优先级 |

- **vision-capable 的唯一判定源 = 模型本体**：
  - 本地：`models.d` 的 `model_type: vl`（经 `resolved_modality` 同源；
    RuntimeView 携带 served_name 集合，选择期零 IO）
  - 云：`cloud_provider.yaml` 的 `supports_vision: true`
- 带图请求但池内无 vision-capable 候选 → 400 `no_compatible_target`
  自解释，指引指向**模型侧**声明入口（云面板视觉 chip / models.d），
  不再指向池编辑器。
- VL 排最前 → 全接（含纯文本）；VL 排后 → 带图流量按优先级找它、
  纯文本自然走前面的候选。**无任何"跳过 VL"规则**（用户裁决 2）。

## 实现

| 层 | 改动 |
|---|---|
| `selector.py` | 删 `required ⊆ allowed` 排除检查与 `required_features()`；`RequestRequirements` 收敛为 `protocol + vision`；`RuntimeView` 增 `local_vision: frozenset`（served_name 集）；静态检查新增：`req.vision` 且候选非 vision-capable → `feature_unsupported`（reason 复用，detail 改为视觉语义） |
| `dispatch.py` | `extract_requirements` 只提取 vision（tools/stream/structured 不再产生路由要求）；`_no_eligible_message` 指引改指模型侧；`runtime_view` 从 `mgr.list_models()` 构建 `local_vision` |
| `catalog.py` | 条目 `features` 字段退役 → `vision: bool`（本地 `model_type=="vl"`；云 `supports_vision is True`）；`modality` 保留展示 |
| `model_pool/config.py` | 候选 `features` 字段**读时容忍忽略**（存量 YAML 含 `[vision]` 可加载，不构成任何路由行为）；编辑器写路径不再产生该键 |
| `registry.py` | Evidence 仍存 `features`（存储格式兼容），选择器不再消费 |
| `cloud_discovery.py` | `supports_tools` 保留字段但退出路由/面板；`supports_vision` 语义 = 唯一能力声明通道 |
| `admin.py` | dry-run detail 透传不变；runtime 端点每候选增 `vision: bool`（执行裁决：小标数据源由 catalog 改为 runtime 端点 — 同 selector 判定源、面板已轮询的既有端点，避免把目录拉进渲染路径破坏 A2 惰性预热裁决 + 引入缓存一致性问题；catalog `vision` 仍保留供编辑器/向导） |
| `dashboard/js/model-pools.js` | 编辑器能力列整体删除（回归「模型+优先级」）；候选卡渲染小「视觉」标（数据源 = runtime 端点每候选 `vision`，首渲染即有、随轮询自动更新；structSig 纳入 vision 保证声明变更重建卡片） |
| `dashboard/js/cloud.js` | `supports_tools` chip 退役；`supports_vision` chip = 视觉声明（点亮=能看图 → 进池自动带视觉；点灭=解除回未声明）；`.x` 排除态退役 |
| `style.css` | 清理 7.8.0 能力控件专属样式（.mp-feat-* / .cpm-cap-chip.x） |

## 存量数据（零手动迁移）

- 池候选 `features`（含 4 条遗留 `[vision]`）读时忽略 → 自动作废；
- `small-vl` 能力自动来自 `models.d` `model_type: vl`；
- 3 个云视觉候选（cand-1a18c915094s/p、cand-mvs52l4h）需在云面板点亮
  `supports_vision`（部署后一次性操作，用户或获授权后由助手执行）。

## 测试（TDD，沙箱逐条红→绿）

- selector：带图过滤（本地 vl / 云 supports_vision=true 各一）、无图全
  参与（VL 排 p1 全接 + VL 排 p2 不跳过）、无视觉候选→400 错误码、
  RuntimeView.local_vision 判定、7.8.0 排除语义测试全部翻面删除。
- dispatch：vision 提取（openai image_url / anthropic image）、tools
  不再构成要求、400 自解释消息（指向模型侧）、既有 feature 400 测试翻面。
- catalog：本地 vision=bool（vl→True/llm→False）、云三态→bool。
- config：features 字段容忍加载。
- Node VM 面板：能力列删除、视觉小标。
- 浏览器硬门禁：test_08 重写 — 池内仅文本候选时带图请求 400 自解释 →
  加 VL 候选（scratch models.d 增 vl 模型 + 桩引擎）→ 同请求 200 直通；
  纯文本请求按优先级走文本候选（VL 在跑也不抢）。云面板门禁对齐
  vision 单 chip。
- 全量 pytest + 干净 worktree 复验 + cross-review（fresh-context 一次）。

## 明确不做

- tools/stream/structured 不建模、不提取、不检查（用户裁决 3）。
- 无「纯文本跳过 VL」规则（用户裁决 2 明确否决）。
- 无 PROBED 视觉探测（声明是唯一来源；探测系统协议证据部分不动）。
- `supports_tools` 字段不从 CloudModel/YAML 删除（格式兼容），只退出
  路由与 UI。
- 会话亲和（I-2 残留）仍不在本 PR 范围。
