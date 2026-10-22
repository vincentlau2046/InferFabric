# 逻辑模型池 — 沙箱评估报告（2026-10-23）

范围：P1（稳定逻辑名 / 热映射 / 双协议过滤 / dry-run / 管理接口）+ P2（预算内
优先级故障回退）。全程 sandbox 内完成（用户要求：生产 IFF 持续供 token），
零 GPU 操作、零付费云请求、零真实探测出网。

- 设计：`docs/superpowers/specs/2026-10-23-logical-model-pools-design.md`
- 计划：`docs/superpowers/plans/2026-10-23-logical-model-pools.md`
- 状态：`specs/model-pools/STATUS.md`
- 提交链：aa7e11c…96cfee5（13 笔，sandbox main）

## 1. 核心验收对照（spec §14，P1+P2 范围）

| # | 验收项 | 结论 | 证据 |
|---|---|---|---|
| 1 | 同端口双协议客户端请求同一逻辑名，只选兼容协议端点 | 通过 | 隔离 HTTP 冒烟：同一 build_app 上 `/v1/chat/completions` 与 `/v1/messages` 均 `iff/fast` → 200；单测 test_dispatch_{openai,anthropic}.py；selector 双协议视图 |
| 2 | 只有 OpenAI 端点的目标不误接 Anthropic 请求 | 通过 | test_selector.py：openai-only 候选对 anthropic 请求全部排除（reason=no_evidence → no_compatible_target 400，不 503）；admin status 双协议视图分别展示 |
| 3 | 请求中改映射，新请求用新 revision；并发编辑不丢更新 | 通过 | 不可变快照 + revision CAS（test_registry.py：过期 revision 409、外部编辑拾取 rev+1；评审修复 #2：内容变化必推进 revision）；请求内持有旧快照引用不受影响；dashboard toggle 同 CAS 路径 |
| 4 | 优先级/冷却/半开恢复可重复验证 | 部分通过 | 优先级与冷却（CooldownStore TTL 60s、选择期排除、跨请求生效）有测试；**半开探测恢复未实现**（冷却到期自动恢复 = 保守等价物；主动半开探测留白） |
| 5 | 总预算不超、超时含等待、部分流输出后绝不切换/重放 | 通过 | run_attempts：max_attempts 计全部真实上游调用、deadline 覆盖 429 等待与限流排队（评审修复 #7，test_pool_forward.py + test_review_fixes.py）；committed 边界四处测试（云 suppress / Anthropic local 含写中途失败 / run_attempts 不重放，b792599 + 评审修复 #9） |
| 6 | tools/图像/结构化/上下文不匹配明确拒绝 | 通过 | extract_requirements + selector reason=feature_unsupported → 400 no_compatible_target（冒烟第 4 项：vision 请求明确拒绝）；证据未覆盖的功能不放行（评审修复 #4）；不可迁移历史（tool 结果/thinking 签名）多候选池明确拒绝（评审修复 #5，spec §7） |
| 7 | 池权限/目标权限不被别名绕过；旧 guest 不自动扩权 | 通过 | 完整逻辑名鉴权（`iff/fast` 整串，不 split("/")[-1]）；单测断言短名不放行 |
| 8 | 池热更新不命中旧缓存；池请求旁路响应缓存 | 通过 | dispatch 不触碰 response_cache（GET/PUT 均不触碰，含 Anthropic 本地 JSON 路径，评审修复 #21）；直连缓存行为不变（forwarder 默认参数路径逐字节不变） |
| 9 | 备用救回时仍能查到全部 attempt 与费用归属 | 通过 | 评审修复 #18 补齐：AttemptRecord 逐 attempt 结构化记录（ordinal/目标/状态/committed/耗时）+ 失败尝试各记一条 RequestLog（路由含物理目标标识）；费用归属实际目标（route 字符串含目标） |
| 10 | 本地未 ready 不触发抢占或 VL 启动 | 通过 | ready_only：runtime_view 只认已启动端口（评审修复 #15：按 served_name 匹配）；`ensure_service` 调用即 AssertionError（单测+冒烟第 10 项）；无任何 GPU 生命周期调用 |
| 11 | 全部测试用假上游；真实 GPU/付费生成分别申请 | 通过 | 174 例 model_pool + 全量 1484 例全部假上游/零出网；隔离冒烟用 aiohttp 假 vLLM 双协议上游；未发生任何真实 GPU/付费操作 |

**结论：P1 全项通过；P2 通过（验收 4 的半开主动探测为显式留白，见 §3；
验收 9 的逐 attempt 观测已在评审修复 pass 补齐）。**

