#!/usr/bin/env bash
# Full offline evaluation suite (anomaly + RCA + baselines + summary).
# Optional: --compare (rule vs Bedrock), --online, --live-e2e, --with-load
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

mkdir -p evaluation/results

echo "=============================================="
echo " AIOps Evaluation Suite (L0 + L1 hard + strict)"
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
echo ">>> [0/5] Scoring unit tests"
$PY -m pytest -q evaluation/test_scoring.py

echo ""
echo ">>> [1/5] Anomaly Detection (core + holdout + hard)"
$PY evaluation/evaluate_anomaly.py \
  --split all \
  --output evaluation/results/anomaly_latest.json

echo ""
echo ">>> [2/5] RCA offline (default + strict grades, hard OOD)"
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
echo ">>> [3/5] Baselines (weak + SRE strong)"
$PY evaluation/evaluate_baselines.py \
  --output evaluation/results/baselines_latest.json \
  --require-beats-baselines

echo ""
echo ">>> [4/5] Summary"
$PY evaluation/report_summary.py

echo ""
echo ">>> [5/5] Quality gates (L0 + honesty floors)"
$PY - <<'PY'
import json, sys
from pathlib import Path

root = Path("evaluation/results")
ok = True

# --- Anomaly ---
a = json.loads((root / "anomaly_latest.json").read_text(encoding="utf-8"))
l0 = a.get("aggregate_l0") or a.get("aggregate") or {}
hard = a.get("aggregate_hard") or {}
core = (a.get("by_split") or {}).get("core") or {}
l0_f1 = float(l0.get("f1") or 0)
core_f1 = float(core.get("f1") or l0_f1)
hard_f1 = float(hard.get("f1") or 0) if hard else None
print(f"anomaly L0 F1={l0_f1:.3f} core F1={core_f1:.3f} hard F1={hard_f1}")
if l0_f1 < 0.70:
    print("FAIL: anomaly L0 F1 < 0.70", file=sys.stderr)
    ok = False
if core_f1 < 0.75:
    print("FAIL: anomaly core F1 < 0.75", file=sys.stderr)
    ok = False
# Hard is informational floor (stats-only suite can be tough)
if hard_f1 is not None and hard_f1 < 0.35:
    print(f"WARN: anomaly hard F1={hard_f1:.3f} < 0.35 (informational)", file=sys.stderr)

# --- RCA ---
r = json.loads((root / "rca_latest.json").read_text(encoding="utf-8"))
agg = r.get("aggregate") or {}
by = r.get("by_split") or {}
# L0 = core+holdout only for CI catalog gate
core_acc = float((by.get("core") or {}).get("accuracy") or 0)
hold_acc = float((by.get("holdout") or {}).get("accuracy") or 0)
hard_acc = float((by.get("hard") or {}).get("accuracy") or 0) if by.get("hard") else None
strict = float(agg.get("accuracy_strict") or 0)
wh = float(agg.get("wrong_hop_rate") or 0)
print(
    f"RCA core={core_acc:.3f} holdout={hold_acc:.3f} hard={hard_acc} "
    f"strict={strict:.3f} wrong_hop={wh:.3f}"
)
if core_acc < 0.85:
    print("FAIL: RCA core accuracy < 0.85", file=sys.stderr)
    ok = False
if hold_acc < 0.55:
    print("FAIL: RCA holdout accuracy < 0.55", file=sys.stderr)
    ok = False
# Strict should not be absurdly inflated forever; floor only
if strict < 0.40:
    print("FAIL: RCA strict accuracy < 0.40", file=sys.stderr)
    ok = False
if wh > 0.25:
    print(f"FAIL: wrong-hop rate {wh:.3f} > 0.25", file=sys.stderr)
    ok = False

# --- Baselines ---
b = json.loads((root / "baselines_latest.json").read_text(encoding="utf-8"))
if not b.get("system_beats_baselines"):
    print("FAIL: system did not beat weak baselines", file=sys.stderr)
    ok = False
print(f"baselines beats_weak={b.get('system_beats_baselines')} "
      f"beats_strong={b.get('system_beats_strong_baselines')}")

if not ok:
    sys.exit(1)
print("All evaluation gates passed.")
PY

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
    --limit "${LIVE_LIMIT:-10}" \
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
echo " Report strict + hard + live on CV — not L0 only"
echo "=============================================="
