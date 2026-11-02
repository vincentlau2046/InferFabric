# PR-27 模型池路由简化 — STATUS

| 项 | 状态 | 备注 |
|---|---|---|
| 事故诊断 | ✅ 2026-11-02 | iff/fast 400 "non-migratable history"：守卫在候选选择前粗粒度解析 messages，多候选池 + 工具/thinking 历史 = 恒真拒绝；不看历史来源模型（fast p1 即会话的 glm-5）；与客户端实测（中途换模型不被拒）不符 |
| 用户裁决 | ✅ 2026-11-02 | 网关不解析会话负载；VL 类优先路由 VL 模型；无配置 VL/无可用 VL 纯优先级正常调用；是否支持是模型/provider 的事。会话亲和需求撤销（I-2 永久关闭） |
| 后端实现 | ✅ | dispatch 删 `_has_non_migratable_history` + 双守卫；selector vision 降为排序偏好（`(not vision_pref, priority, id)`），`feature_unsupported`/`CandidateDecision.detail` 删除，`_vision_capable` 三态保留；admin dry-run 去 detail 键（面板零消费，grep 核实） |
| 前端实现 | 无 | dashboard 零改动（视觉 chip / runtime 数据源不变） |
| TDD | ✅ | 13 例翻面全 RED→GREEN（失败原因均为旧语义）；2 例新钉子；4 例被取代测试删除（净 -3） |
| 全量回归 | ✅ 1958 passed | unit+integration（基线 1961 − 净删 3 = 1958，零回归） |
| 浏览器门禁 | ✅ 10 passed | 真实 chromium + scratch 代理整文件 10/10；test_08 重写为直通 200 契约（TXT-only 池带图 → 200；VL 换模后带图/纯文本均 200 + 视觉 chip） |
| import 冒烟 | ✅ | `python3 -c "import inferfabric"` → 7.10.0 |
| 语义裁决 | ✅ | 云已声明 VL + 本地元数据故障（未知）→ 软优先合法落云（偏好而非 fail-open 改道，判定源 = 云模型本体声明）；原 metadata-failure 测试拆两例钉住 |
| cross-review | ✅ ship | fresh-context subagent 全 diff 评审：0 Critical / 0 Important / 2 Minor（selector 两处注释失实）+ 1 README 表述建议 + 3 Info（云侧 VL home 移位语义记录 / VL 冷却时带图落 TXT 由上游 400 终止 = 新契约推论 / 测试记账口径）— Minor+建议已顺手修（提交 2）；七项逐查全过（守卫零残留 / 排序语义确定 / detail 消费面完整 / 错误码语义不变 / PR-14 冻结面零改动 / 翻面断言为换钉非弱化 / 边界无缺陷），评审本机复跑全量 1958 + 浏览器 10/10 |
| committed-tree 复验 | ✅ | 干净 worktree 全量 1958 passed + import 冒烟 7.10.0（提交 1 后复验；提交 2 仅注释/README，随后复跑 model_pool + import） |
| 版本 | ✅ 7.10.0 | minor（用户裁决即批准）；README 历史行 + 正文契约更新（「未声明能力不投递」→「VL 软优先」；示例 features 行退役） |
| 链路梳理 follow-up | ✅ | 用户裁决（2026-11-02）：修池路径流式 include_usage 缺失并合入 7.10.0 — `handle_pool_chat` 入口注入 `stream_options.include_usage=true`（setdefault 语义，先于选路，本地/云候选同享），4 例新契约测试 RED→GREEN；Anthropic 无对应缺口（流恒带 usage，SSE 旁路提取），全量 1962 |
| 调用树网格对齐 follow-up | ✅ 7.10.1 | 用户批准方案 A：共享六列 subgrid 网格（端口\|链a\|角色\|链b\|候选链\|态徽标）对齐端口/角色/候选链起点，候选内部自然排布；`@supports not` flex 回退；零闪烁（treeHtml DOM 输出不变，applyDynamic by-id 翻转不受影响）；面板单测 4 新钉 + 浏览器 test_05b 时间线坐标断言 + test_05d 四行左缘 ≤1px 断言（RED 实测 16.9px 错位）；cross-review 0 Critical / 1 Important（README 测试数字失实，已修）+ 5 Minor（4 修 1 缓：轨道分割正则嵌套括号前瞻脆弱点） |
| committed-tree 复验 (7.10.1) | ✅ | 干净 worktree 全量 1966 passed + 面板/浏览器 154 passed + import 冒烟 7.10.1 |
| 合并/部署 | ✅ 2026-11-02 | 7.10.0 已随用户授权重启上线并验证（iff/fast 400 根因消除）；7.10.1 ff-merge + BatchMode push |

## 执行记录

- 深挖结论先行（用户质疑初判「换模型问题」正确）：守卫粒度缺陷 +
  无来源追踪，非模型切换问题。
- `test_vision_metadata_failure_does_not_reroute_to_cloud`（7.9.0 安全
  评审钉子）在新语义下前提不成立：元数据故障 + 云声明 VL 时软优先落云
  是用户规则的正例。拆为
  `test_vision_metadata_failure_unknown_local_routes_by_priority`（双未知
  → 本地 p1）+ `test_vision_prefers_declared_cloud_vl_over_unknown_local`
  （云 VL 软优先）。后者实现已就位后补钉（集成层钉子，选择器层同语义
  已 RED 驱动）。
- 旧 anthropic 守卫测试的 fake 返回裸 `200`（无 `committed`）——守卫
  删除前从未走到转发层，暴露后按 CloudResult 契约修正 fake
  （`committed=True, status=200`）。
- `compatible_count` 保留为纯观测字段（注释已更新）；无消费者依赖。
- 7.10.1 调用树对齐（2026-11-02，用户授权「同步处理」）：UI 门禁遵循
  dashboard-ui-testing-gate — 真实 chromium 坐标断言驱动（test_05d RED
  实测 role 行错位 16.9px → 修复后 ≤1px），文本断言不作为可用性证据。
  review 修复随回归 154 passed 后提交（39f2564），committed-tree 复验
  干净 worktree 全绿后合并。
