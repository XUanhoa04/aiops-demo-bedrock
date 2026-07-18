# Evaluation guide (complete)

How quality is measured in SentinelLoop — and what the numbers **do / do not** mean.

## Three evaluation layers

| Layer | Command | Data | Measures |
|-------|---------|------|----------|
| **Offline rules** | `evaluate_rca.py --split all` | Synthetic YAML → EvidencePack | Config-driven pattern catalog coverage |
| **Offline compare** | `evaluate_rca.py --compare` | Same + optional Bedrock | `rule_acc` vs `bedrock_acc` + agreement |
| **Offline anomaly** | `evaluate_anomaly.py --split all` | Labeled series | Hybrid detector P/R/F1 |
| **Baselines** | `evaluate_baselines.py --require-beats-baselines` | Same RCA set | Must beat random / shallow baselines |
| **Live e2e** | `evaluate_live_e2e.py --limit 5` | Real chaos + OTel stack | Runtime detect/RCA path |

```bash
# Full offline (CI / laptop, no Docker required for unit+offline)
bash scripts/run-evaluation.sh
python evaluation/report_summary.py

# Optional: rule vs Bedrock (needs AWS keys)
python evaluation/evaluate_rca.py --split all --compare

# Optional: live stack
docker compose up -d --build
bash scripts/wait_for_stack.sh
python evaluation/evaluate_live_e2e.py --limit 5 --split core
python evaluation/report_summary.py
```

## Config-driven RCA (not hard-coded scenarios)

- Patterns: `config/rca_patterns.yaml`
- Loader: `shared/aiops_shared/rca_patterns.py`
- Engine: `rule_fallback.py` only orchestrates match + topology + metric fallbacks

**Freeze protocol (holdout honesty)**

1. Treat `split: holdout` as regression for the **catalog**, not free-form ML test.
2. When adding holdout scenarios, prefer new *synonyms already justified in the catalog*
   or extend the **YAML catalog first**, never `if scenario_id == ...` in Python.
3. Offline results embed `pattern_catalog.sha256` so you can audit which catalog produced the score.

## CI gates

| Check | Gate |
|-------|------|
| Anomaly overall F1 | ≥ 0.70 |
| Anomaly **core** F1 | ≥ 0.75 |
| RCA overall accuracy | ≥ 0.70 |
| RCA **holdout** accuracy | ≥ 0.55 |
| Baselines | system accuracy **>** best naive baseline |

## What *not* to claim on a CV

| Claim | Reality |
|-------|---------|
| “RCA 100% = production quality” | Offline catalog coverage only |
| “Holdout proves learned generalization” | Still rule/pattern matching |
| “Always uses Bedrock” | Bedrock optional; fail-open to rules |
| “Live accuracy = offline accuracy” | Live depends on timing/OTel fill |

## Artifacts

```
evaluation/results/
  anomaly_latest.json
  rca_latest.json
  rca_compare_latest.json   # when --compare
  baselines_latest.json
  rca_live_e2e_latest.json  # when live e2e run
```
