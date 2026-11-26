#!/usr/bin/env bash
# iff-post-restart-check.sh — 重启后上线金丝雀（#78 复盘 · 方案 B，constitution「重启门禁」）。
#
# 用途：生产网关重启后立即执行；全部检查 PASS 才算上线完成，不 PASS 不离场。
#   1) /health 状态 ok 且版本与预期一致（默认读本地 inferfabric 包版本，或 --version 显式给）
#   2) /v1/models 注册表非空
#   3) 重启窗口内出现云端 discovery 成功行（#78 事故类别：网关活着、云端出站已死，
#      仅查 /health 查不出来——discovery 走的就是修复后的出站链路）
#   4) 可选：IFF_SMOKE_KEY_FILE 指向含可调用 key 的文件时，发一条 1-token fabric-lite
#      云端微请求，实测数据面出站
#
# 用法：
#   scripts/iff-post-restart-check.sh [--port 8999] [--version 7.12.12]
#       [--journal inferfabric-proxy.service]                                # Ubuntu systemd user unit
#       [--log ~/.inferfabric/logs/gateway.launchd.log]                      # macOS 日志文件
#   不带 --journal/--log：能读到 systemd 用户总线的自动用 journalctl（unit 名默认
#   inferfabric-proxy.service），否则退回 macOS 默认日志路径。
#
# 退出码：0 = PASS；1 = FAIL（每项失败给出原因与下一步）。绝不打印 key 内容。
# 窗口语义：只看脚本启动前 5 分钟内的日志——操作者在重启后立刻跑本脚本；更早的
# 重启行不会混入。若两次重启间隔小于 5 分钟，请以最近一次为准人工复核 discovery 行。
set -u

PORT=8999; EXPECTED=""; JOURNAL=""; LOGFILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --version) EXPECTED="$2"; shift 2 ;;
    --journal) JOURNAL="$2"; shift 2 ;;
    --log) LOGFILE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

BASE="http://127.0.0.1:${PORT}"
cget() { curl --noproxy '*' -s --max-time 8 "$@"; }
fail() { echo "FAIL: $*"; echo "POST-RESTART CHECK: FAIL"; exit 1; }
ok()   { echo "PASS: $*"; }

# ── 1. /health ──
health=$(cget "${BASE}/health") || fail "网关 :${PORT} 不可达——服务未起来或端口不对"
printf '%s' "$health" | grep -q '"status": *"ok"' || fail "/health 非 ok：${health}"
ver=$(printf '%s' "$health" | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p')
[ -n "$ver" ] || fail "/health 无 version 字段"
if [ -z "$EXPECTED" ]; then
  EXPECTED=$(python3 -c "import inferfabric; print(inferfabric.__version__)" 2>/dev/null) \
    || fail "无法确定预期版本——请用 --version 显式给出"
fi
[ "$ver" = "$EXPECTED" ] || fail "版本不一致：网关 ${ver} ≠ 预期 ${EXPECTED}（部署未生效？）"
ok "/health ok，版本 ${ver}"

# ── 2. /v1/models ──
models=$(cget "${BASE}/v1/models")
printf '%s' "$models" | grep -q '"id"' || fail "/v1/models 空——注册表未加载：$(printf '%s' "$models" | head -c 200)"
ok "模型注册表非空"

# ── 3. discovery 成功行（重启窗口内）──
if [ -n "$JOURNAL" ]; then SRC="journal:${JOURNAL}"
elif [ -n "$LOGFILE" ]; then SRC="file:${LOGFILE}"
elif journalctl --user -n 1 --no-pager >/dev/null 2>&1; then
  JOURNAL="inferfabric-proxy.service"; SRC="journal:${JOURNAL}"
else
  LOGFILE="${HOME}/.inferfabric/logs/gateway.launchd.log"; SRC="file:${LOGFILE}"
fi

cutoff=$(date -v-5M '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date -d '5 minutes ago' '+%Y-%m-%d %H:%M:%S')
discovery_lines() {
  if [ -n "$JOURNAL" ]; then
    journalctl --user -u "$JOURNAL" --since "$cutoff" --no-pager 2>/dev/null \
      | grep -E "discovered [0-9]+ models|Cloud discovery completed" | tail -3
  else
    [ -r "$LOGFILE" ] || { echo "LOG-UNREADABLE: ${LOGFILE}"; return; }
    awk -v cut="$cutoff" '$1" "$2 >= cut' "$LOGFILE" \
      | grep -E "discovered [0-9]+ models|Cloud discovery completed" | tail -3
  fi
}

found=""
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  found=$(discovery_lines)
  case "$found" in LOG-UNREADABLE*) fail "日志源不可读：${found#LOG-UNREADABLE: }（用 --journal 或 --log 显式指定）" ;; esac
  [ -n "$found" ] && break
  [ "$i" -lt 12 ] && sleep 10
done
[ -n "$found" ] || fail "${SRC} 在 5 分钟窗口内未见云端 discovery 成功行——云端出站可能已断（#78 事故类别）。查 provider 连通性与网关日志中的 discovery 错误"
ok "云端 discovery 成功行（源：${SRC}）：$(printf '%s' "$found" | tail -1 | sed 's/^[^ ]* [^ ]* //')"

# ── 4. 可选微云请求 ──
if [ -n "${IFF_SMOKE_KEY_FILE:-}" ]; then
  [ -r "$IFF_SMOKE_KEY_FILE" ] || fail "IFF_SMOKE_KEY_FILE 不可读：${IFF_SMOKE_KEY_FILE}"
  resp_file=$(mktemp)
  code=$(curl --noproxy '*' -s -o "$resp_file" -w '%{http_code}' --max-time 60 \
    "${BASE}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $(tr -d '[:space:]' < "$IFF_SMOKE_KEY_FILE")" \
    -d '{"model":"fabric-lite","messages":[{"role":"user","content":"ping"}],"max_tokens":8,"temperature":0}')
  rm -f "$resp_file"
  [ "$code" = "200" ] || fail "微云请求 HTTP ${code}（fabric-lite 经云端池应 200）——数据面出站异常"
  ok "微云请求 200（数据面出站实测打通）"
else
  echo "SKIP: 微云请求（未设 IFF_SMOKE_KEY_FILE）——discovery 成功行已覆盖出站存活"
fi

echo "POST-RESTART CHECK: PASS"
