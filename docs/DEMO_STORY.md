# Demo Story — Production AIOps Narrative

Use this script when presenting to a senior SRE / Platform hiring manager.

## Elevator pitch (20 seconds)

> “This is a **closed-loop AIOps** stack on OpenTelemetry + Grafana LGTM.  
> Four commerce services form a real dependency graph — checkout calls inventory and payment, payment calls fraud.  
> We don’t just fire alerts — we **explain** the anomaly, open a ticket,  
> **ground** RCA in metrics/logs/traces (Bedrock or config-driven rules), deep-link the **trace**,  
> propose **risk-gated** remediation, and feed **human thumbs** back into quality metrics.”

## Scene-by-scene

### Scene 1 — Customer pain + topology

- Show topology diagram (`docs/topology-demo-apps.png`) or Mermaid in README.
- Run multi-hop chaos, e.g.:

```bash
python scripts/chaos.py --service fraud --error-rate 0.6 --fault-mode scoring_timeout
python scripts/dynamic_load.py --profile demo --stage-seconds 12
# or: python scripts/demo_story.py
```

- **Say:** “Checkout fails because of a **dependency** — not always the ticket owner. Topology exists so we don’t restart the wrong service.”

### Scene 2 — Explainable detection

- Point at the printed `explanation` line, e.g.  
  `http_error_rate=0.45 is 3.2σ above EWMA baseline …`
- **Say:** “Hybrid detection: EWMA/z-score for *explainability*, IsolationForest for joint outliers, absolute thresholds as cold-start safety.”

### Scene 3 — Correlation & ticket

- Open http://localhost:8002/
- Show title, severity, explainability column.
- **Say:** “Same service+metric within a window correlates into one ticket — noise reduction is an SRE feature.”

### Scene 4 — Grounded RCA + Trace + topology panel

- Click the incident → **🔍 View Trace**
- Show **Service topology** card (upstream/downstream).
- Grafana Explore opens Tempo with the primary slow/error trace.
- **Say:** “RCA is forbidden from inventing evidence. Patterns come from `rca_patterns.yaml`; multi-hop blame uses neighbor RED + logs.  
  Trace UX: zero copy/paste of trace IDs.”

### Scene 5 — Gated remediation

- Open http://localhost:8501
- Show low-risk auto vs high-risk approval (optional API key).
- **Say:** “Auto-remediation without gates is how bots amplify outages. Restart/scale stay human-approved.”

### Scene 6 — Feedback, eval, honesty

- Open http://localhost:8502 → thumbs; or Engine QA :8503.
- Mention offline suite: `bash scripts/run-evaluation.sh` · `report_summary.py`.
- **Say:** “We separate L0 catalog regression (can be ~1.0) from hard/OOD and strict scoring — e.g. anomaly hard F1 ~0.67, RCA hard ~0.60. Live path uses real chaos + OTel; offline ≠ prod ML.”

## Why this impresses seniors

| Bar | How we hit it |
|-----|----------------|
| Safety | Risk gates, rule fallback, optional API key |
| Cost | Decision Engine owns LLM; medium band only |
| Explainability | Sigma/EWMA narratives + config patterns |
| Topology | 4 real apps + wrong-hop / multi-hop scenarios |
| Operability | One-click traces, healthchecks, compose one-shot |
| Learning loop | Feedback + Engine QA + multi-layer eval (L0/hard/strict) + SRE baselines |

## Anti-patterns we deliberately avoided

- Black-box “AI says restart everything”
- Ungrounded LLM RCA from the ticket title alone
- Hard-coded `if scenario_id` RCA branches
- Auto-scale on every blip without correlation
- Claiming offline L0 ~100% accuracy as production quality (use hard/strict instead)

## Optional deep-dive: Astronomy Shop

If the laptop has RAM: `scripts/astronomy/start.ps1` — full OpenTelemetry Demo (~12 services).  
Default demo stays on 4-app compose so interviews don’t depend on multi-GB pulls.
