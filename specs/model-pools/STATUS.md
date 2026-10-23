# model-pools 状态

日期：2026-10-23（全部工作在 sandbox 完成，生产未改动）

| 项目 | 状态 | 证据 |
|---|---|---|
| P1 配置/校验（Task 1-2） | 完成 | config.py + evidence.py；tests/unit/model_pool/test_config.py、test_evidence.py |
| P1 registry/热更新（Task 3） | 完成 | 快照不可变、revision CAS、SIGHUP 热加载、外部编辑拾取 revision+1；test_registry.py |
| P1 选择内核（Task 4） | 完成 | 协议/能力/位置过滤 + 优先级 + 排除原因；test_selector.py |
| P1 双协议入口（Task 5-6） | 完成 | OpenAI/Anthropic iff/ 调度、完整名鉴权、缓存旁路、ready_only；test_dispatch_openai.py、test_dispatch_anthropic.py |
| P1 管理接口（Task 7） | 完成 | GET/POST(CAS)/validate/status/dry-run；test_admin.py；aiohttp 路由冒烟 4 路由 200/409 |
| P2 故障回退（Task 8） | 完成 | 固定错误动作表 + 统一预算 + 提交后不重放 + 端点冷却；test_pool_forward.py（24 例） |
| 安全审查修复 #1-#3 | 完成 | 限流统一 gate（429 不可绕过）/ committed 流中断不重放（b792599）/ str(e) 不入日志与错误串（8a581f9） |
| Dashboard 面板（Task 9） | 完成 | 只读池面板 + 双协议解释 + 服务端原子 toggle（CAS 409 处理）；Node VM 行为测试 6 例 |
| /v1/models 逻辑名（Task 10） | 完成 | iff/<pool> owned_by=iff-pool；无能力并集；空 registry 输出与现状一致；test_v1_models.py |
| 评审修复 pass（Task 11 Step 4） | 完成 | 19 项 Important TDD 修复（配置硬化/revision 推进/证据指纹/证据功能覆盖/§7 不可迁移守卫/跨位置强制/预算覆盖排队/committed 边界/客户端脱敏/501 不回退/云发现/served_name 就绪/物理目标容量门/逐 attempt 观测/重载顺序/缓存旁路/CAS 顺序/竞态窗口）；tests/unit/model_pool/test_review_fixes.py 36 例 |
| 全量回归 | 通过 | 工作区 1484 passed（评审修复 pass 后复验）；提交树干净 worktree 1443 passed（b9beb9f 后复验，差值 41 例为他人未提交 WIP 测试） |
| 提交卫生修复 | 完成 | 0d6b565 误扫入 handler.py 的 cloud-test WIP（=生产 f0c642e/48c5745 内容）经三方合并剥离（commit b9beb9f，提交树 13 例 RED → 全绿）；工作区 WIP 原样保留未提交；合入审查看净 diff `git diff a86f5bc..b9beb9f` |
| import 冒烟 | 通过 | `python3 -c "import inferfabric"` → 6.1.6 |
| 隔离 HTTP 冒烟 | 通过 | 生产 --async 同款 build_app + 假 vLLM 双协议上游：OpenAI/Anthropic iff/fast 200 且内容来自选定候选；unknown_pool 404；no_compatible_target 400；admin GET/PUT/dry-run 全链路；全程零 ensure_service（评审修复后复验 10/10） |
| 终审（opus 独立上下文，净 diff a86f5bc..5e7ff7f） | 完成 | Not ready to merge — 20 Important + 4 Minor；18 项进修复 pass / 2 项裁决 / 4 项 Minor 记账；见评估报告 §6.2 |
| 终审修复 pass | 完成 | 18 项全部 TDD RED→GREEN（test_final_fixes.py 43 例 + test_forwarder_pool_contract.py 15 例 + ratelimit/model_pool 既有用例更新）；工作区全量 1543 passed / 0 failed；GLM 云交叉评审经用户确认改为本地独立交叉审核 |
| 独立代码评审 | 完成 | 20 Important + 3 Minor → 19 项 TDD 修复 / 6 项裁决 / 3 项 Minor 记账；见评估报告 §6 |
| 交叉审核（原 GLM 云交叉评审） | 完成 | 用户决策（2026-10-23）：GLM 锁定不可用，云交叉评审改为本地独立交叉审核 = 终审（opus 独立上下文，评估报告 §6.2） |
| 生产合入 / 部署 | 未执行 | 需用户单独批准：forwarder.py 增量参数涉 PR-14 冻结边界（见评估报告 §7） |
| P3 会话粘性 / P4 跨协议转换 | 留白 | 显式不做；保守拒绝迁移（spec §7/§14-P3） |
| 真实探测（PROBED 证据）/ 预算硬顶 | 留白 | 需管理员显式出网授权 / 独立任务 |

