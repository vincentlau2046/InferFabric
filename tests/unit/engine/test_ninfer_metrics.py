"""
unit/engine/test_ninfer_metrics.py — NInferAdapter.fetch_engine_metrics 引擎 /metrics 权威值

覆盖:
  - 引擎 GET /metrics（Prometheus 文本）经 VllmMetricsCollector(prefix="ninfer_")
    取 KV / running batch / 吞吐（EMA），覆盖日志派生兜底
  - /metrics 不可达时静默回退到日志派生值（KV 缺省，seq/TTFT/TPOT 仍在）
  - 日志空 + 无 /metrics → 仅返回 sleep_state
"""

import time

import pytest
from unittest.mock import MagicMock

from inferfabric.config import ModelConfig, NInferConfig
from inferfabric.engine_adapter.ninfer import NInferAdapter
from inferfabric.prometheus import VllmMetricsCollector

PORT = 8017  # 测试专用端口：避免污染真实 VllmMetricsCollector 的类级 EMA 状态


def _metrics_text(kp=0.42, running=3, tokens=1000):
    return (
        "# HELP ninfer_kv_cache_usage_perc Fraction of device KV pages in use (0.0-1.0).\n"
        "# TYPE ninfer_kv_cache_usage_perc gauge\n"
        f"ninfer_kv_cache_usage_perc {kp}\n"
        "# HELP ninfer_num_requests_running Requests currently running on the device.\n"
        "# TYPE ninfer_num_requests_running gauge\n"
        f"ninfer_num_requests_running {running}\n"
        "# HELP ninfer_generation_tokens_total Monotonic count of generated decode tokens.\n"
        "# TYPE ninfer_generation_tokens_total counter\n"
        f"ninfer_generation_tokens_total {tokens}\n"
    )


def _model():
    cfg = NInferConfig(port=PORT, container_name="iff-ninfer-test",
                       max_concurrency=4)
    return ModelConfig(name="t", description="d", type="ninfer", ninfer=cfg)


def _patch_docker_logs(monkeypatch, text="", rc=0):
    """PR-22A：引擎日志源 = `docker logs --tail <N> <container>`（detached 后
    不再经 attached 客户端流式落盘）。patch 全局 subprocess.run — adapter
    函数体内 import subprocess，调用期查属性即命中。"""
    monkeypatch.setattr(
        "subprocess.run",
        lambda args, **kw: MagicMock(returncode=rc, stdout=text, stderr=""))


def _metrics_text_hist(kp=0.42, running=3, tokens=1000):
    # 引擎侧 PrometheusHistogram 渲染的累积直方图行（le= 为累计上界计数，+Inf 收尾）。
    # 数值对应 2 次请求：TTFT [0.1, 0.3]、TPOT [0.02, 0.04]、prompt [1000, 1100]、gen [400, 500]。
    return (
        "# HELP ninfer_kv_cache_usage_perc Fraction of device KV pages in use (0.0-1.0).\n"
        "# TYPE ninfer_kv_cache_usage_perc gauge\n"
        f"ninfer_kv_cache_usage_perc {kp}\n"
        "# HELP ninfer_num_requests_running Requests currently running on the device.\n"
        "# TYPE ninfer_num_requests_running gauge\n"
        f"ninfer_num_requests_running {running}\n"
        "# HELP ninfer_generation_tokens_total Monotonic count of generated decode tokens.\n"
        "# TYPE ninfer_generation_tokens_total counter\n"
        f"ninfer_generation_tokens_total {tokens}\n"
        "# HELP ninfer_time_to_first_token_seconds Seconds between prompt submission and first generated token.\n"
        "# TYPE ninfer_time_to_first_token_seconds histogram\n"
        "ninfer_time_to_first_token_seconds_bucket{le=\"0.5\"} 2\n"
        "ninfer_time_to_first_token_seconds_bucket{le=\"1\"} 2\n"
        "ninfer_time_to_first_token_seconds_bucket{le=\"+Inf\"} 2\n"
        "ninfer_time_to_first_token_seconds_sum 0.4\n"
        "ninfer_time_to_first_token_seconds_count 2\n"
        "# HELP ninfer_request_time_per_output_token_seconds Seconds per generated output token per request.\n"
        "# TYPE ninfer_request_time_per_output_token_seconds histogram\n"
        "ninfer_request_time_per_output_token_seconds_bucket{le=\"0.05\"} 2\n"
        "ninfer_request_time_per_output_token_seconds_bucket{le=\"+Inf\"} 2\n"
        "ninfer_request_time_per_output_token_seconds_sum 0.06\n"
        "ninfer_request_time_per_output_token_seconds_count 2\n"
        "# HELP ninfer_request_prompt_tokens Prompt token count per request.\n"
        "# TYPE ninfer_request_prompt_tokens histogram\n"
        "ninfer_request_prompt_tokens_bucket{le=\"1024\"} 1\n"
        "ninfer_request_prompt_tokens_bucket{le=\"2048\"} 2\n"
        "ninfer_request_prompt_tokens_bucket{le=\"+Inf\"} 2\n"
        "ninfer_request_prompt_tokens_sum 2100\n"
        "ninfer_request_prompt_tokens_count 2\n"
        "# HELP ninfer_request_generation_tokens Generated token count per request.\n"
        "# TYPE ninfer_request_generation_tokens histogram\n"
        "ninfer_request_generation_tokens_bucket{le=\"512\"} 1\n"
        "ninfer_request_generation_tokens_bucket{le=\"1024\"} 2\n"
        "ninfer_request_generation_tokens_bucket{le=\"+Inf\"} 2\n"
        "ninfer_request_generation_tokens_sum 900\n"
        "ninfer_request_generation_tokens_count 2\n"
    )


