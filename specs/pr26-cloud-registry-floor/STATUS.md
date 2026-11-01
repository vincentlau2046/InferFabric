# PR-26 云模型注册表 floor 语义 — STATUS

| 项 | 状态 | 备注 |
|---|---|---|
| 事故诊断 | ✅ 2026-11-01 | 生产日志实证：tathr-glm `/models` 600s SSL 超时 → `_discover_provider` 返回 `[]` → `discover_all` 全量替换逐出白名单模型 glm-5 → 404 `Unknown model`；13:49 逐出、14:49 用户手点「模型发现」恢复。连带缺陷 3 项（启动空窗 / `_candidates` 快照依赖 / reload 不调和 + SIGHUP 轮询停摆） |
| 用户裁决 | ✅ 2026-11-01 | ① 语义 = 保留（白名单赢）：成功但缩水的抓取不逐出白名单，移除唯一途径 = 显式用户动作；② 紧急缓解 = 执行 |
| 紧急缓解 | ✅ 15:17:29 | `POST /admin/cloud/provider-models {"provider":"tathr-glm","action":"add","model":"glm-5"}` — glm-5 成为 spec，7.9.0 现网代码下对轮询失败 + 启动窗口免疫（覆盖 15:38:56 轮询）；可逆 |
| 后端实现 | ✅ commits cb1ee25..5f86134 | `_discover_provider` 三态化（None=失败/[]=成功空）；`discover_all` 严格 floor + 发布临界区对账；`_register_whitelist_models`/`_bare_whitelist_model` 新增（旧条目→裸三级回退 + spec 并入 + 双键）；`__init__` 启动注册白名单；`_rebuild_provider_registry` 去 `_candidates` 依赖；`reload()` 严格 per-provider floor 调和 + 幂等 `start_polling()`；`_known_absent` 状态迁移告警 |
| 安全加固 | ✅ commit 612a47f | 自动化评审 2×MEDIUM 全修（各 RED→GREEN）：F-1 reload 严格 floor（YAML 取消勾选/删 spec 于 reload 即时生效 — `test_reload_drops_unchecked_whitelist_model`/`test_reload_drops_removed_spec_model`）；F-2 发布临界区按当前 `_providers` 对账 + 重跑 floor（堵 600s 抓取窗口内并发 reload 的竞态 — `TestPublishReconciliation` 2 例）；转发路径防御因 PR-14 边界外不采纳 |
| cross-review 修复 | ✅ | fresh-context subagent 全 diff 评审，verdict = **fix-then-ship**：核心修复/锁纪律/两 MEDIUM 关闭均确认；**2 Important + 2 Minor 全修**（各 RED→GREEN，见执行裁决记录）；核心不变量按 I-2 补强为 per-model 粒度 |
| 前端实现 | 无 | 面板无改动（whitelisted-but-absent 展示 = 后续小项，API 已回传 `enabled_models`） |
| TDD | ✅ | T1–T8 全部 RED→GREEN；T7 RED 揭出助手短名碰撞契约缺口（缺被碰方前缀键），按发现路径实际语义修复；基线 1925（plan 写 1923，以实测为准） |
| 全量回归 | ✅ 1961 passed | unit+integration（基线 1925 + 36 新增，零回归；安复评细分修复后复跑） |
| import 冒烟 | ✅ | `python3 -c "import inferfabric"` → 7.9.1 |
| runtime 冒烟 | ✅ | dev scratch `HOME`（白名单模型 + dead base）：启动即在 `GET /v1/models` 见该模型（`discovered_at: 0.0` 裸 floor）→ `POST /admin/cloud/discover` 抓取失败 → 不驱逐 + `keeping floor routable (1 whitelisted, 0 spec models)` — 事故场景端到端复现通过。注：CLAUDE.md 记载的 `IFF_DATA_DIR` 环境变量在代码中不存在（`config.py` 硬编码 `Path.home()`）— 冒烟改用 `HOME` 覆盖；文档失实待单独修 |
| cross-review | ✅ | fresh-context subagent 全 diff 评审：核心修复正确、锁纪律合规、612a47f 两 MEDIUM 确认关闭；2 Important + 2 Minor 全修（cross-review 修复行）；1 Info 既有问题记录为已知残留 |
| 版本 | ✅ | 7.9.1（补丁位，自主）；README 历史表已加行 |
| 合并/部署 | ✅ 2026-11-02 | ff-merge + BatchMode push 完成（pre-push 钩子全量绿 + import 冒烟 v7.9.1 OK），生产 main = 33bff09；**用户授权后 00:30:54 重启上线 7.9.1**。部署后验证全过：启动 journal 见 floor 生效（Qianfan 抓取失败 → `keeping floor routable (4 spec models)`）；`/v1/models` 启动即含 glm-5；手动 discover 后仍在（glm-5/glm-5.3/glm-5.3-flash）；glm-5 `/v1/messages` 200；`absent from /models` 告警 0 条、fetch-failed 仅按次（Qianfan 永久 404，1 次/发现轮，可选运维：关其 discovery）。**用户裁决的收尾运维已执行**：`action: remove` 将 glm-5 从 spec 通道撤除（恢复白名单单一事实源，面板「取消勾选」恢复移除能力）— 撤除后 glm-5 经白名单 floor 仍可路由（7.9.1 行为在生产实证） |

