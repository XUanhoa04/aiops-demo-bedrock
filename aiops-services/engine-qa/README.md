# Engine QA — giám sát người giám sát

Meta-evaluation layer for the AIOps pipeline: **detector · confidence scorer · RCA/LLM · decision engine**.

| Layer | On-call question | Metric |
|-------|------------------|--------|
| Detector | Anomaly đúng không? | Precision / FP rate / recall proxy |
| Confidence | Score có hợp lý không? | Calibration rate, mean conf error |
| RCA / LLM | RCA hữu ích? Hallucinated? | RCA useful rate, **hallucination rate** |
| Decision | Decision đúng không? | Decision correct rate, **mean iterations** |

## Ports

| Port | Surface |
|------|---------|
| **8007** | FastAPI (`/qa/*`, `/metrics`) |
| **8503** | Streamlit review UI |

## API

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/qa/reviews` | Submit meta-review |
| GET | `/qa/metrics` | Precision, FP, hallucination, iterations… |
| GET | `/qa/tuning` | Weight / threshold suggestions |
| GET | `/qa/tuning/report` | Text report + `.env` snippet |
| GET | `/qa/dashboard` | Bundle for UIs |
| GET | `/qa/review-queue` | Incidents + decision snapshots |
| GET | `/metrics` | Prometheus |

### Example review

```bash
curl -s -X POST http://localhost:8007/qa/reviews \
  -H "Content-Type: application/json" \
  -d '{
    "incident_id": "<id>",
    "anomaly_correct": true,
    "confidence_reasonable": true,
    "rca_useful": false,
    "decision_correct": true,
    "llm_hallucinated": true,
    "expected_confidence": 55,
    "engine_confidence": 88,
    "decision_iterations": 2,
    "comment": "LLM invented a deploy that did not happen",
    "reviewer": "sre-alice"
  }'
```

## Prometheus series

- `engine_qa_precision_estimate`
- `engine_qa_recall_estimate`
- `engine_qa_false_positive_rate`
- `engine_qa_hallucination_rate`
- `engine_qa_mean_decision_iterations`
- `engine_qa_overall_health`
- `engine_qa_decision_correct_rate`
- `engine_qa_confidence_reasonable_rate`

## Tuning (advisory only)

Never auto-applies. When FP / overconfidence / hallucination fire, suggests:

- `ZSCORE_THRESHOLD` / `ERROR_RATE_THRESHOLD`
- `CONFIDENCE_WEIGHT_METRICS|TRACES|LOGS|EVENTS`
- `CONFIDENCE_HIGH` / `CONFIDENCE_MEDIUM`

See `GET /qa/tuning/report`.

## Relation to feedback-collector

| Service | Role |
|---------|------|
| **feedback-collector** (:8005 / :8502) | Per-incident thumbs (anomaly / RCA / action) |
| **engine-qa** (:8007 / :8503) | Aggregate engine+LLM quality + calibration + decision loop |

Engine QA can dual-write a summary into feedback-collector (`SYNC_FEEDBACK_COLLECTOR`).
