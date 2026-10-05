#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Avoid inherited proxy settings that break local runtime.
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

if ! command -v redis-cli >/dev/null 2>&1; then
  echo "redis-cli not found. Install Redis first: brew install redis"
  exit 1
fi

# Start local Redis service if it is not already running.
if ! redis-cli ping >/dev/null 2>&1; then
  echo "Starting Redis via brew services..."
  brew services start redis >/dev/null
  sleep 2
fi

if ! redis-cli ping >/dev/null 2>&1; then
  echo "Redis is not reachable on localhost:6379"
  exit 1
fi

# Prevent duplicate polling and duplicate workers.
if pgrep -f "python -m app.bot.main" >/dev/null 2>&1; then
  echo "Bot process is already running. Stop it first."
  exit 1
fi
if pgrep -f "celery -A app.tasks.celery_app worker" >/dev/null 2>&1; then
  echo "Celery worker is already running. Stop it first."
  exit 1
fi
if pgrep -f "celery -A app.tasks.celery_app beat" >/dev/null 2>&1; then
  echo "Celery beat is already running. Stop it first."
  exit 1
fi

# Prefork pool often breaks on macOS (spawn + billiard _loc unpack error).
CELERY_WORKER_ARGS=(-l info -c 2)
if [[ "$(uname -s)" == "Darwin" ]]; then
  CELERY_WORKER_ARGS=(-l info --pool=solo)
  export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
fi

echo "Starting PriceRadar stack:"
echo "- bot"
echo "- celery worker (${CELERY_WORKER_ARGS[*]})"
echo "- celery beat"

python -m app.bot.main &
BOT_PID=$!

celery -A app.tasks.celery_app worker "${CELERY_WORKER_ARGS[@]}" &
WORKER_PID=$!

celery -A app.tasks.celery_app beat -l info &
BEAT_PID=$!

cleanup() {
  echo ""
  echo "Stopping PriceRadar stack..."
  kill "$BOT_PID" "$WORKER_PID" "$BEAT_PID" 2>/dev/null || true
  wait "$BOT_PID" "$WORKER_PID" "$BEAT_PID" 2>/dev/null || true
}

trap cleanup INT TERM EXIT

echo "PIDs: bot=$BOT_PID worker=$WORKER_PID beat=$BEAT_PID"
echo "Press Ctrl+C to stop all."

# If any process exits unexpectedly, stop the rest and return non-zero.
# macOS ships bash 3.2, which does not support `wait -n`.
set +e
STATUS=0
while true; do
  for pid in "$BOT_PID" "$WORKER_PID" "$BEAT_PID"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid"
      STATUS=$?
      break 2
    fi
  done
  sleep 1
done
set -e

if [[ $STATUS -ne 0 ]]; then
  echo "One of the processes exited with code $STATUS"
  exit "$STATUS"
fi
