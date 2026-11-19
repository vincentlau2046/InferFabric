#!/usr/bin/env bash
# scripts/install.sh — 平台感知安装器（issue #13）
#
# 目标：安装/使用全程零用户干预。平台自动判断（uname -s），
# Linux(x86+NVIDIA) 与 macOS(Apple Silicon) 共用一份脚本分支消化差异：
#
#   Linux : python3 检查 → nvidia-smi（缺失仅警告，非 N 卡走云路由）
#           → _deps 重建 → import 冒烟 → systemd user unit 生成
#   macOS : python3 检查 → _deps 重建 → import 冒烟 → launchd plist 生成
#
# 安全设计：
#   - 已存在的 unit/plist 不覆写（--force-service 显式覆盖）
#   - unit/plist 内不写任何 secret（沿用 secrets.env 注入机制）
#   - 默认只准备环境；守护配置落盘即退出，enable/start 由用户或
#     --enable-service 触发
#
# 测试钩子（hermetic 测试用，生产不设）：
#   IFF_FORCE_PLATFORM=linux|darwin   覆盖 uname 判断
#   IFF_SKIP_DEPS=1                   跳过 _deps 重建（测试/离线）
#   IFF_INSTALL_UNIT_DIR=<dir>        覆写 systemd unit 目标目录
#   IFF_INSTALL_PLIST_DIR=<dir>       覆写 launchd plist 目标目录
#   IFF_INSTALL_LABEL=<label>         launchd Label（默认 com.inferfabric.proxy）
#
# 用法：bash scripts/install.sh [--no-service] [--force-service] [--enable-service]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FORCE_SERVICE=0
ENABLE_SERVICE=0

for arg in "$@"; do
  case "$arg" in
    --no-service)     SERVICE=0 ;;
    --force-service)  FORCE_SERVICE=1 ;;
    --enable-service) ENABLE_SERVICE=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg (--help for usage)" >&2; exit 2 ;;
  esac
done
SERVICE="${SERVICE:-1}"

log() { printf '[install] %s\n' "$*"; }
warn() { printf '[install] WARN: %s\n' "$*" >&2; }

# ── 平台自动判断（零干预；测试钩子仅 hermetic 测试使用）──
detect_platform() {
  local forced="${IFF_FORCE_PLATFORM:-}"
  case "$forced" in
    linux|darwin) echo "$forced"; return ;;
    "") ;;
    *) warn "IFF_FORCE_PLATFORM=$forced unrecognized, falling back to uname" ;;
  esac
  case "$(uname -s)" in
    Linux) echo "linux" ;;
    Darwin) echo "darwin" ;;
    *) echo "other" ;;
  esac
}

# ── 公共步骤 ──
check_python() {
  command -v python3 >/dev/null || { echo "[install] ERROR: python3 not found" >&2; exit 1; }
  local ver
  ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  python3 - "$ver" <<'EOF' || { echo "[install] ERROR: python3 >= 3.10 required (got $ver)" >&2; exit 1; }
import sys
major, minor = (int(x) for x in sys.argv[1].split("."))
sys.exit(0 if (major, minor) >= (3, 10) else 1)
EOF
  log "python3 $ver ok"
}

build_deps() {
  if [[ "${IFF_SKIP_DEPS:-}" == "1" ]]; then
    log "skip _deps rebuild (IFF_SKIP_DEPS=1)"
    return
  fi
  if [[ -x "$REPO_ROOT/scripts/rebuild-deps.sh" ]]; then
    bash "$REPO_ROOT/scripts/rebuild-deps.sh"
  else
    warn "scripts/rebuild-deps.sh missing — ensure _deps/ is populated"
  fi
}

smoke_import() {
  if [[ "${IFF_SKIP_SMOKE:-}" == "1" ]]; then
    log "skip import smoke (IFF_SKIP_SMOKE=1)"
    return
  fi
  (cd "$REPO_ROOT" && python3 -c "import inferfabric; print('[install] import ok', inferfabric.__version__)")
}

