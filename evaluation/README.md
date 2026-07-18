# AIOps Evaluation

Ground-truth datasets + offline harnesses for **quantitative** quality of:

1. **Anomaly Detection** — precision / recall / F1  
2. **RCA Engine** — accuracy, P/R, Jaccard semantic similarity, mean iterations  

## Why a dataset?

Without fixed scenarios, every model/prompt change is “it looked good in the demo”.  
This mini set (**15 RCA** incl. hard + **topology wrong-hop** scenarios + **8 anomaly** series)
is a **regression suite** you can re-run after every change to rules, thresholds, topology, or
Bedrock prompts. CI also requires the system to **beat naive baselines** (`evaluate_baselines.py`).

Topology catalog used by RCA: `config/service_topology.yaml` (checkout → payment).

## Layout

| Path | Purpose |
|------|---------|
| `rca_scenarios.yaml` | 10 RCA scenarios + ground truth |
| `anomaly_scenarios.yaml` | 8 labeled anomaly series |
| `scoring.py` | Jaccard, keyword match, P/R/F1 |
| `evaluate_rca.py` | Offline (default) / online RCA eval |
| `evaluate_anomaly.py` | Offline hybrid detector eval |
| `results/` | JSON outputs |

## Quick start

```bash
# one command (Git Bash / WSL / Linux)
bash scripts/run-evaluation.sh

# or manually
pip install pyyaml
python evaluation/evaluate_anomaly.py
python evaluation/evaluate_rca.py --mode offline
```

Optional:

```bash
# Live stack RCA API
python evaluation/evaluate_rca.py --mode online

# Dynamic telemetry on running compose
python scripts/dynamic_load.py --profile demo
python scripts/run_scenario.py --scenario rca-01-payment-db-pool
```

## Metrics definitions

### RCA

| Metric | Formula |
|--------|---------|
| Accuracy | `#correct / N` |
| Correct | Jaccard(pred, GT) ≥ 0.35 **or** ≥50% keywords in pred **or** substring match |
| Precision / Recall | Fault scenarios: TP=correct, FN=miss; Normal scenarios: TN=correct, FP=false alarm |
| Semantic similarity | Mean Jaccard token overlap |
| Mean iterations | Rule=1; Bedrock+fallback may be 2 |

### Anomaly

| Metric | Definition |
|--------|------------|
| TP / FP / TN / FN | Final sample prediction vs `label: anomaly\|normal` |
| Precision | TP/(TP+FP) |
| Recall | TP/(TP+FN) |
| F1 | 2PR/(P+R) |

## Sample results (latest offline run)

| Suite | Accuracy | Precision | Recall | F1 | Extra |
|-------|----------|-----------|--------|-----|-------|
| Anomaly (n=8) | 100% | 100% | 100% | 100% | TP=4 FP=0 TN=4 FN=0 |
| RCA offline (n=10) | 100% | 100% | 100% | 100% | mean Jaccard 0.46, mean iterations 1.0 |

Re-run with `bash scripts/run-evaluation.sh` — numbers are also in `evaluation/results/*_latest.json`.
