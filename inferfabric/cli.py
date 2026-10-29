#!/usr/bin/env python3
"""
iff — CLI for local LLM model switching (v4.0).

Usage:
  iff status              Show GPU mode, active services, health
  iff models              List available models from models.d/
  iff switch <model>      Switch to a model (enforces tri-state rules)
  iff stop <model>        Stop a single shared service
  iff sleep <model>           Put a running vLLM model to L2 sleep
  iff wake <model>                Wake a sleeping vLLM model
  iff history             Show switch history
  iff reset               Force reset to idle
  iff reconcile           Fix DB vs actual state inconsistencies
  iff gpu-clear           Clear GPU CUDA state (fix fragmentation after ComfyUI)
  iff tune [model] [preset] [--dry|--no-restart|--yes]  场景预设调优（应用/预览/回滚）
  iff tune                List models and their scenarios
  iff tune <model>        Show scenarios + current live values for a model
  iff tune <model> <preset>  Apply a preset (prints diff, confirms, restarts)
  iff tune <model> default   Roll back to pre-preset baseline
"""

import sys
import json
import logging
import datetime
from pathlib import Path
import sqlite3

# Bootstrap logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("inferfabric.cli")

# Add parent to path for import
sys.path.insert(0, str(Path(__file__).parent.parent))
from inferfabric.manager import ModelManager
from inferfabric.state import GPUMode, ServiceState
from inferfabric.health import gpu_used_mb


def cmd_status():
    mgr = ModelManager()
    s = mgr.status()

    gpu_mode_label = {
        GPUMode.IDLE: "⚪ idle",
        GPUMode.EXCLUSIVE: "🔒 exclusive",
        GPUMode.SHARED: "🔓 shared",
    }.get(s.get("gpu_mode", ""), s.get("gpu_mode", "?"))

    print(f"GPU Mode : {gpu_mode_label}")
    print(f"Services : {s['active_services'] or '(none)'}")

    for svc, health in s.get("services_health", {}).items():
        print(f"  {svc}: {health}")

    pid_info = []
    if s.get("vllm_pid"):
        pid_info.append(f"vLLM PID={s['vllm_pid']}")
    if s.get("comfyui_pid"):
        pid_info.append(f"ComfyUI PID={s['comfyui_pid']}")
    if pid_info:
        print(f"PIDs     : {', '.join(pid_info)}")

    print(f"GPU      : {s['gpu_used_mb']}/{s['gpu_total_mb']} MiB used")


def cmd_models(args):
    mgr = ModelManager()
    models = mgr.list_models()

    mode_filter = None
    if "--mode" in args:
        idx = args.index("--mode")
        if idx + 1 < len(args):
            mode_filter = args[idx + 1]

    if mode_filter:
        models = [m for m in models if m["mode"] == mode_filter]

    print(f"\nAvailable Models ({len(models)}):")
    print(f"{'name':<20} {'mode':<12} {'type':<10} {'model_type':<10} {'description'}")
    print("-" * 80)
    for m in models:
        active = " ← active" if m["active"] else ""
        print(f"{m['name']:<20} {m['mode']:<12} {m['type']:<10} {m.get('model_type','llm'):<10} {m['description']}{active}")


