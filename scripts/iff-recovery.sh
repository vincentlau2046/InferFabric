#!/usr/bin/env bash
# ============================================================
# iff-recovery.sh — Emergency GPU Reset
# Usage: ~/inferfabric/scripts/iff-recovery.sh [--full]
# --full: attempts nvidia-smi --gpu-reset if normal recovery fails
# ============================================================

set -euo pipefail

FULL="${1:-}"

echo "=== InferFabric Emergency Recovery ==="
echo "[$(date '+%H:%M:%S')] Starting recovery..."

# Step 1: Stop via iff
echo "[1/6] Stopping via iff..."
iff reset idle 2>/dev/null || {
    # Fallback: use iff directly (legacy scripts removed/deprecated)
    iff switch idle 2>/dev/null || true
    iff stop comfyui 2>/dev/null || true
}
sleep 2

# Step 2: SIGKILL all vLLM processes
echo "[2/6] SIGKILL all vLLM processes..."
pkill -9 -f "vllm serve" 2>/dev/null || true
pkill -9 -f "vllm.*8000" 2>/dev/null || true
pkill -9 -f "vllm.*8001" 2>/dev/null || true
pkill -9 -f "vllm.*8002" 2>/dev/null || true
pkill -9 -f "python.*vllm" 2>/dev/null || true
sleep 2

# Step 2a: Stop Docker containers (NInfer, SGLang)
echo "[2a/6] Stopping GPU Docker containers..."
docker ps -q --filter name=iff-ninfer --filter name=ninfer- --filter name=sglang- 2>/dev/null | xargs -r docker stop 2>/dev/null || true
docker ps -q --filter name=iff-ninfer --filter name=ninfer- --filter name=sglang- 2>/dev/null | xargs -r docker rm -f 2>/dev/null || true

# Step 2b: Kill other GPU-holding processes
echo "[2b/6] Killing other GPU processes..."
pkill -9 -f "llama-server" 2>/dev/null || true          # llama.cpp (CUDA build)
pkill -9 -f "python.*inferfabric.*tts" 2>/dev/null || true
pkill -9 -f "python.*inferfabric.*asr" 2>/dev/null || true
pkill -9 -f "ollama" 2>/dev/null || true                 # ollama daemon
sleep 2

# Step 3: Kill ComfyUI processes
echo "[3/6] Killing ComfyUI processes..."
COMFYUI_DIR="${COMFYUI_DIR:-$HOME/ComfyUI}"
if [ -d "$COMFYUI_DIR" ]; then
    pkill -9 -f "python.*${COMFYUI_DIR}/main.py" 2>/dev/null || true
fi
pkill -9 -f "ComfyUI" 2>/dev/null || true
sleep 2

# Step 4: Check GPU
echo "[4/6] Checking GPU memory..."
USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 || echo "unknown")
echo "  GPU used: ${USED} MB"

PARTIAL=false
if [ "$USED" != "unknown" ] && [ "$USED" -gt 2048 ] 2>/dev/null; then
    PARTIAL=true
    if [ "$FULL" = "--full" ]; then
        echo "[5/6] Attempting nvidia-smi --gpu-reset..."
        nvidia-smi --gpu-reset 2>/dev/null || echo "  GPU reset failed (may need reboot)"
        sleep 5
        USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 || echo "unknown")
        if [ "$USED" != "unknown" ] && [ "$USED" -lt 2048 ] 2>/dev/null; then
            PARTIAL=false
        fi
    fi
fi

# Step 5: Clean lock and re-init state (NEVER rm -rf state.db)
echo "[5/6] Cleaning lock and state..."
rm -f /tmp/inferfabric_gpu.lock
mkdir -p ~/.inferfabric

# Safely re-init state.db (creates tables if missing, keeps existing data)
python3 << 'PYEOF'
import sqlite3, pathlib
db = pathlib.Path.home() / '.inferfabric' / 'state.db'
db.parent.mkdir(exist_ok=True)
c = sqlite3.connect(str(db))
c.execute('PRAGMA journal_mode=WAL')
c.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)')
c.execute('''CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    from_profile TEXT, to_profile TEXT, duration REAL, status TEXT
)''')
c.execute("INSERT OR REPLACE INTO state VALUES ('current_profile', 'idle')")
c.execute("INSERT OR REPLACE INTO state VALUES ('profile_state', 'idle')")
c.execute("INSERT OR REPLACE INTO state VALUES ('vllm_pid', '')")
c.execute("INSERT OR REPLACE INTO state VALUES ('comfyui_pid', '')")
c.commit()
c.close()
PYEOF

# Step 6: Verify
echo "[6/6] Final status..."
USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 || echo "unknown")
echo "  GPU used: ${USED} MB"

if [ "$PARTIAL" = true ]; then
    echo ""
    echo "⚠️ Partial recovery — GPU still busy (${USED} MB)"
    echo "   State DB reset to idle. Orphan CUDA context may need reboot."
    echo ""
    echo "   You can still try: iff switch <profile>"
    echo "   If GPU remains stuck, reboot the machine."
else
    echo ""
    echo "✅ Full recovery — GPU is free"
fi

echo ""
echo "Available commands:"
echo "  iff switch qw36_full    # Start Qwen3.6"
echo "  iff reconcile           # Fix DB vs reality"
echo "  iff switch qwen36-27b  # iff CLI"
