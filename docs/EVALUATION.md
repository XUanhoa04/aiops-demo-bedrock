# Evaluation guide (complete)

How quality is measured in SentinelLoop — and what the numbers **do / do not** mean.

## Three honesty layers

| Layer | What it measures | Typical range | CV guidance |
|-------|------------------|---------------|-------------|
| **L0 Catalog / clean synthetic** | Config pattern coverage + hybrid detector on easy series | Acc/F1 often **0.85–1.0** | Say “catalog regression”, not “production ML” |
| **L1 Hard / OOD** | Noisy anomaly series; unknown faults (DNS/TLS/disk); strict scoring | F1/Acc often **0.4–0.8** | Prefer these numbers on a CV |
| **L2 Live e2e** | Real chaos → OTel → RCA API | Varies with Loki lag / timing | Report with evidence completeness |

```bash
# Full offline (CI / laptop, no Docker required for unit+offline)
bash scripts/run-evaluation.sh
python evaluation/report_summary.py

# Optional: rule vs Bedrock (needs AWS keys)
python evaluation/evaluate_rca.py --split all --compare

# Optional: live stack
docker compose up -d --build
bash scripts/wait_for_stack.sh
python evaluation/evaluate_live_e2e.py --limit 10 --split core
python evaluation/report_summary.py

# Pure Loki path (no ticket fault seed)
python evaluation/evaluate_live_e2e.py --limit 5 --no-seed-context
```

## Scoring modes (RCA)

| Mode | Rule | Use |
|------|------|-----|
| **default** | Jaccard ≥ 0.40 **or** GT⊂pred **or** keywords≥60% + class/service guards | L0 catalog CI |
| **strict** | Class + service OK **and** (Jaccard ≥ 0.50 **or** GT⊂pred); **no** keyword-only | Honest CV metric |

Grades always reported: `exact | partial | wrong_hop | insufficient_ok | false_positive | miss`.

OOD ground truth (“unknown fault class / out of catalog”) is correct **only** if the system says **insufficient / cannot pin** — generic elevated error is **not** credit (that is a shallow metric fallback). Inventing pool/cache/gateway is a false positive.

## Config-driven RCA (not hard-coded scenarios)

- Patterns: `config/rca_patterns.yaml`
- Loader: `shared/aiops_shared/rca_patterns.py`
- Engine: `rule_fallback.py` only orchestrates match + topology + metric fallbacks

**Freeze protocol (holdout / hard honesty)**

1. Treat `split: holdout` as regression for the **catalog**, not free-form ML test.
2. `split: hard` is **OOD** — do **not** extend the catalog just to pass hard scenarios.
3. Offline results embed `pattern_catalog.sha256` so you can audit which catalog produced the score.

## Datasets

| File | Split | Role |
|------|-------|------|
| `evaluation/anomaly_scenarios.yaml` | core / holdout | Clean synthetic (L0) |
| `evaluation/anomaly_scenarios_hard.yaml` | hard | Stats-only, noise, multivariate conflict (L1) |
| `evaluation/rca_scenarios.yaml` | core / holdout | Catalog regression (L0) |
| `evaluation/rca_scenarios_hard.yaml` | hard | OOD / ambiguous / metric-only (L1) |

Hard anomaly scenarios set `absolute_threshold` extremely high so the **manual** threshold path cannot carry the label — only EWMA/z-score/STL/IF.

## Baselines

| Tier | Baselines |
|------|-----------|
| **Weak** | random · always elevated error · empty |
| **Strong (SRE)** | always ticket service · highest error neighbor · last deploy · log phrase bag |

CI requires **beating weak**. Beating strong is reported and desirable, not always required on OOD-heavy sets.

## CI gates

| Check | Gate |
|-------|------|
| Anomaly **L0** F1 | ≥ 0.70 |
| Anomaly **core** F1 | ≥ 0.75 |
| Anomaly hard F1 | reported (warn if &lt; 0.35) |
| RCA **core** accuracy (default) | ≥ 0.85 |
| RCA **holdout** accuracy | ≥ 0.55 |
| RCA **strict** accuracy (overall) | ≥ 0.40 |
| Wrong-hop rate | ≤ 0.25 |
| Baselines | system **>** best weak baseline |

## What *not* to claim on a CV

| Claim | Reality |
|-------|---------|
| “RCA 100% = production quality” | Offline default = catalog coverage |
| “Holdout proves learned generalization” | Still rule/pattern matching |
| “Always uses Bedrock” | Bedrock optional; fail-open to rules |
| “Live accuracy = offline accuracy” | Live depends on timing/OTel fill; use completeness |

**Safe CV wording**

> Offline catalog regression (default) + strict scoring + hard/OOD suite + optional live e2e with evidence completeness. L0 accuracy is high by design; strict/hard/live are the honest quality signals.

## Artifacts

```
evaluation/results/
  anomaly_latest.json      # includes aggregate_l0 + aggregate_hard
  rca_latest.json          # accuracy + accuracy_strict + grades + wrong_hop
  rca_compare_latest.json  # when --compare
  baselines_latest.json    # weak + strong
  rca_live_e2e_latest.json # when live e2e run
```
