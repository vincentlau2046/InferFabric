"""v2 限流器 — 二级令牌桶 (server + model) + 可配置模式

v4.6.3 重构：
  - mode="observe": RPM 门仅记录不拒绝，Semaphore 排队（单用户默认）
  - mode="reject": RPM 门拒绝(429)，Semaphore 排队（多用户场景）
  - rpm=0: 跳过 RPM 门
  - max_concurrent: auto=从 vLLM max_num_seqs 动态获取
  - 超时可配置，默认 5s（reject 模式）/ 无限等待（observe 模式）

配置 iff.yaml:
  rate_limit:
    mode: observe           # observe=记录不拒绝, reject=429拒绝
    server_rpm: 0           # 0=不限流
    model_rpm_default: 0    # 0=不限流
    max_concurrent: auto    # auto=从 max_num_seqs 获取
    timeout: 5              # acquire 超时秒数
"""

import threading
import time
import logging
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger("inferfabric.ratelimit")

# 限流模式
RateLimitMode = Literal["observe", "reject"]


@dataclass
class BucketConfig:
    """单个令牌桶配置。"""
    rpm: float          # 每分钟请求数
    burst: int          # 突发上限（= rpm 上取整，至少 1）
    timeout: float = 30.0


class TokenBucket:
    """线程安全的令牌桶。

    - refill_rate: 每秒补充的令牌数 = rpm / 60
    - burst: 桶容量上限
    - acquire 超时返回 False
    - rpm=0 时 burst=0，acquire 永远返回 True（无限流）
    """

    def __init__(self, config: BucketConfig):
        if config.rpm <= 0:
            self._rate = 0.0
            self._burst = 0
            self._tokens = 0.0
            self._disabled = True
        else:
            self._rate = config.rpm / 60.0
            self._burst = max(1, config.burst)
            self._tokens = float(self._burst)
            self._disabled = False
        self._timeout = config.timeout
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float | None = None) -> bool:
        if self._disabled:
            return True
        deadline = time.monotonic() + (timeout or self._timeout)
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(remaining, 0.1))

    def try_acquire(self) -> bool:
        if self._disabled:
            return True
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def release(self):
        if self._disabled:
            return
        with self._lock:
            self._tokens = min(self._burst, self._tokens + 1.0)

    @property
    def available(self) -> float:
        if self._disabled:
            return float('inf')
        with self._lock:
            self._refill()
            return self._tokens

    @property
    def disabled(self) -> bool:
        return self._disabled

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now


class RateLimiterV2:
    """二级限流器：server + model。"""

    def __init__(
        self,
        server_rpm: float = 0,
        model_rpm_default: float = 0,
        timeout: float = 5.0,
    ):
        self._server_bucket = TokenBucket(BucketConfig(
            rpm=server_rpm, burst=max(1, int(server_rpm)) if server_rpm > 0 else 0, timeout=timeout,
        ))
        self._server_rpm = server_rpm
        self._model_buckets: dict[str, TokenBucket] = {}
        self._model_rpm_default = model_rpm_default
        self._timeout = timeout
        self._lock = threading.Lock()

    def register_model(self, model_name: str, rpm: float | None = None):
        effective_rpm = rpm if rpm is not None else self._model_rpm_default
        with self._lock:
            self._model_buckets[model_name] = TokenBucket(BucketConfig(
                rpm=effective_rpm,
                burst=max(1, int(effective_rpm)) if effective_rpm > 0 else 0,
                timeout=self._timeout,
            ))

    def acquire(self, model_name: str, timeout: float | None = None) -> tuple[bool, str]:
        if not self._server_bucket.acquire(timeout):
            return False, "server_rate_limit"
        bucket = self._get_model_bucket(model_name)
        if not bucket.acquire(timeout):
            self._server_bucket.release()
            return False, f"model_rate_limit:{model_name}"
        return True, "ok"

    def try_acquire(self, model_name: str) -> tuple[bool, str]:
        if not self._server_bucket.try_acquire():
            return False, "server_rate_limit"
        bucket = self._get_model_bucket(model_name)
        if not bucket.try_acquire():
            self._server_bucket.release()
            return False, f"model_rate_limit:{model_name}"
        return True, "ok"

    def release(self, model_name: str):
        self._server_bucket.release()
        bucket = self._get_model_bucket(model_name)
        bucket.release()

    @property
    def server_available(self) -> float:
        return self._server_bucket.available

    def model_available(self, model_name: str) -> float:
        return self._get_model_bucket(model_name).available

    def _get_model_bucket(self, model_name: str) -> TokenBucket:
        with self._lock:
            if model_name not in self._model_buckets:
                effective_rpm = self._model_rpm_default
                self._model_buckets[model_name] = TokenBucket(BucketConfig(
                    rpm=effective_rpm,
                    burst=max(1, int(effective_rpm)) if effective_rpm > 0 else 0,
                    timeout=self._timeout,
                ))
            return self._model_buckets[model_name]

    def clear(self):
        with self._lock:
            self._model_buckets.clear()


