#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Avoid Cursor sandbox proxies if somehow inherited
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
unset socks_proxy socks5_proxy SOCKS_PROXY SOCKS5_PROXY

if [[ ! -f /tmp/priceradar-venv/bin/activate ]]; then
  echo "venv not found at /tmp/priceradar-venv — recreate it first"
  exit 1
fi

source /tmp/priceradar-venv/bin/activate
set -a
source .env
set +a

echo "Starting bot (token ending …${TELEGRAM_BOT_TOKEN: -6})"
exec python -m app.bot.main
