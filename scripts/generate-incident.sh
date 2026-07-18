#!/usr/bin/env bash
# =============================================================================
# generate-incident.sh — demo helper: chaos + load + anomaly → incident pipeline
#
# Usage:
#   chmod +x scripts/generate-incident.sh
#   ./scripts/generate-incident.sh
#   ./scripts/generate-incident.sh --full     # wait for RCA + remediation propose
#   ./scripts/generate-incident.sh --reset    # clear chaos only
#
# Requires: curl, python3 (or py -3 on Windows Git Bash via GENERATE_PYTHON)
# =============================================================================
set -euo pipefail

CHECKOUT_URL="${CHECKOUT_URL:-http://localhost:8080}"
DETECTOR_URL="${DETECTOR_URL:-http://localhost:8001}"
INCIDENTS_URL="${INCIDENTS_URL:-http://localhost:8002}"
RCA_URL="${RCA_URL:-http://localhost:8003}"
REMEDIATION_URL="${REMEDIATION_URL:-http://localhost:8004}"
WAIT_SEC="${WAIT_SEC:-45}"
FULL=0
RESET=0

for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    --reset) RESET=1 ;;
    --help|-h)
      sed -n '2,14p' "$0"
      exit 0
      ;;
  esac
done

json_get() {
  # Usage: json_get URL [METHOD] [BODY]
  local url="$1" method="${2:-GET}" body="${3:-}"
  if [ -n "$body" ]; then
    curl -fsS -X "$method" "$url" \
      -H "Content-Type: application/json" \
      -d "$body"
  else
    curl -fsS -X "$method" "$url" \
      -H "Content-Type: application/json"
  fi
}

echo "== generate-incident: AIOps demo =="

if [ "$RESET" -eq 1 ]; then
  echo "[reset] chaos on checkout + payment"
  json_get "$CHECKOUT_URL/chaos" POST '{"error_rate":0.01,"extra_latency_ms":0}' >/dev/null || true
  json_get "http://localhost:8081/chaos" POST '{"error_rate":0.01,"extra_latency_ms":0}' >/dev/null || true
  echo "done"
  exit 0
fi

echo "[1/5] inject chaos on checkout-service (error_rate=0.45)"
json_get "$CHECKOUT_URL/chaos" POST '{"error_rate":0.45,"extra_latency_ms":200}'
echo

echo "[2/5] generate light load (15s)"
# Prefer shared Python load helper if present
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v py >/dev/null 2>&1; then
  PY="py -3"
else
  PY=""
fi
if [ -n "$PY" ] && [ -f "$(dirname "$0")/load_test.py" ]; then
  $PY "$(dirname "$0")/load_test.py" --rps 12 --duration 15 --url "$CHECKOUT_URL" || true
else
  # Fallback: burst of checkout POSTs
  for i in $(seq 1 40); do
    curl -fsS -X POST "$CHECKOUT_URL/checkout" \
      -H "Content-Type: application/json" \
      -d '{"order_id":"demo-'"$i"'","amount":10}' >/dev/null 2>&1 || true
  done
fi
echo

echo "[3/5] manual anomaly inject (guarantees ticket within seconds)"
DETECT_JSON=$(json_get "$DETECTOR_URL/detect" POST \
  '{"service_name":"checkout-service","metric_name":"http_error_rate","metric_value":0.45,"threshold":0.15}')
echo "$DETECT_JSON" | head -c 400
echo
echo

echo "[4/5] wait up to ${WAIT_SEC}s for incident ticket..."
DEADLINE=$((SECONDS + WAIT_SEC))
INCIDENT_ID=""
while [ "$SECONDS" -lt "$DEADLINE" ]; do
  LIST=$(json_get "$INCIDENTS_URL/incidents?limit=3" || true)
  # crude extract first "id"
  INCIDENT_ID=$(printf '%s' "$LIST" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)
  if [ -n "$INCIDENT_ID" ]; then
    echo "  incident_id=$INCIDENT_ID"
    break
  fi
  sleep 2
  echo "  waiting..."
done

if [ -z "$INCIDENT_ID" ]; then
  echo "ERROR: no incident. Check: docker compose logs -f aiops-anomaly-detector aiops-incident-manager" >&2
  exit 1
fi

echo "[5/5] incident snapshot"
json_get "$INCIDENTS_URL/incidents/$INCIDENT_ID" | head -c 1200
echo
echo

if [ "$FULL" -eq 1 ]; then
  echo "[full] force RCA analyze..."
  json_get "$RCA_URL/analyze-incident/$INCIDENT_ID?force=true&persist=true" POST || \
    json_get "$RCA_URL/rca/analyze" POST "{\"incident_id\":\"$INCIDENT_ID\",\"force\":true,\"wait\":true}" || true
  echo
  echo "[full] wait for root_cause..."
  for _ in $(seq 1 30); do
    INC=$(json_get "$INCIDENTS_URL/incidents/$INCIDENT_ID" || true)
    if printf '%s' "$INC" | grep -q '"root_cause"[[:space:]]*:[[:space:]]*"[^n]'; then
      echo "  RCA present"
      break
    fi
    sleep 2
  done
  echo "[full] propose remediation (idempotent)"
  json_get "$REMEDIATION_URL/remediate/propose" POST \
    "{\"incident_id\":\"$INCIDENT_ID\",\"actions\":[]}" || true
  echo
fi

echo "Done."
echo "Links:"
echo "  Incidents UI:   $INCIDENTS_URL/"
echo "  Remediation UI: http://localhost:8501"
echo "  Feedback UI:    http://localhost:8502"
echo "  Grafana:        http://localhost:3000"
echo "  RCA analyze:    $RCA_URL/analyze-incident/$INCIDENT_ID"
echo "  Incident JSON:  $INCIDENTS_URL/incidents/$INCIDENT_ID"
