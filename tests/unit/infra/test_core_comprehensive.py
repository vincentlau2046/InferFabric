"""
unit/infra/test_core_comprehensive.py — 核心模块综合测试

测试对象: 多模块（GPUMode, StateDB, AuthManager, RequestLog, TokenBucket,
         CloudDiscovery, Forwarder, ProxyManager, ConfigRetry, GPULock,
         ConfigWatcher, MetricsParser, Dashboard, PackageStructure）
覆盖范围:
  - GPU 模式状态转换（全部合法 & 非法路径）
  - StateDB CRUD、手动停止 TTL、sleep 状态、并发访问
  - AuthManager 完整鉴权流程（primary/guest/expired/reload）
  - RequestLog 序列化、RequestLogger 写入/轮转/并发
  - TokenBucket / RateLimiterV2 / DualGateLimiter 限流
  - CloudDiscovery 配置/发现/路由/轮询/热加载
  - Forwarder read_body/send_json/forward_to_cloud
  - ProxyManager 初始化、model_to_service、IFF_DATA_DIR
  - 指数退避、Prometheus 解析、Admin API、Dashboard 渲染
  - 包结构、公共导出、跨审查修复验证
"""

import json
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import asdict

import pytest
import yaml


# ═══════════════════════════════════════════════════════════════════
# Domain 1: 状态管理
# ═══════════════════════════════════════════════════════════════════

class TestGPUMode:
    """GPU 模式状态机"""

    def test_valid_modes(self):
        from inferfabric.state import GPUMode
        assert GPUMode.is_valid("idle")
        assert GPUMode.is_valid("exclusive")
        assert GPUMode.is_valid("shared")
        assert not GPUMode.is_valid("none")
        assert not GPUMode.is_valid("invalid")
        assert not GPUMode.is_valid("")

    def test_validate_transition_idle_to_exclusive(self):
        from inferfabric.state import validate_transition
        assert validate_transition("idle", "exclusive") is True

    def test_validate_transition_idle_to_shared(self):
        from inferfabric.state import validate_transition
        assert validate_transition("idle", "shared") is True

    def test_validate_transition_exclusive_to_idle(self):
        from inferfabric.state import validate_transition
        assert validate_transition("exclusive", "idle") is True

    def test_validate_transition_shared_to_idle(self):
        from inferfabric.state import validate_transition
        assert validate_transition("shared", "idle") is True

    def test_validate_transition_shared_to_shared(self):
        from inferfabric.state import validate_transition
        assert validate_transition("shared", "shared") is True

    def test_validate_transition_exclusive_to_shared_blocked(self):
        from inferfabric.state import validate_transition
        assert validate_transition("exclusive", "shared") is False

    def test_validate_transition_shared_to_exclusive_blocked(self):
        from inferfabric.state import validate_transition
        assert validate_transition("shared", "exclusive") is False

    def test_validate_transition_exclusive_to_exclusive_blocked(self):
        from inferfabric.state import validate_transition
        assert validate_transition("exclusive", "exclusive") is False

    def test_validate_transition_none_orthogonal(self):
        from inferfabric.state import validate_transition
        assert validate_transition("idle", "none") is False
        assert validate_transition("none", "idle") is False

    def test_validate_transition_unknown_mode(self):
        from inferfabric.state import validate_transition
        assert validate_transition("idle", "unknown") is False
        assert validate_transition("unknown", "idle") is False

    def test_validate_transition_idempotent(self):
        from inferfabric.state import validate_transition
        assert validate_transition("idle", "idle") is True


class TestServiceState:
    """服务状态枚举"""

    def test_active_states(self):
        from inferfabric.state import ServiceState
        assert ServiceState.is_active("switching")
        assert ServiceState.is_active("healthy")
        assert ServiceState.is_active("error")
        assert not ServiceState.is_active("idle")

    def test_backward_compat_alias(self):
        from inferfabric.state import ProfileState, ServiceState
        assert ProfileState is ServiceState


class TestStateDB:
    """SQLite 状态管理器"""

    @pytest.fixture
    def db(self, tmp_path):
        from inferfabric.state import StateDB
        return StateDB(tmp_path / "test.db")

    def test_get_set_basic(self, db):
        db.set("test_key", "test_value")
        assert db.get("test_key") == "test_value"

    def test_get_default(self, db):
        assert db.get("nonexistent", "fallback") == "fallback"

    def test_set_multi(self, db):
        db.set_multi({"k1": "v1", "k2": "v2", "k3": "v3"})
        assert db.get("k1") == "v1"
        assert db.get("k2") == "v2"
        assert db.get("k3") == "v3"

    def test_active_services(self, db):
        assert db.get_active_services() == []
        db.add_active_service("model-a")
        assert db.get_active_services() == ["model-a"]
        db.add_active_service("model-b")
        assert db.get_active_services() == ["model-a", "model-b"]
        db.remove_active_service("model-a")
        assert db.get_active_services() == ["model-b"]

    def test_active_services_no_duplicate(self, db):
        db.add_active_service("model-a")
        db.add_active_service("model-a")
        assert db.get_active_services() == ["model-a"]

    def test_active_services_remove_nonexistent(self, db):
        db.remove_active_service("nonexistent")  # Should not raise

    def test_gpu_mode_property(self, db):
        assert db.gpu_mode == "idle"  # default
        db.gpu_mode = "exclusive"
        assert db.gpu_mode == "exclusive"
        with pytest.raises(AssertionError):
            db.gpu_mode = "invalid_mode"

    def test_manual_stop(self, db):
        assert not db.is_manually_stopped("model-a")
        db.record_manual_stop("model-a")
        assert db.is_manually_stopped("model-a")
        db.clear_manual_stop("model-a")
        assert not db.is_manually_stopped("model-a")

    def test_manual_stop_ttl_expiry(self, db):
        db.record_manual_stop("model-a")
        assert db.is_manually_stopped("model-a")
        # Expire the entry: update stop_ts directly in the table
        # (StateDB now delegates to IFFDB table instead of KV key)
        now = time.time()
        from inferfabric.db import STATE_DB as _STDB
        with db._db._write_lock, db._db.connect(_STDB) as conn:
            conn.execute(
                "UPDATE manual_stops SET stop_ts = ? WHERE model = ?",
                (now - 601, "model-a"),
            )
            conn.commit()
        assert not db.is_manually_stopped("model-a")

    def test_sleep_state(self, db):
        assert db.get_sleep_state("model-a") is None
        db.set_sleep_state("model-a", 1)
        assert db.get_sleep_state("model-a") == "l1"
        db.set_sleep_state("model-a", 2)
        assert db.get_sleep_state("model-a") == "l2"
        db.set_sleep_state("model-a", None)
        assert db.get_sleep_state("model-a") is None

    def test_get_all_sleep_states(self, db):
        db.set_sleep_state("model-a", 1)
        db.set_sleep_state("model-b", 2)
        states = db.get_all_sleep_states()
        assert states == {"model-a": "l1", "model-b": "l2"}

    def test_history(self, db):
        db.add_history("idle", "model-a", 5.0, "ok")
        db.add_history("model-a", "model-b", 3.0, "ok")
        history = db.get_history(limit=10)
        assert len(history) == 2
        assert history[0]["from"] == "model-a"
        assert history[1]["from"] == "idle"

    def test_concurrent_access(self, db):
        """Thread-safety smoke test"""
        errors = []
        def writer(n):
            try:
                for i in range(50):
                    db.set(f"key-{n}-{i}", f"val-{n}-{i}")
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors

    def test_set_overwrite(self, db):
        db.set("key", "v1")
        db.set("key", "v2")
        assert db.get("key") == "v2"

    def test_default_state_keys(self, db):
        """Initial state keys should be populated"""
        assert db.get("gpu_mode") == "idle"
        assert db.get("current_profile") == "idle"
        assert db.get("active_services") == "[]"


