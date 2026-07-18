#!/bin/sh
# Start FastAPI (8007) + Streamlit Engine QA UI (8503).
set -e

echo "starting engine-qa API on :${PORT:-8007}"
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8007}" &
API_PID=$!

i=0
while [ "$i" -lt 30 ]; do
  if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${PORT:-8007}/health', timeout=1)" 2>/dev/null; then
    break
  fi
  i=$((i + 1))
  sleep 0.5
done

echo "starting Streamlit Engine QA UI on :${STREAMLIT_PORT:-8503}"
export ENGINE_QA_API_URL="${ENGINE_QA_API_URL:-http://127.0.0.1:${PORT:-8007}}"
streamlit run app/streamlit_app.py \
  --server.address 0.0.0.0 \
  --server.port "${STREAMLIT_PORT:-8503}" \
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
