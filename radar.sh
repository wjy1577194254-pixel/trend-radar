#!/usr/bin/env bash
# 命令行快捷入口：./radar.sh 直接跑一轮
cd "$(dirname "$0")" || exit 1
exec python3 radar.py "$@"
