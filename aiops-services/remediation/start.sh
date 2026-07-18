#!/bin/sh
# Start FastAPI (8004) + Streamlit UI (8501) in one container.
set -e

echo "starting remediation API on :${PORT:-8004}"
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8004}" &
API_PID=$!

# Wait for API health before Streamlit (best-effort)
i=0
while [ "$i" -lt 30 ]; do
  if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${PORT:-8004}/health', timeout=1)" 2>/dev/null; then
    break
  fi
  i=$((i + 1))
  sleep 0.5
done

echo "starting Streamlit UI on :${STREAMLIT_PORT:-8501}"
export REMEDIATION_API_URL="${REMEDIATION_API_URL:-http://127.0.0.1:${PORT:-8004}}"
streamlit run app/streamlit_app.py \
  --server.address 0.0.0.0 \
  --server.port "${STREAMLIT_PORT:-8501}" \
  --server.headless true \
  --browser.gatherUsageStats false &
UI_PID=$!

term() {
  kill "$API_PID" "$UI_PID" 2>/dev/null || true
  wait "$API_PID" "$UI_PID" 2>/dev/null || true
}
trap term INT TERM

# Exit if either process dies
while kill -0 "$API_PID" 2>/dev/null && kill -0 "$UI_PID" 2>/dev/null; do
  sleep 2
done
term
exit 1
