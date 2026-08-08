# Architecture & production trade-offs

Honest companion to the README: what we built, why, and what is deliberately *not* production.

## System diagram

![SentinelLoop architecture](architecture-sentinel-loop.png)

Sources:

- Graphviz: [`architecture-sentinel-loop.dot`](architecture-sentinel-loop.dot)
- Python (optional): [`generate_architecture_diagram.py`](generate_architecture_diagram.py)

```bash
# Graphviz (recommended)
dot -Tpng -Gdpi=150 -o docs/architecture-sentinel-loop.png docs/architecture-sentinel-loop.dot
dot -Tpng -Gdpi=150 -o docs/topology-demo-apps.png docs/topology-demo-apps.dot

# or diagrams.mingrammer.com (needs Graphviz + pip install diagrams)
python docs/generate_architecture_diagram.py
python docs/generate_topology_diagram.py
```

## Demo app topology

![Demo topology](topology-demo-apps.png)

```text
checkout-service (:8080)
  ├─► inventory-service (:8082)   stock reserve
  └─► payment-service   (:8081)
        └─► fraud-service (:8083) scoring
```

Catalog: `config/service_topology.yaml`.  
Optional multi-tenant-scale graph: Astronomy Shop — [`OTEL_DEMO.md`](OTEL_DEMO.md).

## Pipeline (logical)

```
checkout → inventory
   └────► payment → fraud     (OTel → LGTM)
              │
              ▼
         LGTM (Prom / Loki / Tempo)
              │ PromQL pull
              ▼
 anomaly-detector ── hybrid score + multi-signal confidence
              │ Redis: aiops:anomalies
              ▼
 incident-manager (tickets, correlation, topology UI, Trace deep-links)
              │
              ▼  ★ single control plane (default)
 decision-engine (policy: auto / RCA / escalate)
              │
              ├ conf < 60 ──────────────► escalate (no LLM)
              ├ conf ≥ 85 + pattern ────► remediation propose (gated, no force)
              └ medium / high no-pattern ► rca-engine
                                            ├ topology neighbors
                                            ├ config/rca_patterns.yaml
                                            └ Bedrock | rule fallback
                                                  │
                                                  ▼
                                            remediation (risk-gated + optional API key)
                                                  │
                                                  ▼
                                            feedback / engine-qa
```

**Control-plane invariant:** Incident Manager does **not** call RCA by default.  
`ENABLE_DIRECT_RCA_FANOUT=true` restores the legacy dual path.  
RCA Redis poll defaults **off**.

## Topology (RCA)

**Default:** 4 running apps + static catalog (+ Tempo-inferred edges).

At gather time RCA expands **upstream/downstream** neighbors into `EvidencePack`, fetches a bounded number of full OTLP span trees from Tempo, and derives caller→callee edges plus error/critical spans. Search-hit metadata alone is not treated as causal proof.

**Rule patterns** are data-driven: `config/rca_patterns.yaml` via `aiops_shared.rca_patterns` — extend synonyms in YAML, not `if scenario_id` in Python.

## Why these algorithms & weights

| Choice | Rationale |
|--------|-----------|
| EWMA + Z-score | Explainable to on-call (“2.8σ above EWMA”); works with short windows |
| STL (optional) | Avoid diurnal false positives when seasonality strength is real |
| IsolationForest | Joint RED outliers rules miss; `contamination=auto` plus a robust-z gate over historical model scores avoids assuming a fixed anomaly percentage |
| Confidence 40/30/20/10 (metrics/traces/logs/events) | Detector is metric-first; traces beat logs for RCA; events sparse |
| Decision bands 85 / 60 | High conf + known pattern → gated remediate; medium → LLM; low → escalate |
| Bedrock only on medium band | Cost control — don’t spend tokens on obvious chaos resets or empty context |
| Config pattern catalog | Ops can add fault synonyms without code; still explainable rules |

## What is demo-grade (not full prod)

