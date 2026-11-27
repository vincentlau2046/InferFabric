"""Migration 012: request_log 增列 cost/metadata — 补齐老库缺失的迁移。

背景（2026-11-27 cross-review 勘误根因表述）：request_log 自 v4.x（6ece7a8
时代）由旧 request_log_db.py 建表，本无 cost/metadata 两列；v5.0（65f564c）
引入迁移框架时 v002 建表即已含两列（该文件全历史仅此一个 commit，并非
"事后补列"），v002 的 CREATE TABLE IF NOT EXISTS 对老库是 no-op，此后
v006/v008/v010 均只加各自新列——v4.x 老库升级到 v5.0+ 后始终没有获得
这两列的 ALTER。老库环境下 insert_request_log 每次 flush 绑定 :metadata 即
触发 sqlite3.OperationalError: table request_log has no column named metadata，
整批遥测丢失并刷 ERROR 日志。

本迁移幂等补齐两列，默认值与 v002 建表保持一致：
  cost     TEXT DEFAULT 'local'
  metadata TEXT DEFAULT '{}'
（insert_request_log 当前仅写入 metadata；cost 为 v002 契约列，缺省 'local'。）
"""


def upgrade(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(request_log)").fetchall()]
    if "cost" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN cost TEXT DEFAULT 'local'")
    if "metadata" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN metadata TEXT DEFAULT '{}'")
    conn.commit()
