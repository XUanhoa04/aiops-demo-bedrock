# AIOps Evaluation

Ground-truth datasets + offline harnesses for **quantitative** quality of:

1. **Anomaly Detection** — precision / recall / F1  
2. **RCA Engine** — accuracy, P/R, Jaccard semantic similarity, mean iterations  

## Why a dataset?

Without fixed scenarios, every model/prompt change is “it looked good in the demo”.  
Suite size (approx.): **~40 RCA** (core + holdout) and **~28 anomaly** series.
Rule RCA matches **`config/rca_patterns.yaml`** (config-driven), not hard-coded
`if scenario_id` branches. This is a **regression suite** for that catalog — not
learned ML quality.

CI gates:
- Anomaly overall F1 ≥ 0.70, **core** F1 ≥ 0.75  
- RCA overall accuracy ≥ 0.70, **holdout** accuracy ≥ 0.55  
- System must **beat naive baselines**

Scoring is strict: fault-class match + wrong-hop service guards.
Topology catalog: `config/service_topology.yaml` (checkout/payment/inventory/fraud).

## Layout

| Path | Purpose |
|------|---------|
| `rca_scenarios.yaml` | RCA core + holdout (split field) |
| `anomaly_scenarios.yaml` | Anomaly core + holdout (uni + multivariate) |
| `dataset_io.py` | Multi-file / split loader |
| `scoring.py` | Jaccard, keyword, class/service guards, P/R/F1 |
| `evaluate_rca.py` | Offline / online RCA; reports core vs holdout |
| `evaluate_anomaly.py` | Offline hybrid detector; multivariate IF path |
| `results/` | JSON outputs |

## Quick start

```bash
# one command (Git Bash / WSL / Linux)
bash scripts/run-evaluation.sh

# or manually
pip install pyyaml
python evaluation/evaluate_anomaly.py --split all
python evaluation/evaluate_rca.py --mode offline --split all
# holdout only (anti-overfit check)
python evaluation/evaluate_rca.py --split holdout
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