def cmd_switch(args):
    if not args:
        print("Usage: iff switch <model_name|idle>")
        print("\nAvailable models:")
        mgr = ModelManager()
        for m in mgr.list_models():
            print(f"  {m['name']:<20} ({m['mode']}, {m['type']}, {m.get('model_type','llm')})")
        print("  idle                  (release GPU)")
        sys.exit(1)

    target = args[0]
    mgr = ModelManager()

    if target == "idle":
        # Record manual stop for all active services
        for svc in list(mgr.active_services):
            mgr.state.record_manual_stop(svc)
        print("Switching to idle...")
    else:
        model = mgr.get_model(target)
        if not model:
            print(f"❌ Unknown model: {target}")
            print("\nAvailable models:")
            for m in mgr.list_models():
                print(f"  {m['name']:<20} ({m['mode']}, {m['type']}, {m.get('model_type','llm')})")
            sys.exit(1)
        # Clear manual stop for target (user explicitly wants it)
        mgr.state.clear_manual_stop(target)
        print(f"Switching to '{target}' (gpu_role={model.gpu_role})...")

    result = mgr.switch(target)

    if result["status"] == "already_active":
        print(f"Already active: {target}")
    elif result["status"] == "switched":
        gpu_mode = result.get("gpu_mode", "")
        elapsed = result.get("elapsed_sec", 0)
        print(f"✅ Switched to '{target}' in {elapsed}s (GPU: {gpu_mode})")
        if result.get("active_services"):
            print(f"  Active services: {result['active_services']}")
        for key, res in result.get("results", {}).items():
            status = "✅" if res.get("status") in ("healthy", "started", "ok") else "❌"
            print(f"  {status} {key}: {res.get('status', '?')}")
    else:
        print(f"❌ {result['message']}")
        if result.get("results"):
            for key, res in result["results"].items():
                print(f"  {key}: {res}")
        sys.exit(1)


def cmd_stop(args):
    if not args:
        print("Usage: iff stop <model_name>")
        sys.exit(1)

    target = args[0]
    mgr = ModelManager()

    result = mgr.stop_service(target)

    if result["status"] == "stopped":
        mgr.state.record_manual_stop(target)
        gpu_mode = result.get("gpu_mode", "?")
        print(f"✅ Stopped '{target}' (GPU: {gpu_mode})")
        if result.get("remaining"):
            print(f"  Remaining: {result['remaining']}")
        if result.get("message"):
            print(f"  {result['message']}")
    else:
        print(f"❌ {result['message']}")
        sys.exit(1)


