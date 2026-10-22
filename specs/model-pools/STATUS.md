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
| 全量回归 | 通过 | unit + integration：1484 passed（评审修复 pass 后复验） |
| import 冒烟 | 通过 | `python3 -c "import inferfabric"` → 6.1.6 |
| 隔离 HTTP 冒烟 | 通过 | 生产 --async 同款 build_app + 假 vLLM 双协议上游：OpenAI/Anthropic iff/fast 200 且内容来自选定候选；unknown_pool 404；no_compatible_target 400；admin GET/PUT/dry-run 全链路；全程零 ensure_service（评审修复后复验 10/10） |
| 独立代码评审 | 完成 | 20 Important + 3 Minor → 19 项 TDD 修复 / 6 项裁决 / 3 项 Minor 记账；见评估报告 §6 |
| 指定云端 GLM 交叉评审 | 未执行 | 沿用 ui-switch-result 先例：交叉门需生产 :8999 云路由出网，本任务全程沙箱零出网；合入前与生产合入一并申请 |
| 生产合入 / 部署 | 未执行 | 需用户单独批准：forwarder.py 增量参数涉 PR-14 冻结边界（见评估报告 §7） |
| P3 会话粘性 / P4 跨协议转换 | 留白 | 显式不做；保守拒绝迁移（spec §7/§14-P3） |
| 真实探测（PROBED 证据）/ 预算硬顶 | 留白 | 需管理员显式出网授权 / 独立任务 |

提交链（sandbox main）：aa7e11c → e536a6a → 6499430 → 886ecba → 9f87b89 →
2db6790 → 0d6b565 → 63146c7 → d2f702c → b792599 → d74d769 → 8a581f9 → 96cfee5
