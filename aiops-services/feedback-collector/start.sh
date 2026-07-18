#!/bin/sh
# Start FastAPI (8005) + Streamlit UI (8502) in one container.
set -e

echo "starting feedback API on :${PORT:-8005}"
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8005}" &
API_PID=$!

i=0
while [ "$i" -lt 30 ]; do
  if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${PORT:-8005}/health', timeout=1)" 2>/dev/null; then
    break
  fi
  i=$((i + 1))
  sleep 0.5
done

echo "starting Streamlit feedback UI on :${STREAMLIT_PORT:-8502}"
export FEEDBACK_API_URL="${FEEDBACK_API_URL:-http://127.0.0.1:${PORT:-8005}}"
streamlit run app/streamlit_app.py \
  --server.address 0.0.0.0 \
  --server.port "${STREAMLIT_PORT:-8502}" \
  --server.headless true \
  --browser.gatherUsageStats false &
UI_PID=$!

term() {
  kill "$API_PID" "$UI_PID" 2>/dev/null || true
  wait "$API_PID" "$UI_PID" 2>/dev/null || true
}
trap term INT TERM

while kill -0 "$API_PID" 2>/dev/null && kill -0 "$UI_PID" 2>/dev/null; do
  sleep 2
done
term
exit 1