# ── 解释器固化（评审 PR#14 B2，mac端真机实证）：install 时的 python3 与
# 守护进程运行时的 python3 必须是同一个——launchd/systemd 均不继承交互式
# PATH，`/usr/bin/env python3` 会解析到系统旧解释器（如 macOS 自带 3.9），
# install 期检查通过而服务每次 KeepAlive 重启都崩。安装时烘焙绝对路径。
PY_BIN="$(command -v python3)"

# ── Linux：systemd user unit ──
SYSTEMD_UNIT_DIR="${IFF_INSTALL_UNIT_DIR:-$HOME/.config/systemd/user}"
write_systemd_unit() {
  local target="$SYSTEMD_UNIT_DIR/inferfabric-proxy.service"
  if [[ -f "$target" && "$FORCE_SERVICE" != "1" ]]; then
    log "unit exists, skip: $target (--force-service to overwrite)"
    return 0
  fi
  mkdir -p "$SYSTEMD_UNIT_DIR"
  cat > "$target" <<EOF
[Unit]
Description=InferFabric Auto-Routing Proxy + Dashboard
After=network.target

[Service]
Type=simple
ExecStart=$PY_BIN -m inferfabric.proxy.handler --async
Restart=always
RestartSec=5
WorkingDirectory=$REPO_ROOT
Environment=EDGE_PROXY_HOST=127.0.0.1
Environment=EDGE_PROXY_PORT=8999
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
  log "systemd unit written: $target"
  if [[ "$ENABLE_SERVICE" == "1" ]]; then
    systemctl --user daemon-reload
    systemctl --user enable --now inferfabric-proxy.service
    log "service enabled and started"
  fi
}

# ── macOS：launchd plist（语义由 Mac 端真机验证/修正）──
LAUNCHD_PLIST_DIR="${IFF_INSTALL_PLIST_DIR:-$HOME/Library/LaunchAgents}"
LAUNCHD_LABEL="${IFF_INSTALL_LABEL:-com.inferfabric.proxy}"
LOG_DIR="${IFF_INSTALL_LOG_DIR:-$HOME/Library/Logs/inferfabric}"

xml_escape() {
  # 评审 PR#14 N3：REPO_ROOT/路径插值进 XML 前转义 & < >
  local s="$1"
  s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"
  printf '%s' "$s"
}

write_launchd_plist() {
  local target="$LAUNCHD_PLIST_DIR/$LAUNCHD_LABEL.plist"
  if [[ -f "$target" && "$FORCE_SERVICE" != "1" ]]; then
    log "plist exists, skip: $target (--force-service to overwrite)"
    return 0
  fi
  mkdir -p "$LAUNCHD_PLIST_DIR" "$LOG_DIR"
  local repo_xml
  repo_xml="$(xml_escape "$REPO_ROOT")"
  cat > "$target" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LAUNCHD_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY_BIN</string>
    <string>-m</string>
    <string>inferfabric.proxy.handler</string>
    <string>--async</string>
  </array>
  <key>WorkingDirectory</key><string>$repo_xml</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>EDGE_PROXY_HOST</key><string>127.0.0.1</string>
    <key>EDGE_PROXY_PORT</key><string>8999</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/proxy.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/proxy.err.log</string>
</dict>
</plist>
EOF
  log "launchd plist written: $target"
  if [[ "$ENABLE_SERVICE" == "1" ]]; then
    launchctl load "$target"
    log "plist loaded"
  fi
}

main() {
  local platform
  platform="$(detect_platform)"
  log "platform: $platform (repo: $REPO_ROOT)"

  check_python
  build_deps

  case "$platform" in
    linux)
      if command -v nvidia-smi >/dev/null; then
        log "nvidia-smi found: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
      else
        warn "nvidia-smi not found — non-NVIDIA route (vLLM CPU/SGLang/Ollama/cloud) only"
      fi
      smoke_import
      [[ "$SERVICE" == "1" ]] && write_systemd_unit
      ;;
    darwin)
      smoke_import
      [[ "$SERVICE" == "1" ]] && write_launchd_plist
      ;;
    *)
      warn "unsupported platform '$platform' — environment prepared, no service config"
      smoke_import
      ;;
  esac
  log "done"
}

main "$@"