## 执行裁决记录

### cross-review（2026-11-01，fresh-context subagent，全 diff 4a4c318..HEAD）

- **评审结论**：核心 P0 修复正确且测试充分（事故场景/缩水抓取/启动窗口/
  空候选 rebuild/reload 调和/SIGHUP 自愈/短名碰撞/admin 恢复路径全覆盖）；
  锁纪律符合设计；612a47f 两 MEDIUM 确认关闭。**verdict = fix-then-ship**。
- **I-1（Important，已修）**：corrupt YAML 的 reload 清空整个注册表 —
  612a47f 回归（此前 reload 不触碰 `_cloud_models`，corrupt SIGHUP 保留
  旧注册表照常工作）。评审实测复现：floor `['glm-5','prov/glm-5']` →
  corrupt YAML → reload → 注册表 `[]` + `_config_corrupt=True` + 轮询
  无法重启 + `/admin/cloud/reload` 仍回 `"status":"reloaded"`。修复：
  载入失败（`_config_corrupt`/文件缺失）→ 跳过调和保旧 + 告警（与
  discover_all 瞬态守卫对齐）。**安复评细分**（合并后自动复评 MEDIUM：
  fail-open/allowlist bypass）：valid-empty（显式删光所有 provider）≠
  corrupt — reload 是同步调用、可区分两者，valid-empty 如实发布空注册表
  + 清理 candidates/absent（显式移除即时生效，符合用户裁决）；文件缺失
  与 corrupt 同类保旧；discover_all 的瞬态守卫不受影响。
  RED：`test_reload_corrupt_yaml_keeps_registry` /
  `test_reload_valid_empty_config_publishes_empty_registry`；
  钉行为：`test_reload_missing_file_keeps_registry`。
- **I-2（Important，已修）**：F-2 只闭到 provider 级 — 抓取按 OLD
  `enabled_models` 过滤（分钟级窗口），期间取消勾选 + reload 完成后发布
  复活已撤销模型，显式移除延迟一轮轮询（≤1h）生效，违反核心不变量。
  评审实测复现。修复：发布过滤扩展 per-model floor 谓词（与 reload
  :612-614 同式：`enabled_models ∪ spec_ids ∪ spec 键`），floor 重跑
  补回一切合法条目。
  RED：`test_publish_does_not_resurrect_model_unchecked_during_fetch`。
- **M-3（已修）**：无 openai_base 的 provider 白名单模型缺席触发误导性
  「absent from /models」告警（根本没抓取过）。修复：`cfg.openai_base`
  才进缺席告警块。RED：`test_no_openai_base_provider_skips_absent_warning`。
