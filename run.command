#!/bin/bash
cd "$(dirname "$0")" || exit 1
clear
echo "=========================================="
echo "  Social Trend Radar — fetch a round"
echo "=========================================="
echo
python3 radar.py --open
echo
read -n 1 -s -r -p "Done. Press any key to close."
echo
