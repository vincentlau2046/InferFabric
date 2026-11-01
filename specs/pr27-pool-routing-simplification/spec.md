# PR-27 模型池路由简化 — spec

**日期：** 2026-11-02　**版本：** 7.10.0（minor，用户裁决即批准）
**级别：** P0（生产事故：coding agent 会话被网关 400 拒绝）

## 背景与事故（2026-11-02）

用户经 `iff/fast` 使用 coding agent（Claude Code 等）时命中：

```
API Error: 400 Request for iff/fast carries non-migratable history
(thinking signatures / tool results) and the pool has multiple eligible
targets — refusing to migrate it
```

**根因（深挖后确认）**：`dispatch.py` 的「不可迁移历史守卫」（PR-23
spec §7 / 终审 #3 引入）在**候选选择之前**解析请求 messages——只要
`compatible_count > 1` 且历史含 tool_calls/tool_result/thinking 签名即
400 拒绝。对 coding agent 会话这是恒真条件（几乎每条请求都带工具历史），
且：

1. 守卫不看历史来源模型——fast p1 就是会话正在使用的 glm-5，根本
   不存在「迁移」；
2. 与真实客户端行为不符——用户实测在 Claude/coding agent 生产过程中
   直接换模型不会被拒（客户端不复放异模型 thinking 块，工具历史是
   可移植 JSON）；
3. 无会话亲和（I-2 残留）背景下的过度设计。

## 用户裁决（2026-11-02，原话）

> 不需要将功能设置的这么复杂，你解析那么payload干啥，我只需要VL类
> 优先路由VL模型，并且没有配置VL或者没有可用VL模型的情况以优先级
> 为准进行路由正常调用，而不是让你过渡设计，我不需要管其他的标志，
> 这是模型或者provider需要考虑是否支持的问题。

**新契约**：

1. **网关不解析会话历史**——`_has_non_migratable_history` 及 OpenAI/
   Anthropic 双守卫整体删除；历史块是否被目标模型接受由模型/provider
   自身处理。会话亲和（session affinity）需求一并撤销（I-2 永久关闭）。
2. **VL 软优先**：带图请求把 vision-capable 候选排到尝试链前面
   （排序键 `(not vision_pref, priority, id)`，`vision_pref` = 请求带图
   且候选 `_vision_capable(...) is True`），其余候选仍按优先级参与
   同一尝试链。
3. **没配置 VL / 无可用 VL → 纯优先级正常调用**——不再 400
   `no_compatible_target`（视觉理由）。
4. `no_ready_target`（503，全部候选未就绪）与 `no_compatible_target`
   （400，无协议兼容候选）语义不变；`unknown_pool` 404 不变。

## 实现面

| 改动 | 文件 | 说明 |
|---|---|---|
| 删守卫函数 + 双守卫块 | `model_pool/dispatch.py` | `_has_non_migratable_history` / openai :321-331 / anthropic :439-448（7.9.1 行号）整体删除；`compatible_count` 降为纯观测（注释更新） |
| `_no_eligible_message` 简化 | `model_pool/dispatch.py` | 删 `d.detail` 拼接与视觉声明指引后缀（feature_unsupported 已不存在）；只列 `候选id: 理由` |
| vision 降为排序偏好 | `model_pool/selector.py` | `_static_reason` 删视觉检查（返回值从 tuple 简化为 `str \| None`）；排序键加 `not vision_pref` 首位；`feature_unsupported` 理由删除；`CandidateDecision.detail` 字段删除；`_vision_capable` 三态保留（None=未知=无偏好，元数据故障不改变路由）；home_loc 在偏好序上计算（allow_cross_location=false 时带图请求的 home 可合法变为 VL 候选位置） |
| dry-run 序列化 | `model_pool/admin.py` | decisions 去掉 `detail` 键（面板 JS 零消费，grep 核实） |
| 注释 | `model_pool/config.py` | `features` 字段注释更新为全域退役 |
| README 正文 | `README.md` | 「未声明的能力不投递」→「VL 软优先」；配置示例 features 行退役 |

**明确不做**：无 dashboard 代码改动（视觉 chip / runtime 端点数据源
不变）；不改转发核心路径（PR-14；dispatch/selector 是池层）；无
YAML 迁移（features 读时容忍语义延续）。

## 语义裁决记录（实现中发现）

- **云 VL 声明 vs 本地元数据故障**：`local_vision=None`（mgr 列举异常）
  时，唯一已知 vision-capable 的云候选获得软优先——带图请求合法落云。
  这是偏好而非 7.9.0 评审担心的「元数据故障改道」：判定源是云模型
  本体的显式声明，且符合用户「VL 优先」规则。7.9.0 的
  `test_vision_metadata_failure_does_not_reroute_to_cloud` 据此拆为两例：
  双方未知 → 纯优先级走本地；云已声明 VL → 软优先落云。
- **evidence features 不背书偏好**：证据里记过 vision 也不构成偏好，
  判定源恒为模型本体（云 supports_vision / 本地 model_type）。

## 测试

- **翻面**（13 例 RED→GREEN，全部因旧语义失败、新实现通过）：
  selector 5（软优先排序 / 无 VL 纯优先级 / 云未知无偏好 / features
  忽略 / 错误分类不含视觉）+ dispatch_openai 2 + dispatch_anthropic 1
  + review_fixes 2（历史直通 openai/anthropic）+ final_fixes 1
  （冷却备用下工具历史直通）+ admin 2（dry-run detail 键删除 +
  无 VL 纯优先级）。
- **删除**（被新契约取代）：`test_vision_unsupported_detail_is_self_explaining`、
  `test_feature_unsupported_400_message_self_explaining`、
  `test_redacted_thinking_is_non_migratable`、
  `test_unsigned_thinking_is_migratable`（净 -3，套件 1961 → 1958）。
- **新增钉子**：`test_vision_prefers_declared_cloud_vl_over_unknown_local`
  （语义裁决记录第 1 条）+ `test_vision_request_without_vision_capable_routes_by_priority`。
- **浏览器门禁**（真实 chromium + scratch 代理，test_08 重写）：
  TXT-only premium 池带图请求 → **200 直通**（原 400 自解释断言删除）；
  fast 池换 VL 候选后带图/纯文本均 200、视觉 chip 渲染 — 10/10。
