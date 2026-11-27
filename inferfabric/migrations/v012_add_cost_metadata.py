"""Migration 012: request_log 增列 cost/metadata — 补齐老库缺失的迁移。

背景：v002 的 CREATE TABLE 事后补入了 cost/metadata 两列（新库直接建出），
但已初始化的老库从未获得对应 ALTER——v006/v008/v010 均只加各自新列。
老库环境下 insert_request_log 每次 flush 绑定 :metadata 即触发
sqlite3.OperationalError: table request_log has no column named metadata，
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
