# request-log-cost-metadata Status

| Phase | 状态 | 日期 | 备注 |
|-------|------|------|------|
| P7 Implement | ✅ | 2026-11-26 | v012_add_cost_metadata 迁移 + 注册 |
| 质量门 | ✅ | 2026-11-26 | 老库模拟复现→修复→幂等；定向 pytest 61 passed；`import inferfabric` 冒烟 ✓ |
| 收口（cross-review 跟进） | ✅ | 2026-11-27 | fresh-context cross-review 无阻塞（F1/F2 均 LOW）；`test_migration_v012.py` 回归 5 例补齐（F2）；根因表述勘误（F1）；PR #81 合入 main（c0ff8c1） |

## 背景

request_log 自 v4.x（6ece7a8 时代）由旧 `request_log_db.py` 建表，本无 `cost`/`metadata`
两列；v5.0（65f564c）引入迁移框架时 v002 建表**即已含两列**（该文件全历史仅此一个
commit，并非初版所述"事后补列"——2026-11-27 cross-review 勘误）。v002 的
`CREATE TABLE IF NOT EXISTS` 对已初始化的老库是 no-op，此后 v006/v008/v010 均只加各自
新列——v4.x 老库升级到 v5.0+ 后始终没有获得这两列的 ALTER。老库环境下
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
- cross-review（fresh context，2026-11-27）五类库状态实测：老库 uv=0 / uv=11 / 全新库 / 幂等双跑 / 半迁移态自愈，全部通过
- `tests/unit/config/test_migration_v012.py` 5 例沉淀上述场景为回归测试（uv=11 案例走真实 `insert_request_log` flush 路径断言）
