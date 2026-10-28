# PR-21 模型池双修复 — STATUS

| 项 | 状态 | 备注 |
|---|---|---|
| 诊断 | ✅ 2026-10-28 | 两 bug 根因链见 spec.md；ninfer 双协议以 7 天线上日志实证 |
| 实现 | ✅ 2026-10-28 | catalog 映射 + registry `local_protocols` 自愈回调 + proxy_manager 接线；JS 外科点亮 + `mpEditPoolParam` 重烘 |
| TDD | ✅ | 4 单测 + 1 VM 测 + 浏览器 test_05c + I-1 VM 测全部先观察到 RED（预期原因），后 GREEN |
| 全量回归 | ✅ | 1809 passed（unit+integration；含 I-1/M-4 新测） |
| 浏览器门禁 | ✅ | 9/9 passed（含新增 test_05c；console 零 error；I-1 修复后复跑仍 9/9） |
| import 冒烟 | ✅ | `python3 -c "import inferfabric"` → 7.6.0（版本随 bump 提交更新为 7.6.1） |
| 干净 worktree 复验 | ✅ | 提交树全量 1807 passed（沙箱工作区绿 ≠ 提交树绿） |
| cross-review | ✅ | fresh-context subagent 评审：无 Critical；1 Important（I-1 在途改动竞态）当场 TDD 修复；M-4 补测；M-1/2/3/5 存档下方留用户决策 |
| 版本 | ✅ | 7.6.1（补丁位自主升级，per version-bump-autonomy 记忆） |
| 合并/部署 | ✅ | 2026-10-28 用户放行（"ok，ok，顺带完成了"）：sglang 双协议顺带开启（331c4c6）→ 生产合并 6b00ccc + push；重启验收通过（目录 TXT=['openai','anthropic']；4 池 anthropic 干跑全 err=None；面板新 JS 已内联服务 mpEditApply×5）。PR-20 验收发现引擎幸存前提证伪（另见 pr20 STATUS.md 第二次验收记录） |

## 验证记录

- RED（预期原因逐条核对）：
  - `test_catalog.py::test_local_protocols_by_engine` — `['openai'] != ['openai','anthropic']`
  - `test_registry.py` 自愈三测 — `TypeError: unexpected keyword argument 'local_protocols'`
  - 浏览器 `test_05c` — `anthropic → no_compatible_target`（bug #1 实锤）；
    修 #1 后复跑仍 RED 于应用钮 disabled（bug #2 实锤，随 JS 修复转 GREEN）
  - VM `test_..._mutation_during_inflight_preview_does_not_rearm` — 迟到完成
    确定点亮应用钮（I-1 实锤），随代次修复转 GREEN
- GREEN：面板 VM + registry 150/150；浏览器 9/9 ×2 轮（bug #2 修复后、
  I-1 修复后各一轮）；全量 unit+integration **1809 passed**。
- 过程缺陷（测试自身，已修）：test_05c 首版等待条件用模型名匹配预览文本，
  实际预览显示候选 id — 永不满足；改为断言「openai/anthropic 均『选择』」。

## 评审记录（fresh-context subagent，2026-10-28）

评审 `a54cfb4..293de4e` 全分支 diff + spec，独立复跑专项 434 passed。

- **无 Critical。**
- **Important I-1（已修复，a51cc10）：** 模态预演在途改动 → 迟到完成重置
  validated=true + 点亮应用钮 → 未预演配置被放行（门禁绕过；mutator 就地
  改 _edit，会话令牌不设防；行内编辑器同问题早有会话换代设防）。修复 =
  rev 代次 + mpEditInvalidated() 统一失效拍子 + 完成时身份/代次双校验 +
  pv await 后重查；VM 测试 noAwait+yieldTicks+_drain 确定性复现，RED→GREEN。
  评审者判定本 PR 前该竞态潜伏不活跃（按钮永不亮），点亮路径使其变活 —
  故当场修，不留活门禁绕过进发布。
- **Minor 留用户决策（未修）：**
  - M-1：两处守卫保护下的按钮亮态不一致（预览失败于成功后/409 冲突后，
    按钮亮但 validated=false，apply 被守卫拦 + toast）— spec 已声明依赖守卫，
    可选对称修复（外科 re-disable）。
  - M-2：`mpEditPoolParam` 整模态重渲染可能在 Tab 提交 max_attempts 时丢
    焦点 — 可选改为只重烘按钮态。
  - M-3：本地端点指纹不含引擎类型 → 引擎换型后 store 旧 PROBED 条目仍注入
    （「收窄」只对 DECLARED 基线成立）。既有行为，spec 措辞已精确化；
    后续可选：指纹并入引擎类型。
  - M-5：local_protocols 回调若抛异常会传导到启动（与 known_local/
    provider_route 同暴露面，生产回调实际不可抛）— 可选 try/except 硬化。
- 评审者确认干净项：线程安全（全路径同锁、回调无锁、_models 换绑非就地）、
  mgr 构造顺序、known_local 与回调无分歧、云候选不受影响、热更新+CAS 均
  自愈、UI 唯一声明来源=目录（无手工声明分歧面）、存量池徽标经 evidence
  键自愈、五个 mutator/开关重开/undo/409 的按钮态走查、attrOps 纯加法、
  `_make_registry` 后向兼容、PR-14 零触碰、版本/文档一致。