提交链（sandbox main）：aa7e11c → e536a6a → 6499430 → 886ecba → 9f87b89 →
2db6790 → 0d6b565 → 63146c7 → d2f702c → b792599 → d74d769 → 8a581f9 →
96cfee5 → 468d006（评审修复）→ b9beb9f（提交卫生修复）→ 5e7ff7f（提交卫生记录）→ 终审修复 pass（本次）


## 运营视图 Phase 1（v7.1.0，2026-10-23）

日期：2026-10-23（sandbox 完成，生产未改动）；设计：
`docs/superpowers/specs/2026-10-23-model-pool-dashboard-ops-design.md`；
计划：`docs/superpowers/plans/2026-10-23-model-pool-dashboard-ops-phase1.md`

| 项目 | 状态 | 证据 |
|---|---|---|
| T1 冷却原因/最近尝试（CooldownStore 扩展） | 完成 | entries()/record_attempt()/last_attempts() + reason 传递；test_cooldown_runtime.py 16 例 |
| T2 run_attempts 尝试记录 | 完成 | admitted 传输后 record_attempt；401/403 带 cooldown_<status>；404 quarantine_404 |
| T3 registry 三态 + raw 基底 | 完成 | configured/last_load_error/raw_config()/compile_pool()；test_registry_runtime_state.py 12 例 |
| T4 管理端点扩展 | 完成 | GET /admin/model-pools 增 configured/load_error/raw；GET /admin/model-pools/runtime；dry-run 增 config 编辑态预览；test_admin.py +11 例 |
| T5 运营视图渲染（健康灯/运行态/三态分支） | 完成 | model-pools.js lampForProtocol/runtimeBadge；test_model_pools_panel.py TestOpsRender 6 例 |
| T6 三段式编辑器 | 完成 | raw 基底防丢数据；替换继承优先级（D4）；validate+dry-run 预览门禁；CAS 应用基点固定为打开时 revision；会话内回滚；TestEditor 7 例 |
| T7 空态初始化向导 | 完成 | 四角色卡（可移除/恢复）+ 预填启发式；CAS expected_revision=0；损坏文件只出横幅零 POST；TestWizard 5 例 |
| T8 接入片段 | 完成 | OpenAI/Anthropic 复制即用；ACL 与 /v1 陷阱提示；字面量 JSON.stringify 注入免疫；协议跟随 dashboard 源；TestSnippet 4 例 |
| T9 版本/文档/门禁 | 完成 | __init__ 7.1.0；README（运营视图/runtime 端点/dry-run config/版本历史）|
| 安全评审修复（commit 后台扫描 ×2 轮） | 完成 | #1 编辑器 CAS 旁路（后台刷新推进 revision → 打开时基点固定 + 409 后预览失效）；#2 片段池名注入 + http 降级（JSON.stringify 字面量 + location.protocol）|
| 全量回归 / 提交树复验 | 见提交 | 面板+池 28+…；全量见 quality gates |
| 一次性回滚语义 | 裁决 | doMpUndo 重取最新 revision 后回 POST 编辑前配置 — 用户显式回滚（先见最新状态再主动撤销），非旁路 |
| 配置历史/多版本回滚（跨会话） | 留白 Phase 2 | 本期为会话内单步回滚 |
| 延迟迷你图（候选行 TTFT/TPOT 趋势） | 留白 Phase 2 | 用户决策：内嵌候选行 |
| 真实探测（PROBED 证据）出网授权 | 留白 | 独立任务（同 v7.0.0 STATUS） |
