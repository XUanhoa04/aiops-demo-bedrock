# Short demo / video recording guide (~3–5 minutes)

Record a screen walkthrough of the full AIOps loop for CV / interviews.

## Prep (once)

```bash
cp .env.example .env
# Optional: put real AWS keys for Bedrock RCA (otherwise rule-based RCA still runs)
docker compose up -d --build
# Wait until healthy (~1–2 min first boot for LGTM)
python scripts/demo_e2e.py
```

## Shot list

| # | Time | What to show | Voice-over |
|---|------|--------------|------------|
| 1 | 0:00–0:30 | Architecture (README Mermaid or this table) | “Detect → ticket → grounded RCA on Bedrock → risk-gated remediation → human feedback.” |
| 2 | 0:30–0:50 | `docker compose ps` / Grafana :3000 | “One compose file: apps, LGTM, Redis, five AIOps services.” |
| 3 | 0:50–1:20 | Run `python scripts/generate_incident.py --full` | “Chaos + load + anomaly inject creates a real ticket.” |
| 4 | 1:20–1:50 | http://localhost:8002/ incident detail + root_cause | “Incident Manager correlated the alert; RCA wrote root_cause + confidence.” |
| 5 | 1:50–2:20 | RCA docs or logs (`docker compose logs aiops-rca-engine --tail 30`) | “Evidence pack from Prometheus/Loki/Tempo; Bedrock returns strict JSON (fallback if no keys).” |
| 6 | 2:20–2:50 | http://localhost:8501 Approve & Execute | “Low-risk auto; high-risk restart/scale needs human approval; history in SQLite.” |
| 7 | 2:50–3:20 | http://localhost:8502 thumbs + comment | “Closes the loop: FP labels feed threshold tuning suggestions.” |
| 8 | 3:20–3:40 | `/metrics` or Grafana import AIOps Engine Health | “feedback_positive_rate, rca_accuracy_estimate, false_positive_count.” |
| 9 | 3:40–4:00 | Production notes (README) | “Swap Redis→Kafka, SQLite→Postgres, add change freezes, outbox for RCA.” |

## Commands to run on camera

```bash
# Full automated path
python scripts/demo_e2e.py

# Or step-by-step
python scripts/generate_incident.py --full
python scripts/suggest_threshold.py
```

## Optional OBS / Loom tips

- 1920×1080, browser zoom 110% on UIs.
- Split terminal (left) + browser (right).
- Avoid showing `.env` secrets; blur AWS keys.
- End on the Mermaid architecture diagram in README.

## Cleanup

```bash
python scripts/generate_incident.py --reset
docker compose down
```
