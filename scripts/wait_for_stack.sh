#!/usr/bin/env sh
# Wait until core AIOps endpoints are healthy (Linux/macOS/Git Bash).
set -eu

TRIES="${1:-60}"
i=0
while [ "$i" -lt "$TRIES" ]; do
  ok=0
  for url in \
    "http://localhost:3000/api/health" \
    "http://localhost:8001/health" \
    "http://localhost:8002/health" \
    "http://localhost:8080/health" \
    "http://localhost:8081/health"
  do
    if curl -fsS "$url" >/dev/null 2>&1; then
      ok=$((ok + 1))
    fi
  done
  if [ "$ok" -eq 5 ]; then
    echo "stack healthy"
    exit 0
  fi
  i=$((i + 1))
  echo "waiting... ($i/$TRIES) healthy_checks=$ok/5"
  sleep 5
done
echo "timeout waiting for stack" >&2
exit 1
