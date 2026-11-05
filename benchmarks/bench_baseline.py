#!/usr/bin/env python3
"""InferFabric Model Performance Benchmark — Baseline v1

Tests all deployable models across:
  - Input length scaling (short / medium / long, relative to max_model_len)
  - Concurrency levels (1, 2, 4, 8 — capped by max_num_seqs)
  - Output length variants (short 128tok / long 1024tok)

Metrics per test:
  - TTFT (ms): time to first token (streaming)
  - TPS: output tokens / (total_time - ttft)
  - E2E (ms): total request wall time
  - Output tokens: actual count

Results saved as JSON baseline + Markdown report.
"""

import sys
import os
import json
import time
import yaml
import asyncio
import aiohttp
import statistics
import argparse
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict

sys.path.insert(0, str(Path(__file__).parent.parent))

# Line-buffered stdout for live progress
sys.stdout.reconfigure(line_buffering=True)
from inferfabric.config import load_models, MODEL_TYPE_TO_MODALITY

PROXY_URL = "http://localhost:8999"
CLI_MODULE = "inferfabric.cli"

# ─── Model test matrix ───────────────────────────────────────────────

# Context length fractions of max_model_len to test
CTX_FRACTIONS = {
    "short":  0.01,   # ~1% of max ctx (few hundred tokens)
    "medium": 0.05,   # ~5% (few thousand tokens)
    "long":   0.10,   # ~10% (tens of thousands)
}

# Output token budgets
OUTPUT_BUDGETS = {
    "short_out": 512,    # enough for reasoning + short content
    "long_out":  4096,   # enough for reasoning + long content
}

# Concurrency levels to test (capped by model's max_num_seqs)
CONCURRENCY_LEVELS = [1, 2, 4]

# Warmup & repeat
WARMUP_RUNS = 1
REPEAT_RUNS = 3

# Models to skip
SKIP_MODEL_TYPES = {"aigc", "infra"}
SKIP_MODELS = set()  # add specific names if needed


@dataclass
class ModelSpec:
    name: str
    model_type: str
    gpu_role: str
    framework: str
    port: int
    max_model_len: int
    max_num_seqs: int
    gpu_memory_utilization: float
    quantization: str


@dataclass
class BenchResult:
    model: str
    input_len_tokens: int
    output_budget: int
    concurrency: int
    run_idx: int
    ttft_ms: float
    tps: float
    e2e_ms: float
    output_tokens: int
    total_tokens: int


def load_model_specs() -> list[ModelSpec]:
    """Load model specs from YAML files."""
    specs = []
    models_dir = Path(__file__).parent.parent / "models.d"
    for f in sorted(models_dir.glob("*.yaml")):
        d = yaml.safe_load(f.read_text())
        name = d.get("name", "")
        mt = d.get("model_type", "llm")
        if mt in SKIP_MODEL_TYPES or name in SKIP_MODELS:
            continue
        vllm = d.get("vllm", {}) or {}
        oc = d.get("llama_cpp", {}) or {}
        max_len = vllm.get("max_model_len", 0) or oc.get("context_size", 0)
        max_seqs = vllm.get("max_num_seqs", 1)
        gpu_util = vllm.get("gpu_memory_utilization", 0)
        port = vllm.get("port", 0) or oc.get("port", 0)
        if not port or not max_len:
            continue
        specs.append(ModelSpec(
            name=name,
            model_type=mt,
            gpu_role=d.get("gpu_role", ""),
            framework=d.get("type", ""),
            port=port,
            max_model_len=max_len,
            max_num_seqs=max_seqs,
            gpu_memory_utilization=gpu_util,
            quantization=d.get("quantization", ""),
        ))
    return specs


# ─── Text generation helpers ─────────────────────────────────────────

