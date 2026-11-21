#!/bin/bash
# publish.sh — 把最新报告同步到 dist/ 并部署到 Cloudflare Pages
#
# 产物：https://$PROJECT.pages.dev
#   /            最新 HTML 报告
#   /latest.md   给 AI 读的纯文本版
#   /data.json   全量数据
#
# token 从 portfolio-build/.env.deploy 读，不在本目录留副本。

set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1

ROOT="$(cd "$HERE/.." && pwd)"
ENVF="$ROOT/portfolio-build/.env.deploy"
PROJECT="pulse"

if [[ ! -f "$ENVF" ]]; then
  echo "找不到 $ENVF" >&2
  exit 1
fi

export CLOUDFLARE_API_TOKEN="$(grep -E '^CLOUDFLARE_API_TOKEN=' "$ENVF" | cut -d= -f2- | tr -d "\"' ")"
export CLOUDFLARE_ACCOUNT_ID="$(grep -E '^CLOUDFLARE_ACCOUNT_ID=' "$ENVF" | cut -d= -f2- | tr -d "\"' ")"
# 本机 TUN 代理会劫持 api.cloudflare.com，wrangler 走代理
: "${RADAR_HTTP_PROXY:=}"
[ -n "$RADAR_HTTP_PROXY" ] && export HTTPS_PROXY="$RADAR_HTTP_PROXY"
[ -n "$RADAR_HTTP_PROXY" ] && export HTTP_PROXY="$RADAR_HTTP_PROXY"

# launchd 环境下 PATH 很干净，node 得手动找
NODE_BIN="$(dirname "$(command -v node 2>/dev/null || echo "$ROOT/.tools/node-v24.22.1-darwin-arm64/bin/node")")"
export PATH="$NODE_BIN:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

WRANGLER="$ROOT/portfolio-build/deploy/node_modules/.bin/wrangler"
if [[ ! -e "$WRANGLER" ]]; then
  echo "找不到 wrangler：$WRANGLER" >&2
  exit 1
fi

LATEST_DIR="$(ls -d out/*/ 2>/dev/null | tail -1)"
if [[ -z "$LATEST_DIR" ]]; then
  echo "out/ 里没有报告，先跑 radar.py" >&2
  exit 1
fi

rm -rf dist && mkdir -p dist
cp out/latest.html dist/index.html
[[ -f out/latest.md ]] && cp out/latest.md dist/latest.md
[[ -f "${LATEST_DIR}trends.json" ]] && cp "${LATEST_DIR}trends.json" dist/data.json
# 让人一眼知道什么时候更新的
date "+%Y-%m-%d %H:%M:%S" > dist/updated.txt

echo "→ 部署 $(du -sh dist | cut -f1) 到 $PROJECT …"
"$WRANGLER" pages deploy dist --project-name "$PROJECT" --commit-dirty=true 2>&1 | grep -E 'Success|Deploying|complete|error|Error' | head -6

echo "✓ https://$PROJECT.pages.dev"
