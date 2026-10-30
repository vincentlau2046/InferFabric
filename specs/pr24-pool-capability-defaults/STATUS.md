# PR-24 模型池能力默认语义反转 — STATUS

| 项 | 状态 | 备注 |
|---|---|---|
| 诊断 | ✅ 2026-10-31 | 生产 dry-run 复现 4×feature_unsupported；根因与用户裁决见 spec.md |
| 后端实现 | ✅ commit 94992f7 | selector 反转 + detail / dispatch 自解释 400 / catalog 三态推导 / cloud_discovery 三态标志 / admin 透传 / config 注释 |
| 前端实现 | ✅ commit f08a111 | 编辑器能力列（草稿复选框）+ 卡片 chips/全能力徽章 + 树叶 title + CSS |
| 浏览器门禁 | ✅ commit 2b9c7fa | test_08 端到端（约束 → 自解释 400 → 解除 → 200 直通）；10/10 passed |
| 顺带修复 | ✅ commit 2b9c7fa | `_forward_request` 池路径 `settimeout` 必败 bug（本地池候选 unreachable）→ make_conn 构造参数；单测 RED→GREEN |
| TDD | ✅ | 单测逐条先观察到 RED；test_08 RED×2（declared 序断言修正 + 503 揭出 settimeout bug）后 GREEN |
| 全量回归 | ✅ | 1914 passed（unit+integration；基线 1903 + 新增 11） |
| import 冒烟 | ✅ | `python3 -c "import inferfabric"` → 7.8.0 |
| 干净 worktree 复验 | ✅ | 提交树 440f31a：全量 1914 passed + 浏览器门禁 10/10（沙箱工作区绿 ≠ 提交树绿，故独立复验） |
| cross-review | ⏳ | fresh-context subagent 评审中 |
| 版本 | ✅ | 7.8.0（minor，用户随方案 B 批准）；README 历史表已加行 |
| 合并/部署 | ⏳ | ff-merge 生产 main + BatchMode push；**重启需用户单独授权**；重启后 dry-run 验证 `iff/premium` tools=true → chosen |

## 验证记录（截至 commit 2b9c7fa）

- 浏览器门禁 10/10（test_01–07 + test_08；console 零 error）。
- 转发邻近单测 14 passed（nonstream_no_ttft / r5_cache_parity /
  forward_request_timeout）。
- RED 实录：
  - test_08 首跑：`declared=[stream, structured, vision]` vs 草稿序断言
    （后端排序稳定，测试期望错误 → 修正断言）。
  - test_08 二跑：末段 `assert code == 200` 失败于 503 pool_unavailable →
    proxy.log 实锤 `'HTTPConnection' object has no attribute 'settimeout'`
    → 修复 + 单测钉契约。
- 单测 RED→GREEN 明细见各 commit 描述（94992f7 / f08a111）。