## 2. 质量门

| 门 | 结果 |
|---|---|
| 全量 pytest（unit + integration） | **1484 passed / 0 failed**（43.4s，sandbox main，评审修复后复验） |
| `python3 -c "import inferfabric"` | 通过（6.1.6） |
| 隔离 HTTP 冒烟（生产 --async 同款 build_app） | 10/10 通过（评审修复后复验） |
| `node --check`（model-pools.js） | 通过 |
| 独立代码评审 | 完成：20 Important + 3 Minor → 19 修复（TDD）/ 6 裁决 / 3 Minor 记账（见 §6） |
| 云端 GLM 交叉评审 | 未执行（零出网约束；合入前与生产合入一并申请） |

自动化安全审查（提交后台扫描）3 项 MEDIUM 全部修复：
1. 池路径限流可被绕过 → 统一 dual_gate 以逻辑名为键（d2f702c 内）；
2. 已提交流中断 fail-open（可能重放/双信封）→ committed 信号贯通云与
   Anthropic 本地传输边界（b792599，4 例回归测试）；
3. `str(e)` 插值进日志与 CloudResult.error（可携带上游响应片段）→
   固定错误码 + 异常类名（8a581f9）。

## 3. 已知限制（显式留白）

- **P3 会话粘性**：无会话固定；带不可迁移历史（tool 结果/thinking 签名）的
  请求在多候选池被保守拒绝（spec §7，评审修复 #5）。
- **P4 跨协议转换**：完全不做；透传不跨协议（spec §4.2）。
- **真实探测（PROBED 证据）**：不自动执行，需管理员显式操作 + 出网授权；
  当前兼容性判断依赖管理员声明（accept_declared）。
- **预算硬顶（费用上限）**：未实现；max_attempts/deadline 是次数/时延预算，
  不是金额预算。
- **半开主动探测**：冷却到期即恢复（被动），无主动半开验证。
- **OpenAI 本地候选上游 5xx 不回退**：`_forward_request` 直发上游响应
  （PR-14 冻结语义）；云候选与 Anthropic 本地候选有完整回退信号。
  评审裁决维持（#8）；跨位置回退默认关闭后单位置池无备用目标，影响面进一步收窄。

## 4. 配置示例（models_pools.yaml，热加载）

```yaml
schema_version: 1
revision: 7                     # registry 管理，勿手改
pools:
  fast:
    enabled: true
    allowed_locations: [local]  # 跨本地/云回退默认关闭：显式加 cloud 才开
    accept_declared: true       # 显式接受管理员声明证据
    max_attempts: 2             # 全部真实上游调用总数（含同目标重试）
    deadline_ms: 60000
    candidates:
      - id: qwen-small
        priority: 1
        kind: local
        model_name: qwen-small
        declared_protocols: [openai, anthropic]
        features: [tools]       # 未声明的能力不投递（vision 未声明则拒绝）
      # 跨云回退示例（需 allowed_locations 含 cloud）：
      # - id: ds-flash
      #   priority: 2
      #   kind: cloud
      #   provider: baidu-codingplan
      #   model_id: deepseek-v4-flash
      #   declared_protocols: [openai]
```

管理面：`GET/POST /admin/model-pools`（CAS 更新）、`/status`（双协议视图）、
`/dry-run`、`/toggle`（单池开关）；dashboard「模型池」TAB 可视化同一能力。
`/v1/models` 列出启用池为 `{"id":"iff/fast","owned_by":"iff-pool"}`。

## 5. 用户确认过的边界（实施中遵守）

- 本地候选**只用已启动端口**，池调度**绝不自动启动本地模型**（防异常调度
  引发系统故障）— 冒烟与单测双重断言 `ensure_service` 零调用。
- 跨本地/云回退：显式开关、默认关闭、逐池开启。
- VL 限制不解除：未声明 vision 证据的候选不接图像请求。

## 6. 独立代码评审（Task 11 Step 4）

评审范围：`inferfabric/model_pool/`（7 个新文件）+ `chat_handlers.py` /
`handler.py` / `forwarder.py` 的增量 diff（aa7e11c..96cfee5）。
评审在独立上下文执行（spec + plan + Review Focus + ledger 全量提供），
结论 **Request changes：20 Important + 3 Minor**。按影响重评级后：
**19 项 Important 进单一修复 pass（全部 TDD RED→GREEN），6 项裁决维持
现状，3 项 Minor 记账**。修复后全量回归 **1484 passed / 0 failed**
（较修复前 +36 例回归测试），隔离 HTTP 冒烟 10/10 复验通过。

