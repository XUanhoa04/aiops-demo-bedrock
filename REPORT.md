# AIOps Demo Bedrock — Status Report

**Updated:** 2026-07-18 · Repo: https://github.com/XUanhoa04/aiops-demo-bedrock

## What shipped

| Capability | Status |
|------------|--------|
| Compose monorepo (apps + AIOps services + LGTM) | ✅ |
| Hybrid anomaly (EWMA/Z/STL/IForest) + explanations | ✅ |
| Multi-signal context + confidence scorer | ✅ |
| Decision Engine (policy table, gated remediate / RCA / escalate) | ✅ |
| Incident Manager + correlation + Tempo deep-links | ✅ |
| RCA grounded evidence + Bedrock + rule fallback | ✅ |
| Remediation risk-gated + Streamlit UI | ✅ |
| Feedback collector + Engine QA meta-SLOs | ✅ |
| Dynamic multi-stage load + fault_mode chaos | ✅ |
| Evaluation suite (anomaly + RCA + **baselines**) | ✅ |
| GitHub Actions CI (tests + eval gates) | ✅ |
| One-shot demo script | ✅ |

## Quantitative offline results (local / CI)

| Suite | Metric | Value |
|-------|--------|-------|
| Anomaly (8) | F1 / Precision / Recall | ~1.00 (gate ≥ 0.75) |
| RCA (12 scenarios incl. hard) | Accuracy | gate ≥ 0.70 |
| Baselines | System beats random / always-error / empty | required in CI |

See `evaluation/results/*_latest.json` after `bash scripts/run-evaluation.sh`.

## Production gaps (intentional)

See `docs/ARCHITECTURE.md` for full trade-off table. Short list:

- Redis LIST not Streams/Kafka; SQLite not Postgres
- Detector state is in-memory
- Control-plane APIs are open on localhost (no SSO)
- Eval set is small (regression gate, not full prod coverage)

## How to run

```bash
cp .env.example .env
docker compose up -d --build
bash scripts/wait_for_stack.sh
python scripts/demo_one_shot.py

# Offline quality gates (no AWS)
bash scripts/run-evaluation.sh
# or: make ci
```

## CV talking points

1. **Safety** — LLM fallback; high-risk remediations need approval; gated auto path.
2. **Explainability** — sigma/EWMA sentences + confidence breakdown.
3. **Grounded GenAI** — RCA only on Prom/Loki/Tempo evidence.
4. **Cost-aware routing** — Decision Engine bands limit Bedrock to medium confidence.
5. **Measurable quality** — offline P/R + baseline comparison in CI.
