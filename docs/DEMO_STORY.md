# Demo Story — Production AIOps Narrative

Use this script when presenting to a senior SRE / Platform hiring manager.

## Elevator pitch (20 seconds)

> “This is a **closed-loop AIOps** stack on OpenTelemetry + Grafana LGTM.  
> We don’t just fire alerts — we **explain** the anomaly, open a ticket,  
> **ground** RCA in metrics/logs/traces (Bedrock), deep-link the **trace**,  
> propose **risk-gated** remediation, and feed **human thumbs** back into quality metrics.”

## Scene-by-scene

### Scene 1 — Customer pain

- Open Grafana (optional) and checkout health.
- Run: `python scripts/demo_story.py`
- **Say:** “A checkout request starts failing / slowing because of chaos on the path to payment.”

### Scene 2 — Explainable detection

- Point at the printed `explanation` line, e.g.  
  `http_error_rate=0.45 is 3.2σ above EWMA baseline …`
- **Say:** “Hybrid detection: EWMA/z-score for *explainability*, IsolationForest for joint outliers, absolute thresholds as cold-start safety.  
  We choose hybrid because pure ML is opaque and cold-start blind; pure thresholds miss novel shapes.”

### Scene 3 — Correlation & ticket

- Open http://localhost:8002/
- Show title, severity, explainability column.
- **Say:** “Same service+metric within a window correlates into one ticket — noise reduction is an SRE feature, not a nice-to-have.”

### Scene 4 — Grounded RCA + Trace experience

- Click the incident → **🔍 Xem Trace**
- Grafana Explore opens Tempo with the primary slow/error trace (or service error TraceQL).
- **Say:** “RCA is forbidden from inventing evidence. It only reasons over Prom/Loki/Tempo packs and returns strict JSON.  
  Trace UX is deliberate: on-call time is the scarce resource — zero copy/paste of trace IDs.”

### Scene 5 — Gated remediation

- Open http://localhost:8501
- Show low-risk auto vs high-risk approval.
- **Say:** “Auto-remediation without gates is how bots amplify outages. Restart/scale stay human-approved.”

### Scene 6 — Feedback & self-monitoring

- Open http://localhost:8502 → thumbs.
- Show http://localhost:8005/metrics (`feedback_positive_rate`, `rca_accuracy_estimate`, `false_positive_count`).
- **Say:** “AIOps must observe itself. High FP rate drives threshold suggestions — not silent model drift.”

## Why this impresses seniors

| Bar | How we hit it |
|-----|----------------|
| Safety | Risk gates, rule fallback, fail-open telemetry |
| Cost | Bedrock only on ticket path; Prom pull not full stream processing |
| Explainability | Sigma/EWMA narratives + method_details |
| Operability | One-click traces, healthchecks, compose one-shot |
| Learning loop | Feedback metrics + threshold advisor |

## Anti-patterns we deliberately avoided

- Black-box “AI says restart everything”
- Ungrounded LLM RCA from the ticket title alone
- Auto-scale on every blip without correlation
- Demo-only scripts with no production commentary
