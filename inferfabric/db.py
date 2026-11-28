"""
inferfabric/db.py — Unified database abstraction.

Physical DB isolation preserved (state.db vs request_log.db),
logical API unified. Migration engine via PRAGMA user_version.

Connection strategy:
  - Per-thread connection via threading.local (ConnectionPool)
  - WAL journal mode for both DBs
  - Write lock serializes concurrent writers (avoids "database is locked")
  - busy_timeout=5000 as safety net
  - Automatic reconnect on OperationalError (stale connection)
"""

from __future__ import annotations
import json
import sqlite3
import threading
import logging
from pathlib import Path
from typing import Any
from contextlib import contextmanager

log = logging.getLogger("inferfabric.db")

STATE_DB = "state"
REQUEST_LOG_DB = "request_log"

_DB_PATHS: dict[str, Path] = {}


def set_db_paths(data_dir: Path):
    _DB_PATHS[STATE_DB] = data_dir / "state.db"
    _DB_PATHS[REQUEST_LOG_DB] = data_dir / "request_log.db"


class ConnectionPool:
    """Per-thread SQLite connection pool."""

    def __init__(self, db_path: Path, pragma_setup: list[str] | None = None):
        self._db_path = db_path
        self._local = threading.local()
        self._pragma_setup = pragma_setup or ["PRAGMA journal_mode=WAL"]

    def get(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except sqlite3.OperationalError:
                log.debug("Stale connection to %s, reconnecting", self._db_path)
                try:
                    conn.close()
                except Exception as e:
                    log.warning("DB init failed: %s", e)
                self._local.conn = None
        conn = sqlite3.connect(str(self._db_path), timeout=10, check_same_thread=True)
        for pragma in self._pragma_setup:
            conn.execute(pragma)
        self._local.conn = conn
        return conn

    def close_all(self):
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as e:
                log.warning("DB connection failed: %s", e)
            try:
                conn.close()
            except Exception as e:
                log.warning("DB operation failed: %s", e)
            self._local.conn = None

    @property
    def db_path(self) -> Path:
        return self._db_path


class IFFDB:
    """IFF database facade: typed API for state + request_log."""

    _MIGRATIONS: list[tuple[int, str, str]] = []

    def __init__(self, data_dir: Path | None = None):
        if data_dir is not None:
            set_db_paths(data_dir)
        self._pools: dict[str, ConnectionPool] = {}
        self._write_lock = threading.Lock()
        self._init_pools()
        import inferfabric.migrations  # noqa: F401 — ensure migration modules are loaded
        self._run_migrations()

    def _init_pools(self):
        self._pools[STATE_DB] = ConnectionPool(
            _DB_PATHS.get(STATE_DB, Path.home() / ".inferfabric" / "state.db"),
            pragma_setup=[
                "PRAGMA journal_mode=WAL",
                "PRAGMA busy_timeout=5000",
            ],
        )
        self._pools[REQUEST_LOG_DB] = ConnectionPool(
            _DB_PATHS.get(REQUEST_LOG_DB, Path.home() / ".inferfabric" / "request_log.db"),
            pragma_setup=[
                "PRAGMA journal_mode=WAL",
                "PRAGMA synchronous=NORMAL",
                "PRAGMA auto_vacuum=INCREMENTAL",
                "PRAGMA wal_autocheckpoint=1000",
                "PRAGMA busy_timeout=5000",
            ],
        )

    @contextmanager
    def connect(self, db_name: str) -> sqlite3.Connection:
        pool = self._pools.get(db_name)
        if not pool:
            raise KeyError(f"Unknown DB: {db_name}")
        yield pool.get()

    def close(self):
        for pool in self._pools.values():
            pool.close_all()

    @classmethod
    def register_migration(cls, version: int, db_name: str, description: str):
        cls._MIGRATIONS.append((version, db_name, description))
        cls._MIGRATIONS.sort(key=lambda x: x[0])

    def _run_migrations(self):
        grouped: dict[str, list[tuple[int, str]]] = {}
        for version, db_name, desc in self._MIGRATIONS:
            grouped.setdefault(db_name, []).append((version, desc))
        for db_name, migrations in grouped.items():
            for version, desc in migrations:
                with self.connect(db_name) as conn:
                    current = conn.execute("PRAGMA user_version").fetchone()[0] or 0
                    if current < version:
                        self._apply_migration(conn, version, desc, db_name)
                        conn.execute(f"PRAGMA user_version = {version}")
                        log.info("Migration %03d '%s' applied to %s", version, desc, db_name)

    def _apply_migration(self, conn: sqlite3.Connection,
                         version: int, description: str, db_name: str):
        from importlib import import_module
        safe_name = description.replace(" ", "_").replace("-", "_")
        mod = import_module(f"inferfabric.migrations.v{version:03d}_{safe_name}")
        mod.upgrade(conn)

    # ── State: low-level ───────────────────────────────────────

    def _get_raw(self, key: str) -> str | None:
        with self.connect(STATE_DB) as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def _set_raw(self, key: str, value: str):
        with self._write_lock:
            with self.connect(STATE_DB) as conn:
                conn.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))
                conn.commit()

    def set_multi(self, kv: dict[str, str]):
        """Atomically set multiple state keys in a single transaction."""
        if not kv:
            return
        with self._write_lock:
            with self.connect(STATE_DB) as conn:
                conn.execute("BEGIN IMMEDIATE")
                for k, v in kv.items():
                    conn.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (k, v))
                conn.commit()

    # ── State: typed API ───────────────────────────────────────

    def get_gpu_mode(self) -> str:
        r = self._get_raw("gpu_mode")
        return r if r else "idle"

    def set_gpu_mode(self, mode: str):
        self._set_raw("gpu_mode", mode)

    def get_active_services(self) -> list[str]:
        r = self._get_raw("active_services")
        if not r:
            return []
        return json.loads(r)

    def set_active_services(self, services: list[str]):
        self._set_raw("active_services", json.dumps(services))

    def add_active_service(self, name: str):
        with self._write_lock:
            with self.connect(STATE_DB) as conn:
                svc = self.get_active_services()
                if name not in svc:
                    svc.append(name)
                    conn.execute(
                        "INSERT OR REPLACE INTO state VALUES (?, ?)",
                        ("active_services", json.dumps(svc)),
                    )
                    conn.commit()

    def remove_active_service(self, name: str):
        with self._write_lock:
            with self.connect(STATE_DB) as conn:
                svc = self.get_active_services()
                if name in svc:
                    svc.remove(name)
                    conn.execute(
                        "INSERT OR REPLACE INTO state VALUES (?, ?)",
                        ("active_services", json.dumps(svc)),
                    )
                    conn.commit()

    # ── State: sleep state (v5.2: table-backed) ───────────────

    def get_sleep_state(self, model: str) -> str | None:
        with self.connect(STATE_DB) as conn:
            row = conn.execute(
                "SELECT level FROM sleep_state WHERE model=?", (model,)
            ).fetchone()
            return row[0] if row else None

    def set_sleep_state(self, model: str, level: int | None):
        with self._write_lock:
            with self.connect(STATE_DB) as conn:
                if level is None:
                    conn.execute("DELETE FROM sleep_state WHERE model=?", (model,))
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO sleep_state (model, level) VALUES (?, ?)",
                        (model, f"l{level}"),
                    )
                conn.commit()

    def get_all_sleep_states(self) -> dict[str, str]:
        with self.connect(STATE_DB) as conn:
            rows = conn.execute("SELECT model, level FROM sleep_state").fetchall()
            return {r[0]: r[1] for r in rows}

    def get_current_profile(self) -> str:
        r = self._get_raw("current_profile")
        return r if r else ""

    def set_current_profile(self, profile: str):
        self._set_raw("current_profile", profile)

    def get_switch_target(self) -> str:
        r = self._get_raw("switch_target")
        return r if r else ""

    def set_switch_target(self, target: str):
        self._set_raw("switch_target", target)

    def get_vllm_pid(self) -> int | None:
        r = self._get_raw("vllm_pid")
        return int(r) if r else None

    def set_vllm_pid(self, pid: int | None):
        self._set_raw("vllm_pid", str(pid) if pid is not None else "")

    def get_comfyui_pid(self) -> int | None:
        r = self._get_raw("comfyui_pid")
        return int(r) if r else None

    def set_comfyui_pid(self, pid: int | None):
        self._set_raw("comfyui_pid", str(pid) if pid is not None else "")

    # ── State: history ─────────────────────────────────────────

    def add_switch_history(self, from_profile: str, to_profile: str,
                           duration: float, status: str = "ok"):
        with self._write_lock:
            with self.connect(STATE_DB) as conn:
                conn.execute(
                    "INSERT INTO history (from_profile, to_profile, duration, status) "
                    "VALUES (?, ?, ?, ?)",
                    (from_profile, to_profile, duration, status),
                )
                conn.execute(
                    "DELETE FROM history WHERE id NOT IN "
                    "(SELECT id FROM history ORDER BY id DESC LIMIT 100)"
                )
                conn.commit()

    def get_switch_history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect(STATE_DB) as conn:
            rows = conn.execute(
                "SELECT timestamp, from_profile, to_profile, duration, status "
                "FROM history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {"timestamp": r[0], "from": r[1], "to": r[2],
                 "duration": r[3], "status": r[4]}
                for r in rows
            ]

    # ── State: manual stops (v5.2: table-backed) ──────────────

    MANUAL_STOP_TTL: float = 60.0  # 60s — auto-switch unblock cooldown (align with StateDB)

    def record_manual_stop(self, name: str):
        import time
        with self._write_lock:
            with self.connect(STATE_DB) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO manual_stops (model, stop_ts) VALUES (?, ?)",
                    (name, time.time()),
                )
                conn.commit()

    def is_manually_stopped(self, name: str) -> bool:
        import time
        now = time.time()
        with self.connect(STATE_DB) as conn:
            row = conn.execute(
                "SELECT stop_ts FROM manual_stops WHERE model=? AND stop_ts > ?",
                (name, now - self.MANUAL_STOP_TTL),
            ).fetchone()
            return row is not None

    def clear_manual_stop(self, name: str):
        with self._write_lock:
            with self.connect(STATE_DB) as conn:
                conn.execute("DELETE FROM manual_stops WHERE model=?", (name,))
                conn.commit()

    def get_all_manual_stops(self) -> dict[str, float]:
        with self.connect(STATE_DB) as conn:
            rows = conn.execute("SELECT model, stop_ts FROM manual_stops").fetchall()
            return {r[0]: r[1] for r in rows}

    # ── Request Log API ───────────────────────────────────────

    def insert_request_log(self, entries: list[dict]):
        if not entries:
            return
        # 旧调用方（v6.5 前构造的 entry dict / 测试 fixture）可能缺键 →
        # 全列缺省归一（mac prod 上游化，2026-11-22）：缺 ttft_ms 曾撞
        # sqlite3.ProgrammingError（INSERT 绑定 :ttft_ms 而条目无该键）。
        # route 防御性兜底：显式 None 曾撞 NOT NULL 约束被 INSERT OR
        # IGNORE 静默丢弃（失败日志丢失根因之一）— 兜底 'local'。
        # metadata：缺省 '{}'；dict 序列化为 JSON 文本（schema v002 本有
        # 该列，此前 INSERT 从不写入 → 池归属 pool_summary 丢失）。
        import json as _json
        entries = [dict(e, tokens_in_cached=e.get("tokens_in_cached", 0),
                        agent=e.get("agent", ""), ua=e.get("ua", ""),
                        x_app=e.get("x_app", ""),
                        key_name=e.get("key_name", ""),
                        ttft_ms=e.get("ttft_ms"),
                        tokens_in=e.get("tokens_in", 0),
                        tokens_out=e.get("tokens_out", 0),
                        duration_ms=e.get("duration_ms", 0.0),
                        cloud_provider=e.get("cloud_provider"),
                        error=e.get("error"), ts=e.get("ts", ""),
                        route=e.get("route") or "local",
                        metadata=_json.dumps(e.get("metadata") or {},
                                             ensure_ascii=False)
                        if not isinstance(e.get("metadata"), str)
                        else e["metadata"]) for e in entries]
        with self._write_lock:
            with self.connect(REQUEST_LOG_DB) as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO request_log "
                    "(req_id, key_name, model, status, ttft_ms, tokens_in, "
                    " tokens_in_cached, tokens_out, agent, ua, x_app, "
                    " duration_ms, route, cloud_provider, error, timestamp, ts, "
                    " metadata) "
                    "VALUES (:req_id, :key_name, :model, :status, :ttft_ms, :tokens_in, "
                    ":tokens_in_cached, :tokens_out, :agent, :ua, :x_app, "
                    ":duration_ms, :route, :cloud_provider, :error, "
                    ":timestamp, :ts, :metadata)",
                    entries,
                )
                conn.commit()

    def query_request_log(self, since: float, until: float | None = None,
                          model: str | None = None, limit: int = 10000,
                          before_ts: float | None = None,
                          before_id: int | None = None) -> list[dict]:
        """键集查询 [since, until) 的请求日志行（timestamp,id 降序）。

        mac prod 上游化（2026-11-22）：
        - 下界含 since（[since, until) 契约，prod 终审 I1）；既有调用方
          语义影响仅为边界行（时间戳恰等于 since）从排除改为纳入。
        - before_ts+before_id：monitor 报告的 (timestamp, id) 真键集分页
          游标 —— 返回 timestamp < 游标，或 timestamp 相等而 id 更小的行
          （id 单调唯一 → 页间无重叠，海量同秒行不漏不计重）。
        - metadata JSON 文本 → dict 回读（写入侧见 insert_request_log）；
          损坏/非 JSON 保持原值由消费方按 unknown 处理，不抛异常。
        """
        with self.connect(REQUEST_LOG_DB) as conn:
            cols = [d[0] for d in conn.execute("SELECT * FROM request_log LIMIT 0").description]
            sql = "SELECT * FROM request_log WHERE timestamp >= ?"
            params: list[Any] = [since]
            if until is not None:
                sql += " AND timestamp < ?"
                params.append(until)
            if model is not None:
                sql += " AND model = ?"
                params.append(model)
            if before_ts is not None and before_id is not None:
                sql += " AND (timestamp < ? OR (timestamp = ? AND id < ?))"
                params.extend([before_ts, before_ts, before_id])
            sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            import json as _json
            out = []
            for r in rows:
                d = dict(zip(cols, r))
                m = d.get("metadata")
                if isinstance(m, str):
                    try:
                        d["metadata"] = _json.loads(m) if m else {}
                    except Exception:
                        pass
                out.append(d)
            return out

    # 状态类 → 整数区间（B1 window 模式）；未知类 ValueError，handler 先行 400。
    _STATUS_CLASS_RANGES = {"2xx": (200, 299), "4xx": (400, 499),
                            "5xx": (500, 599)}

    def query_request_log_window(self, since: float, until: float, *,
                                 model: str | None = None,
                                 agent: str | None = None,
                                 status_class: str | None = None,
                                 q: str | None = None,
                                 limit: int = 50,
                                 offset: int = 0) -> dict:
        """window 模式只读查询（dashboard-ux-v3 B1 独立读方法）。

        [since, until) 半开窗；过滤先于 LIMIT；页与 total/failed_total 在
        **同一读事务**内取（单快照，WAL 下不阻塞写入）。同秒排序
        (timestamp DESC, id DESC)，offset 翻页。q 对 req_id/model/agent
        做字面子串匹配（instr，大小写敏感；LIKE 通配字符不具通配语义）。
        既有 query_request_log 不变，继续服务 snapshot/monitor-report。
        返回 {"logs": [行 dict], "total": int, "failed_total": int}。
        """
        if status_class is not None \
                and status_class not in self._STATUS_CLASS_RANGES:
            raise ValueError(f"unknown status_class: {status_class!r}")
        conds = ["timestamp >= ?", "timestamp < ?"]
        params: list[Any] = [since, until]
        if model is not None:
            conds.append("model = ?")
            params.append(model)
        if agent is not None:
            conds.append("agent = ?")
            params.append(agent)
        if status_class is not None:
            lo, hi = self._STATUS_CLASS_RANGES[status_class]
            conds.append("status BETWEEN ? AND ?")
            params.extend([lo, hi])
        if q:
            conds.append("(instr(req_id, ?) > 0 OR instr(model, ?) > 0 "
                         "OR instr(agent, ?) > 0)")
            params.extend([q, q, q])
        where = " AND ".join(conds)
        count_sql = (
            "SELECT COUNT(*), SUM(CASE WHEN status BETWEEN 400 AND 599 "
            f"THEN 1 ELSE 0 END) FROM request_log WHERE {where}")
        page_sql = (f"SELECT * FROM request_log WHERE {where} "
                    "ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?")
        with self.connect(REQUEST_LOG_DB) as conn:
            in_txn = conn.in_transaction
            if not in_txn:
                conn.execute("BEGIN")   # 单读事务：页与计数同一快照
            try:
                total, failed_total = conn.execute(count_sql, params).fetchone()
                cur = conn.execute(page_sql, params + [limit, offset])
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
            finally:
                if not in_txn:
                    conn.execute("COMMIT")
        import json as _json
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            m = d.get("metadata")
            if isinstance(m, str):
                try:
                    d["metadata"] = _json.loads(m) if m else {}
                except Exception:
                    pass
            out.append(d)
        return {"logs": out, "total": int(total or 0),
                "failed_total": int(failed_total or 0)}

    def reclassify_request_log(self, classify_fn, known_ids: set | None = None) -> int:
        """对无真实 def 的行（unknown + observed 残留如 smoke-tmp）用 classify_fn
        重新分类。known_ids = registry 里所有真实 def 的 id 集合；不在集合内且
        非 historical 的行都重新 classify。认领后调用——映射层变了，事实层不变。
        """
        with self._write_lock:
            with self.connect(REQUEST_LOG_DB) as conn:
                # 取所有有 ua、非 historical、非已知 def 的行
                if known_ids:
                    placeholders = ",".join("?" * len(known_ids))
                    rows = conn.execute(
                        f"SELECT id, ua, x_app, agent FROM request_log "
                        f"WHERE ua!='' AND agent!='historical' "
                        f"AND agent NOT IN ({placeholders})",
                        list(known_ids)).fetchall()
                else:
                    # 无 known_ids 时回退：只处理 unknown（向后兼容）
                    rows = conn.execute(
                        "SELECT id, ua, x_app, agent FROM request_log "
                        "WHERE agent='unknown' AND ua!=''").fetchall()
                updated = 0
                for row_id, ua, x_app, old_agent in rows:
                    headers = {"user-agent": ua or ""}
                    if x_app:
                        headers["x-app"] = x_app
                    new_agent = classify_fn("openai", headers)
                    agent_id = getattr(new_agent, "agent", new_agent)
                    # 仅当新分类与当前不同（且非 unknown，除非原来就是 unknown）才更新
                    if agent_id and agent_id != old_agent:
                        conn.execute(
                            "UPDATE request_log SET agent=? WHERE id=?",
                            (agent_id, row_id))
                        updated += 1
                conn.commit()
                return updated

    def prune_request_log(self, before: float) -> int:
        with self._write_lock:
            with self.connect(REQUEST_LOG_DB) as conn:
                cur = conn.execute("DELETE FROM request_log WHERE timestamp < ?", (before,))
                conn.commit()
                deleted = cur.rowcount
                if deleted > 0:
                    try:
                        conn.execute("PRAGMA auto_vacuum")
                    except Exception as e:
                        log.warning("DB query failed: %s", e)
                return deleted

    # ── 设备功耗时间序列（v011 device_power_samples，平台中立）──

    def insert_device_power_samples(self, samples: list[dict]):
        """落盘设备功耗采样（每拍一个聚合样本）。

        主键 (ts, accelerator, device_id) → 同秒 INSERT OR IGNORE 幂等去重。
        """
        if not samples:
            return
        rows = [{
            "ts": int(s["ts"]),
            "platform": str(s.get("platform", "unknown")),
            "accelerator": str(s.get("accelerator", "unknown")),
            "device_id": str(s.get("device_id", "aggregate")),
            "watts": float(s["watts"]),
            "source": str(s.get("source", "device_telemetry")),
        } for s in samples]
        with self._write_lock:
            with self.connect(REQUEST_LOG_DB) as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO device_power_samples "
                    "(ts, platform, accelerator, device_id, watts, source) "
                    "VALUES (:ts, :platform, :accelerator, :device_id, "
                    ":watts, :source)",
                    rows,
                )
                conn.commit()

    def query_device_power_samples(self, since: float, until: float | None = None,
                                   limit: int = 300000,
                                   device_id: str | None = None) -> list[dict]:
        """按 ts 升序返回全部元数据列；[since, until) 半开区间。

        device_id=None 返回所有流（raw 元数据查询）；分桶序列按
        device_id 过滤，保证 legacy 与 aggregate 流分开积分。
        """
        with self.connect(REQUEST_LOG_DB) as conn:
            sql = ("SELECT ts, platform, accelerator, device_id, watts, source "
                   "FROM device_power_samples WHERE ts >= ?")
            params: list[Any] = [since]
            if until is not None:
                sql += " AND ts < ?"
                params.append(until)
            if device_id is not None:
                sql += " AND device_id = ?"
                params.append(device_id)
            sql += " ORDER BY ts ASC LIMIT ?"
            params.append(limit)
            cols = ["ts", "platform", "accelerator", "device_id", "watts", "source"]
            rows = conn.execute(sql, params).fetchall()
            return [dict(zip(cols, r)) for r in rows]

    def prune_device_power_samples(self, before: float) -> int:
        with self._write_lock:
            with self.connect(REQUEST_LOG_DB) as conn:
                cur = conn.execute("DELETE FROM device_power_samples WHERE ts < ?",
                                   (before,))
                conn.commit()
                return cur.rowcount

    # ── GPU 功耗时间序列（v6.2 → v011 兼容层）──

    def insert_gpu_power_samples(self, samples: list[dict]):
        """DEPRECATED shim — legacy 写入重定向到 device_power_samples
        （unknown/unknown/legacy 标记），不写兼容视图（视图只读）。"""
        self.insert_device_power_samples([{
            "ts": s["ts"], "platform": "unknown", "accelerator": "unknown",
            "device_id": "unknown", "watts": s["watts"],
            "source": "legacy_gpu_power_samples",
        } for s in samples])

    def query_gpu_power_samples(self, since: float, until: float | None = None,
                                limit: int = 300000) -> list[dict]:
        """按 ts 升序返回 [{ts, watts}]；读兼容视图（历史 + 非冲突聚合）。"""
        with self.connect(REQUEST_LOG_DB) as conn:
            sql = "SELECT ts, watts FROM gpu_power_samples WHERE ts >= ?"
            params: list[Any] = [since]
            if until is not None:
                sql += " AND ts < ?"
                params.append(until)
            sql += " ORDER BY ts ASC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [{"ts": r[0], "watts": r[1]} for r in rows]

    def prune_gpu_power_samples(self, before: float) -> int:
        """DEPRECATED shim — 重定向到新表清理（无法 DELETE 视图）。"""
        return self.prune_device_power_samples(before)

    def wal_checkpoint(self):
        with self.connect(REQUEST_LOG_DB) as conn:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as e:
                log.warning("DB write failed: %s", e)

    # ── Legacy backward compat ─────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        r = self._get_raw(key)
        return r if r is not None else default

    def set(self, key: str, value: str):
        self._set_raw(key, value)