# ── DualGate (R4: per-model 并发池) ──

class DualGateLimiter:
    """二级嵌套限流门 — RPM 软门 + per-model 并发硬门

    Gate 1: RateLimiterV2 (TokenBucket, RPM) — per-minute rate
    Gate 2: per-model Semaphore + optional global Semaphore — concurrency
    """

    def __init__(
        self,
        rpm_limiter: RateLimiterV2,
        max_concurrent: int = 8,
        mode: RateLimitMode = "observe",
        timeout: float = 5.0,
        global_max_concurrent: int = 0,
    ):
        self._rpm = rpm_limiter
        self._per_model: dict[str, threading.Semaphore] = {}
        self._model_default = max_concurrent
        self._global_sem: threading.Semaphore | None = (
            threading.Semaphore(global_max_concurrent) if global_max_concurrent > 0 else None
        )
        self._max_concurrent = max_concurrent
        self._global_max = global_max_concurrent
        self._mode = mode
        self._timeout = timeout
        self._lock = threading.Lock()

    def describe(self) -> dict:
        """当前生效的流控配置（只读展示，供 dashboard snapshot 渲染；无写入入口）。

        - server_rpm / model_rpm_default 为 0 → 对应 RPM 门禁用（TokenBucket 禁用，
          见 TokenBucket.__init__ rpm<=0 分支）
        - mode=observe → 超限只记日志不拒绝；mode=reject → 超限 429
        - max_concurrent 由调用方（ProxyManager）保证为 int：iff.yaml 的 auto 档在
          _compute_max_concurrent() 已解析为 vLLM max_num_seqs 最大值（下限 4）。
          本类 Semaphore 初值依赖 int，故此处透传 int，前端数值判断（> 0）安全，
          不会遇到字符串 'auto'（NaN 风险）。
        - global_max_concurrent 为预留契约字段，当前 dashboard 不渲染。
        配置来自 iff.yaml rate_limit 段，仅在 ProxyManager 构造时读取一次
        （无热加载），改动需重启 proxy。
        """
        return {
            "mode": self._mode,
            "server_rpm": getattr(self._rpm, "_server_rpm", 0),
            "model_rpm_default": getattr(self._rpm, "_model_rpm_default", 0),
            "max_concurrent": self._max_concurrent,
            "global_max_concurrent": self._global_max,
        }

    def _get_or_create_sem(self, model: str) -> threading.Semaphore:
        with self._lock:
            if model not in self._per_model:
                self._per_model[model] = threading.Semaphore(self._model_default)
            return self._per_model[model]

    def acquire(self, model: str, timeout: int | None = None) -> 'GateResult':
        effective_timeout = timeout or self._timeout

        # Gate 1: RPM
        rpm_held = False
        if self._mode == "observe":
            rpm_ok, rpm_reason = self._rpm.try_acquire(model)
            if rpm_ok:
                rpm_held = True
            else:
                log.warning("RPM observe: %s would be rate-limited (%s) — allowing", model, rpm_reason)
        else:
            rpm_ok, rpm_reason = self._rpm.acquire(model, effective_timeout)
            if not rpm_ok:
                return GateResult(self, model, ok=False, reason=f"rpm_limit: {rpm_reason}", rpm_held=False)

        # Gate 2a: Global concurrency (optional)
        global_held = False
        if self._global_sem is not None:
            if self._mode == "observe":
                acquired = self._global_sem.acquire(timeout=300)
                if not acquired:
                    log.error("Global concurrency observe: waited 300s — bug?")
                    return GateResult(self, model, ok=True, reason="", rpm_held=rpm_held,
                                      model_sem_held=False, global_held=False)
            else:
                acquired = self._global_sem.acquire(timeout=effective_timeout)
                if not acquired:
                    if rpm_held:
                        self._rpm.release(model)
                    return GateResult(self, model, ok=False, reason="global_concurrency_limit", rpm_held=False)
            global_held = True

        # Gate 2b: Per-model concurrency
        sem = self._get_or_create_sem(model)
        if self._mode == "observe":
            acquired = sem.acquire(timeout=300)
            if not acquired:
                log.error("Model concurrency observe: %s waited 300s — bug?", model)
                if global_held and self._global_sem:
                    self._global_sem.release()
                return GateResult(self, model, ok=True, reason="", rpm_held=rpm_held,
                                  model_sem_held=False, global_held=global_held)
        else:
            acquired = sem.acquire(timeout=effective_timeout)
            if not acquired:
                if rpm_held:
                    self._rpm.release(model)
                if global_held and self._global_sem:
                    self._global_sem.release()
                return GateResult(self, model, ok=False, reason=f"model_concurrency_limit:{model}",
                                  rpm_held=False, global_held=False)

        return GateResult(self, model, ok=True, reason="", rpm_held=rpm_held,
                          model_sem_held=True, global_held=global_held)

    def acquire_target(self, model: str, timeout: float | None = None) -> 'GateResult':
        """物理目标容量门（池路径专用，终审 #8）：只占 per-model 并发信号量。

        池调度在逻辑名 gate（acquire）之内按物理目标再加一层并发门 —
        RPM 与全局并发已由外层计费，这里重复 acquire 会双扣 server RPM
        并占用双份全局并发。仅 Gate 2b，release 也只还 per-model 信号量。
        """
        effective_timeout = timeout if timeout is not None else self._timeout
        sem = self._get_or_create_sem(model)
        if self._mode == "observe":
            acquired = sem.acquire(timeout=300)
            if not acquired:
                log.error("Target concurrency observe: %s waited 300s — bug?", model)
                return GateResult(self, model, ok=True, reason="",
                                  rpm_held=False, model_sem_held=False,
                                  global_held=False)
        else:
            acquired = sem.acquire(timeout=effective_timeout)
            if not acquired:
                return GateResult(self, model, ok=False,
                                  reason=f"model_concurrency_limit:{model}",
                                  rpm_held=False, model_sem_held=False,
                                  global_held=False)
        return GateResult(self, model, ok=True, reason="",
                          rpm_held=False, model_sem_held=True, global_held=False)

    def _release(self, model: str, rpm_held: bool, model_sem_held: bool = True, global_held: bool = False):
        if model_sem_held:
            sem = self._per_model.get(model)
            if sem:
                sem.release()
        if global_held and self._global_sem:
            self._global_sem.release()

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def mode(self) -> str:
        return self._mode


class GateResult:
    """acquire 返回的句柄 — 确保 acquire/release 对称。"""

    __slots__ = ('_limiter', '_model', 'ok', 'reason', '_rpm_held', '_model_sem_held', '_global_held')

    def __init__(
        self,
        limiter: DualGateLimiter,
        model: str,
        ok: bool,
        reason: str,
        rpm_held: bool,
        model_sem_held: bool = True,
        global_held: bool = False,
    ):
        self._limiter = limiter
        self._model = model
        self.ok = ok
        self.reason = reason
        self._rpm_held = rpm_held
        self._model_sem_held = model_sem_held
        self._global_held = global_held

    @property
    def _sem_held(self):
        """向下兼容：旧代码访问 _sem_held → _model_sem_held。"""
        return self._model_sem_held

    def release(self):
        self._limiter._release(self._model, self._rpm_held, self._model_sem_held, self._global_held)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.ok:
            self.release()
        return False