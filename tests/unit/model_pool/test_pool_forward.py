"""model_pool.pool_forward — P2 预算内优先级故障回退（spec §6.2–§6.4）。

核心不变量：
- 固定错误动作表：连接失败→下一候选；429→预算内等待重试原目标，超预算切候选；
  5xx 未提交→重试原目标 1 次再切；401/403→端点冷却+切候选；
  400/404/422 等确定性 4xx→终止不回退；已提交任何字节→永不重试/切换（§6.4）
- 统一预算：max_attempts 计所有真实上游调用，deadline 覆盖等待（§6.3）
- suppress_client_errors=True 的传输边界不发客户端错误、不内层重试；
  默认 False 时 forwarder 现行为逐字节不变（既有 forwarder 测试证明）
"""

from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError as _UrllibHTTPError

import pytest

import inferfabric.model_pool.dispatch as dispatch
from inferfabric.model_pool.pool_forward import (
    AttemptResult,
    CooldownStore,
    run_attempts,
)
from inferfabric.model_pool.registry import PoolRegistry
from inferfabric.model_pool.selector import (
    RequestRequirements,
    RuntimeView,
    select,
)

from tests.unit.model_pool.test_dispatch_openai import (
    FakePM,
    FakeHandler,
    _local_model,
    _raw_config,
)


# ── run_attempts 单元层 fakes（无网络、无时间依赖） ──

def _cand(cid):
    from inferfabric.model_pool.config import CandidateConfig
    from inferfabric.model_pool.registry import ResolvedCandidate
    return ResolvedCandidate(
        candidate=CandidateConfig(id=cid, priority=1, kind="cloud",
                                  provider="prov", model_id="m",
                                  declared_protocols=("openai",)),
        endpoint_fingerprint=f"fp-{cid}", evidence={})


def _pool(max_attempts=3, deadline_ms=60000):
    return SimpleNamespace(max_attempts=max_attempts, deadline_ms=deadline_ms)


