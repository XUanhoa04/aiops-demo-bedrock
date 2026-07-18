# AIOps Evaluation

Ground-truth datasets + offline harnesses for **quantitative** quality of:

1. **Anomaly Detection** — precision / recall / F1  
2. **RCA Engine** — accuracy, P/R, Jaccard semantic similarity, mean iterations  

## Why a dataset?

Without fixed scenarios, every model/prompt change is “it looked good in the demo”.  
This mini set (**~17 RCA** incl. hard + **topology wrong-hop** + **paraphrased hold-out** +
**8 anomaly** series) is a **regression suite** — not a claim of production-perfect RCA.
CI requires accuracy ≥ 0.70 **and** the system to **beat naive baselines**.

Scoring is intentionally strict: fault-class match + wrong-hop service guards
(keywords alone are insufficient). Topology catalog: `config/service_topology.yaml`.

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
| Correct | Fault-class + service guards; Jaccard ≥ 0.40 **or** GT⊂pred **or** ≥60% keywords *with* class match |
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

## Sample results

Re-run after every change — do **not** treat a green CI as “100% prod quality”:

```bash
bash scripts/run-evaluation.sh
# → evaluation/results/{anomaly,rca,baselines}_latest.json
```

Gates: anomaly F1 ≥ 0.75, RCA accuracy ≥ 0.70, system beats baselines.
