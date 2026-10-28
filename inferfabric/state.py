"""
inferfabric/state.py — State machine + SQLite state management (IFFDB-delegated).

v4.0: Added GPUMode (idle/exclusive/shared), validate_transition(),
      StateDB.get/set_active_services().
v5.0: StateDB delegates to IFFDB. Internal SQLite removed.
      Keeps same public API for backward compatibility.
"""

import functools
import threading
import json
import time
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("inferfabric")


def _sealed_noop(fn):
    """PR-20 ③：封印后写方法静默 no-op——垂死进程不得再写状态。"""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._seal_lock:
            if self._sealed:
                return None
            return fn(self, *args, **kwargs)
    return wrapper


# ─── GPU Mode State Machine ──────────────────────────────────────

class GPUMode:
    """Valid GPU mode states."""
    IDLE = "idle"
    EXCLUSIVE = "exclusive"
    SHARED = "shared"

    @classmethod
    def is_valid(cls, mode: str) -> bool:
        return mode in (cls.IDLE, cls.EXCLUSIVE, cls.SHARED)


# Valid transitions: (from_mode, to_mode) → True
# Invalid transitions → False (must go through idle first)
_VALID_TRANSITIONS = {
    # From idle
    ("idle", "idle"): True,          # no-op
    ("idle", "exclusive"): True,     # deploy exclusive model
    ("idle", "shared"): True,        # deploy shared model/service
    # From exclusive
    ("exclusive", "idle"): True,     # stop exclusive model
    ("exclusive", "exclusive"): False,    # must idle first (different model swap)
    ("exclusive", "shared"): False,  # must idle first
    # From shared
    ("shared", "idle"): True,        # stop all shared services
    ("shared", "shared"): True,      # add/remove shared service (hot-plug)
    ("shared", "exclusive"): False,  # must idle first
}


def validate_transition(from_mode: str, to_mode: str) -> bool:
    """Check if a GPU mode transition is valid.

    Rules:
      - idle → exclusive: ✅ deploy exclusive model, GPU fully locked
      - idle → shared:    ✅ deploy shared service
      - exclusive → idle: ✅ stop exclusive model
      - shared → idle:    ✅ stop all shared services
      - shared → shared:  ✅ add/remove shared service
      - exclusive → exclusive: ✅ same-port swap
      - exclusive → shared: ❌ must idle first
      - shared → exclusive: ❌ must idle first
    """
    # "none" is orthogonal to GPU mode — not a GPUMode value
    if to_mode == "none" or from_mode == "none":
        return False
    result = _VALID_TRANSITIONS.get((from_mode, to_mode))
    if result is None:
        log.warning("Unknown GPU mode transition: %s → %s", from_mode, to_mode)
        return False
    return result


class ServiceState:
    """Service health state machine."""
    SWITCHING = "switching"
    HEALTHY = "healthy"
    IDLE = "idle"
    ERROR = "error"

    @classmethod
    def is_active(cls, state: str) -> bool:
        return state in (cls.SWITCHING, cls.HEALTHY, cls.ERROR)


# Backward-compat alias (deprecated, will be removed in v4.4)
ProfileState = ServiceState


# ─── State Manager (IFFDB-delegated) ──────────────────────────────