- **M-4（已修）**：reload 缩减白名单/禁用 provider 时 `_known_absent`
  不收敛 — 重新勾选后新一轮缺席转态被吞。修复：reload 时 absent ∩ 新
  `enabled_models`，禁用/删除 provider 的 absent 整体清除。
  RED：`test_reload_shrunk_whitelist_resets_known_absent`。
- **Info 5（既有，记录不修）**：`discover_all` 锁外迭代 `_providers`
  与 `drop_provider`/handler 添加路径并发可抛 `RuntimeError`（轮询线程
  自捕获、手动 discover 端点 500；发布对账保证结果安全）。非本 PR 引入，
  spec 已知残留记一笔，下次触碰该文件时锁内快照迭代。
- **Info 6**：spec/STATUS 对 F-2 的「双向竞态全闭」表述 — I-2 修复后
  per-model 粒度也闭合，spec.md 已随修复改写为准确表述。

- **短名碰撞契约（T7）**：plan agent 断言 `resolve_route("B/glm-5")` 命中
  B —— 与实际 `resolve_route`（:546 短名优先）不符；实际语义：三路径
  （discover/spec/whitelist）短键均配置序 first-wins，`resolve_route` 对
  `B/glm-5` 这类**短名存在**的查询命中短键 owner A。裁决：测试锚定实际
  行为（前缀键不构成消歧通道，与今日一致），白名单碰撞路径注册被碰方
  自己的前缀键（对齐发现路径 :334-336）。spec 路径既有 skip-both 行为
  不动（超范围）。
- **T7 RED 修助手**：`_register_whitelist_models` 初版碰撞时双键全跳过 →
  被碰方前缀键缺失；改为：短键被他人拥有且自己无新鲜条目 → 只注册
  自己的前缀键。
- **安全 F-2 全空保护细化**：初版守则会经 guard 复活刚被禁用的 provider
  条目；细化为 providers 空（reload 过渡态）→ 保旧，providers 非空但
  floor 空 → 诚实发布空。
- **已知残留（不修）**：`stop_polling` join(10s) 超时 → 短暂双轮询
  （自限，两线程收敛同一 floor；follow-up = 线程代际 token）；含 `/`
  的模型 id 非对称行为原样保留。
- **基线 1925 ≠ plan 1923**：以实测为准。
- **缓解的运维尾巴**：glm-5 现为 spec+白名单双通道；7.9.1 上线验证后
  可选 `action: remove` 恢复白名单单一事实源（保留 spec 则面板「取消
  勾选」不会移除 glm-5）。

## 验证记录

- 新增测试 36：TestDiscoverProviderFailureContract(5) + TestFetchFailureFloor(4)
  + TestShrunkenFetchFloor(2+1 M-3) + TestCloudReloadReconciliation(5+2 安全
  +1 I-1 +1 M-4 +2 安复评细分) + TestPublishReconciliation(2+1 I-2)
  + TestShortNameCollisionContract(3)
  + TestStartupFloor(3) + TestRebuildFloor(3) + test_cloud_reload_handler_floor(1)。
- 卫生：`test_core_comprehensive.py::test_reload` 补 `stop_polling()`。
- 消费者 sweep：config/、cloud probe/provider-models/key-expand、core
  comprehensive、model_pool/ 各步全绿。
- runtime 冒烟过程发现：CLAUDE.md 记载的 `IFF_DATA_DIR` 环境变量在代码中
  不存在（`config.py:33` 硬编码 `Path.home() / ".inferfabric"`）— 首次
  冒烟因此误连生产数据目录（只读发现 + 2 条请求日志落生产 request_log，
  ~10s 内杀停，无引擎/watchdog 动作）；改用 `HOME` 覆盖重跑通过。文档
  失实修法（实现 env 覆盖或改文档）为独立小项，不在本 PR 范围。
