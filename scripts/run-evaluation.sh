#!/usr/bin/env bash
# Full offline evaluation suite (anomaly + RCA + baselines + summary).
# Optional: --compare (rule vs Bedrock), --online, --live-e2e, --with-load
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

mkdir -p evaluation/results

echo "=============================================="
echo " AIOps Evaluation Suite (complete)"
echo "=============================================="

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v py >/dev/null 2>&1; then
  PY="py -3"
else
  PY=python
fi

$PY -c "import yaml" 2>/dev/null || $PY -m pip install --quiet pyyaml

export PYTHONPATH="${ROOT}/shared:${ROOT}/aiops-services/anomaly-detector:${ROOT}/aiops-services/rca-engine:${ROOT}:${PYTHONPATH:-}"

echo ""
echo ">>> [1/4] Anomaly Detection (core + holdout)"
$PY evaluation/evaluate_anomaly.py \
  --split all \
  --output evaluation/results/anomaly_latest.json

echo ""
echo ">>> [2/4] RCA offline config-driven rules (core + holdout)"
COMPARE_ARGS=()
if [[ "${1:-}" == "--compare" ]] || [[ "${*}" == *"--compare"* ]]; then
  COMPARE_ARGS+=(--compare)
  echo "    (also comparing Bedrock if credentials exist)"
fi
$PY evaluation/evaluate_rca.py \
  --mode offline \
  --split all \
  "${COMPARE_ARGS[@]}" \
  --output evaluation/results/rca_latest.json

echo ""
echo ">>> [3/4] Baselines (must beat naive baselines)"
$PY evaluation/evaluate_baselines.py \
  --output evaluation/results/baselines_latest.json \
  --require-beats-baselines

echo ""
echo ">>> [4/4] Summary"
$PY evaluation/report_summary.py

if [[ "${*}" == *"--online"* ]]; then
  echo ""
  echo ">>> [bonus] RCA online HTTP"
  $PY evaluation/evaluate_rca.py \
    --mode online \
    --incident-url "${INCIDENT_URL:-http://localhost:8002}" \
    --rca-url "${RCA_URL:-http://localhost:8003}" \
    --output evaluation/results/rca_online_latest.json || true
fi

if [[ "${*}" == *"--live-e2e"* ]]; then
  echo ""
  echo ">>> [bonus] Live E2E chaos → RCA"
  $PY evaluation/evaluate_live_e2e.py \
    --limit "${LIVE_LIMIT:-5}" \
    --split core \
    --output evaluation/results/rca_live_e2e_latest.json || true
  $PY evaluation/report_summary.py
fi

if [[ "${*}" == *"--with-load"* ]]; then
  echo ""
  echo ">>> Dynamic multi-stage load (demo profile)"
  $PY scripts/dynamic_load.py --profile demo --stage-seconds "${STAGE_SECONDS:-15}" || true
fi

echo ""
echo "=============================================="
echo " Results under evaluation/results/"
echo "=============================================="
