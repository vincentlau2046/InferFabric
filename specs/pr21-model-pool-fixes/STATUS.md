# PR-21 模型池双修复 — STATUS

| 项 | 状态 | 备注 |
|---|---|---|
| 诊断 | ✅ 2026-10-28 | 两 bug 根因链见 spec.md；ninfer 双协议以 7 天线上日志实证 |
| 实现 | ✅ 2026-10-28 | catalog 映射 + registry `local_protocols` 自愈回调 + proxy_manager 接线；JS 外科点亮 + `mpEditPoolParam` 重烘 |
| TDD | ✅ | 4 单测 + 1 VM 测 + 浏览器 test_05c 全部先观察到 RED（预期原因），后 GREEN |
| 全量回归 | ✅ | 1807 passed（unit+integration，60.22s） |
| 浏览器门禁 | ✅ | 9/9 passed（含新增 test_05c；console 零 error） |
| import 冒烟 | ✅ | `python3 -c "import inferfabric"` → 7.6.0（版本随 bump 提交更新为 7.6.1） |
| 干净 worktree 复验 | ✅ | 提交树全量 1807 passed（沙箱工作区绿 ≠ 提交树绿） |
| cross-review | ⏳ | fresh-context subagent 评审分支 diff — 评审后回填结论 |
| 版本 | ✅ | 7.6.1（补丁位自主升级，per version-bump-autonomy 记忆） |
| 合并/部署 | ⏳ | 待用户授权：合生产 + push + proxy 重启（后端生效需重启；JS 可 SIGHUP） |

## 验证记录

- RED（预期原因逐条核对）：
  - `test_catalog.py::test_local_protocols_by_engine` — `['openai'] != ['openai','anthropic']`
  - `test_registry.py` 自愈三测 — `TypeError: unexpected keyword argument 'local_protocols'`
  - 浏览器 `test_05c` — `anthropic → no_compatible_target`（bug #1 实锤）；
    修 #1 后复跑仍 RED 于应用钮 disabled（bug #2 实锤，随 JS 修复转 GREEN）
- GREEN：单测 catalog+registry 29/29；面板 VM 130/130；浏览器 9/9；
  全量 unit+integration **1807 passed**。
- 过程缺陷（测试自身，已修）：test_05c 首版等待条件用模型名匹配预览文本，
  实际预览显示候选 id — 永不满足；改为断言「openai/anthropic 均『选择』」。

## 评审记录

（待回填）fresh-context subagent 评审 `a54cfb4..HEAD` 分支 diff。
