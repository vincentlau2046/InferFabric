# STATUS — 别名/云路由缺陷修复一轮（#66–#73）

- 状态：已实施（2026-11-27，分支 fix/alias-round-66-73，基于 main f63ff96）；合并/重启等 PR #81 收口后进行
- 提交：#66 f23b409 / #67 adaa944 / #68 7c46876 / #69 cc40e9d / #70 5c835f9 / #71 5d8c75d / #72 3427b86（+fake 补齐）/ #73 e732ed5，每 issue 独立 RED→GREEN
- 验收：Mac 全量 2961 passed（基线 2937 + 新增 24 例）；vince-pc 全套见 PR 回执
- 分支计划：`fix/alias-round-66-73`（基于合并后 main）
- 验收口径：双平台全套 pytest + §4.3 清理
- 裁决记录：
  - #69 校验失败走 corrupt 全有或全无（issue 允许 corrupt 或丢弃+warning）— 与坏条目既有行为一致 — 代价：单条坏别名拖整份配置不应用（可接受，安全侧）
  - #70 实现「写侧持 _models_lock（RLock）+ 读侧快照」双保险（issue 允许锁或整体重绑二选一）— 代价：读侧多点 list()/dict() — 换确定性
  - #67 退役名先于 local 检查 — README 删除承诺字面语义 — 代价：本地模型新建时若与退役别名同名需先清理 retired（显式操作，可接受）
