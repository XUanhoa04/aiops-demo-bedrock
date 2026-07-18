# Architecture & production trade-offs

This document is the **honest** companion to the README: what we built, why, and what is deliberately *not* production.

## System diagram

Architecture as code via [diagrams.mingrammer.com](https://diagrams.mingrammer.com/)  
(source: [`generate_architecture_diagram.py`](generate_architecture_diagram.py)).

![SentinelLoop architecture](architecture-sentinel-loop.png)

Regenerate after structural changes:

```bash
# needs: pip install diagrams  +  Graphviz (`dot` on PATH)
python docs/generate_architecture_diagram.py
```

## Pipeline (logical)

```
checkout/payment (OTel)
        │
        ▼
   LGTM (Prom / Loki / Tempo)
        │ PromQL pull
        ▼
 anomaly-detector ── hybrid score + multi-signal context + confidence
        │ Redis: aiops:anomalies (+ aiops:decisions mirror)
        ├──────────────────► incident-manager (tickets, correlation, UI)
        │                           │
        │                           ▼
        │                     rca-engine
        │                       ├ topology catalog (config/service_topology.yaml)
        │                       ├ neighbor evidence expand (upstream/downstream)
        │                       └ Bedrock | topology-aware rules
        │                           │
        │                           ▼
        │                     remediation (risk-gated)
        │
        └──► decision-engine (policy: auto / RCA / escalate)
                    │
                    └──► engine-qa (meta-SLOs from on-call labels)
```

## Topology (RCA)

Static catalog: `config/service_topology.yaml` (checkout → payment + shared redis/DB).

At gather time RCA also merges **runtime edges** from Tempo (`root_service` / patterns)
and pulls RED + error logs for **upstream/downstream** neighbors into `EvidencePack`.

Rule + prompt policy: if an upstream is significantly sicker (error/latency margin +
logs), prefer that dependency as `root_cause` — avoid wrong-hop blame on the ticket owner.

## Why these algorithms & weights

| Choice | Rationale |
|--------|-----------|
| EWMA + Z-score | Explainable to on-call (“2.8σ above EWMA”); works with short windows |
| STL (optional) | Avoid diurnal false positives when seasonality strength is real |
| IsolationForest | Joint RED outliers rules miss; contamination~0.08 for mostly-healthy demos |
| Confidence 40/30/20/10 (metrics/traces/logs/events) | Detector is metric-first; traces beat logs for RCA; events sparse |
| Decision bands 85 / 60 | High conf + known pattern → gated remediate; medium → LLM; low → escalate |
| Bedrock only on medium band | Cost control — don’t spend tokens on obvious chaos resets or empty context |

## What is demo-grade (not full prod)

| Area | Demo choice | Production direction |
|------|-------------|----------------------|
| Queue | Redis LIST LPUSH/BRPOP | Kafka / SQS / Redis Streams + consumer groups + DLQ |
| Tickets | SQLite file volume | Postgres / Jira / PagerDuty |
| Detector state | In-process deques | Feature store / stream processor; survive restarts |
| Auth | Open APIs on localhost | mTLS, SSO, RBAC on approve/execute |
| Multi-tenant | Single compose network | Namespace isolation, per-tenant quotas |
| Topology | Static YAML + Tempo-inferred edges (demo graph) | Mesh/CMDB service graph + continuous discovery |
| Eval dataset | 15 RCA + 8 anomaly scenarios | Larger labeled set + shadow traffic + human agreement |
| Auto-remediation | Propose / low-risk chaos reset only | Change windows, canary, automated rollback |

## Safety invariants (keep these)

1. High-risk remediation (restart/scale) requires human approval.
2. Decision Engine **gated** auto path never force-executes.
3. RCA fails open to **rule-based** fallback — never silent black-hole.
4. Confidence penalties when critical context is missing.
5. Offline evaluation must **beat naive baselines** in CI.

## Sequence: one anomaly

1. Prom scrape → hybrid methods vote → explanation string.
2. Context gather (parallel Prom/Loki/Tempo) → completeness ratio.
3. Confidence scorer → 0–100 + breakdown.
4. Publish AnomalyEvent (context embeds confidence for Decision Engine).
5. IM correlates → ticket; optional RCA fan-out.
6. Decision Engine policy row → escalate / RCA suggest / gated remediate.
7. On-call reviews in Engine QA → precision / hallucination / tuning advice.

## Evaluation honesty

- Offline RCA uses the **same** `rule_based_rca` path as production fallback.
- Dataset keywords overlap intentionally with realistic log lines (pool exhaustion, cache miss) — that is how production runbooks work.
- CI requires system accuracy **> best baseline** (random / always-error-rate / empty).
- 100% on a 10-scenario set is a **regression gate**, not a claim of perfect prod RCA.