def generate_prompt_text(target_tokens: int) -> str:
    """Generate a ~target_tokens length prompt using repeated paragraphs."""
    # Average English token ≈ 4 chars; Chinese ≈ 1.5 chars per token
    # Mix both for realistic workload
    paragraph = (
        "InferFabric is a GPU-aware model deployment system that provides automatic routing, "
        "tri-state GPU scheduling (idle/shared/exclusive), and local-first cloud-fallback proxy. "
        "The system supports multiple model types including LLM, VL, Omni, OCR, Embedding, and Rerank. "
        "Each model is configured via YAML in models.d/ with type-specific parameters. "
        "The proxy layer handles chat completions, embeddings, and rerank endpoints. "
        "GPU scheduling ensures optimal resource utilization across shared and exclusive workloads. "
        "这是一个支持多模态模型部署的智能路由系统，具备GPU三态调度能力。"
        "系统采用本地优先、云端兜底的策略，确保推理服务的稳定性和低延迟。"
    )
    # ~150 tokens per paragraph
    n_paragraphs = max(1, target_tokens // 150)
    return "\n\n".join([paragraph] * n_paragraphs)


def estimate_token_count(text: str) -> int:
    """Rough token count estimate (English ~4 chars/tok, Chinese ~1.5 chars/tok)."""
    en_chars = sum(1 for c in text if ord(c) < 0x4E00)
    zh_chars = len(text) - en_chars
    return int(en_chars / 4 + zh_chars / 1.5)


# ─── Streaming benchmark ─────────────────────────────────────────────

async def bench_single_stream(
    session: aiohttp.ClientSession,
    model: str,
    prompt: str,
    max_tokens: int,
    url: str = PROXY_URL,
) -> dict:
    """Single streaming request → TTFT, TPS, E2E, output_tokens."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    t_start = time.perf_counter()
    ttft = None
    output_tokens = 0
    total_tokens = 0

    async with session.post(
        f"{url}/v1/chat/completions",
        json=payload,
        timeout=aiohttp.ClientTimeout(total=600),
    ) as resp:
        if resp.status != 200:
            body = await resp.text()
            return {"error": f"HTTP {resp.status}: {body[:200]}"}

        async for line in resp.content:
            line = line.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            # TTFT on first token (reasoning or content)
            if ttft is None:
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    if delta.get("content") or delta.get("reasoning"):
                        ttft = time.perf_counter() - t_start

            # Usage (final chunk)
            usage = chunk.get("usage")
            if usage:
                output_tokens = usage.get("completion_tokens", 0)
                total_tokens = usage.get("total_tokens", 0)

    t_end = time.perf_counter()
    e2e = t_end - t_start

    if ttft is None:
        ttft = e2e  # fallback

    decode_time = e2e - ttft
    tps = output_tokens / decode_time if decode_time > 0 and output_tokens > 0 else 0

    return {
        "ttft_ms": round(ttft * 1000, 1),
        "tps": round(tps, 2),
        "e2e_ms": round(e2e * 1000, 1),
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


async def bench_concurrent(
    model: str,
    prompt: str,
    max_tokens: int,
    concurrency: int,
) -> list[dict]:
    """Run `concurrency` parallel streaming requests."""
    async with aiohttp.ClientSession() as session:
        tasks = [
            bench_single_stream(session, model, prompt, max_tokens)
            for _ in range(concurrency)
        ]
        return await asyncio.gather(*tasks)


# ─── Embedding benchmark ─────────────────────────────────────────────

async def bench_embedding(
    model: str,
    text: str,
    url: str = PROXY_URL,
) -> dict:
    """Single embedding request → E2E latency + dim."""
    payload = {"model": model, "input": text}
    t_start = time.perf_counter()
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{url}/v1/embeddings",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            result = await resp.json()
    e2e = time.perf_counter() - t_start
    data = result.get("data", [])
    dim = len(data[0].get("embedding", [])) if data else 0
    return {"e2e_ms": round(e2e * 1000, 1), "dim": dim}


# ─── Model lifecycle ─────────────────────────────────────────────────

def switch_model(model_name: str) -> bool:
    """Switch to model via CLI. Returns True if healthy."""
    import subprocess
    # Check if already active
    try:
        import urllib.request
        with urllib.request.urlopen(f"{PROXY_URL}/models", timeout=5) as resp:
            models = json.loads(resp.read().decode())
            for m in models:
                if m.get("name") == model_name and m.get("active"):
                    return True
    except Exception:
        pass
    result = subprocess.run(
        [sys.executable, "-m", CLI_MODULE, "switch", model_name],
        capture_output=True, text=True, timeout=300,
    )
    return result.returncode == 0 and ("healthy" in result.stdout or "Already active" in result.stdout)


def stop_model(model_name: str) -> None:
    """Stop a model."""
    import subprocess
    subprocess.run(
        [sys.executable, "-m", CLI_MODULE, "stop", model_name],
        capture_output=True, text=True, timeout=30,
    )


# ─── Main benchmark runner ──────────────────────────────────────────

async def run_benchmark(
    specs: list[ModelSpec],
    output_dir: Path,
    quick: bool = False,
    models_filter: set[str] = None,
):
    """Run full benchmark suite."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    all_results: list[BenchResult] = []
    embedding_results: list[dict] = []

    # Group by gpu_role for scheduling
    exclusive = [s for s in specs if s.gpu_role == "exclusive"]
    shared = [s for s in specs if s.gpu_role == "shared"]
    cpu = [s for s in specs if s.gpu_role == "none"]

    if quick:
        repeat, warmup = 1, 0
        ctx_fracs = {"short": 0.02, "long": 0.30}
        out_buds = {"short_out": 128}
        conc_levels = [1, 4]
    else:
        repeat, warmup = REPEAT_RUNS, WARMUP_RUNS
        ctx_fracs = CTX_FRACTIONS
        out_buds = OUTPUT_BUDGETS
        conc_levels = CONCURRENCY_LEVELS

    # ── CPU models (embedding + omni) — always available ──
    for spec in cpu:
        if models_filter and spec.name not in models_filter:
            continue
        print(f"\n{'='*60}")
        print(f"📊 CPU Model: {spec.name} ({spec.model_type}, {spec.quantization})")
        print(f"{'='*60}")

        if spec.model_type == "embedding":
            # Embedding benchmark
            for label, frac in ctx_fracs.items():
                n_tokens = max(32, int(spec.max_model_len * frac))
                text = generate_prompt_text(n_tokens)
                for run in range(max(1, repeat)):
                    r = await bench_embedding(spec.name, text)
                    if "error" in r:
                        print(f"  ❌ {label} run{run}: {r['error']}")
                        continue
                    print(f"  ✅ {label} run{run}: e2e={r['e2e_ms']}ms dim={r['dim']}")
                    embedding_results.append({
                        "model": spec.name, "input_tokens": n_tokens,
                        "run": run, **r,
                    })
            continue

        # Non-embedding CPU models (omni) — chat completions
        if not switch_model(spec.name):
            print(f"  ❌ Failed to start {spec.name}")
            continue

        for ctx_label, frac in ctx_fracs.items():
            n_tokens = max(32, int(spec.max_model_len * frac))
            prompt = generate_prompt_text(n_tokens)
            for out_label, max_tok in out_buds.items():
                for conc in conc_levels:
                    if conc > spec.max_num_seqs:
                        continue
                    for run in range(warmup + repeat):
                        results = await bench_concurrent(spec.name, prompt, max_tok, conc)
                        for i, r in enumerate(results):
                            if "error" in r:
                                print(f"  ❌ {ctx_label}/{out_label}/c{conc} run{run}#{i}: {r['error']}")
                                continue
                            br = BenchResult(
                                model=spec.name, input_len_tokens=n_tokens,
                                output_budget=max_tok, concurrency=conc,
                                run_idx=run - warmup if run >= warmup else -1,
                                **r,
                            )
                            all_results.append(br)
                            if run >= warmup:
                                print(f"  ✅ {ctx_label}/{out_label}/c{conc} run{run}: "
                                      f"TTFT={r['ttft_ms']}ms TPS={r['tps']} E2E={r['e2e_ms']}ms")

    # ── Shared models — can coexist on GPU ──
    for spec in shared:
        if models_filter and spec.name not in models_filter:
            continue
        print(f"\n{'='*60}")
        print(f"📊 Shared Model: {spec.name} ({spec.model_type}, {spec.quantization})")
        print(f"{'='*60}")

        if not switch_model(spec.name):
            print(f"  ❌ Failed to start {spec.name}")
            continue

        for ctx_label, frac in ctx_fracs.items():
            n_tokens = max(32, int(spec.max_model_len * frac))
            prompt = generate_prompt_text(n_tokens)
            for out_label, max_tok in out_buds.items():
                for conc in conc_levels:
                    if conc > spec.max_num_seqs:
                        continue
                    for run in range(warmup + repeat):
                        results = await bench_concurrent(spec.name, prompt, max_tok, conc)
                        for i, r in enumerate(results):
                            if "error" in r:
                                print(f"  ❌ {ctx_label}/{out_label}/c{conc} run{run}#{i}: {r['error']}")
                                continue
                            br = BenchResult(
                                model=spec.name, input_len_tokens=n_tokens,
                                output_budget=max_tok, concurrency=conc,
                                run_idx=run - warmup if run >= warmup else -1,
                                **r,
                            )
                            all_results.append(br)
                            if run >= warmup:
                                print(f"  ✅ {ctx_label}/{out_label}/c{conc} run{run}: "
                                      f"TTFT={r['ttft_ms']}ms TPS={r['tps']} E2E={r['e2e_ms']}ms")

    # ── Exclusive models — one at a time ──
    for spec in exclusive:
        if models_filter and spec.name not in models_filter:
            continue
        print(f"\n{'='*60}")
        print(f"📊 Exclusive Model: {spec.name} ({spec.model_type}, {spec.quantization})")
        print(f"{'='*60}")

        if not switch_model(spec.name):
            print(f"  ❌ Failed to start {spec.name}")
            continue

        for ctx_label, frac in ctx_fracs.items():
            n_tokens = max(32, int(spec.max_model_len * frac))
            prompt = generate_prompt_text(n_tokens)
            for out_label, max_tok in out_buds.items():
                for conc in conc_levels:
                    if conc > spec.max_num_seqs:
                        continue
                    for run in range(warmup + repeat):
                        results = await bench_concurrent(spec.name, prompt, max_tok, conc)
                        for i, r in enumerate(results):
                            if "error" in r:
                                print(f"  ❌ {ctx_label}/{out_label}/c{conc} run{run}#{i}: {r['error']}")
                                continue
                            br = BenchResult(
                                model=spec.name, input_len_tokens=n_tokens,
                                output_budget=max_tok, concurrency=conc,
                                run_idx=run - warmup if run >= warmup else -1,
                                **r,
                            )
                            all_results.append(br)
                            if run >= warmup:
                                print(f"  ✅ {ctx_label}/{out_label}/c{conc} run{run}: "
                                      f"TTFT={r['ttft_ms']}ms TPS={r['tps']} E2E={r['e2e_ms']}ms")

        stop_model(spec.name)

    # ── Save results ──────────────────────────────────────────────
    output_dir.mkdir(parents=True, exist_ok=True)

    # JSON raw
    raw_path = output_dir / f"baseline-{timestamp}.json"
    raw_data = {
        "timestamp": timestamp,
        "git_commit": get_git_commit(),
        "gpu": get_gpu_info(),
        "models": [asdict(s) for s in specs],
        "chat_results": [asdict(r) for r in all_results if r.run_idx >= 0],
        "embedding_results": embedding_results,
    }
    raw_path.write_text(json.dumps(raw_data, indent=2, ensure_ascii=False))
    print(f"\n📁 Raw results: {raw_path}")

    # Markdown report
    md_path = output_dir / f"baseline-{timestamp}.md"
    md = generate_report(raw_data)
    md_path.write_text(md)
    print(f"📁 Report: {md_path}")

    return raw_data


def get_git_commit() -> str:
    import subprocess
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).parent.parent),
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def get_gpu_info() -> str:
    import subprocess
    try:
        return subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def generate_report(data: dict) -> str:
    """Generate Markdown summary report from benchmark data."""
    lines = []
    lines.append(f"# InferFabric Performance Baseline")
    lines.append(f"\n**Date**: {data['timestamp']}")
    lines.append(f"**Commit**: `{data['git_commit']}`")
    lines.append(f"**GPU**: {data['gpu']}")

    # Group chat results by model
    from collections import defaultdict
    by_model = defaultdict(list)
    for r in data["chat_results"]:
        by_model[r["model"]].append(r)

    for model_name, results in sorted(by_model.items()):
        lines.append(f"\n## {model_name}")
        lines.append(f"\n| Input Tokens | Output Budget | Concurrency | TTFT (ms) | TPS | E2E (ms) | Output Tok |")
        lines.append(f"|---|---|---|---|---|---|---|")

        # Aggregate by (input, output, concurrency)
        groups = defaultdict(list)
        for r in results:
            key = (r["input_len_tokens"], r["output_budget"], r["concurrency"])
            groups[key].append(r)

        for (inp, out, conc), group in sorted(groups.items()):
            ttfts = [r["ttft_ms"] for r in group]
            tpss = [r["tps"] for r in group]
            e2es = [r["e2e_ms"] for r in group]
            out_toks = [r["output_tokens"] for r in group]
            def _fmt(vals, fmt=".0f"):
                m = statistics.mean(vals)
                if len(vals) >= 2:
                    s = statistics.stdev(vals)
                    return f"{m:{fmt}}±{s:{fmt}}"
                return f"{m:{fmt}}"
            lines.append(
                f"| {inp} | {out} | {conc} | "
                f"{_fmt(ttfts)} | "
                f"{_fmt(tpss, '.1f')} | "
                f"{_fmt(e2es)} | "
                f"{statistics.mean(out_toks):.0f} |"
            )

    # Embedding results
    if data["embedding_results"]:
        lines.append(f"\n## Embedding Models")
        lines.append(f"\n| Model | Input Tokens | E2E (ms) | Dim |")
        lines.append(f"|---|---|---|---|")
        for r in data["embedding_results"]:
            lines.append(f"| {r['model']} | {r['input_tokens']} | {r['e2e_ms']} | {r['dim']} |")

    return "\n".join(lines)


# ─── CLI ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="InferFabric Model Benchmark")
    parser.add_argument("--quick", action="store_true", help="Quick mode: fewer repeats/levels")
    parser.add_argument("--models", nargs="*", help="Only benchmark these models")
    parser.add_argument("--output", default="benchmarks", help="Output directory")
    args = parser.parse_args()

    specs = load_model_specs()
    print(f"📋 Found {len(specs)} benchmarkable models:")
    for s in specs:
        print(f"  {s.name:<25} type={s.model_type:<10} gpu={s.gpu_role:<10} "
              f"ctx={s.max_model_len:<8} max_seqs={s.max_num_seqs} quant={s.quantization}")

    models_filter = set(args.models) if args.models else None

    asyncio.run(run_benchmark(
        specs,
        output_dir=Path(args.output),
        quick=args.quick,
        models_filter=models_filter,
    ))


if __name__ == "__main__":
    main()