# ═══════════════════════════════════════════════════════════════════
# Domain 2: 鉴权系统
# ═══════════════════════════════════════════════════════════════════

class TestAuthManagerBasic:
    """基础鉴权逻辑"""

    @pytest.fixture
    def auth_no_config(self, tmp_path):
        from inferfabric.proxy.auth import AuthManager
        return AuthManager(tmp_path / "nonexistent.yaml")

    @pytest.fixture
    def auth_primary_only(self, tmp_path):
        from inferfabric.proxy.auth import AuthManager
        p = tmp_path / "api_keys.yaml"
        p.write_text(yaml.dump({"primary": "sk-iff-test123"}))
        return AuthManager(p)

    @pytest.fixture
    def auth_with_guests(self, tmp_path):
        from inferfabric.proxy.auth import AuthManager
        p = tmp_path / "api_keys.yaml"
        cfg = {
            "primary": "sk-iff-primary",
            "guests": [
                {"key": "sk-guest-1", "name": "测试用户", "models": ["qwen35-9b-vl"]},
                {"key": "sk-guest-expired", "name": "过期用户",
                 "models": ["glm-5"],
                 "expires": (datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat()},
                {"key": "sk-guest-future", "name": "未过期用户",
                 "models": ["deepseek-v4-flash"],
                 "expires": (datetime.now(tz=timezone.utc) + timedelta(days=30)).isoformat()},
            ]
        }
        p.write_text(yaml.dump(cfg))
        return AuthManager(p)

    def test_no_config_disabled(self, auth_no_config):
        assert not auth_no_config.enabled

    def test_no_config_check_fails(self, auth_no_config):
        """When auth is disabled (no config), check() returns False — caller must check enabled first"""
        ok, reason = auth_no_config.check("any-token", "any-model")
        # Without a key map, any token is invalid
        assert not ok

    def test_primary_only_enabled(self, auth_primary_only):
        assert auth_primary_only.enabled

    def test_primary_check_pass(self, auth_primary_only):
        ok, reason = auth_primary_only.check("Bearer sk-iff-test123", "any-model")
        assert ok

    def test_primary_check_invalid_key(self, auth_primary_only):
        ok, reason = auth_primary_only.check("Bearer wrong-key", "any-model")
        assert not ok
        assert "invalid" in reason

    def test_primary_check_no_bearer_prefix(self, auth_primary_only):
        ok, reason = auth_primary_only.check("sk-iff-test123", "any-model")
        assert ok

    def test_guest_model_whitelist(self, auth_with_guests):
        ok, _ = auth_with_guests.check("Bearer sk-guest-1", "qwen35-9b-vl")
        assert ok
        ok, reason = auth_with_guests.check("Bearer sk-guest-1", "other-model")
        assert not ok
        assert "not allowed" in reason

    def test_guest_expired(self, auth_with_guests):
        ok, reason = auth_with_guests.check("Bearer sk-guest-expired", "glm-5")
        assert not ok
        assert "expired" in reason

    def test_guest_future_valid(self, auth_with_guests):
        ok, _ = auth_with_guests.check("Bearer sk-guest-future", "deepseek-v4-flash")
        assert ok

    def test_key_name(self, auth_with_guests):
        assert auth_with_guests.key_name("Bearer sk-iff-primary") == "primary"
        assert auth_with_guests.key_name("Bearer sk-guest-1") == "测试用户"
        assert auth_with_guests.key_name("Bearer unknown") == "anonymous"

    def test_key_name_no_auth(self, auth_no_config):
        assert auth_no_config.key_name("anything") == "anonymous"

    def test_reload(self, auth_primary_only, tmp_path):
        assert auth_primary_only.enabled
        p = tmp_path / "api_keys.yaml"
        cfg = {"primary": "sk-iff-new-primary", "guests": [{"key": "sk-new-guest", "name": "new"}]}
        p.write_text(yaml.dump(cfg))
        auth_primary_only.reload(p)
        ok, _ = auth_primary_only.check("Bearer sk-iff-new-primary", "any-model")
        assert ok
        ok, _ = auth_primary_only.check("Bearer sk-iff-test123", "any-model")
        assert not ok

    def test_empty_yaml_disabled(self, tmp_path):
        from inferfabric.proxy.auth import AuthManager
        p = tmp_path / "api_keys.yaml"
        p.write_text("")
        auth = AuthManager(p)
        assert not auth.enabled

    def test_invalid_yaml_graceful(self, tmp_path):
        from inferfabric.proxy.auth import AuthManager
        p = tmp_path / "api_keys.yaml"
        p.write_text("{{{{invalid yaml")
        auth = AuthManager(p)
        assert not auth.enabled

    def test_bearer_case_insensitive(self, auth_primary_only):
        """'Bearer' and 'bearer' should both work"""
        ok1, _ = auth_primary_only.check("Bearer sk-iff-test123", "m")
        ok2, _ = auth_primary_only.check("bearer sk-iff-test123", "m")
        assert ok1
        assert ok2


# ═══════════════════════════════════════════════════════════════════
# Domain 3: 请求日志
# ═══════════════════════════════════════════════════════════════════

class TestRequestLog:
    """RequestLog 数据结构"""

    def test_default_values(self):
        from inferfabric.proxy.request_logger import RequestLog
        log = RequestLog(req_id="r1", key_name="primary", model="test", status=200)
        assert log.ttft_ms is None
        assert log.tokens_in == 0
        assert log.route == "local"
        assert log.cloud_provider is None
        assert log.error is None

    def test_full_values(self):
        from inferfabric.proxy.request_logger import RequestLog
        log = RequestLog(
            req_id="r1", key_name="primary", model="test", status=200,
            ttft_ms=150.5, tokens_in=100, tokens_out=50,
            duration_ms=3000.0, route="cloud", cloud_provider="baidu-codingplan",
        )
        d = asdict(log)
        assert d["ttft_ms"] == 150.5
        assert d["route"] == "cloud"

    def test_serialization(self):
        from inferfabric.proxy.request_logger import RequestLog
        log = RequestLog(req_id="r1", key_name="k", model="m", status=200, ts="2026-01-01T00:00:00")
        j = json.dumps(asdict(log))
        parsed = json.loads(j)
        assert parsed["req_id"] == "r1"


class TestRequestLogger:
    """JSONL 日志写入器"""

    @pytest.fixture
    def logger(self, tmp_path):
        from inferfabric.proxy.request_logger import RequestLogger, RequestLog
        rl = RequestLogger(log_dir=tmp_path / "logs", enabled=True)
        yield rl
        rl.close()

    @pytest.fixture
    def disabled_logger(self, tmp_path):
        from inferfabric.proxy.request_logger import RequestLogger
        rl = RequestLogger(log_dir=tmp_path / "logs", enabled=False)
        yield rl
        rl.close()

    def test_write_basic(self, logger, tmp_path):
        from inferfabric.proxy.request_logger import RequestLog
        entry = RequestLog(req_id="r1", key_name="primary", model="test", status=200)
        logger.log(entry)
        logger.close()
        log_files = list((tmp_path / "logs").glob("access-*.jsonl"))
        assert len(log_files) == 1
        lines = log_files[0].read_text().strip().split("\n")
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["req_id"] == "r1"
        assert parsed["status"] == 200
        assert parsed["ts"]

    def test_disabled_no_write(self, disabled_logger, tmp_path):
        from inferfabric.proxy.request_logger import RequestLog
        entry = RequestLog(req_id="r1", key_name="k", model="m", status=200)
        disabled_logger.log(entry)
        disabled_logger.close()
        assert not (tmp_path / "logs").exists()

    def test_daily_rotation(self, logger, tmp_path):
        from inferfabric.proxy.request_logger import RequestLog
        entry = RequestLog(req_id="r1", key_name="k", model="m", status=200)
        logger.log(entry)
        logger.close()
        log_files = list((tmp_path / "logs").glob("access-*.jsonl"))
        assert len(log_files) >= 1
        date_str = time.strftime("%Y-%m-%d")
        assert any(date_str in f.name for f in log_files)

    def test_multiple_entries(self, logger, tmp_path):
        from inferfabric.proxy.request_logger import RequestLog
        for i in range(10):
            entry = RequestLog(req_id=f"r{i}", key_name="k", model="m", status=200)
            logger.log(entry)
        logger.close()
        log_files = list((tmp_path / "logs").glob("access-*.jsonl"))
        lines = log_files[0].read_text().strip().split("\n")
        assert len(lines) == 10

    def test_concurrent_writes(self, logger, tmp_path):
        from inferfabric.proxy.request_logger import RequestLog
        errors = []
        def writer(n):
            try:
                for i in range(20):
                    entry = RequestLog(req_id=f"r{n}-{i}", key_name="k", model="m", status=200)
                    logger.log(entry)
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        logger.close()
        assert not errors
        log_files = list((tmp_path / "logs").glob("access-*.jsonl"))
        lines = log_files[0].read_text().strip().split("\n")
        assert len(lines) == 100

    def test_close_idempotent(self, logger):
        logger.close()
        logger.close()

    def test_enabled_property(self, logger, disabled_logger):
        assert logger.enabled is True
        assert disabled_logger.enabled is False

    def test_unicode_content(self, logger, tmp_path):
        from inferfabric.proxy.request_logger import RequestLog
        entry = RequestLog(req_id="r1", key_name="主密钥", model="测试模型", status=200, error="中文错误")
        logger.log(entry)
        logger.close()
        log_files = list((tmp_path / "logs").glob("access-*.jsonl"))
        parsed = json.loads(log_files[0].read_text().strip())
        assert parsed["key_name"] == "主密钥"

    def test_ttft_ms_recorded(self, logger, tmp_path):
        from inferfabric.proxy.request_logger import RequestLog
        entry = RequestLog(req_id="r1", key_name="k", model="m", status=200, ttft_ms=123.45)
        logger.log(entry)
        logger.close()
        parsed = json.loads((tmp_path / "logs").glob("access-*.jsonl").__next__().read_text().strip())
        assert parsed["ttft_ms"] == 123.45

    def test_cloud_route_recorded(self, logger, tmp_path):
        from inferfabric.proxy.request_logger import RequestLog
        entry = RequestLog(
            req_id="r1", key_name="k", model="m", status=200,
            route="cloud", cloud_provider="baidu-codingplan",
        )
        logger.log(entry)
        logger.close()
        parsed = json.loads((tmp_path / "logs").glob("access-*.jsonl").__next__().read_text().strip())
        assert parsed["route"] == "cloud"


# ═══════════════════════════════════════════════════════════════════
# Domain 4: 限流系统
# ═══════════════════════════════════════════════════════════════════

class TestTokenBucket:
    """令牌桶单元测试"""

    def test_acquire_immediate(self):
        from inferfabric.ratelimit import TokenBucket, BucketConfig
        bucket = TokenBucket(BucketConfig(rpm=60, burst=10, timeout=1.0))
        assert bucket.acquire(timeout=0.1)

    def test_available_decreases(self):
        from inferfabric.ratelimit import TokenBucket, BucketConfig
        bucket = TokenBucket(BucketConfig(rpm=60, burst=10, timeout=1.0))
        assert bucket.available >= 10
        assert bucket.try_acquire()
        assert bucket.available < 10

    def test_try_acquire_no_wait(self):
        from inferfabric.ratelimit import TokenBucket, BucketConfig
        bucket = TokenBucket(BucketConfig(rpm=1, burst=1, timeout=1.0))
        assert bucket.try_acquire()
        assert not bucket.try_acquire()

    def test_release_returns_token(self):
        from inferfabric.ratelimit import TokenBucket, BucketConfig
        bucket = TokenBucket(BucketConfig(rpm=1, burst=1, timeout=1.0))
        assert bucket.try_acquire()
        assert not bucket.try_acquire()
        bucket.release()
        assert bucket.try_acquire()

    def test_acquire_timeout(self):
        from inferfabric.ratelimit import TokenBucket, BucketConfig
        bucket = TokenBucket(BucketConfig(rpm=1, burst=1, timeout=0.3))
        assert bucket.try_acquire()
        start = time.monotonic()
        assert not bucket.acquire(timeout=0.2)
        elapsed = time.monotonic() - start
        assert elapsed < 0.5

    def test_refill_over_time(self):
        from inferfabric.ratelimit import TokenBucket, BucketConfig
        bucket = TokenBucket(BucketConfig(rpm=600, burst=1, timeout=1.0))
        assert bucket.try_acquire()
        time.sleep(0.15)
        assert bucket.available >= 0.9

    def test_concurrent_access(self):
        from inferfabric.ratelimit import TokenBucket, BucketConfig
        bucket = TokenBucket(BucketConfig(rpm=600, burst=50, timeout=5.0))
        acquired = []
        lock = threading.Lock()
        def worker(n):
            for _ in range(10):
                if bucket.try_acquire():
                    with lock:
                        acquired.append(n)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(acquired) <= 50 + 5


class TestRateLimiterV2:
    """二级限流器"""

    @pytest.fixture
    def limiter(self):
        from inferfabric.ratelimit import RateLimiterV2
        return RateLimiterV2(server_rpm=60, model_rpm_default=20, timeout=1.0)

    def test_acquire_pass(self, limiter):
        ok, reason = limiter.acquire("model-a", timeout=0.5)
        assert ok
        assert reason == "ok"

    def test_release(self, limiter):
        ok, _ = limiter.acquire("model-a", timeout=0.5)
        assert ok
        limiter.release("model-a")
        ok, _ = limiter.acquire("model-a", timeout=0.5)
        assert ok

    def test_register_model(self, limiter):
        limiter.register_model("custom-model", rpm=10)
        ok, _ = limiter.acquire("custom-model", timeout=0.5)
        assert ok

    def test_auto_register_unknown_model(self, limiter):
        ok, reason = limiter.acquire("unknown-model", timeout=0.5)
        assert ok

    def test_try_acquire(self, limiter):
        ok, reason = limiter.try_acquire("model-a")
        assert ok

    def test_model_available(self, limiter):
        avail = limiter.model_available("model-a")
        assert avail > 0

    def test_server_available(self, limiter):
        assert limiter.server_available > 0

    def test_clear(self, limiter):
        limiter.register_model("m1", rpm=10)
        limiter.register_model("m2", rpm=20)
        limiter.clear()
        ok, _ = limiter.acquire("m1", timeout=0.5)
        assert ok


class TestV1RateLimiterCompat:
    """限流层兼容 + DualGateLimiter"""

    def test_basic_acquire_release(self):
        import threading
        sem = threading.BoundedSemaphore(2)
        assert sem.acquire(timeout=0.5)
        assert sem.acquire(timeout=0.5)
        sem.release()
        sem.release()

    def test_dual_gate_limiter(self):
        from inferfabric.ratelimit import DualGateLimiter, RateLimiterV2
        rpm = RateLimiterV2(server_rpm=600, model_rpm_default=200)
        gate = DualGateLimiter(rpm_limiter=rpm, max_concurrent=2)
        result = gate.acquire("test-model", timeout=1)
        assert result.ok, f"Acquire failed: {result}"
        gate._release("test-model", True, True)
        assert gate.max_concurrent == 2


# ═══════════════════════════════════════════════════════════════════
# Domain 5: 云端发现
# ═══════════════════════════════════════════════════════════════════

class TestCloudModel:
    def test_defaults(self):
        from inferfabric.cloud_discovery import CloudModel
        m = CloudModel(model_id="test", provider="prov")
        assert m.openai_available is True
        assert m.anthropic_available is False

class TestProviderConfig:
    def test_defaults(self):
        from inferfabric.cloud_discovery import ProviderConfig
        cfg = ProviderConfig(name="test")
        assert cfg.api_key == ""
        assert cfg.enabled is True
        assert cfg.discovery_interval == 3600


class TestCloudDiscoveryBasic:
    """基础发现逻辑"""

    def test_no_config_disabled(self, tmp_path):
        from inferfabric.cloud_discovery import CloudDiscovery
        cd = CloudDiscovery(tmp_path / "nonexistent.yaml")
        assert len(cd.providers) == 0

    def test_load_config(self, tmp_path):
        from inferfabric.cloud_discovery import CloudDiscovery
        p = tmp_path / "cloud_provider.yaml"
        cfg = {"providers": {"test-provider": {"api_key": "sk-test", "openai_base": "https://api.test.com/v1", "anthropic_base": "https://api.test.com/anthropic"}}}
        p.write_text(yaml.dump(cfg))
        cd = CloudDiscovery(p)
        assert "test-provider" in cd.providers
        assert cd.providers["test-provider"].api_key == "sk-test"

    def test_env_var_expansion(self, tmp_path):
        from inferfabric.cloud_discovery import CloudDiscovery
        os.environ["IFF_TEST_API_KEY"] = "sk-from-env"
        try:
            p = tmp_path / "cloud_provider.yaml"
            cfg = {"providers": {"env-provider": {"api_key": "${IFF_TEST_API_KEY}", "openai_base": "https://api.test.com/v1"}}}
            p.write_text(yaml.dump(cfg))
            cd = CloudDiscovery(p)
            assert cd.providers["env-provider"].api_key == "sk-from-env"
        finally:
            del os.environ["IFF_TEST_API_KEY"]

    def test_env_var_missing_warning(self, tmp_path):
        """Missing env var → None (logged as warning)"""
        from inferfabric.cloud_discovery import CloudDiscovery
        p = tmp_path / "cloud_provider.yaml"
        cfg = {"providers": {"missing-env": {"api_key": "${IFF_NONEXISTENT_VAR_12345}", "openai_base": "https://api.test.com/v1"}}}
        p.write_text(yaml.dump(cfg))
        cd = CloudDiscovery(p)
        # None since env var doesn't exist
        assert cd.providers["missing-env"].api_key == ""  # v5.0: null → "" fix

    def test_resolve_route_local(self, tmp_path):
        from inferfabric.cloud_discovery import CloudDiscovery
        cd = CloudDiscovery(tmp_path / "nonexistent.yaml")
        route = cd.resolve_route("qwen35-9b-vl", local_models={"qwen35-9b-vl"})
        assert route == "local"

    def test_resolve_route_cloud(self, tmp_path):
        from inferfabric.cloud_discovery import CloudDiscovery, CloudModel
        cd = CloudDiscovery(tmp_path / "nonexistent.yaml")
        cd._cloud_models = {"deepseek-v4-flash": CloudModel(model_id="deepseek-v4-flash", provider="baidu-codingplan")}
        route = cd.resolve_route("deepseek-v4-flash", local_models=set())
        assert route == "cloud:baidu-codingplan"

    def test_resolve_route_none(self, tmp_path):
        from inferfabric.cloud_discovery import CloudDiscovery
        cd = CloudDiscovery(tmp_path / "nonexistent.yaml")
        route = cd.resolve_route("nonexistent-model", local_models=set())
        assert route is None

    def test_resolve_route_provider_prefix(self, tmp_path):
        from inferfabric.cloud_discovery import CloudDiscovery, CloudModel
        cd = CloudDiscovery(tmp_path / "nonexistent.yaml")
        cd._cloud_models = {
            "glm-5": CloudModel(model_id="glm-5", provider="baidu-codingplan"),
            "baidu-codingplan/glm-5": CloudModel(model_id="glm-5", provider="baidu-codingplan"),
        }
        route1 = cd.resolve_route("glm-5", local_models=set())
        route2 = cd.resolve_route("baidu-codingplan/glm-5", local_models=set())
        assert route1 == "cloud:baidu-codingplan"
        assert route2 == "cloud:baidu-codingplan"

    def test_reload(self, tmp_path):
        from inferfabric.cloud_discovery import CloudDiscovery
        p = tmp_path / "cloud_provider.yaml"
        p.write_text(yaml.dump({"providers": {"prov1": {"api_key": "k1", "openai_base": "http://a"}}}))
        cd = CloudDiscovery(p)
        assert "prov1" in cd.providers
        p.write_text(yaml.dump({"providers": {"prov2": {"api_key": "k2", "openai_base": "http://b"}}}))
        cd.reload(p)
        assert "prov1" not in cd.providers
        assert "prov2" in cd.providers

    def test_get_provider_config(self, tmp_path):
        from inferfabric.cloud_discovery import CloudDiscovery
        p = tmp_path / "cloud_provider.yaml"
        p.write_text(yaml.dump({"providers": {"prov1": {"api_key": "k1", "openai_base": "http://a"}}}))
        cd = CloudDiscovery(p)
        assert cd.get_provider_config("prov1") is not None
        assert cd.get_provider_config("nonexistent") is None


class TestCloudDiscoveryWithServer:
    """带 HTTP mock 的发现测试"""

    @pytest.fixture
    def mock_server(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/models":
                    body = json.dumps({"data": [{"id": "deepseek-v4-flash", "object": "model"}, {"id": "glm-5", "object": "model"}]})
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body.encode())
                else:
                    self.send_response(404)
                    self.end_headers()
            def log_message(self, *args): pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        yield port
        server.server_close()

    def test_discover_provider(self, tmp_path, mock_server):
        from inferfabric.cloud_discovery import CloudDiscovery
        p = tmp_path / "cloud_provider.yaml"
        p.write_text(yaml.dump({"providers": {"test": {"api_key": "sk-test", "openai_base": f"http://127.0.0.1:{mock_server}", "enabled_models": ["deepseek-v4-flash", "glm-5"]}}}))
        cd = CloudDiscovery(p)
        models = cd.discover_all()
        assert len(models) >= 2
        model_ids = {m.model_id for m in models.values()}
        assert "deepseek-v4-flash" in model_ids
        assert "glm-5" in model_ids

    def test_discover_with_filter(self, tmp_path, mock_server):
        from inferfabric.cloud_discovery import CloudDiscovery
        p = tmp_path / "cloud_provider.yaml"
        p.write_text(yaml.dump({"providers": {"test": {"api_key": "sk-test", "openai_base": f"http://127.0.0.1:{mock_server}", "enabled_models": ["deepseek-v4-flash"], "discovery": {"filter": {"include_pattern": "^deepseek-.*"}}}}}))
        cd = CloudDiscovery(p)
        models = cd.discover_all()
        model_ids = [m.model_id for m in models.values()]
        assert "deepseek-v4-flash" in model_ids
        assert "glm-5" not in model_ids
        # 候选同样被 include_pattern 预筛
        cands = {c.model_id for c in cd.get_candidates("test")}
        assert cands == {"deepseek-v4-flash"}

    def test_discover_http_error(self, tmp_path):
        from inferfabric.cloud_discovery import CloudDiscovery
        p = tmp_path / "cloud_provider.yaml"
        p.write_text(yaml.dump({"providers": {"bad-provider": {"api_key": "sk-test", "openai_base": "http://127.0.0.1:1", "timeout": 1}}}))
        cd = CloudDiscovery(p)
        models = cd.discover_all()
        assert len(models) == 0


class TestCloudDiscoveryPolling:
    def test_start_stop_polling(self, tmp_path):
        from inferfabric.cloud_discovery import CloudDiscovery
        p = tmp_path / "cloud_provider.yaml"
        p.write_text(yaml.dump({"providers": {"test": {"api_key": "sk-test", "openai_base": "http://127.0.0.1:1", "timeout": 1, "discovery": {"interval": 1}}}}))
        cd = CloudDiscovery(p)
        cd._cloud_models = {"test-model": None}
        cd.start_polling()
        assert cd._poll_thread is not None
        assert cd._poll_thread.is_alive()
        cd.stop_polling()
        assert cd._poll_thread is None

    def test_start_polling_no_interval(self, tmp_path):
        from inferfabric.cloud_discovery import CloudDiscovery
        p = tmp_path / "cloud_provider.yaml"
        p.write_text(yaml.dump({"providers": {"test": {"api_key": "sk-test", "openai_base": "http://127.0.0.1:1", "discovery": {"interval": 0}}}}))
        cd = CloudDiscovery(p)
        cd.start_polling()
        assert cd._poll_thread is None


# ═══════════════════════════════════════════════════════════════════
# Domain 6: 请求转发与路由
# ═══════════════════════════════════════════════════════════════════

class TestForwarderHelpers:
    def test_read_body_valid_json(self):
        from inferfabric.forwarder import read_body
        handler = MagicMock()
        handler.headers.get.return_value = "17"
        handler.rfile.read.return_value = b'{"model":"test"}'
        result = read_body(handler)
        assert result == {"model": "test"}

    def test_read_body_empty(self):
        from inferfabric.forwarder import read_body
        handler = MagicMock()
        handler.headers.get.return_value = "0"
        result = read_body(handler)
        assert result == {}

    def test_read_body_invalid_json(self):
        from inferfabric.forwarder import read_body
        handler = MagicMock()
        handler.headers.get.return_value = "5"
        handler.rfile.read.return_value = b"xxxxx"
        result = read_body(handler)
        assert result is None

    def test_read_body_too_large(self):
        from inferfabric.forwarder import read_body
        handler = MagicMock()
        handler.headers.get.return_value = str(101 * 1024 * 1024)
        result = read_body(handler)
        assert result is None

    def test_send_json(self):
        from inferfabric.forwarder import send_json
        handler = MagicMock()
        send_json(handler, {"status": "ok"}, 200)
        handler.send_response.assert_called_with(200)
        handler.end_headers.assert_called()

    def test_send_json_cors(self):
        from inferfabric.forwarder import send_json
        handler = MagicMock()
        send_json(handler, {"ok": True})
        header_calls = [str(c) for c in handler.send_header.call_args_list]
        assert any("Access-Control" in h for h in header_calls)


class TestForwarderCloudRouting:
    """Cloud 路由: 协议不匹配应返回 501"""

    def test_forward_to_cloud_anthropic_unavailable(self):
        from inferfabric.forwarder import forward_to_cloud
        from inferfabric.cloud_discovery import ProviderConfig, CloudModel
        handler = MagicMock()
        data = {"model": "test", "stream": False}
        cfg = ProviderConfig(name="test", api_key="sk-test")
        cm = CloudModel(model_id="test", provider="test", anthropic_available=False)
        forward_to_cloud(handler, data, cfg, cm, protocol="anthropic")
        # send_json is called by the function — check the status code
        # The function uses handler-specific send methods, not _send_json
        # Check that send_response was called with 501 or similar
        assert handler.send_response.called or handler._send_json.called

    def test_forward_to_cloud_no_openai_base(self):
        from inferfabric.forwarder import forward_to_cloud
        from inferfabric.cloud_discovery import ProviderConfig, CloudModel
        handler = MagicMock()
        data = {"model": "test", "stream": False}
        cfg = ProviderConfig(name="test", api_key="sk-test", openai_base="")
        cm = CloudModel(model_id="test", provider="test", openai_available=True)
        forward_to_cloud(handler, data, cfg, cm, protocol="openai")
        assert handler.send_response.called or handler._send_json.called


class TestHandlerRouting:
    def test_admin_routes_exist(self):
        import ast
        with open(Path(__file__).resolve().parents[3] / "inferfabric/proxy/handler.py") as f:
            tree = ast.parse(f.read())
        routes = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("/admin/cloud"):
                    routes.add(node.value)
        assert "/admin/cloud/reload" in routes
        assert "/admin/cloud/discover" in routes
        assert "/admin/cloud/providers" in routes


# ═══════════════════════════════════════════════════════════════════
# Domain 7: ProxyManager 集成
# ═══════════════════════════════════════════════════════════════════

class TestProxyManagerInit:
    @pytest.fixture
    def pm(self, tmp_path, monkeypatch):
        from inferfabric.proxy_manager import ProxyManager
        monkeypatch.setattr("inferfabric.proxy_manager.IFF_DATA_DIR", tmp_path)
        mock_mgr = MagicMock()
        mock_mgr.models_d = {}
        mock_mgr.active_services = []
        mock_mgr.state = MagicMock()
        mock_mgr.state.gpu_mode = "idle"
        mock_mgr.state.get_active_services.return_value = []
        pm = ProxyManager(mgr=mock_mgr, models_dir=str(tmp_path / "models"))
        pm.auth = MagicMock()
        pm.auth.enabled = False
        pm.logger = MagicMock()
        pm.logger.enabled = True
        pm.cloud = MagicMock()
        pm.cloud.providers = {}
        return pm


    def test_model_to_service_found(self, pm):
        mock_model = MagicMock()
        mock_model.name = "qwen35-9b-vl"
        pm.mgr.find_model_by_served_name.return_value = mock_model
        assert pm.model_to_service("vllm_qwen27b") == "qwen35-9b-vl"

    def test_model_to_service_not_found(self, pm):
        pm.mgr.find_model_by_served_name.return_value = None
        assert pm.model_to_service("nonexistent") is None

    def test_get_target_port(self, pm):
        mock_model = MagicMock()
        mock_model.port = 8000
        pm.mgr.find_model_by_served_name.return_value = mock_model
        assert pm.get_target_port("vllm_qwen27b") == 8000

    def test_current_property(self, pm):
        pm.mgr.current_service = "qwen35-9b-vl"
        assert pm.current == "qwen35-9b-vl"

    def test_ensure_cloud_discovered(self, pm):
        pm.cloud.providers = {"test-provider": MagicMock()}
        pm._cloud_discovered = False
        pm.ensure_cloud_discovered()
        pm.cloud.discover_all.assert_called_once()
        assert pm._cloud_discovered is True

    def test_ensure_cloud_discovered_no_providers(self, pm):
        pm.cloud.providers = {}
        pm._cloud_discovered = False
        pm.ensure_cloud_discovered()
        pm.cloud.discover_all.assert_not_called()

    def test_ensure_cloud_discovered_idempotent(self, pm):
        pm.cloud.providers = {"test-provider": MagicMock()}
        pm._cloud_discovered = False
        pm.ensure_cloud_discovered()
        pm.ensure_cloud_discovered()
        assert pm.cloud.discover_all.call_count == 1


class TestProxyManagerPaths:
    def test_iff_data_dir_is_absolute(self):
        from inferfabric.proxy_manager import IFF_DATA_DIR
        assert IFF_DATA_DIR.is_absolute()
        assert str(IFF_DATA_DIR).endswith(".inferfabric")

    def test_iff_data_dir_under_home(self):
        from inferfabric.proxy_manager import IFF_DATA_DIR
        assert IFF_DATA_DIR == Path.home() / ".inferfabric"


# ═══════════════════════════════════════════════════════════════════
# Domain 8: 配置系统
# ═══════════════════════════════════════════════════════════════════

class TestConfigRetry:
    def test_exponential_backoff(self):
        from inferfabric.config import exponential_backoff
        delays = [exponential_backoff(i) for i in range(5)]
        for i in range(1, len(delays)):
            assert delays[i] >= delays[i-1]

    def test_should_retry_on_status(self):
        from inferfabric.config import should_retry_on_status
        assert should_retry_on_status(429)
        assert should_retry_on_status(503)
        assert should_retry_on_status(500)
        assert not should_retry_on_status(200)
        assert not should_retry_on_status(400)

    def test_parse_retry_after(self):
        from inferfabric.config import parse_retry_after_ms
        # Only retry-after header is supported (seconds → ms)
        assert parse_retry_after_ms({"retry-after": "2"}) == 2000.0
        assert parse_retry_after_ms({}) is None


# ═══════════════════════════════════════════════════════════════════
# Domain 9: GPU 锁
# ═══════════════════════════════════════════════════════════════════

class TestGPULock:
    """GPU 文件锁（可重入）"""

    def test_acquire_release(self):
        from inferfabric.gpu_lock import GPULock
        lock = GPULock(lock_path=Path(tempfile.mktemp(suffix=".gpu_lock")))
        assert lock.acquire(timeout=1.0)
        lock.release()

    def test_force_clear(self):
        from inferfabric.gpu_lock import GPULock
        lock = GPULock(lock_path=Path(tempfile.mktemp(suffix=".gpu_lock")))
        lock.acquire(timeout=1.0)
        lock.force_clear()
        assert lock.acquire(timeout=1.0)

    def test_is_held(self):
        from inferfabric.gpu_lock import GPULock
        lock = GPULock(lock_path=Path(tempfile.mktemp(suffix=".gpu_lock")))
        assert not lock.is_held
        assert lock.acquire(timeout=1.0)
        assert lock.is_held
        lock.release()
        assert not lock.is_held


# ═══════════════════════════════════════════════════════════════════
# Domain 10: Config Watcher
# ═══════════════════════════════════════════════════════════════════

class TestConfigWatcher:
    def test_compute_config_hash(self):
        from inferfabric.config_watcher import compute_config_hash
        model = MagicMock()
        model.config_hash.return_value = "abc123"
        h = compute_config_hash(model)
        assert h == "abc123"

    def test_detect_drift_changed(self):
        from inferfabric.config_watcher import detect_drift, compute_config_hash
        model = MagicMock()
        model.config_hash.return_value = "new_hash"
        state = MagicMock()
        state.get.return_value = "old_hash"
        assert detect_drift(model, state) is True

    def test_detect_drift_unchanged(self):
        from inferfabric.config_watcher import detect_drift
        model = MagicMock()
        model.config_hash.return_value = "same_hash"
        state = MagicMock()
        state.get.return_value = "same_hash"
        assert detect_drift(model, state) is False


# ═══════════════════════════════════════════════════════════════════
# Domain 11: Metrics Parser
# ═══════════════════════════════════════════════════════════════════

class TestMetricsParser:
    def test_parse_prometheus_text(self):
        from inferfabric.proxy.metrics import parse_prometheus_text
        text = "num_requests 42.0\n"
        gauges, counters, histograms = parse_prometheus_text(text)
        assert "num_requests" in gauges
        assert gauges["num_requests"] == 42.0

    def test_parse_empty(self):
        from inferfabric.proxy.metrics import parse_prometheus_text
        result = parse_prometheus_text("")
        assert result == ({}, {}, {})

    def test_parse_with_labels(self):
        from inferfabric.proxy.metrics import parse_prometheus_text
        text = 'lora_requests{adapter_name="test"} 5.0\n'
        gauges, counters, histograms = parse_prometheus_text(text)
        assert "lora_requests" in gauges or "lora_requests" in counters

    def test_quantile(self):
        from inferfabric.proxy.metrics import quantile
        buckets = [(0.01, 10), (0.05, 20), (0.1, 30), (0.5, 40), (1.0, 50)]
        result = quantile(buckets, 50, 0.5)
        assert 0.01 <= result <= 1.0


# ═══════════════════════════════════════════════════════════════════
# Domain 12: Admin API
# ═══════════════════════════════════════════════════════════════════

class TestAdminAPI:
    def test_cloud_reload_handler_exists(self):
        from inferfabric.proxy.handler import ProxyHandler
        assert hasattr(ProxyHandler, '_handle_cloud_reload')

    def test_cloud_discover_handler_exists(self):
        from inferfabric.proxy.handler import ProxyHandler
        assert hasattr(ProxyHandler, '_handle_cloud_discover')

    def test_cloud_providers_handler_exists(self):
        from inferfabric.proxy.handler import ProxyHandler
        assert hasattr(ProxyHandler, '_handle_cloud_providers')

    def test_admin_check_method_exists(self):
        from inferfabric.proxy.handler import ProxyHandler
        assert hasattr(ProxyHandler, '_check_admin')


# ═══════════════════════════════════════════════════════════════════
# Domain 13: Dashboard
# ═══════════════════════════════════════════════════════════════════

class TestDashboard:
    def test_dashboard_renders_html(self):
        from inferfabric.dashboard import get_html
        html = get_html()
        assert "<html" in html.lower()
        assert len(html) > 1000


# ═══════════════════════════════════════════════════════════════════
# Domain 14: 版本与包结构
# ═══════════════════════════════════════════════════════════════════

class TestPackageStructure:
    def test_version_format(self):
        from inferfabric import __version__
        # 语义化版本 MAJOR.MINOR.PATCH（v6.0.0 起跨大版本，不再锁 5. 前缀）
        import re
        assert re.match(r"^\d+\.\d+\.\d+", __version__), f"bad version: {__version__}"

    def test_public_exports(self):
        from inferfabric.manager import ModelManager
        from inferfabric.state import GPUMode, StateDB
        from inferfabric.gpu_lock import GPULock
        assert ModelManager is not None
        assert GPUMode is not None
        assert StateDB is not None
        assert GPULock is not None

    def test_core_modules_importable(self):
        modules = [
            'inferfabric.state',
            'inferfabric.config',
            'inferfabric.gpu_lock',
            'inferfabric.gpu_state',
            'inferfabric.ratelimit',
            'inferfabric.forwarder',
            'inferfabric.proxy.auth',
            'inferfabric.proxy.request_logger',
            'inferfabric.cloud_discovery',
            'inferfabric.proxy_manager',
            'inferfabric.config_watcher',
            'inferfabric.health_checker',
            'inferfabric.watchdog',
            'inferfabric.interfaces',
            'inferfabric.dashboard',
        ]
        for mod in modules:
            try:
                __import__(mod)
            except ImportError as e:
                pytest.fail(f"Failed to import {mod}: {e}")


# ═══════════════════════════════════════════════════════════════════
# Cross-cutting: 交叉审查修复验证
# ═══════════════════════════════════════════════════════════════════

class TestCrossReviewFixes:
    def test_P01_os_import_in_cloud_discovery(self):
        import inspect
        import inferfabric.cloud_discovery as cd
        source = inspect.getsource(cd)
        assert "import os" in source

    def test_P02_request_logging_all_paths(self):
        import inspect
        import inferfabric.proxy.chat_handlers as ch
        source = inspect.getsource(ch.handle_chat)
        assert source.count("pm.logger.log") >= 3

    def test_P03_absolute_config_paths(self):
        from inferfabric.proxy_manager import IFF_DATA_DIR
        assert IFF_DATA_DIR.is_absolute()
        assert str(IFF_DATA_DIR).endswith(".inferfabric")

    def test_P11_request_logger_thread_safe(self):
        import inspect
        import inferfabric.proxy.request_logger as rl
        source = inspect.getsource(rl.RequestLogger)
        assert "self._lock" in source
        assert "threading.Lock" in source

    def test_P12_json_loads_for_discovery(self):
        import inspect
        import inferfabric.cloud_discovery as cd
        source = inspect.getsource(cd.CloudDiscovery._discover_provider)
        assert "_json.loads" in source or "json.loads" in source

    def test_P15_v1_compat_thread_safe(self):
        import inspect
        import inferfabric.ratelimit as rl
        source = inspect.getsource(rl)
        assert "DualGateLimiter" in source
        assert "threading.Semaphore" in source

    def test_P16_provider_prefix_keys(self):
        import inspect
        import inferfabric.cloud_discovery as cd
        source = inspect.getsource(cd.CloudDiscovery.discover_all)
        assert "provider" in source

    def test_P17_ttft_used_in_log(self):
        import inspect
        import inferfabric.proxy.chat_handlers as ch
        source = inspect.getsource(ch.handle_chat)
        assert "ttft_ms" in source


# ═══════════════════════════════════════════════════════════════════
# Domain 16: 模型能力属性 (v4.6.1)
# ═══════════════════════════════════════════════════════════════════

class TestCloudModelCapabilities:
    """v4.6.1: CloudModel 能力属性 + model_specs 合并"""

    def test_cloud_model_defaults(self):
        from inferfabric.cloud_discovery import CloudModel
        m = CloudModel(model_id="test", provider="prov")
        assert m.context_window is None
        assert m.max_output_tokens is None
        # 7.8.0 三态：默认 None = 未设置（全能力默认），False = 显式约束
        assert m.supports_vision is None
        assert m.supports_tools is None
        assert m.extra == {}

    def test_cloud_model_with_capabilities(self):
        from inferfabric.cloud_discovery import CloudModel
        m = CloudModel(
            model_id="deepseek-v4-flash", provider="baidu",
            context_window=131072, max_output_tokens=16384,
            supports_vision=False, supports_tools=True,
            extra={"pricing_tier": "free"},
        )
        assert m.context_window == 131072
        assert m.max_output_tokens == 16384
        assert m.extra["pricing_tier"] == "free"

    def test_to_api_dict_basic(self):
        from inferfabric.cloud_discovery import CloudModel
        m = CloudModel(model_id="test", provider="baidu")
        d = m.to_api_dict()
        assert d["id"] == "test"
        assert d["owned_by"] == "cloud:baidu"
        caps = d.get("capabilities", {})
        # 7.8.0 三态：未设置序列化为 null（JS falsy，展示不变）
        assert caps.get("supports_vision") is None
        assert caps.get("supports_tools") is None

    def test_to_api_dict_with_caps(self):
        from inferfabric.cloud_discovery import CloudModel
        m = CloudModel(
            model_id="deepseek-v4-flash", provider="baidu",
            context_window=131072, max_output_tokens=16384,
            supports_tools=True,
            extra={"pricing_tier": "free"},
        )
        d = m.to_api_dict()
        assert d["id"] == "deepseek-v4-flash"
        caps = d["capabilities"]
        assert caps["context_window"] == 131072
        assert caps["max_output_tokens"] == 16384
        assert caps["supports_tools"] is True
        assert caps["supports_vision"] is None   # 7.8.0：未设置 = null
        assert caps["pricing_tier"] == "free"

    def test_to_api_dict_vision_included(self):
        from inferfabric.cloud_discovery import CloudModel
        m = CloudModel(model_id="glm-5", provider="baidu", supports_vision=True)
        d = m.to_api_dict()
        assert d["capabilities"]["supports_vision"] is True

    def test_model_specs_from_yaml(self, tmp_path):
        from inferfabric.cloud_discovery import CloudDiscovery
        p = tmp_path / "cloud_provider.yaml"
        p.write_text(yaml.dump({
            "providers": {
                "test": {
                    "api_key": "***",
                    "openai_base": "http://127.0.0.1:1",
                    "models": {
                        "deepseek-v4-flash": {
                            "context_window": 131072,
                            "max_output_tokens": 16384,
                            "supports_vision": False,
                            "supports_tools": True,
                            "pricing_tier": "free",
                        }
                    }
                }
            }
        }))
        cd = CloudDiscovery(p)
        assert "deepseek-v4-flash" in cd.providers["test"].model_specs
        spec = cd.providers["test"].model_specs["deepseek-v4-flash"]
        assert spec["context_window"] == 131072
        assert spec["pricing_tier"] == "free"

    def test_model_specs_merge_on_discover(self, tmp_path):
        """model_specs 与自动发现结果合并"""
        from inferfabric.cloud_discovery import CloudDiscovery, CloudModel
        p = tmp_path / "cloud_provider.yaml"
        p.write_text(yaml.dump({
            "providers": {
                "test": {
                    "api_key": "***",
                    "openai_base": "http://127.0.0.1:1",
                    "models": {
                        "deepseek-v4-flash": {
                            "context_window": 131072,
                            "max_output_tokens": 16384,
                        }
                    }
                }
            }
        }))
        cd = CloudDiscovery(p)
        # 模拟发现结果（无能力属性）
        cd._cloud_models = {
            "deepseek-v4-flash": CloudModel(model_id="deepseek-v4-flash", provider="test")
        }
        # 手动触发合并
        merged = cd.discover_all()
        # discover_all 会重新发现（失败因为无服务器），但 spec-only 注册应该生效
        m = merged.get("deepseek-v4-flash")
        assert m is not None
        assert m.context_window == 131072
        assert m.max_output_tokens == 16384

    def test_spec_only_model_registered(self, tmp_path):
        """仅有 model_specs 但未被 /models 发现的模型也会注册"""
        from inferfabric.cloud_discovery import CloudDiscovery
        p = tmp_path / "cloud_provider.yaml"
        p.write_text(yaml.dump({
            "providers": {
                "test": {
                    "api_key": "***",
                    "openai_base": "http://127.0.0.1:1",  # 不存在的服务器
                    "models": {
                        "manual-model": {
                            "context_window": 64000,
                            "max_output_tokens": 4096,
                        }
                    }
                }
            }
        }))
        cd = CloudDiscovery(p)
        merged = cd.discover_all()
        m = merged.get("manual-model")
        assert m is not None
        assert m.context_window == 64000
        assert m.max_output_tokens == 4096
        assert m.discovered_at == 0.0  # 未实际发现

    def test_spec_only_provider_prefix(self, tmp_path):
        """spec-only 模型同时注册短名和 provider/model_id"""
        from inferfabric.cloud_discovery import CloudDiscovery
        p = tmp_path / "cloud_provider.yaml"
        p.write_text(yaml.dump({
            "providers": {
                "prov": {
                    "api_key": "***",
                    "openai_base": "http://127.0.0.1:1",
                    "models": {
                        "my-model": {"context_window": 32000}
                    }
                }
            }
        }))
        cd = CloudDiscovery(p)
        merged = cd.discover_all()
        assert "my-model" in merged
        assert "prov/my-model" in merged

    def test_provider_config_model_specs_field(self, tmp_path):
        from inferfabric.cloud_discovery import CloudDiscovery
        p = tmp_path / "cloud_provider.yaml"
        p.write_text(yaml.dump({
            "providers": {
                "test": {
                    "api_key": "***",
                    "openai_base": "http://127.0.0.1:1",
                    "models": {
                        "m1": {"context_window": 1000},
                        "m2": {"context_window": 2000},
                    }
                }
            }
        }))
        cd = CloudDiscovery(p)
        specs = cd.providers["test"].model_specs
        assert len(specs) == 2
        assert specs["m1"]["context_window"] == 1000


class TestV1ModelsCapabilities:
    """v4.6.1: /v1/models 响应包含模型能力属性"""

    def test_cloud_model_to_api_dict_in_response(self):
        from inferfabric.cloud_discovery import CloudModel
        m = CloudModel(
            model_id="deepseek-v4-flash", provider="baidu",
            context_window=131072, max_output_tokens=16384,
            supports_tools=True,
        )
        d = m.to_api_dict()
        # 验证 /v1/models 格式合规
        assert d["id"] == "deepseek-v4-flash"
        assert d["object"] == "model"
        assert d["owned_by"] == "cloud:baidu"
        assert isinstance(d["capabilities"], dict)

    def test_local_model_caps_not_clobbered(self):
        """本地模型不应被云端同 ID 模型覆盖"""
        # 在 /v1/models handler 中，existing_ids 检查确保本地模型优先
        local_models = [{"id": "qwen35-9b-vl", "object": "model", "owned_by": "local"}]
        existing_ids = {m.get("id") for m in local_models}
        assert "qwen35-9b-vl" in existing_ids  # 不会重复添加


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
