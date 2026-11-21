#!/bin/bash
# install-daily.sh — 把趋势雷达装成每日自动刷新 + 常驻网页
#
#   ./install-daily.sh              每天 09:00 抓一次，8822 端口常驻
#   HOUR=7 MIN=30 PORT=9001 ./install-daily.sh
#   ./install-daily.sh --uninstall  卸掉
#
# 装的是用户级 LaunchAgent（~/Library/LaunchAgents），不碰系统目录。

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || true)"
PORT="${PORT:-8822}"
HOURS="${HOURS:-12,17}"   # 逗号分隔，多个时间点
MIN="${MIN:-0}"

LABEL_FETCH="com.wangjingyu.trend-radar.fetch"
LABEL_SERVE="com.wangjingyu.trend-radar.serve"
LABEL_PAPER="com.wangjingyu.trend-radar.paper"
PAPER_PORT="${PAPER_PORT:-8833}"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HERE/logs"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl unload "$AGENTS/$LABEL_FETCH.plist" 2>/dev/null || true
  launchctl unload "$AGENTS/$LABEL_SERVE.plist" 2>/dev/null || true
  launchctl unload "$AGENTS/$LABEL_PAPER.plist" 2>/dev/null || true
  rm -f "$AGENTS/$LABEL_PAPER.plist"
  rm -f "$AGENTS/$LABEL_FETCH.plist" "$AGENTS/$LABEL_SERVE.plist"
  echo "已卸载两个 LaunchAgent（服务若仍占用端口，:${PORT} 可手动 kill）"
  exit 0
fi

if [[ -z "$PY" ]]; then
  echo "找不到 python3" >&2
  exit 1
fi

mkdir -p "$AGENTS" "$LOGS"

# ---------- 1) 每天定时抓取（多个时间点） ----------
CAL_ITEMS=""
IFS=',' read -ra HH <<< "$HOURS"
for h in "${HH[@]}"; do
  CAL_ITEMS="$CAL_ITEMS    <dict><key>Hour</key><integer>$h</integer><key>Minute</key><integer>$MIN</integer></dict>
"
done

cat > "$AGENTS/$LABEL_FETCH.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL_FETCH</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$HERE/radar.py</string>
    <string>--with-browser</string>
    <string>--publish</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>StartCalendarInterval</key>
  <array>
$CAL_ITEMS  </array>
  <key>StandardOutPath</key><string>$LOGS/fetch.log</string>
  <key>StandardErrorPath</key><string>$LOGS/fetch.err</string>
</dict>
</plist>
EOF

# ---------- 2) 常驻网页服务 ----------
cat > "$AGENTS/$LABEL_SERVE.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL_SERVE</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>-m</string><string>http.server</string>
    <string>$PORT</string>
    <string>--bind</string><string>127.0.0.1</string>
    <string>--directory</string><string>$HERE</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGS/serve.log</string>
  <key>StandardErrorPath</key><string>$LOGS/serve.err</string>
</dict>
</plist>
EOF

# ---------- 3) 论文讲解器 ----------
cat > "$AGENTS/$LABEL_PAPER.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL_PAPER</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$HERE/paper_server.py</string>
    <string>$PAPER_PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGS/paper.log</string>
  <key>StandardErrorPath</key><string>$LOGS/paper.err</string>
</dict>
</plist>
EOF

# ---------- 装载 ----------
launchctl unload "$AGENTS/$LABEL_FETCH.plist" 2>/dev/null || true
launchctl unload "$AGENTS/$LABEL_SERVE.plist" 2>/dev/null || true
launchctl unload "$AGENTS/$LABEL_PAPER.plist" 2>/dev/null || true
launchctl load "$AGENTS/$LABEL_FETCH.plist"
launchctl load "$AGENTS/$LABEL_PAPER.plist"
launchctl load "$AGENTS/$LABEL_SERVE.plist"

sleep 2
echo "装好了："
echo "  每天 ${HOURS}:$(printf '%02d' "$MIN") 抓取  →  $LOGS/fetch.log"
echo "  常驻服务 127.0.0.1:${PORT}          →  $LOGS/serve.log"
echo "  论文讲解器 127.0.0.1:${PAPER_PORT}       →  $LOGS/paper.log"
echo
echo "  报告：http://127.0.0.1:${PORT}/out/latest.html"
echo "  索引：http://127.0.0.1:${PORT}/out/"
echo
echo "  立即跑一次抓取：launchctl start $LABEL_FETCH"
echo "  看状态：        launchctl list | grep trend-radar"
