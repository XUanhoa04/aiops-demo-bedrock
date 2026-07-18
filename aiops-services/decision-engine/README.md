# Decision Engine

Confidence-gated router between **anomaly-detector** (Confidence Scorer) and RCA / remediation / on-call.

## Decision table

| Condition | Action | LLM? | Notes |
|-----------|--------|------|-------|
| `confidence ≥ 85` **and** known remediation pattern | `auto_remediate_gated` | No | Log + **propose only** (no force-execute) |
| `confidence ≥ 85` **without** known pattern | `rca_suggest` | Yes | Need analysis for suggestions |
| `60 ≤ confidence < 85` | `rca_suggest` | Yes | Bedrock via RCA (limited tokens) + on-call suggestions |
| `confidence < 60` | `escalate_oncall` | No | Immediate handoff + explanation |
| Missing **critical** context (`sufficient_metrics`, `trace_id`, …) | `escalate_oncall` | No | Do not trust automation |
| Iteration budget exhausted (max **2–3**) | `escalate_oncall` | No | Forced handoff |

Thresholds: `CONFIDENCE_HIGH=85`, `CONFIDENCE_MEDIUM=60`, `MAX_ITERATIONS=3` (env).

Runtime copy: `GET http://localhost:8006/decision-table`

## Limited iteration loop

1. Evaluate policy (`select_action`).
2. If medium path and `missing_context` → **enrich** once via anomaly-detector `POST /score`.
3. Re-evaluate; call Bedrock RCA only on `rca_suggest`.
4. If still unresolved after `MAX_ITERATIONS` → **handoff** to on-call.

## LLM trigger policy

- Bedrock is **not** called on LOW or on HIGH+known-pattern.
- Called only for **MEDIUM** (and HIGH without pattern when LLM enabled).
- Input = enriched Confidence Scorer context on the incident.
- Output = structured RCA JSON including **LLM confidence**; if LLM conf &lt; `MIN_LLM_CONFIDENCE` → escalate.

## Explainability logs

Every decision emits:

- why (`reason` + `decision_trace[]`)
- `confidence_score` + `confidence_breakdown`
- `missing_context`
- iteration records

Example log line:

```text
decide.iter=1 action=rca_suggest band=medium conf=72.0 reason=confidence=72.0 in [60, 85) → call Bedrock RCA…
```

## API

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/decide` | Primary: Confidence Scorer payload → `EngineDecision` |
| POST | `/decide/from-anomaly` | From detector `AnomalyEvent` |
| GET | `/decisions` | Recent decisions |
| GET | `/decision-table` | Markdown policy matrix |
| GET | `/metrics` | Prometheus |
| GET | `/health` | Liveness |

### Example

```bash
curl -s -X POST http://localhost:8006/decide \
  -H "Content-Type: application/json" \
  -d '{
    "service_name": "checkout-service",
    "metric_name": "http_error_rate",
    "metric_value": 0.45,
    "confidence_score": 88,
    "confidence_breakdown": {"metrics": 35, "traces": 25, "logs": 15, "events": 8},
    "missing_context": [],
    "context_completeness": 1.0,
    "explanation": "error rate cao hơn 3.2 sigma so với EWMA baseline",
    "skip_side_effects": true
  }'
```

## Port

**8006**