def _patch_urlopen(monkeypatch, text=None, raise_exc=False):
    """替换 urllib.request.urlopen（adapter 在函数体内 import，故此处 patch 生效）。"""

    def _factory(*args, **kwargs):
        if raise_exc:
            raise ConnectionError("no /metrics endpoint")
        resp = MagicMock()
        resp.read.return_value = (text or "").encode("utf-8")
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    monkeypatch.setattr("urllib.request.urlopen", _factory)


@pytest.fixture(autouse=True)
def _clean_collector():
    # 类级 EMA 状态 keyed by port；清掉本测试端口，避免跨用例污染。
    yield
    VllmMetricsCollector.gen_counters.pop(PORT, None)
    VllmMetricsCollector.throughput_ema.pop(PORT, None)


def test_engine_metrics_override_log_fallback(monkeypatch):
    _patch_docker_logs(monkeypatch, text="")  # 空日志 → 引擎 /metrics 是 KV/Batch/Throughput 的唯一来源
    _patch_urlopen(monkeypatch, text=_metrics_text(kp=0.42, running=3, tokens=1000))
    # 预置 EMA 前值，使单次调用即产出 throughput（首个样本无前值 → 无法求 delta）。
    VllmMetricsCollector.gen_counters[PORT] = (time.time() - 10.0, 500)

    result = NInferAdapter().fetch_engine_metrics(_model())

    assert result is not None
    assert result["kv_cache_usage_perc"] == 42.0  # 0.42 * 100
    assert result["running_batch"] == 3           # live running gauge
    assert result["throughput"] is not None and result["throughput"] > 0  # EMA 平滑
    assert result["max_batch"] == 4


def test_engine_metrics_absent_falls_back_to_log(monkeypatch):
    # 一条 req#done 行 → seq/TTFT/TPOT 仍由日志派生；/metrics 不可达 → KV 缺省。
    _patch_docker_logs(
        monkeypatch,
        text="req#7 done prompt 1,024 | output 512 TTFT 250ms decode 18.0k tok/s\n")
    _patch_urlopen(monkeypatch, raise_exc=True)

    result = NInferAdapter().fetch_engine_metrics(_model())

    assert result is not None
    assert "kv_cache_usage_perc" not in result  # 回退：无引擎值
    assert result["seq_length"] == 1024 + 512   # 日志派生 seq 仍在
    assert result["max_batch"] == 4


def test_no_metrics_and_empty_log_returns_sleep_only(monkeypatch):
    _patch_docker_logs(monkeypatch, text="")
    _patch_urlopen(monkeypatch, raise_exc=True)

    result = NInferAdapter().fetch_engine_metrics(_model())

    assert result == {"sleep_state": 0}


def test_docker_logs_unavailable_returns_none(monkeypatch):
    """docker logs 失败（容器不在/driver 不支持）→ None（fail-soft，同旧缺文件行为）。"""
    _patch_docker_logs(monkeypatch, rc=1)
    _patch_urlopen(monkeypatch, raise_exc=True)

    assert NInferAdapter().fetch_engine_metrics(_model()) is None


def test_engine_histograms_override_log_derived_seq_ttft_tpot(monkeypatch):
    # 日志行给出不同的 seq/TTFT/TPOT；引擎直方图值应覆盖（权威）。
    _patch_docker_logs(
        monkeypatch,
        text="req#7 done prompt 1,024 | output 512 TTFT 250ms decode 18.0k tok/s\n")
    _patch_urlopen(monkeypatch, text=_metrics_text_hist())
    # 预置 EMA 前值，使单次调用即产出 throughput。
    VllmMetricsCollector.gen_counters[PORT] = (time.time() - 10.0, 500)

    result = NInferAdapter().fetch_engine_metrics(_model())

    assert result is not None
    # 引擎直方图权威值覆盖日志派生（日志会给出 seq_length=1536 / ttft mean=0.25）。
    assert result["seq_length"] == 1050 + 450
    assert result["seq_prompt"] == 1050
    assert result["seq_generation"] == 450
    assert result["seq_count"] == 2
    assert result["ttft_seconds"] == {"p50": 0.25, "p95": 0.25, "mean": 0.2, "count": 2}
    assert result["ttft_cum_mean"] == 0.2 and result["ttft_cum_n"] == 2
    assert result["tpot_seconds"] == {"p50": 0.025, "p95": 0.025, "mean": 0.03, "count": 2}
    assert result["tpot_cum_mean"] == 0.03 and result["tpot_cum_n"] == 2
    assert result["prompt_tokens_sum"] == 2100
    assert result["generation_tokens_sum"] == 900
    # KV / running batch / 吞吐照旧来自引擎 gauge + counter。
    assert result["kv_cache_usage_perc"] == 42.0
    assert result["running_batch"] == 3
    assert result["throughput"] is not None and result["throughput"] > 0
    assert result["max_batch"] == 4
