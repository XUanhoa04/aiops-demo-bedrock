#!/usr/bin/env bash
# Run full offline evaluation (anomaly + RCA) with one command.
# Works without Docker for offline mode; optional --online if stack is up.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

mkdir -p evaluation/results

echo "=============================================="
echo " AIOps Evaluation Suite"
echo "=============================================="

# Prefer python3 / py launcher
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v py >/dev/null 2>&1; then
  PY="py -3"
else
  PY=python
fi

# Ensure PyYAML for dataset load
$PY -c "import yaml" 2>/dev/null || $PY -m pip install --quiet pyyaml

echo ""
echo ">>> [1/3] Anomaly Detection evaluation (core + holdout)"
$PY evaluation/evaluate_anomaly.py \
  --split all \
  --output evaluation/results/anomaly_latest.json

echo ""
echo ">>> [2/3] RCA evaluation offline (core + holdout)"
$PY evaluation/evaluate_rca.py \
  --mode offline \
  --split all \
  --output evaluation/results/rca_latest.json

echo ""
echo ">>> [3/3] Baselines (system must beat naive baselines)"
$PY evaluation/evaluate_baselines.py \
  --output evaluation/results/baselines_latest.json \
  --require-beats-baselines

if [[ "${1:-}" == "--online" ]]; then
  echo ""
  echo ">>> [bonus] RCA evaluation (online HTTP)"
  $PY evaluation/evaluate_rca.py \
    --mode online \
    --incident-url "${INCIDENT_URL:-http://localhost:8002}" \
    --rca-url "${RCA_URL:-http://localhost:8003}" \
    --output evaluation/results/rca_online_latest.json || true
fi

if [[ "${1:-}" == "--with-load" ]]; then
  echo ""
  echo ">>> Dynamic multi-stage load (demo profile)"
  $PY scripts/dynamic_load.py --profile demo --stage-seconds "${STAGE_SECONDS:-15}" || true
fi

echo ""
echo "=============================================="
echo " Results written under evaluation/results/"
echo "  - anomaly_latest.json"
echo "  - rca_latest.json"
echo "=============================================="

# Print short summary if jq not available use python
$PY - <<'PY'
import json
from pathlib import Path
for name in ("anomaly_latest.json", "rca_latest.json"):
    p = Path("evaluation/results") / name
    if not p.exists():
        continue
    d = json.loads(p.read_text(encoding="utf-8"))
    agg = d.get("aggregate") or {}
    print(f"\n{name}:")
    for k, v in agg.items():
        print(f"  {k}: {v}")
PY
