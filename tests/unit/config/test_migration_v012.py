"""unit/config/test_migration_v012.py — request_log 老库补列 cost/metadata 迁移测试。

回归背景（#80/#81，cross-review F2 补测试）：v4.x 老库的 request_log 表本无
cost/metadata 列，v5.0 迁移框架的 v002（CREATE TABLE IF NOT EXISTS）对老库是
no-op，此后 v006/v008/v010 均只加各自新列——老库升级后始终没有这两列的 ALTER，
insert_request_log 每次 flush 绑定 :metadata 即触发 OperationalError，
整批遥测丢失。v012 幂等补齐。
"""
import sqlite3

from inferfabric.db import IFFDB, REQUEST_LOG_DB


def _migrate(tmp_path):
    db = IFFDB(tmp_path)
    import inferfabric.migrations  # noqa: F401 — 注册迁移
    db._run_migrations()
    return db


def _old_style_db(tmp_path, user_version, extra_cols=()):
    """复刻 v4.x 老库：request_log 无 cost/metadata，含一行历史数据。

    user_version >= 6/8/10 时带上 v006/v008/v010 已加的列（生产老库真实状态：
    这些 ALTER 已应用、唯独缺 cost/metadata）。extra_cols 用于半迁移态模拟。
    """
    p = tmp_path / "request_log.db"
    conn = sqlite3.connect(p)
    cols = ""
    if user_version >= 6:
        cols += ",\n            tokens_in_cached INTEGER NOT NULL DEFAULT 0"
    if user_version >= 8:
        cols += (",\n            agent TEXT NOT NULL DEFAULT '',"
                 "\n            ua TEXT NOT NULL DEFAULT ''")
    if user_version >= 10:
        cols += ",\n            x_app TEXT NOT NULL DEFAULT ''"
    for c in extra_cols:
        if c == "cost":
            cols += ",\n            cost TEXT DEFAULT 'local'"
        elif c == "metadata":
            cols += ",\n            metadata TEXT DEFAULT '{}'"
    conn.execute(f"""CREATE TABLE request_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, req_id TEXT NOT NULL UNIQUE,
        key_name TEXT NOT NULL DEFAULT '', model TEXT NOT NULL,
        status INTEGER NOT NULL, ttft_ms REAL, tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0, duration_ms REAL NOT NULL DEFAULT 0.0,
        route TEXT NOT NULL DEFAULT 'local', cloud_provider TEXT, error TEXT,
        timestamp REAL NOT NULL, ts TEXT NOT NULL DEFAULT ''{cols})""")
    conn.execute(f"PRAGMA user_version = {user_version}")
    conn.execute("INSERT INTO request_log (req_id, model, status, timestamp) "
                 "VALUES ('r1', 'm', 200, 1.0)")
    conn.commit()
    conn.close()


def _cols(db):
    with db.connect(REQUEST_LOG_DB) as conn:
        return [r[1] for r in conn.execute("PRAGMA table_info(request_log)").fetchall()]


def _uv(db):
    with db.connect(REQUEST_LOG_DB) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def test_v012_backfills_pre_framework_old_db(tmp_path):
    """老库（前框架时代表，user_version=0）→ 全链升级补齐两列、历史行回填默认值。"""
    _old_style_db(tmp_path, 0)
    db = _migrate(tmp_path)
    cols = _cols(db)
    assert "cost" in cols and "metadata" in cols
    with db.connect(REQUEST_LOG_DB) as conn:
        row = conn.execute(
            "SELECT cost, metadata FROM request_log WHERE req_id='r1'").fetchone()
    assert row == ("local", "{}")
    assert _uv(db) == 12
    db.close()


def test_v012_fixes_uv11_production_state(tmp_path):
    """生产故障态（uv=11、缺两列）→ v012 补齐，且 flush 路径 INSERT 成功（#80 回归点）。"""
    _old_style_db(tmp_path, 11)
    db = _migrate(tmp_path)
    cols = _cols(db)
    assert "cost" in cols and "metadata" in cols
    # 复现 #80 的 flush 失败路径：insert_request_log 绑定 :metadata
    db.insert_request_log([{"req_id": "r2", "model": "m", "status": 200,
                            "timestamp": 2.0}])
    with db.connect(REQUEST_LOG_DB) as conn:
        row = conn.execute(
            "SELECT metadata FROM request_log WHERE req_id='r2'").fetchone()
    assert row[0] == "{}"
    assert _uv(db) == 12
    db.close()


def test_v012_skips_on_fresh_db(tmp_path):
    """全新库：v002 建表已含两列，v012 的 PRAGMA 守卫应跳过（无 duplicate column）。"""
    db = _migrate(tmp_path)
    assert "cost" in _cols(db) and "metadata" in _cols(db)
    db.insert_request_log([{"req_id": "r1", "model": "m", "status": 200,
                            "timestamp": 1.0}])
    assert _uv(db) == 12
    db.close()


def test_v012_idempotent_double_run(tmp_path):
    """幂等：迁移双跑（框架层 + upgrade 本体直跑）不报错、不重复加列。"""
    _old_style_db(tmp_path, 11)
    db = _migrate(tmp_path)
    db._run_migrations()  # 第二跑：uv=12，无迁移可执行
    import inferfabric.migrations.v012_add_cost_metadata as v012
    with db.connect(REQUEST_LOG_DB) as conn:
        v012.upgrade(conn)  # upgrade 本体直跑第二遍
    assert "cost" in _cols(db) and "metadata" in _cols(db)
    db.close()


def test_v012_self_heals_half_migrated(tmp_path):
    """半迁移态（有 cost 无 metadata、uv=11）→ v012 只补 metadata，自愈到 12。"""
    _old_style_db(tmp_path, 11, extra_cols=("cost",))
    db = _migrate(tmp_path)
    cols = _cols(db)
    assert "cost" in cols and "metadata" in cols
    assert _uv(db) == 12
    db.close()
