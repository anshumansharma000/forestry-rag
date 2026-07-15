#!/bin/sh
set -eu

celery -A tasks.celery_app worker \
  --loglevel="${LOG_LEVEL:-INFO}" \
  --pool=threads \
  --concurrency=1 &
celery_pid=$!

uvicorn app:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" &
api_pid=$!

shutdown() {
  kill -TERM "$celery_pid" "$api_pid" 2>/dev/null || true
  wait "$celery_pid" "$api_pid" 2>/dev/null || true
}

trap shutdown INT TERM EXIT

while kill -0 "$celery_pid" 2>/dev/null && kill -0 "$api_pid" 2>/dev/null; do
  sleep 2
done

shutdown
exit 1
