#!/bin/bash
# 验收专用隔离网关启动器（issue #50）— 浏览器验收/临时实验用。
#
# 防孤儿三重防护（背景：11-20 验收网关在 job 结束后存活 5 天占用 8990）：
#   1. HOME 级隔离：数据目录在临时目录，绝不触碰生产 ~/.inferfabric；
#   2. IFF_PARENT_WATCH=1：父进程（本脚本）消亡 → 网关 N 秒内自退；
#   3. trap EXIT/INT/TERM：脚本任何方式退出都 kill 网关（含 Ctrl-C）。
# 隔离补充（issue #56 教训）：
#   - IFF_MODELS_DIR 指向空目录：不加载仓库 models.d，避免验收网关
#     健康检查打到生产引擎端口（跨实例串扰）；
#   - no_proxy 显式含 localhost：系统代理（FlClash bypass 缺 localhost）
#   也不至于劫持本机请求 —— health.py 已内置直连，此处 belt-and-braces。
#
# 用法：
#   scripts/iff-acceptance.sh                 # 默认 :8990
#   IFF_ACCEPTANCE_PORT=8991 scripts/iff-acceptance.sh
#
# 端口分工：8999 = 生产 launchd 网关（com.macfabric.gateway）；
#          8990+ = 验收专用（网关启动日志会打「非生产实例」横幅）。
set -euo pipefail

PORT="${IFF_ACCEPTANCE_PORT:-8990}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/iff-acceptance.XXXXXX")"
mkdir -p "$WORKDIR/home"

GW_PID=""
cleanup() {
  if [[ -n "$GW_PID" ]] && kill -0 "$GW_PID" 2>/dev/null; then
    kill "$GW_PID" 2>/dev/null || true
    wait "$GW_PID" 2>/dev/null || true
  fi
  # 留给调用者的现场：不删 WORKDIR（验收产物可能要查），打印位置即可
  echo "[iff-acceptance] workspace kept for inspection: $WORKDIR" >&2
}
trap cleanup EXIT INT TERM

echo "[iff-acceptance] port=$PORT home=$WORKDIR/home (isolated)"
HOME="$WORKDIR/home" \
IFF_DATA_DIR="$WORKDIR/home/.inferfabric" \
IFF_MODELS_DIR="$WORKDIR/home/models.d" \
IFF_PARENT_WATCH=1 \
EDGE_PROXY_HOST=127.0.0.1 \
EDGE_PROXY_PORT="$PORT" \
no_proxy="localhost,127.0.0.1,::1" \
NO_PROXY="localhost,127.0.0.1,::1" \
  "$PY" -m inferfabric.proxy.handler --async &
GW_PID=$!

echo "[iff-acceptance] gateway pid=$GW_PID — Ctrl-C 或脚本退出即清理"
wait "$GW_PID"