修复 pass 覆盖（编号=评审发现，全部有先失败后通过的测试锁定）：

| 类别 | 修复 |
|---|---|
| 配置健壮性 | #1 畸形类型降级为校验问题（不再 TypeError 逃逸）+ registry 解析崩溃保留最后有效快照；#2 外部编辑保留旧 revision 键时内容变化仍推进 revision（堵 CAS 覆盖窗口） |
| 证据可信 | #3 依赖指纹不符的 PROBED 证据不注入；#4 功能覆盖以放行证据为准（text-only 探测不放行 tools） |
| spec §7 | #5 不可迁移历史守卫：tool_calls/tool_result/thinking 签名 + 多候选 → 400 拒绝（单候选不受限） |
| 用户边界 | #6 allow_cross_location=False 强制生效（跨位置候选退出回退序，标注 cross_location_disabled） |
| 预算/重放 | #7 deadline 覆盖限流排队；#9 非流式 committed 先于写客户端；#12 501 归入不回退类（原被 5xx 分支吞掉） |
| 安全 | #10 客户端信封固定文案（上游错误体/凭证只进日志）；#11 错误体读取断连降级不逃逸；#16 物理目标容量门（同目标跨池/跨直连共享并发预算）+ 副本释放配对修复 |
| 就绪/发现 | #14 池含云候选时补云发现（守护式）；#15 就绪集合用 served_name（name≠served_name 模型此前恒不 ready）；#23 竞态窗口 AttributeError → AttemptResult |
| 观测/运维 | #18 逐 attempt 结构化记录 + 失败尝试日志带目标标识路由；#20 池重载并入 reload_all 且在依赖域之后；#21 池路径不写响应缓存（含 Anthropic 本地 JSON 路径）；#22 toggle CAS 先于存在性检查 |

裁决维持现状（理由与成本见 ledger，摘录）：
- **#8 OpenAI 本地候选上游 5xx 不回退** — PR-14 冻结核心语义；跨位置
  默认关闭后单位置池本就无备用目标。
- **#13 已提交流截断遥测** — pipe_stream_response 属冻结核心；async 桥接
  写 EOF，committed 不重放已闭环。
- **#17 冷却按端点（双协议共享）** — 401/403 是凭证级故障，共享冷却是
  正确语义。
- **#19 POST+expected_revision 即 CAS-PUT** — 同步 server 仅路由 GET/POST。
- **#3b 请求内旧快照** / **#1b GET 视图剔除悬空候选** — spec 设计行为。

Minor（记账不修）：selector 证据预过滤纯优化；route 字段与 record 单源
统一留 P3；dashboard 候选表虚拟滚动（当前规模远低于阈值）。

## 7. 生产合入前需用户单独批准的事项

1. **PR-14 边界**：`forwarder.py` 改动为增量可选参数
   （`suppress_client_errors: bool = False`，默认 False 时行为逐字节不变，
   由既有 forwarder 测试套件证明）；`CloudResult` 新增
   `committed`/`retry_after` 字段。属「不修改转发核心路径」约定的最小
   例外，需按 spec §13/§15 明确授权。
2. **云端 GLM 交叉评审**：需经生产 :8999 云路由出网。
3. **部署**：合入后需重启 `inferfabric-proxy.service`（systemd user）才生效；
   dashboard 新 TAB 同样需要重启（内存缓存 HTML）。
4. **现役配置**：`model_pools.yaml` 尚不存在于生产 — 不创建任何池时，
   全部新代码路径零激活（`iff/` 前缀检查 + registry 空快照直通），
   `/v1/models` 与直连行为与现状一致。

## 8. 部署步骤草案（不执行）

```bash
# 1. 沙箱终态复验（已通过，合入前重跑）
cd sandbox && python3 -m pytest tests/unit/ tests/integration/ -q
# 2. diff 审查后合入生产（main-only 工作流：ff-merge，精确文件清单）
#    - inferfabric/model_pool/（新增目录）
#    - inferfabric/forwarder.py / proxy/handler.py / proxy/chat_handlers.py（增量）
#    - inferfabric/dashboard/（新增 TAB）
#    - inferfabric/proxy_manager.py（CooldownStore 挂载，2 行）
#    - tests/（新增 model_pool + dashboard panel 测试）
# 3. 重启服务
systemctl --user restart inferfabric-proxy.service
# 4. 冒烟（生产）：GET /health；GET /admin/model-pools → {"revision":0,"pools":{}}
# 5. （可选）配置首个池后 SIGHUP 热加载或 /reload-config
```
