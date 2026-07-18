#!/usr/bin/env sh
# Wait until the full AIOps stack is healthy.
set -eu

TRIES="${1:-90}"
i=0
URLS="
http://localhost:8080/health
http://localhost:8081/health
http://localhost:8001/health
http://localhost:8002/health
http://localhost:8003/health
http://localhost:8004/health
http://localhost:8005/health
"
# Grafana is slow on first boot — best-effort only
GRAFANA="http://localhost:3000/api/health"

count_urls() {
  n=0
  for _ in $URLS; do n=$((n + 1)); done
  echo "$n"
}
TOTAL=$(count_urls)

while [ "$i" -lt "$TRIES" ]; do
  ok=0
  for url in $URLS; do
    if curl -fsS "$url" >/dev/null 2>&1; then
      ok=$((ok + 1))
    fi
  done
  g="no"
  if curl -fsS "$GRAFANA" >/dev/null 2>&1; then
    g="yes"
  fi
  echo "waiting... ($i/$TRIES) aiops=$ok/$TOTAL grafana=$g"
  if [ "$ok" -eq "$TOTAL" ]; then
    echo "stack healthy (grafana=$g)"
    exit 0
  fi
  i=$((i + 1))
  sleep 5
done
echo "timeout waiting for stack" >&2
exit 1
