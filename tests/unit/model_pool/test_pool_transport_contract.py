"""池传输契约 — PLAN-2026-11-15 v2 Task 3（P1）。

契约（计划 Interfaces）：
- 布尔"发送过响应"≠ ok：非 2xx 保真（ok=False committed=True），未提交
  才回退；已提交不写第二个错误信封。
- 流结束分类：completed / missing_terminal / error_event / read_error /
  client_disconnect；中断不记成功、不伪造 usage。
- Mac 双协议：llama_cpp anthropic_native 模型声明 anthropic（原生透传）；
  未声明不悄悄改选云端由声明准确性保证。
- 直连/池观测同源：attempt 计时独立于先前失败/等待；usage 缺失不伪造。
  （注：pool_summary 摘要逐字段回读属池观测体系，随 pick/pool-summary-
  observability 批次单独摘取，见 CHERRY-PICK-MANIFEST §5 条目 12。）
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from inferfabric.model_pool import dispatch
from inferfabric.model_pool.config import CandidateConfig
from inferfabric.model_pool.pool_forward import (
    AttemptResult,
    run_attempts,
)
from inferfabric.model_pool.selector import RequestRequirements, select
from inferfabric.model_pool.registry import PoolSnapshot, RegistrySnapshot, ResolvedCandidate
from inferfabric.proxy.chat_handlers import _forward_request_ex


# ── 伪上游 HTTP 服务 ──

class _FakeUpstream(BaseHTTPRequestHandler):
    scenario = {}  # 类属性：测试注入

    def log_message(self, *a):
        pass

    def do_POST(self):
        sc = type(self).scenario
        status = sc.get("status", 200)
        if sc.get("abort_read"):
            self.close_connection = True
            return
        extra = sc.get("extra_headers", {})
        if sc.get("stream"):
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            for chunk in sc.get("chunks", []):
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
                if sc.get("close_mid") and chunk is sc["chunks"][-1]:
                    # 读完最后给定 chunk 后直接断连，不写 0 终止块
                    self.close_connection = True
                    return
            self.wfile.write(b"0\r\n\r\n")
        else:
            body = json.dumps(sc.get("body", {
                "choices": [{"message": {"content": "hi"}}],
                "usage": sc.get("usage", {"prompt_tokens": 3,
                                          "completion_tokens": 5}),
            })).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)


@pytest.fixture()
def fake_upstream():
    server = HTTPServer(("127.0.0.1", 0), _FakeUpstream)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield port
    server.shutdown()


def set_scenario(**kw):
    _FakeUpstream.scenario = kw


# ── 伪 handler / pm ──

class FakeHandler:
    def __init__(self):
        self.headers = {"Authorization": "Bearer x"}
        self.status_line = None
        self.out_headers = []
        self.written = b""
        self.ended = False
        self._req_start = time.monotonic()
        self._usage = {}
        self._ttft_ms = None
        self._client_gone = False

    def send_response(self, code):
        self.status_line = code

    def send_header(self, k, v):
        self.out_headers.append((k, v))

    def end_headers(self):
        self.ended = True

    def _safe_write(self, data):
        if self._client_gone:
            self._client_gone = True  # 生产 _safe_write 同款：失败置位
            return
        self.written += data


def make_pm(port, active=("qwen3-4b",)):
    gates = SimpleNamespace(
        acquire=lambda model, timeout=None: SimpleNamespace(ok=True, reason=""),
        acquire_target=lambda name, timeout=None: SimpleNamespace(
            ok=True, reason="", release=lambda: None),
        release=lambda: None)
    model_obj = SimpleNamespace(name="qwen3-4b", served_name="qwen3-4b",
                                type="llama_cpp", port=port)
    return SimpleNamespace(
        mgr=SimpleNamespace(active_services=set(active),
                            find_model_by_served_name=lambda s: model_obj),
        dual_gate=gates,
        get_target_port=lambda name: port,
        release_port=lambda *a, **k: None,
        make_conn=lambda p, timeout=None: __import__(
            "http.client", fromlist=["HTTPConnection"]).HTTPConnection(
            "127.0.0.1", p, timeout=timeout),
        response_cache=None,
    )


def local_cand(model_name="qwen3-4b"):
    return CandidateConfig(id="c1", priority=10, kind="local",
                           model_name=model_name,
                           declared_protocols=("openai",))


def local_rc(model_name="qwen3-4b"):
    from inferfabric.model_pool.registry import ResolvedCandidate as RC
    return RC(candidate=local_cand(model_name), endpoint_fingerprint="fp-c1",
              evidence={}, route={})


def attempt_local(pm, data, cand=None):
    cand = cand or local_rc()
    return dispatch._attempt_local_openai(
        FakeHandler(), pm, data, model="fabric-flash", pool="flash", cand=cand,
        remaining=10.0)


# ── 非 2xx 保真 / 已提交不重放 ──

class TestNon2xxIsNotSuccess:

    def test_local_upstream_502_is_committed_error(self, fake_upstream):
        # 终审 I6 起：502 属动作表状态，suppress 未提交 → 池层决定重试/切换；
        # 直发 committed 语义仅保留于非回退类（见 TestLocalSuppressActionableStatuses）
        set_scenario(status=502)
        pm = make_pm(fake_upstream)
        res = attempt_local(pm, {"messages": [{"role": "user", "content": "hi"}]})
        assert res.committed is False
        assert res.ok is False
        assert res.status == 502

    def test_local_ok_keeps_real_status(self, fake_upstream):
        set_scenario(status=200)
        pm = make_pm(fake_upstream)
        res = attempt_local(pm, {"messages": [{"role": "user", "content": "hi"}]})
        assert res.ok is True and res.committed is True and res.status == 200
        assert res.usage.get("completion_tokens") == 5

    def test_committed_error_never_replays(self):
        # committed 错误在动作表中终止：仅 1 次调用，不重试不切换，
        # dispatch 层只结束当前响应（不写第二信封由 _finish_outcome 分支锁定）
        from inferfabric.model_pool.config import PoolConfig
        pool = PoolConfig(name="flash", enabled=True,
                          allowed_locations=("local",), accept_declared=True,
                          max_attempts=3, deadline_ms=5000,
                          candidates=(local_cand(),))
        rc = ResolvedCandidate(candidate=local_cand(), endpoint_fingerprint="fp",
                               evidence={}, route={})
        calls = []

        def transport(cand, remaining):
            calls.append(1)
            return AttemptResult(ok=False, committed=True, status=502,
                                 error="upstream_http_502")

        outcome = run_attempts(pool, [rc], transport)
        assert len(calls) == 1
        assert outcome.committed is True and outcome.ok is False
        assert outcome.status == 502


class TestStreamTerminal:

    def test_completed_stream(self, fake_upstream):
        chunks = [b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n',
                  b'data: [DONE]\n\n']
        set_scenario(stream=True, chunks=chunks, close_mid=False)
        pm = make_pm(fake_upstream)
        res = attempt_local(pm, {"messages": [], "stream": True})
        assert res.ok is True and res.committed is True
        assert res.stream_state == "completed"

    def test_missing_terminal_is_not_success(self, fake_upstream):
        chunks = [b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n']
        set_scenario(stream=True, chunks=chunks, close_mid=False)
        pm = make_pm(fake_upstream)
        res = attempt_local(pm, {"messages": [], "stream": True})
        assert res.ok is False and res.committed is True
        assert res.stream_state == "missing_terminal"

    def test_error_event_classified(self, fake_upstream):
        chunks = [b'data: {"error": {"message": "boom"}}\n\n',
                  b'data: [DONE]\n\n']
        set_scenario(stream=True, chunks=chunks, close_mid=False)
        pm = make_pm(fake_upstream)
        res = attempt_local(pm, {"messages": [], "stream": True})
        assert res.ok is False and res.committed is True
        assert res.stream_state == "error_event"

    def test_read_error_classified(self, fake_upstream):
        chunks = [b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n']
        set_scenario(stream=True, chunks=chunks, close_mid=True)
        pm = make_pm(fake_upstream)
        res = attempt_local(pm, {"messages": [], "stream": True})
        assert res.ok is False and res.committed is True
        assert res.stream_state in ("read_error", "missing_terminal")
        # 不伪造 usage
        assert not res.usage or not any(res.usage.values())

    def test_client_disconnect_classified(self, fake_upstream):
        chunks = [b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n',
                  b'data: [DONE]\n\n']
        set_scenario(stream=True, chunks=chunks, close_mid=False)
        pm = make_pm(fake_upstream)
        h = FakeHandler()
        h._client_gone = True  # 模拟写失败（生产 _safe_write 置位语义）
        res = dispatch._attempt_local_openai(
            h, pm, {"messages": [], "stream": True}, model="fabric-flash",
            pool="flash", cand=local_rc(), remaining=10.0)
        assert res.ok is False and res.committed is True
        assert res.stream_state == "client_disconnect"


# ── Mac 双协议路径 ──

class TestMacAnthropicPoolPaths:

    def test_llama_cpp_native_declares_anthropic(self):
        from inferfabric.model_pool.catalog import local_protocols_for_model
        assert local_protocols_for_model("llama_cpp", anthropic_native=True) == \
            ["openai", "anthropic"]
        assert local_protocols_for_model("llama_cpp", anthropic_native=False) == \
            ["openai"]
        assert local_protocols_for_model("vllm", anthropic_native=False) == \
            ["openai", "anthropic"]

    # 注：test_registry_selfheal_uses_native_flag（proxy_manager._local_protocols
    # 按模型实例 anthropic_native 自愈）依赖 ModelConfig.anthropic_native 字段
    # ——上游尚无该字段（休眠功能），随协议桥批次（F1）摘取。

    def test_native_model_selected_for_anthropic_request(self):
        from inferfabric.model_pool.selector import RuntimeView
        from inferfabric.model_pool.evidence import Evidence, EvidenceState
        cand = CandidateConfig(id="c1", priority=10, kind="local",
                               model_name="qwen3-4b",
                               declared_protocols=("openai", "anthropic"))
        ev = {p: Evidence(state=EvidenceState.DECLARED, protocol=p, features=(),
                          fingerprint="fp", recorded_at="", source="t")
              for p in ("openai", "anthropic")}
        rc = ResolvedCandidate(candidate=cand, endpoint_fingerprint="fp",
                               evidence=ev, route={})
        from inferfabric.model_pool.config import PoolConfig
        pool = PoolConfig(name="flash", enabled=True,
                          allowed_locations=("local",), accept_declared=True,
                          candidates=(cand,))
        snap = PoolSnapshot(revision=1, pool=pool, candidates=(rc,), issues=())
        sel = select(snap, RequestRequirements(protocol="anthropic", vision=False),
                     RuntimeView(active_services={"qwen3-4b"},
                                 cloud_models={}, cooling_fingerprints=frozenset(),
                                 local_vision=None))
        assert sel.chosen is not None
        assert sel.chosen.candidate.model_name == "qwen3-4b"


# ── 直连/池观测同源 ──

class TestObservationParity:

    def _clock(self):
        state = {"t": 100.0}

        def mono():
            return state["t"]

        def advance(dt):
            state["t"] += dt

        return mono, advance

    def test_attempt_clock_excludes_prior_failures(self):
        mono, advance = self._clock()

        def transport(cand, remaining):
            advance(2.0)  # attempt 1 耗时 2s
            return AttemptResult(ok=False, committed=False, status=502)

        def transport2(cand, remaining):
            advance(1.0)  # attempt 2 自身 1s
            return AttemptResult(ok=True, committed=True, status=200,
                                 usage={"prompt_tokens": 3,
                                        "completion_tokens": 5},
                                 ttft_ms=120.0, stream_state="completed")
        from inferfabric.model_pool.config import PoolConfig
        pool = PoolConfig(name="flash", enabled=True,
                          allowed_locations=("local",), accept_declared=True,
                          max_attempts=3, deadline_ms=60000,
                          candidates=(local_cand(),))
        rc = ResolvedCandidate(candidate=local_cand(), endpoint_fingerprint="fp",
                               evidence={}, route={})
        seq = [transport, transport2]

        def pick(cand, remaining):
            return seq.pop(0)(cand, remaining)

        outcome = run_attempts(pool, [rc], pick, monotonic=mono)
        a1, a2 = outcome.attempts
        assert a1.duration_ms == pytest.approx(2000.0)
        assert a2.duration_ms == pytest.approx(1000.0)  # 不含先前 2s
        assert a2.ttft_ms == 120.0
        assert a2.stream_state == "completed"

    def test_usage_missing_is_unknown_not_zero(self):
        mono, _ = self._clock()
        from inferfabric.model_pool.config import PoolConfig
        pool = PoolConfig(name="flash", enabled=True,
                          allowed_locations=("local",), accept_declared=True,
                          candidates=(local_cand(),))
        rc = ResolvedCandidate(candidate=local_cand(), endpoint_fingerprint="fp",
                               evidence={}, route={})
        outcome = run_attempts(pool, [rc],
                               lambda c, r: AttemptResult(
                                   ok=True, committed=True, status=200,
                                   usage={}, ttft_ms=None),
                               monotonic=mono)
        # outcome 层不伪造 usage（摘要层 usage_known=False 断言归池观测批次）
        assert outcome.usage == {} or not any(outcome.usage.values())


# ── 终审修复轮（I6）：本地 OpenAI 池候选动作表状态不直发 ──

class TestLocalSuppressActionableStatuses:
    """动作表状态（401/403/429/5xx，501 除外）未提交返回 → 池层冷却/
    等待/重试/回退照常动作；非回退类（400/404/413/422/501）保留 PR-14
    直发语义。suppress 时客户端一个字节都不写。"""

    def test_local_401_not_committed(self, fake_upstream):
        set_scenario(status=401)
        pm = make_pm(fake_upstream)
        res = attempt_local(pm, {"messages": [{"role": "user", "content": "hi"}]})
        assert res.committed is False
        assert res.ok is False
        assert res.status == 401

    def test_local_502_not_committed(self, fake_upstream):
        set_scenario(status=502)
        pm = make_pm(fake_upstream)
        res = attempt_local(pm, {"messages": [{"role": "user", "content": "hi"}]})
        assert res.committed is False
        assert res.status == 502

    def test_local_429_carries_retry_after(self, fake_upstream):
        set_scenario(status=429, extra_headers={"Retry-After": "3"})
        pm = make_pm(fake_upstream)
        res = attempt_local(pm, {"messages": [{"role": "user", "content": "hi"}]})
        assert res.committed is False
        assert res.status == 429
        assert res.retry_after == pytest.approx(3.0)

    def test_local_400_still_committed_pr14(self, fake_upstream):
        set_scenario(status=400)
        pm = make_pm(fake_upstream)
        res = attempt_local(pm, {"messages": [{"role": "user", "content": "hi"}]})
        assert res.committed is True   # PR-14 冻结保留：非回退类直发
        assert res.status == 400

    def test_local_401_stream_not_committed(self, fake_upstream):
        set_scenario(status=401, stream=True)
        pm = make_pm(fake_upstream)
        res = attempt_local(pm, {"messages": [], "stream": True})
        assert res.committed is False
        assert res.status == 401

    def test_suppressed_error_never_written_to_client(self, fake_upstream):
        set_scenario(status=401)
        pm = make_pm(fake_upstream)
        h = FakeHandler()
        dispatch._attempt_local_openai(
            h, pm, {"messages": [{"role": "user", "content": "hi"}]},
            model="fabric-flash", pool="flash", cand=local_rc(), remaining=10.0)
        assert h.status_line is None   # 未 send_response
        assert h.written == b""        # 未写任何字节
