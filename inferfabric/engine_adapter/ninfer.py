"""NInferAdapter — NInfer Docker inference engine.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from inferfabric.engine_adapter.base import EngineAdapter
from inferfabric.engine_adapter import register

if TYPE_CHECKING:
    from inferfabric.config import ModelConfig

log = logging.getLogger("inferfabric.ninfer_adapter")


class NInferAdapter(EngineAdapter):
    def __init__(self, process_manager=None):
        self._proc = process_manager

    @property
    def engine_type(self) -> str:
        return "ninfer"

    def check_health(self, model: ModelConfig) -> str:
        try:
            from inferfabric.health import check_http_status
            port = model.ninfer.port if model.ninfer else None
            if port:
                return check_http_status(f"http://localhost:{port}/v1/models")
            return "?"
        except Exception as e:
            log.warning("[ninfer] health check failed for %s: %s", model.name, e)
            return "?"

    def get_context_window(self, model: ModelConfig) -> int | None:
        return model.ninfer.max_context if model.ninfer else None

    def get_metadata(self, model: ModelConfig) -> dict:
        if not model.ninfer:
            return {}
        return {
            "context_window": self.get_context_window(model),
            "weight_path": model.ninfer.weight_path,
            "docker_image": model.ninfer.docker_image,
        }

    def validate_config(self, model: ModelConfig) -> list[str]:
        issues = []
        if not model.ninfer:
            return ["Missing ninfer config block"]
        cfg = model.ninfer
        if not cfg.weight_path:
            issues.append("ninfer.weight_path is empty")
        if not cfg.docker_image:
            issues.append("ninfer.docker_image is empty")
        if cfg.port <= 0:
            issues.append(f"Invalid ninfer.port: {cfg.port}")
        weight = Path(cfg.weight_path).expanduser()
        if not weight.exists():
            issues.append(f"Weight file not found: {weight}")
        return issues

    def start(self, model: ModelConfig) -> dict:
        """Start NInfer via ProcessManager delegation (D1).

        container_name threaded from ModelConfig.container_name (single source).
        """
        if self._proc is None:
            raise RuntimeError("ProcessManager not set")
        cfg = model.ninfer
        if not cfg:
            return {"status": "error", "message": "No ninfer config"}
        return self._proc.start_ninfer(cfg, model.container_name)

    def stop(self, model: ModelConfig) -> dict:
        """Stop NInfer via ProcessManager delegation (migrated off base helper, D1)."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set")
        cfg = model.ninfer
        if not cfg:
            return {"status": "error", "message": "No ninfer config"}
        return self._proc.stop_ninfer(model.container_name)

    def is_alive(self, model: ModelConfig) -> bool:
        return self.check_health(model) == "✅"

    def get_port(self, model: ModelConfig) -> int | None:
        return model.ninfer.port if model.ninfer else None

    def get_pid_state_key(self) -> str | None:
        return 'ninfer_pid'

    # ── 场景预设调优（iff tune）钩子 ──────────────────────────────
    # kv_capacity 是可选场景字段：场景可用它**覆盖**模型 YAML 的固定物理 KV 池
    # （TXT=632K / VL=410K 是 default 基线），以便每个场景按自己的 C 开销
    # 独立配池——C 高先降 KV 防 OOM、C 低可提 KV 多吃额度。不写则沿用模型 YAML 值。
    # 引擎硬校验兜底：kv ∈ [max_context, C×⌈W/64⌉×64]（见 layouts_impl.h
    # validate_target_options），越界启动即 throw。
    _SCENARIO_FIELDS = [
        "max_concurrency", "max_context", "default_max_tokens",
        "prefill_chunk", "enable_mtp", "draft_tokens",
        "kv_capacity",
    ]

    def scenario_fields(self, model: ModelConfig) -> list[str]:
        """NInfer 可被场景预设修改的字段白名单（kv_capacity 可选，默认=模型 YAML 值）。"""
        return list(self._SCENARIO_FIELDS)

    def engine_caps(self, model: ModelConfig) -> dict[str, dict]:
        """引擎 C++ 硬限（engine.cpp/serve_options/speculative_options 核实）:
        max_concurrency [1,8]、draft_tokens [1,5]、prefill_chunk 128 倍数、
        max_context ≤ 256K、kv_capacity ≥ 1（64 页对齐由引擎启动时处理）。"""
        return {
            "max_concurrency": {"min": 1, "max": 8},
            "draft_tokens": {"min": 1, "max": 5},
            "prefill_chunk": {"step": 128},
            "max_context": {"min": 16, "max": 262144},
            "default_max_tokens": {"min": 1},
            "kv_capacity": {"min": 1},
            "enable_mtp": {},
        }

    def validate_scenario(self, model: ModelConfig, values: dict) -> list[str]:
        """超卖 / 满载余量 / MTP 保护。values 为已钳制的场景目标值。

        kv 取场景目标值（values 里写了 kv_capacity 用场景值；未写 = 模型 YAML 固定值）。
        池顶 = C × ⌈window/64⌉ × 64（池按 64-token 页分配）。
        - 池顶 > kv：超卖 (池顶−kv)/池顶 —— 满载时超出物理池的部分靠 preempt 兜底，
          是刻意设计（场景按需求配 C/W，池不够就换小场景），仅 ⚠ 不阻塞。
        - 池顶 < kv：满载余量 (1−池顶/kv) —— 池没吃满，可加大 C/W。
        - kv < 窗口：引擎硬校验不通过（validate_target_options）、启动必失败，❌ 阻塞。
        """
        issues: list[str] = []
        c = values.get("max_concurrency")
        w = values.get("max_context")
        kv = values.get("kv_capacity")
        if kv is None:
            kv = model.ninfer.kv_capacity if model.ninfer else 0
        if kv is not None and w is not None and kv < w:
            issues.append(
                f"❌kv_capacity {kv} < max_context {w}（引擎要求 kv ≥ 窗口，否则启动失败）")
        if c and w and kv:
            import math
            pool_top = c * math.ceil(w / 64) * 64
            if pool_top > kv:
                oversell = (pool_top - kv) / pool_top * 100
                issues.append(
                    f"⚠超卖 {oversell:.1f}%：池顶 {pool_top}（= {c}×⌈{w}/64⌉×64）"
                    f" > kv_capacity {kv}（满载超出部分由 preempt 兜底，预期行为）")
            elif pool_top < kv:
                slack = (1 - pool_top / kv) * 100
                issues.append(
                    f"⚠满载余量 {slack:.1f}%：池顶 {pool_top} < kv_capacity {kv}"
                    f"（池未吃满，可加大并发/窗口）")
        dt = values.get("draft_tokens")
        if dt is not None and dt > 1:
            issues.append(
                "⚠draft_tokens>1：此模型仅 1 层 MTP，draft>1 未经实测"
                "（vLLM 下曾崩 6 次），失败可一键 `iff tune default` 回滚")
        return issues

    def restart(self, model: ModelConfig) -> dict:
        """停→启，走完整 GPU 状态机编排（与 Dashboard「stop→start」同构）。

        stop_service 更新状态/释放 GPU → clear_manual_stop → switch 重新部署。
        switch 期间 profile_state=SWITCHING → 在途请求收 503+Retry-After。
        无 mgr（未注入 ProcessManager）时退回基类 stop+start。
        """
        mgr = getattr(self._proc, "mgr", None)
        if mgr is None:
            return super().restart(model)
        name = model.name
        r = mgr.stop_service(name)
        if r.get("status") not in ("stopped", "already_stopped"):
            # already_stopped 也继续（可能本来就没在跑，switch 会直接部署）
            if r.get("status") == "error":
                return {"status": "error", "message": f"stop 失败: {r.get('message')}",
                        "step": "stop", "detail": r}
        mgr.state.clear_manual_stop(name)
        sw = mgr.switch(name)
        mgr.state.set("switching_target", "")
        return {"status": sw.get("status", "error"),
                "message": f"restart: {sw.get('message')}", "detail": sw}

    def fetch_engine_metrics(self, model: ModelConfig) -> dict | None:
        if not model.ninfer: return None
        import re as _re
        import subprocess
        cfg = model.ninfer
        # PR-22A：引擎日志源 = `docker logs --tail`（detached 后不再经 attached
        # 客户端流式落盘 log_file）。失败（容器不在等）→ None，与旧缺文件同语义。
        # 5000 行 ≈ 原 500KB 窗口的行数等价；日志经 --log-opt 旋转有界。
        try:
            r = subprocess.run(
                ["docker", "logs", "--tail", "5000", model.container_name],
                timeout=10, capture_output=True, text=True, check=False)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        if r.returncode != 0:
            return None
        lines = ((r.stdout or "") + (r.stderr or "")).splitlines()
        reqs = []; tput = None; run_samples = []; saw_running = False
        for line in lines:
            line = line.strip()
            # live running batch：throughput 行每 5s 打印 `running N`（在途并发数，非累计）。
            # 收非零采样供 running_batch 求平均（跳过 idle 间隙的 0，避免单点跌 0）；
            # saw_running 标记引擎存活（即便全 0 idle 也算活着）。
            if "throughput" in line:
                mr = _re.search(r"running\s+(\d+)", line)
                if mr:
                    v = int(mr.group(1))
                    saw_running = True
                    if v > 0:
                        run_samples.append(v)
            m = _re.search(r"throughput.*?decode\s+([\d.]+)(k?)\s+tok", line)
            if m: tput = float(m.group(1)) * 1000 if m.group(2)=="k" else float(m.group(1)); continue
            m = _re.search(r"req#\d+\s+done.+?prompt\s+([\d,]+)\s*\|.+?output\s+([\d,]+).+?TTFT\s+([\d.]+)\s*(ms|s).+?decode\s+([\d.]+)k?\s*", line)
            if m:
                p = int(m.group(1).replace(",",""))
                o = int(m.group(2).replace(",",""))
                t = float(m.group(3))/1000 if m.group(4)=="ms" else float(m.group(3))
                ds = m.group(5); dl = m.group(0)
                av = dl[dl.find(ds)+len(ds):]
                dt = float(ds)*1000 if av.lstrip().startswith("k") else float(ds)
                reqs.append({"prompt":p,"output":o,"ttft_s":t,"tpot_s":1.0/dt if dt>0 else 0})
        r = {"sleep_state": 0}
        rec = reqs[-50:] if len(reqs)>50 else reqs
        if rec:
            r["seq_length"] = int(sum(x["prompt"]+x["output"] for x in rec)/len(rec))
            r["seq_prompt"] = int(sum(x["prompt"] for x in rec)/len(rec))
            r["seq_generation"] = int(sum(x["output"] for x in rec)/len(rec))
            r["seq_count"] = len(rec)
            ts = sorted(x["ttft_s"] for x in rec)
            r["ttft_cum_mean"] = round(sum(ts)/len(ts),3); r["ttft_cum_n"] = len(ts)
            r["ttft_seconds"] = {"mean":round(sum(ts)/len(ts),3),"p50":round(ts[len(ts)//2],3),"p95":round(ts[int(len(ts)*0.95)],3),"count":len(ts)}
            ps = sorted(x["tpot_s"] for x in rec)
            r["tpot_cum_mean"] = round(sum(ps)/len(ps),4); r["tpot_cum_n"] = len(ps)
            r["tpot_seconds"] = {"p50":round(ps[len(ps)//2],4),"p95":round(ps[int(len(ps)*0.95)],4),"mean":round(sum(ps)/len(ps),4),"count":len(ps)}
        # 日志派生兜底值（引擎 /metrics 不可用时的旧行为）。
        if tput: r["throughput"] = str(round(tput,1)); r["throughput_inst"] = str(round(tput,1))
        if rec: r["throughput_cum_n"] = sum(x["output"] for x in rec)
        # 日志 running_batch 兜底：取最近 20 条非零采样的平均（引擎 /metrics 不可用时保留）。
        if run_samples:
            r["running_batch"] = round(sum(run_samples[-20:]) / len(run_samples[-20:]), 1)
        # 权威引擎值：NInfer 引擎 GET /metrics（Prometheus 文本）经共享 VllmMetricsCollector
        # （prefix="ninfer_"）取 KV / running batch / 吞吐（EMA）+ 直方图派生的 seq/TTFT/TPOT。
        # 有值即覆盖上面的日志兜底；旧镜像（无直方图）或 /metrics 不可达时静默回退日志派生值。
        try:
            from urllib.request import urlopen
            from inferfabric.prometheus import VllmMetricsCollector, parse_prometheus_text
            with urlopen(f"http://127.0.0.1:{cfg.port}/metrics", timeout=10) as resp:
                text = resp.read().decode("utf-8")
            gauges, counters, histos = parse_prometheus_text(text)
            engine_r = VllmMetricsCollector.compute(cfg.port, gauges, counters, histos, prefix="ninfer_")
            for k in ("kv_cache_usage_perc", "running_batch", "throughput", "throughput_inst",
                      "throughput_cum_n", "ttft_seconds", "ttft_cum_mean", "ttft_cum_n",
                      "tpot_seconds", "tpot_cum_mean", "tpot_cum_n", "seq_length", "seq_prompt",
                      "seq_generation", "seq_count", "prompt_tokens_sum", "generation_tokens_sum"):
                if engine_r.get(k) is not None:
                    r[k] = engine_r[k]
        except Exception:
            pass  # 旧镜像 / 无 /metrics → 保持日志派生值
        r["max_batch"] = cfg.max_concurrency
        has_value = (r.get("kv_cache_usage_perc") is not None or bool(rec) or saw_running
                    or tput is not None)
        return r if has_value else {"sleep_state": 0}


register("ninfer", NInferAdapter)