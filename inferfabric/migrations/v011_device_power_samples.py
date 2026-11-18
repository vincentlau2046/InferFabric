"""Migration 011: device_power_samples — 平台中立设备功耗时间序列 (DeviceTelemetry T4).

替换 v007 的 gpu_power_samples（NVIDIA 专用 (ts, watts)）为
device_power_samples(ts, platform, accelerator, device_id, watts, source)，
主键 (ts, accelerator, device_id)。

历史行一次性拷贝为 platform=accelerator=device_id='unknown'、
source='legacy_gpu_power_samples' —— 不把旧数据归于虚构的 GPU/设备。
新采样写入 device_id='aggregate'（每拍一个全卡聚合样本）。

同事务创建只读兼容视图 gpu_power_samples(ts, watts)：历史行 + NVIDIA
聚合样本；同一 ts 冲突时优先 legacy（老口径为准）。全部写入走新表。
"""


def upgrade(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Review fix: decide inside the write transaction — a concurrent
        # process may commit v011 between our check and our lock; re-checking
        # here (type='table') makes the copy/drop race-free and idempotent.
        legacy_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='gpu_power_samples'").fetchone() is not None

        conn.execute("""
            CREATE TABLE IF NOT EXISTS device_power_samples (
                ts INTEGER NOT NULL,
                platform TEXT NOT NULL,
                accelerator TEXT NOT NULL,
                device_id TEXT NOT NULL,
                watts REAL NOT NULL CHECK (watts >= 0),
                source TEXT NOT NULL,
                PRIMARY KEY (ts, accelerator, device_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_device_power_ts "
            "ON device_power_samples(ts)")

        if legacy_exists:
            conn.execute("""
                INSERT OR IGNORE INTO device_power_samples
                    (ts, platform, accelerator, device_id, watts, source)
                SELECT ts, 'unknown', 'unknown', 'unknown', watts,
                       'legacy_gpu_power_samples'
                FROM gpu_power_samples
            """)
            conn.execute("DROP TABLE gpu_power_samples")

        # Read-only compat view (historical + aggregate; legacy wins on a
        # same-ts collision). Writes are redirected to the new table.
        conn.execute("""
            CREATE VIEW IF NOT EXISTS gpu_power_samples (ts, watts) AS
            SELECT d.ts, d.watts
            FROM device_power_samples d
            WHERE d.device_id = 'unknown'
               OR (d.device_id = 'aggregate' AND NOT EXISTS (
                    SELECT 1 FROM device_power_samples l
                    WHERE l.ts = d.ts AND l.device_id = 'unknown'))
        """)

        conn.execute("PRAGMA user_version = 11")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