class StateDB:
    """Thread-safe state manager — IFFDB-delegated.

    Constructor accepts either:
      db_path (old):  Path to state.db — IFFDB auto-created from parent dir.
      iffdb  (new):  IFFDB instance for delegation.

    Keeps the same public API (get/set/set_multi/gpu_mode/etc.) as v4.x.
    """

    def __init__(self, db_path: Path | None = None, iffdb=None):
        if iffdb is not None:
            self._db = iffdb
        elif db_path is not None:
            from inferfabric.db import IFFDB
            self._db = IFFDB(Path(db_path).parent)
        else:
            raise ValueError("StateDB requires db_path or iffdb")
        self._models_lookup = None  # 由 ModelManager 在 models 加载后设置
        self._seal_lock = threading.RLock()
        self._sealed = False  # PR-20 ③：关停期封印（垂死进程禁写状态）

    # ─── Seal (PR-20 ③ 重启零中断) ─────────────────────────────

    def seal(self, reason: str = "", timeout: float = 5.0):
        """封印状态库：此后所有写方法 no-op（读不受影响），幂等。

        P0 实测旧进程关停期 reconcile 毒写 gpu_mode/services，把错误状态
        交给下一个进程（2026-10-27 12:27:58 事件）。SIGTERM 第一时间封印，
        下一个进程从现实重新推导，不继承垂死进程的误判。

        提交屏障有界（安全评审补丁）：已准入写持锁时最多等 timeout 秒。
        超时则仍置 _sealed（标志只 False→True，无锁置位无害）——至多
        已准入的写晚到提交（记警告日志），之后的写照旧 no-op。否则
        begin_shutdown 会在 SIGTERM 关键路径上无限挂死，排空窗口退化
        回 systemd SIGKILL。
        """
        # Wait for writes already admitted by the decorator to commit. After
        # seal returns, no StateDB writer can commit behind the shutdown barrier.
        acquired = self._seal_lock.acquire(timeout=timeout)
        try:
            if not self._sealed:
                self._sealed = True
                if not acquired:
                    log.warning("StateDB seal: admitted write still in flight after "
                                "%.1fs — sealing anyway (straggler may commit)", timeout)
                log.warning("StateDB sealed (%s) — state writes disabled until process exit",
                            reason or "unspecified")
        finally:
            if acquired:
                self._seal_lock.release()

    # ─── Generic KV ────────────────────────────────────────────

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get state value. Compat with generic get()."""
        return self._db.get(key, default)

    @_sealed_noop
    def set(self, key: str, value: str):
        """Set state value. Compat with generic set()."""
        self._db.set(key, value)

    @_sealed_noop
    def set_multi(self, kv: dict[str, str]):
        """Atomically set multiple state keys in a single DB transaction."""
        self._db.set_multi(kv)

    # ─── Active Services ────────────────────────────────────────

    def get_active_services(self) -> list[str]:
        return self._db.get_active_services()

    @_sealed_noop
    def set_active_services(self, services: list[str]):
        self._db.set_active_services(services)

    @_sealed_noop
    def add_active_service(self, name: str):
        self._db.add_active_service(name)

    @_sealed_noop
    def remove_active_service(self, name: str):
        self._db.remove_active_service(name)

    # ─── Manual Stop Protection (delegated to IFFDB table) ──────

    MANUAL_STOP_TTL = 60  # 60s — auto-switch unblock cooldown after manual stop/reset

    @_sealed_noop
    def record_manual_stop(self, name: str):
        """Record that user manually stopped a model (blocks auto-switch)."""
        self._db.record_manual_stop(name)

    def is_manually_stopped(self, name: str) -> bool:
        """Check if model was manually stopped within TTL."""
        return self._db.is_manually_stopped(name)

    @_sealed_noop
    def clear_manual_stop(self, name: str):
        """Clear manual stop record."""
        self._db.clear_manual_stop(name)

    def get_all_manual_stops(self) -> dict[str, float]:
        """Return all manual stop entries for cleanup."""
        return self._db.get_all_manual_stops()

    # ─── Models Lookup Callback ──────────────────────────────────

    def set_models_lookup(self, lookup_fn):
        """设置模型查找回调，用于 gpu_mode 推导。ModelManager 在 models 加载后调用。"""
        self._models_lookup = lookup_fn

    # ─── GPU Mode ───────────────────────────────────────────────

    @property
    def gpu_mode(self) -> str:
        """从 active_services 实时推导 gpu_mode，不依赖存储值。

        推导规则:
          - 过滤出 gpu_role != 'none' 的服务
          - 无 GPU 服务 → idle
          - 有 exclusive 服务 → exclusive
          - 其余（只有 shared 服务）→ shared
        """
        if self._models_lookup is None:
            # 回调尚未设置时 fallback 到 DB 存储值
            return self._db.get_gpu_mode()
        active = self.get_active_services()
        # 先查 exclusive（有 exclusive 优先级最高）
        for svc_name in active:
            model = self._models_lookup(svc_name)
            if model and model.is_exclusive:
                return GPUMode.EXCLUSIVE
        # 再查 shared（有非 none 服务但不是 exclusive）
        for svc_name in active:
            model = self._models_lookup(svc_name)
            if model and not model.is_gpu_none:
                return GPUMode.SHARED
        # 无 GPU 服务 → idle（包括空列表、只有 none 服务）
        return GPUMode.IDLE

    @gpu_mode.setter
    @_sealed_noop
    def gpu_mode(self, mode: str):
        """保留 setter 写 DB（调试可查 + 向后兼容）。"""
        assert GPUMode.is_valid(mode), f"Invalid GPU mode: {mode}"
        self._db.set_gpu_mode(mode)

    # ─── Sleep State ────────────────────────────────────────────

    def get_sleep_state(self, model_name: str) -> Optional[str]:
        return self._db.get_sleep_state(model_name)

    @_sealed_noop
    def set_sleep_state(self, model_name: str, level: Optional[int]):
        self._db.set_sleep_state(model_name, level)

    def get_all_sleep_states(self) -> dict[str, str]:
        return self._db.get_all_sleep_states()

    # ─── History ────────────────────────────────────────────────

    @_sealed_noop
    def add_history(self, from_profile: str, to_profile: str, duration: float, status: str = "ok"):
        self._db.add_switch_history(from_profile, to_profile, duration, status)

    def get_history(self, limit: int = 20) -> list[dict]:
        return self._db.get_switch_history(limit)