| Area | Demo choice | Production direction |
|------|-------------|----------------------|
| Queue | Redis LIST atomic reserve/ACK, startup recovery, retry + DLQ | Kafka / SQS / Redis Streams + consumer groups + replay |
| Tickets | SQLite file volume | Postgres / Jira / PagerDuty |
| Detector state | Bounded deques checkpointed as JSON to AOF-backed Redis; thresholds remain a cold-start fallback | Feature store / stream processor with source replay |
| Auth | Optional `REMEDIATION_API_KEY`; open localhost APIs | mTLS, SSO, RBAC on approve/execute |
| Multi-tenant | Single compose network | Namespace isolation, per-tenant quotas |
| Topology | YAML seed merged with Tempo-derived runtime edges; optional Astronomy Shop | Mesh/CMDB service graph + continuous discovery |
| Eval dataset | ~42 RCA + ~28 anomaly (L0) + hard/OOD suites | Larger labeled set + shadow traffic + human agreement |
| Auto-remediation | Propose-only; SQLite per-service locks; verify + rollback for reversible chaos resets | Policy engine, change windows, canary, workload-native rollback |

## Safety invariants (keep these)

1. High-risk remediation (restart/scale) requires human approval.
2. Decision Engine **gated** auto path never force-executes.
3. RCA fails open to **rule-based** fallback — never silent black-hole.
4. Confidence penalties when critical context is missing.
5. Offline evaluation must **beat weak baselines** in CI (SRE baselines reported).
6. Default delivery is one canonical path: detector Redis → Incident Manager → Decision HTTP. Secondary webhook/decision queues are opt-in.
7. At-least-once retries are idempotent by anomaly id; poison payloads reach a DLQ instead of disappearing.
8. Mutating remediation actions acquire a TTL-bound service lock across API requests/processes.
9. Reversible chaos resets capture prior state, verify health/state, and roll back on verification failure. Restart/scale still require platform-native rollback in production.
10. LLM evidence is reduced by complete JSON records under a character budget; it is never sliced into invalid JSON mid-record.
11. Python application logs use an OTLP `LoggingHandler`; trace-context instrumentation alone is not considered log delivery.

### Known gaps that remain

- Kafka/RabbitMQ publish-consume causality is not modeled; trace/runtime edge inference currently targets request/span relationships.
- Redis remains a single demo queue/state dependency with no producer backpressure or HA failover.
- `REMEDIATION_API_KEY` is authentication, not role-based authorization or dual control.
- Restart/scale actions do not have a platform-native canary/rollback controller.
- Offline/live-short evaluation does not prove quiet-day false-positive rate, MTTR improvement, or operator cognitive load.

## Sequence: one anomaly

1. Prom scrape → hybrid methods vote → explanation string.
2. Context gather (parallel Prom/Loki/Tempo) → completeness ratio.
3. Confidence scorer → 0–100 + breakdown.
4. Publish AnomalyEvent (context embeds confidence for Decision Engine).
5. IM correlates → ticket; Decision Engine routes.
6. Medium band → RCA (evidence + topology + patterns / Bedrock).
7. On-call reviews in Feedback / Engine QA → precision / hallucination / tuning advice.

## Evaluation honesty

See [`EVALUATION.md`](EVALUATION.md) (includes sample numbers + CV wording).

- Offline RCA uses the same **config-driven** `rule_based_rca` path as production fallback.
- Report **L0** (core/holdout), **hard/OOD**, **strict** accuracy, wrong-hop rate; optional `--compare` for rule vs Bedrock.
- Live e2e: `evaluation/evaluate_live_e2e.py` (real chaos + OTel + evidence completeness).
- High **L0** offline scores = catalog regression coverage, **not** learned ML perfection.
- Prefer citing **hard anomaly F1 (~0.89)** and **hard RCA (~0.60)** over L0 100% alone.

## Optional modes

| Mode | How | When |
|------|-----|------|
| **Default 4-app** | `docker compose up -d --build` | CI, daily demo, multi-hop topology |
| **Astronomy Shop** | `scripts/astronomy/start.ps1` | Full OTel Demo (~12 services) |
| **Legacy dual RCA fan-out** | `ENABLE_DIRECT_RCA_FANOUT=true` | Debug only |
