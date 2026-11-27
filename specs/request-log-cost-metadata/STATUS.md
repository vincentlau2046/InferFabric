# request-log-cost-metadata Status

| Phase | 状态 | 日期 | 备注 |
|-------|------|------|------|
| P7 Implement | ✅ | 2026-11-26 | v012_add_cost_metadata 迁移 + 注册 |
| 质量门 | ✅ | 2026-11-26 | 老库模拟复现→修复→幂等；定向 pytest 61 passed；`import inferfabric` 冒烟 ✓ |

## 背景

v002 的 CREATE TABLE 被事后补入 `cost`/`metadata` 两列（新库直接建出），但已初始化的老库
（2026-09 前后）从未获得对应 ALTER——v006/v008/v010 均只加各自新列。老库环境下
`insert_request_log` 每次 flush 绑定 `:metadata` 即触发
`sqlite3.OperationalError: table request_log has no column named metadata`，
整批遥测丢失并持续刷 ERROR 日志（mac/vince-pc 生产均复现）。

## 修复

- 新增 `inferfabric/migrations/v012_add_cost_metadata.py`：幂等补齐两列，
  默认值与 v002 一致（`cost TEXT DEFAULT 'local'`、`metadata TEXT DEFAULT '{}'`）
- `inferfabric/migrations/__init__.py` 注册 version 12（"request_log" 组）

## 验证

- 复刻线上老库 schema → 复现报错 → 应用迁移（两次，幂等）→ 原 INSERT 语句成功、默认值正确
- 老库 `user_version=11`，升级时自动应用 v012；新库 v002 已含两列，v012 PRAGMA 检查跳过（无副作用）