class Recorder:
    """按序返回预置结果，记录每次调用的候选 id。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, cand):
        self.calls.append(cand.candidate.id)
        return self.results.pop(0) if self.results else AttemptResult()


def _ok():
    return AttemptResult(ok=True, status=200, committed=True)


class TestRunAttempts:
    def test_conn_fail_falls_to_next_candidate(self):
        t = Recorder([AttemptResult(status=None, error="conn"), _ok()])
        out = run_attempts(_pool(), [_cand("c1"), _cand("c2")], t)
        assert out.ok is True
        assert t.calls == ["c1", "c2"]           # 总上游调用 = 2

    def test_budget_exhausted_stops(self):
        t = Recorder([AttemptResult(status=None, error="conn")])
        out = run_attempts(_pool(max_attempts=1), [_cand("c1"), _cand("c2")], t)
        assert out.ok is False
        assert out.committed is False
        assert out.error_code == "pool_unavailable"
        assert t.calls == ["c1"]                  # 预算耗尽不再尝试

    def test_429_waits_within_budget_and_retries_same_target(self):
        sleeps = []
        clock = iter([0.0, 0.0, 1.0, 1.0, 2.0])   # start,check,after-sleep,...
        t = Recorder([AttemptResult(status=429, retry_after=1.0), _ok()])
        out = run_attempts(_pool(deadline_ms=5000), [_cand("c1")], t,
                           monotonic=lambda: next(clock),
                           sleep=sleeps.append)
        assert out.ok is True
        assert t.calls == ["c1", "c1"]            # 同目标等待后重试
        assert sleeps == [1.0]

    def test_429_over_budget_switches_candidate(self):
        # Retry-After=10s > 剩余 deadline=3s → 无法在预算内尊重 → 切候选
        sleeps = []
        clock = iter([0.0, 0.0, 0.0, 0.0])   # start,check,remaining,再check
        t = Recorder([AttemptResult(status=429, retry_after=10.0), _ok()])
        out = run_attempts(_pool(deadline_ms=3000),
                           [_cand("c1"), _cand("c2")], t,
                           monotonic=lambda: next(clock), sleep=sleeps.append)
        assert out.ok is True
        assert t.calls == ["c1", "c2"]
        assert sleeps == []

    def test_5xx_retried_once_then_switches(self):
        t = Recorder([AttemptResult(status=500), AttemptResult(status=500), _ok()])
        out = run_attempts(_pool(), [_cand("c1"), _cand("c2")], t)
        assert out.ok is True
        assert t.calls == ["c1", "c1", "c2"]      # 原目标最多重试 1 次

    def test_committed_failure_never_switches(self):
        # 响应已提交（流中断）→ 终止，第二候选零调用（§6.4）
        t = Recorder([AttemptResult(ok=False, committed=True, status=502,
                                    error="stream broken")])
        out = run_attempts(_pool(), [_cand("c1"), _cand("c2")], t)
        assert out.ok is False
        assert out.committed is True
        assert t.calls == ["c1"]

    def test_non_fallback_400_terminates(self):
        t = Recorder([AttemptResult(status=400, error="bad request")])
        out = run_attempts(_pool(), [_cand("c1"), _cand("c2")], t)
        assert out.ok is False and out.committed is False
        assert out.status == 400
        assert t.calls == ["c1"]                  # 400 不回退

    def test_non_fallback_404_terminates(self):
        t = Recorder([AttemptResult(status=404, error="model gone")])
        out = run_attempts(_pool(), [_cand("c1"), _cand("c2")], t)
        assert out.status == 404 and t.calls == ["c1"]

    def test_401_cools_endpoint_and_switches(self):
        cooldowns = CooldownStore()
        t = Recorder([AttemptResult(status=401, error="creds"), _ok()])
        out = run_attempts(_pool(), [_cand("c1"), _cand("c2")], t,
                           cooldowns=cooldowns)
        assert out.ok is True
        assert t.calls == ["c1", "c2"]
        assert cooldowns.is_cooling("fp-c1")      # 端点进入冷却
        assert not cooldowns.is_cooling("fp-c2")


class TestCooldownStore:
    def test_expiry_restores(self):
        store = CooldownStore(ttl_s=60)
        store.add("fp", now=100.0)
        assert store.is_cooling("fp", now=159.0)
        assert not store.is_cooling("fp", now=160.0)   # 过期恢复

    def test_cooling_fingerprints_prunes_expired(self):
        store = CooldownStore(ttl_s=10)
        store.add("a", now=0.0)
        store.add("b", now=5.0)
        assert store.cooling_fingerprints(now=8.0) == frozenset({"a", "b"})
        assert store.cooling_fingerprints(now=14.0) == frozenset({"b"})
        assert store.cooling_fingerprints(now=16.0) == frozenset()


# ── selector 集成：ordered 全量有序候选 + 冷却排除 ──

@pytest.fixture
def registry(tmp_path):
    reg = PoolRegistry(
        config_path=tmp_path / "model_pools.yaml",
        evidence_path=tmp_path / "evidence.json",
        known_local={"qwen-small"},
        known_providers={"prov"},
        provider_base=lambda p: "https://base/v1",
    )
    reg.update(_raw_config(), expected_revision=0)
    return reg


class TestSelectorIntegration:
    def test_select_returns_ordered_eligible(self, registry):
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": object()})
        sel = select(registry.snapshot().pools["fast"],
                     RequestRequirements(protocol="openai"),
                     dispatch.runtime_view(pm))
        assert sel.ordered is not None
        assert [rc.candidate.id for rc in sel.ordered] == ["loc", "cld"]
        assert sel.chosen is sel.ordered[0]

    def test_cooldown_excludes_candidate(self, registry):
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": object()})
        pm.pool_cooldowns = CooldownStore()
        # 冷却本地候选（fingerprint 与 registry 解析一致）
        fp = registry.snapshot().pools["fast"].candidates[0].endpoint_fingerprint
        pm.pool_cooldowns.add(fp)
        sel = select(registry.snapshot().pools["fast"],
                     RequestRequirements(protocol="openai"),
                     dispatch.runtime_view(pm))
        reasons = {d.candidate_id: d.reason for d in sel.decisions}
        assert reasons["loc"] == "cooldown"
        assert [rc.candidate.id for rc in sel.ordered] == ["cld"]


# ── dispatch 层：本地连接失败 → 云回退；401 冷却 → 后续请求排除 ──

class TestDispatchFailover:
    def test_local_conn_fail_falls_to_cloud(self, registry, monkeypatch):
        local_calls = []

        def fake_forward(handler, pm, target_port, body, stream, model_name="",
                         upstream_path=None, cache_body=None):
            local_calls.append(target_port)
            return False                       # 连接失败，headers 未发送

        cloud_calls = []

        def fake_cloud(handler, data, provider_cfg, cloud_model, protocol="openai",
                       original_model=None, suppress_client_errors=False):
            cloud_calls.append({"model": data["model"],
                                "suppress": suppress_client_errors})
            return SimpleNamespace(status=200, usage={}, ttft_ms=None,
                                   duration_ms=1.0, error=None, committed=True)

        monkeypatch.setattr(dispatch, "_forward_request", fake_forward)
        monkeypatch.setattr(dispatch.forwarder, "forward_to_cloud", fake_cloud)
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": object()})
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        assert len(local_calls) == 1
        assert len(cloud_calls) == 1                    # 总 POST 数 = 2
        assert cloud_calls[0]["suppress"] is True       # 池路径必须抑制直发
        assert cloud_calls[0]["model"] == "cloud-m"
        assert h.sent == []                             # 成功无错误信封
        assert pm.dual_gate.released == 1

    def test_cloud_401_cooldown_excludes_next_request(self, registry, monkeypatch):
        cloud_calls = []

        def fake_cloud(handler, data, provider_cfg, cloud_model, protocol="openai",
                       original_model=None, suppress_client_errors=False):
            cloud_calls.append(data["model"])
            return SimpleNamespace(status=401, usage={}, ttft_ms=None,
                                   duration_ms=1.0,
                                   error="Cloud provider error (401): bad key",
                                   committed=False)

        monkeypatch.setattr(dispatch.forwarder, "forward_to_cloud", fake_cloud)
        # 池只留云候选 → 401 冷却后无候选可用
        cfg = _raw_config()
        cfg["pools"]["fast"]["candidates"] = [
            {"id": "cld", "priority": 1, "kind": "cloud",
             "provider": "prov", "model_id": "cloud-m",
             "declared_protocols": ["openai"]}]
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        pm = FakePM(registry, cloud_models={"prov/cloud-m": object()},
                    providers={"prov": object()})
        pm.pool_cooldowns = CooldownStore()

        h1 = FakeHandler()
        assert dispatch.handle_pool_chat(h1, pm, {"model": "iff/fast",
                                                  "messages": []}, "iff/fast") is True
        status, body, _ = h1.sent[-1]
        assert status == 503 and body["status"] == "pool_unavailable"
        assert len(cloud_calls) == 1
        assert pm.pool_cooldowns.cooling_fingerprints()   # 端点已冷却

        # 第二次请求：冷却中 → 选择期排除，不再出网
        h2 = FakeHandler()
        assert dispatch.handle_pool_chat(h2, pm, {"model": "iff/fast",
                                                  "messages": []}, "iff/fast") is True
        status, body, _ = h2.sent[-1]
        assert status == 503 and body["status"] == "no_ready_target"
        assert len(cloud_calls) == 1                       # 零新上游调用


# ── forwarder 传输边界：suppress_client_errors 语义（默认 False 行为不变
#     由 tests/unit/proxy/test_r2_cloud_retry.py 等既有测试证明） ──

class _WFile:
    def __init__(self):
        self.data = b""

    def write(self, b):
        if isinstance(b, str):
            b = b.encode()
        self.data += b

    def flush(self):
        pass


class _FakeRespHandler:
    def __init__(self):
        self.wfile = _WFile()
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.sent_headers[k] = str(v)

    def end_headers(self):
        pass


class _FakeHTTPError(_UrllibHTTPError):
    """模拟 urllib.error.HTTPError（必须子类化才能命中 except _HTTPError）。"""

    def __init__(self, code, body=b'{"error":"x"}'):
        from io import BytesIO
        super().__init__("http://fake/", code, f"HTTP {code}", {}, BytesIO(body))
        self._body = body

    def read(self):
        return self._body

    def close(self):
        pass


def _provider_cfg():
    from inferfabric.cloud_discovery import CloudModel, ProviderConfig
    cfg = ProviderConfig(name="prov", api_key="sk-x",
                         openai_base="https://example.com/v1")
    cm = CloudModel(model_id="m", provider="prov",
                    openai_available=True, anthropic_available=True)
    return cfg, cm


class TestForwarderSuppressBoundary:
    @patch("inferfabric.forwarder.urlopen")
    def test_suppress_500_no_client_write_no_inner_retry(self, mock_urlopen):
        from inferfabric.forwarder import forward_to_cloud
        cfg, cm = _provider_cfg()
        mock_urlopen.side_effect = [_FakeHTTPError(500), _FakeHTTPError(500)]
        h = _FakeRespHandler()
        result = forward_to_cloud(h, {"model": "m", "messages": []}, cfg, cm,
                                  protocol="openai",
                                  suppress_client_errors=True)
        assert result.committed is False
        assert result.status == 500
        assert mock_urlopen.call_count == 1        # 不内层重试
        assert h.status is None and h.wfile.data == b""   # 未写客户端

    @patch("inferfabric.forwarder.urlopen")
    def test_suppress_429_carries_retry_after(self, mock_urlopen):
        from inferfabric.forwarder import forward_to_cloud
        cfg, cm = _provider_cfg()
        err = _FakeHTTPError(429)
        err.headers = {"Retry-After": "7"}
        mock_urlopen.side_effect = [err]
        h = _FakeRespHandler()
        result = forward_to_cloud(h, {"model": "m", "messages": []}, cfg, cm,
                                  protocol="openai",
                                  suppress_client_errors=True)
        assert result.committed is False
        assert result.status == 429
        assert result.retry_after == 7.0
        assert h.status is None                    # 未写客户端

    @patch("inferfabric.forwarder.urlopen")
    def test_suppress_400_terminated_without_client_write(self, mock_urlopen):
        from inferfabric.forwarder import forward_to_cloud
        cfg, cm = _provider_cfg()
        mock_urlopen.side_effect = [_FakeHTTPError(400)]
        h = _FakeRespHandler()
        result = forward_to_cloud(h, {"model": "m", "messages": []}, cfg, cm,
                                  protocol="openai",
                                  suppress_client_errors=True)
        assert result.committed is False and result.status == 400
        assert h.status is None

    @patch("inferfabric.forwarder.urlopen")
    def test_default_mode_unchanged_writes_client(self, mock_urlopen):
        from inferfabric.forwarder import forward_to_cloud
        cfg, cm = _provider_cfg()
        mock_urlopen.side_effect = [_FakeHTTPError(400)]
        h = _FakeRespHandler()
        result = forward_to_cloud(h, {"model": "m", "messages": []}, cfg, cm,
                                  protocol="openai")
        assert result.status == 400                # 现行为：直发客户端
        assert h.status == 400 and h.wfile.data    # 客户端已收到错误


class _FlakyStreamResp:
    """流式响应替身：输出 chunks_before_fail 个块后上游断连（IncompleteRead）。"""

    def __init__(self, chunks_before_fail=1, status=200):
        self.status = status
        self._chunks = [b"data: c1\n\n"] * chunks_before_fail

    def getheader(self, h, default=None):
        return {"content-type": "text/event-stream"}.get(h, default)

    def read(self, n=-1):
        if self._chunks:
            return self._chunks.pop(0)
        from http.client import IncompleteRead
        raise IncompleteRead(b"", None)

    def close(self):
        pass


class TestCommittedStreamBoundary:
    """安全审查 2026-10-23 #2：已提交（headers/首块已写）后上游中断，
    必须报告 committed — 池层据此不重放、不发第二信封（spec §6.4）。"""

    @pytest.mark.parametrize("chunks", [0, 1])   # headers 后断 / 首块后断
    @patch("inferfabric.forwarder.urlopen")
    def test_cloud_suppress_midstream_fail_is_committed(self, mock_urlopen, chunks):
        from inferfabric.forwarder import forward_to_cloud
        cfg, cm = _provider_cfg()
        mock_urlopen.return_value = _FlakyStreamResp(chunks_before_fail=chunks)
        h = _FakeRespHandler()
        result = forward_to_cloud(
            h, {"model": "m", "messages": [], "stream": True}, cfg, cm,
            protocol="openai", suppress_client_errors=True)
        assert result.committed is True           # 已输出 → 不可重放
        assert mock_urlopen.call_count == 1       # 无第二次上游调用
        assert h.status == 200                    # 客户端已收到响应头

    @pytest.mark.parametrize("chunks", [0, 1])
    @patch("inferfabric.forwarder.HTTPConnection")
    def test_anthropic_local_suppress_midstream_fail_is_committed(self,
                                                                  mock_conn, chunks):
        from inferfabric.forwarder import forward_anthropic_local
        conn = mock_conn.return_value
        conn.getresponse.return_value = _FlakyStreamResp(chunks_before_fail=chunks)
        h = _FakeRespHandler()
        pm = SimpleNamespace(response_cache=None)
        model_obj = SimpleNamespace(name="m", served_name="s", port=8101)
        status = forward_anthropic_local(
            h, pm, {"model": "x", "messages": [], "stream": True},
            "key", model_obj, "iff/fast", suppress_client_errors=True)
        assert status == 200                      # committed 信号（None = 可重放）
        assert h.status == 200

    @patch("inferfabric.forwarder.urlopen")
    def test_cloud_committed_stream_error_is_fixed_code(self, mock_urlopen, caplog):
        """安全审查 2026-10-23 #3：已提交流中断的 error/log 不插值 str(e)。

        str(e) 可携带上游响应片段/内部细节 → 固定错误码 + 日志只记异常类名。
        """
        from inferfabric.forwarder import forward_to_cloud
        cfg, cm = _provider_cfg()
        mock_urlopen.return_value = _FlakyStreamResp(chunks_before_fail=1)
        h = _FakeRespHandler()
        with caplog.at_level("ERROR", logger="inferfabric.forwarder"):
            result = forward_to_cloud(
                h, {"model": "m", "messages": [], "stream": True}, cfg, cm,
                protocol="openai", suppress_client_errors=True)
        assert result.committed is True
        assert result.error == "upstream_stream_failed_after_commit"   # 固定码
        assert "IncompleteRead" not in result.error                    # 无异常细节
        err_msgs = [r.getMessage() for r in caplog.records
                    if r.levelname == "ERROR"]
        assert err_msgs, "expected an ERROR log for committed stream failure"
        joined = " ".join(err_msgs)
        assert "IncompleteRead" in joined        # 异常类名（安全元数据）在
        assert "bytes read" not in joined        # str(e) 细节不在

    def test_run_attempts_does_not_replay_committed_stream(self):
        # 端到端不变量：committed 流中断 → 第二候选零调用
        t = Recorder([AttemptResult(ok=False, committed=True, status=200,
                                    error="stream truncated")])
        out = run_attempts(_pool(), [_cand("c1"), _cand("c2")], t)
        assert out.committed is True and out.ok is False
        assert t.calls == ["c1"]
