#!/bin/bash
# daily.sh — 每日任务：抓一轮 + 发布到 Cloudflare Pages
# 由 launchd 的 com.wangjingyu.trend-radar.fetch 调用
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1

PY=/opt/homebrew/bin/python3
[[ -x "$PY" ]] || PY="$(command -v python3)"

echo "===== $(date '+%Y-%m-%d %H:%M:%S') 开始 ====="
"$PY" radar.py --with-browser
echo "----- 抓取结束，开始发布 -----"
./publish.sh
echo "===== $(date '+%H:%M:%S') 完成 ====="
