# AIOps Evaluation

Ground-truth datasets + offline harnesses for **quantitative** quality of:

1. **Anomaly Detection** — precision / recall / F1 (L0 clean + L1 hard)
2. **RCA Engine** — default & **strict** accuracy, grades, wrong-hop rate, Jaccard

## Why a dataset?

Without fixed scenarios, every model/prompt change is “it looked good in the demo”.  
Suites:

| Suite | Approx size | Meaning |
|-------|-------------|---------|
| Anomaly L0 | ~28 | Clean synthetic; absolute thresholds allowed |
| Anomaly L1 hard | ~16 | Stats-only (huge thr); noise / seasonal / MV |
| RCA L0 | ~42 | Catalog regression (`config/rca_patterns.yaml`) |
| RCA L1 hard | ~10 | OOD (DNS/TLS/disk) + ambiguous |

Rule RCA matches the **YAML catalog**, not hard-coded `if scenario_id` branches.  
**L0 is a regression suite — not learned ML quality.**

## Scoring honesty

| Mode | Correct when |
|------|----------------|
| **default** | Jaccard ≥ 0.40 **or** GT⊂pred **or** keywords + class |
| **strict** | class + service + (Jaccard ≥ 0.50 **or** GT⊂pred) |

Also report: `wrong_hop_rate`, grade histogram (`exact/partial/wrong_hop/…`).

## CI gates

- Anomaly **L0** F1 ≥ 0.70, **core** F1 ≥ 0.75  
- RCA **core** ≥ 0.85, **holdout** ≥ 0.55, **strict** ≥ 0.40, wrong-hop ≤ 0.25  
- System must **beat weak baselines** (strong SRE baselines reported)

## Layout

| Path | Purpose |
|------|---------|
| `rca_scenarios.yaml` | RCA core + holdout |
| `rca_scenarios_hard.yaml` | RCA OOD / hard |
| `anomaly_scenarios.yaml` | Anomaly core + holdout |
| `anomaly_scenarios_hard.yaml` | Anomaly hard (stats-only) |
| `dataset_io.py` | Multi-file / split loader |
| `scoring.py` | Dual-mode RCA scoring, grades, P/R/F1 |
| `evaluate_rca.py` | Offline / online RCA; default + strict |
| `evaluate_anomaly.py` | Hybrid detector; L0 + hard |
| `evaluate_baselines.py` | Weak + SRE baselines |
| `evaluate_live_e2e.py` | Live chaos → RCA (+ optional ticket seed) |
| `test_scoring.py` | Unit tests for scoring honesty |
| `results/` | JSON outputs |

## Quick start

```bash
bash scripts/run-evaluation.sh
python evaluation/report_summary.py

# holdout / hard only
python evaluation/evaluate_rca.py --split hard
python evaluation/evaluate_anomaly.py --split hard
```

Optional:

```bash
# Live stack RCA
python evaluation/evaluate_live_e2e.py --limit 10 --split core
# Pure observability (no fault seed on ticket)
python evaluation/evaluate_live_e2e.py --limit 5 --no-seed-context
```

## Metrics definitions

### RCA

| Metric | Formula |
|--------|---------|
| Accuracy (default/strict) | `#correct / N` under that mode |
| Wrong-hop rate | fraction blaming wrong root service |
| Precision / Recall | Fault: TP/FN; No-fault: TN/FP |
| Semantic | Mean Jaccard token overlap |

### Anomaly

| Metric | Definition |
|--------|------------|
| TP/FP/TN/FN | Final sample vs `label` |
| L0 aggregate | core + holdout only |
| Hard aggregate | `split: hard` only |

## What to put on a CV

Prefer:

> strict RCA accuracy, hard-suite F1, live e2e accuracy + evidence completeness

Avoid leading with:

> offline default RCA 100% / anomaly F1 97% without the honesty layer story

Deep guide: [`docs/EVALUATION.md`](../docs/EVALUATION.md).