def cmd_history(args):
    mgr = ModelManager()
    history = mgr.state.get_history(limit=30)
    if not history:
        print("No switch history.")
        return
    print(f"\nSwitch History (last {len(history)} entries):")
    print(f"{'#':<4} {'from':<20} {'to':<20} {'cost':<8} {'status':<8} {'time'}")
    print("-" * 80)
    for i, h in enumerate(history):
        ts = datetime.datetime.fromisoformat(h["timestamp"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        ts = ts.astimezone()
        dur = f"{h['duration']:.1f}s" if h["duration"] else "-"
        status = h.get("status", "?")
        print(f"{i+1:<4} {h['from']:<20} {h['to']:<20} {dur:<8} {status:<8} {ts.strftime('%Y-%m-%d %H:%M:%S')}")


def cmd_reset(args):
    mgr = ModelManager()
    # Record manual stop for all active services
    for svc in list(mgr.active_services):
        mgr.state.record_manual_stop(svc)
    print("Force resetting to idle...")

    result = mgr.force_reset()

    if result["status"] == "reset":
        print(f"✅ Reset to idle (GPU: {result['gpu_mode']})")
        if not result["gpu_free"]:
            print(f"⚠️ WARNING: GPU still has {gpu_used_mb()} MB used — orphan CUDA context likely")
            print(f"   May need 'nvidia-smi --gpu-reset' or reboot")
    else:
        print(f"❌ Reset failed")
        sys.exit(1)


def cmd_reconcile(args):
    mgr = ModelManager()
    result = mgr.reconcile()

    print("State Reconciliation:")
    print(f"  DB gpu_mode   : {result['db_gpu_mode']}")
    print(f"  Actual gpu_mode: {result['actual_gpu_mode']}")
    print(f"  DB services   : {result['db_services']}")
    print(f"  Actual services: {result['actual_services']}")
    if result["actions"]:
        print("\n  Actions taken:")
        for a in result["actions"]:
            print(f"    • {a}")
    else:
        print("  ✓ State is consistent")


def cmd_sleep(args):
    if not args:
        print("Usage: iff sleep <model_name>")
        print("\nPut a running vLLM model to sleep (L2: discard weights, wake ~3-6s).")
        sys.exit(1)

    target = args[0]
    mgr = ModelManager()
    model = mgr.get_model(target)
    if not model:
        print(f"❌ Unknown model: {target}")
        sys.exit(1)

    print(f"Sleeping '{target}'...")

    result = mgr.sleep_model(target)

    if result["status"] == "ok":
        elapsed = result.get("elapsed_sec", 0)
        # Check GPU mode change
        gpu_info = ""
        if result.get("gpu_mode"):
            gpu_info = f", GPU={result['gpu_mode']}"
        print(f"✅ '{target}' sleeping ({elapsed:.1f}s)" + gpu_info)
    elif result["status"] == "already_sleeping":
        print(f"Already sleeping: {target}")
    else:
        print(f"❌ {result['message']}")
        sys.exit(1)


def cmd_wake(args):
    if not args:
        print("Usage: iff wake <model_name>")
        print("\nWake a sleeping vLLM model. Exclusive models require GPU=idle.")
        sys.exit(1)

    target = args[0]
    mgr = ModelManager()
    model = mgr.get_model(target)
    if not model:
        print(f"❌ Unknown model: {target}")
        sys.exit(1)

    print(f"Waking '{target}'...")

    result = mgr.wake_model(target)

    if result["status"] == "ok":
        elapsed = result.get("elapsed_sec", 0)
        print(f"✅ '{target}' awake ({elapsed:.1f}s)")
    elif result["status"] == "already_awake":
        print(f"Already awake: {target}")
    else:
        print(f"❌ {result['message']}")
        sys.exit(1)


def cmd_gpu_clear(args):
    """Manually clear GPU CUDA state to eliminate memory fragmentation.

    This is useful after ComfyUI (or any GPU-heavy service) has been running
    for a while and has exited, but CUDA internal memory fragmentation prevents
    new models from allocating large contiguous VRAM blocks.

    Safe to run when GPU is idle. Tries multiple approaches:
    1. nvidia-smi --gpu-reset (most effective, requires no running CUDA contexts)
    2. nvidia-smi -pm toggle (fallback)
    3. nvidia-smi -r (secondary fallback)
    """
    gpu_index = 0
    if args and args[0] == "--gpu":
        try:
            gpu_index = int(args[1])
            args = args[2:]
        except (ValueError, IndexError):
            pass

    mgr = ModelManager()
    # Initialize dependencies so we have access to the process manager
    # force=True bypasses GPU_AUTO_CLEAR_CUDA_STATE config
    result = mgr._proc.clear_gpu_cuda_state(gpu_index, force=True)

    utils = {
        "gpu-reset": "nvidia-smi --gpu-reset (full state reset)",
        "pm-toggle": "nvidia-smi persistence mode toggle",
        "nvidia-smi-r": "nvidia-smi -r (secondary reset)",
    }
    method_name = utils.get(result.get("method", ""), result.get("method", "?"))

    if result["status"] == "ok":
        print(f"✅ GPU CUDA state cleared via {method_name}")
        print(f"   Memory: {result.get('before_mb', '?')} MB → {result.get('after_mb', '?')} MB")
    else:
        print(f"❌ Failed to clear GPU CUDA state: {result.get('message', 'unknown error')}")
        print("   Try: sudo nvidia-smi --gpu-reset (manual)")


def cmd_pull(args):
    """Pre-download model files: ollama pull / huggingface-cli download."""
    if not args:
        print("Usage: iff pull <model_name>")
        print("\nPre-download model files for offline switch.")
        sys.exit(1)

    target = args[0]
    mgr = ModelManager()
    model = mgr.get_model(target)
    if not model:
        print(f"❌ Unknown model: {target}")
        sys.exit(1)

    if model.is_ollama:
        import subprocess
        ref = model.ollama.model_ref
        print(f"⬇  Pulling {ref} via ollama...")
        result = subprocess.run(
            ["ollama", "pull", ref],
            capture_output=False, timeout=600
        )
        if result.returncode == 0:
            print(f"✅ Model pulled: {ref}")
        else:
            print(f"❌ Pull failed with code {result.returncode}")
            sys.exit(1)
    elif model.is_ollama_cpp:
        from pathlib import Path
        model_path = Path(model.ollama_cpp.model_path).expanduser()
        if model_path.exists():
            print(f"✅ Model already downloaded: {model_path}")
        else:
            print(f"❌ GGUF model not found: {model_path}")
            print(f"   Download from HuggingFace and place at: {model_path.parent}")
            sys.exit(1)
    elif model.is_vllm:
        model_dir = Path.home() / "models" / model.vllm.model_dir
        if model_dir.exists():
            print(f"✅ Model already downloaded: {model_dir}")
        else:
            print(f"❌ Model not found: {model_dir}")
            print(f"   Use huggingface-cli download or modelscope to download.")
            sys.exit(1)
    else:
        print(f"Pull not supported for {model.type} models")


def cmd_list_downloaded(args):
    """List pre-downloaded models on disk."""
    mgr = ModelManager()
    models = mgr.list_models()
    ollama_models = []
    local_models = []

    for m in models:
        if m["type"] == "ollama":
            import subprocess
            try:
                result = subprocess.run(
                    ["ollama", "list"],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    ollama_models.extend(
                        [l.split()[0] for l in result.stdout.strip().splitlines()[1:] if l.strip()]
                    )
            except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                log.debug("Ollama list failed: %s", e)
            break

    print(f"\nOllama models (via 'ollama list'):")
    if ollama_models:
        for m in ollama_models:
            print(f"  {m}")
    else:
        print("  (ollama not installed or no models pulled)")

    models_base = Path.home() / "models"
    if models_base.exists():
        dirs = [d.name for d in models_base.iterdir() if d.is_dir()]
        print(f"\nvLLM models (~/models/):")
        for d in sorted(dirs):
            print(f"  {d}/")


def _fmt_k(v):
    if v is None:
        return "?"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, int) and v >= 1000:
        if v % 1000 == 0:
            return f"{v // 1000}K"    # 十进制 K（kv_capacity 锁值 768000→768K）
        if v % 1024 == 0:
            return f"{v // 1024}K"    # 二进制 K（max_context 32768→32K）
        return str(v)
    return str(v)


def _print_diff(p):
    print(f"\n{p['model']}  [{p['type']}]  active: {p['active_preset'] or 'default'}")
    print(f"  场景 {p['preset']}")
    for k in p["after"]:
        b, a = p["before"].get(k), p["after"].get(k)
        arrow = "→" if b != a else "＝"
        print(f"    {k:16s} {_fmt_k(b):8s} {arrow}  {_fmt_k(a)}")
    for i in p["issues"]:
        print(f"    {i}")
    for n in p["clamp_notes"]:
        print(f"    ⚙ {n}")


def _print_tune_result(r, no_restart):
    st = r["status"]
    if st == "applied":
        print(f"✓ 已应用 {r['preset']} 并重启")
    elif st == "restarted":
        print("✓ 已重启到 default（= 模型 YAML 当前值；此前无应用条目，本次重启使手改的 YAML 生效）")
    elif st == "already_default":
        print(f"✓ {r.get('message', '当前已在 default，无需操作')}")
    elif st == "applied_restart_pending":
        print(f"✓ 已写应用层（{r['preset']}），重启后生效（当前未重启）")
    elif st == "rolled_back":
        print(f"✓ 已回滚到模型 YAML 值（{r.get('preset', 'default')}）")
    elif st == "rolled_back_pending":
        print("✓ 已回滚到模型 YAML 值（重启后生效，当前未重启）")
    elif st in ("failed_rolled_back", "failed_rollback_failed", "failed_rollback_error"):
        print(f"⚠ {r.get('error')}")
        print(f"  状态: {st}")
    else:
        print(f"  状态: {st}")
    rr = r.get("restart")
    if rr:
        print(f"  重启: {rr.get('status')} {rr.get('message')}")
    if r.get("mtp_smoke") is not None:
        print(f"  MTP 冒烟: {'通过' if r['mtp_smoke'] else '失败'}")


def cmd_tune(args):
    """iff tune [model] [preset] [--dry|--no-restart|--yes] — 场景预设调优。"""
    from inferfabric import tune
    mgr = ModelManager()

    # 无参数: 列出所有模型及其场景
    if not args:
        for name, m in mgr._models.items():
            presets = tune.list_presets(m)
            if not presets:
                continue
            active = tune.get_active(m) or "default"
            print(f"{name:24s} [{m.type}]  场景: {', '.join(presets)}  active: {active}")
        if not any(tune.list_presets(m) for m in mgr._models.values()):
            print("（没有任何模型定义场景预设——编辑 models.d/scenarios.yaml 侧车文件后生效）")
        return

    target = args[0]
    if target not in mgr._models:
        print(f"Unknown model: {target}")
        sys.exit(1)
    model = mgr._models[target]

    # 只给模型名: 列出该模型的场景 + 当前 live 值
    if len(args) == 1:
        presets = tune.list_presets(model)
        if not presets:
            print(f"{model.name}: 未定义场景预设（编辑 models.d/scenarios.yaml）")
            sys.exit(1)
        print(f"{model.name}  [{model.type}]  active: {tune.get_active(model) or 'default'}")
        print(f"  场景: {', '.join(presets)}")
        print(f"  当前 live 值（{tune.get_active(model) or 'default (= model.yaml)'}）:")
        cur = tune._current_values(model)
        for k, v in cur.items():
            print(f"    {k:16s} {_fmt_k(v)}")
        return

    preset = args[1]
    dry = "--dry" in args
    no_restart = "--no-restart" in args
    yes = "--yes" in args

    if preset != "default" and preset not in tuple(tune.list_presets(model)):
        print(f"未知场景 {preset!r}，可选: {', '.join(tune.list_presets(model)) or '(无)'}")
        sys.exit(1)

    p = tune.preview(model, preset)
    _print_diff(p)
    if dry:
        print("\n[--dry 预览] 未写盘、未重启")
        return

    # 确认（非交互环境需要 --yes）：仅在「将重启容器」时确认。
    # default 无条目且模型未运行 = 纯 no-op（不写盘不重启）→ 免确认；
    # default 有条目（清除）或模型运行中（手改 YAML 重启生效）→ 会重启 → 确认。
    default_noop = (preset == "default"
                    and not tune.has_applied_entry(model)
                    and model.name not in mgr.active_services)
    if not (yes or no_restart or default_noop):
        print("\n应用后自动重启（NInfer ~3-6s），在途请求短暂 503；失败自动回滚。")
        try:
            ans = input("确认应用？[y/N] ")
        except EOFError:
            ans = "n"
        if ans.lower() not in ("y", "yes"):
            print("已取消")
            sys.exit(0)

    try:
        r = tune.apply(model, preset, dry=False, restart=not no_restart, mgr=mgr)
    except tune.TuneError as e:
        print(f"✗ {e}")
        sys.exit(1)
    _print_tune_result(r, no_restart)


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(1)

    cmd = sys.argv[1]
    rest = sys.argv[2:]

    if cmd == "status":
        cmd_status()
    elif cmd == "models":
        cmd_models(rest)
    elif cmd == "switch":
        cmd_switch(rest)
    elif cmd == "stop":
        cmd_stop(rest)
    elif cmd == "sleep":
        cmd_sleep(rest)
    elif cmd == "wake":
        cmd_wake(rest)
    elif cmd == "history":
        cmd_history(rest)
    elif cmd == "reset":
        cmd_reset(rest)
    elif cmd == "reconcile":
        cmd_reconcile(rest)
    elif cmd == "pull":
        cmd_pull(rest)
    elif cmd == "list-downloaded":
        cmd_list_downloaded(rest)
    elif cmd in ("gpu-clear", "gpu_clear"):
        cmd_gpu_clear(rest)
    elif cmd == "tune":
        cmd_tune(rest)
    elif cmd == "keys":
        from inferfabric.security.cli import run_keys
        try:
            run_keys(rest)
        except (ValueError, OSError, sqlite3.Error) as e:
            print(f"Keys: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Unknown command: {cmd}")
        print("Available: status, models, switch, stop, pull, list-downloaded, sleep, wake, history, reset, reconcile, gpu-clear, tune")
        sys.exit(1)


if __name__ == "__main__":
    main()