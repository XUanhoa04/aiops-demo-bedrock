# Observability

The stack uses **[grafana/otel-lgtm](https://github.com/grafana/docker-otel-lgtm)** as an all-in-one backend:

| Port | Component |
|------|-----------|
| 3000 | Grafana UI (anonymous admin enabled for local demo) |
| 4317 | OTLP gRPC |
| 4318 | OTLP HTTP (default for all services) |
| 9090 | Prometheus-compatible metrics API |
| 3100 | Loki |
| 3200 | Tempo |

All application containers set:

```text
OTEL_EXPORTER_OTLP_ENDPOINT=http://lgtm:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

## Why not full OpenTelemetry Demo?

The official multi-service OTel demo is excellent but heavy (many images, high RAM).  
This repo ships **checkout-service** + **payment-service** under `apps/` — enough to produce RED metrics, distributed traces, and chaos hooks for AIOps pipelines.

To vendor the full demo later:

```bash
git submodule add https://github.com/open-telemetry/opentelemetry-demo.git apps/opentelemetry-demo
```

Then extend `docker-compose.yml` with selected services only.

## AIOps Engine Health dashboard

Import the pre-built dashboard JSON:

1. Open Grafana → **http://localhost:3000**
2. **Dashboards → New → Import**
3. Upload `observability/grafana/dashboards/aiops-engine-health.json`
4. Select the Prometheus / Mimir datasource shipped with LGTM

Panels use metrics from **feedback-collector** (`:8005/metrics`):

| Metric | Meaning |
|--------|---------|
| `feedback_positive_rate` | Overall 👍 rate across cast votes |
| `rca_accuracy_estimate` | 👍 rate on RCA useful |
| `false_positive_count` | Count of anomaly_correct=false |
| `anomaly_precision_estimate` | 👍 rate on anomaly correct |
| `action_success_rate` | 👍 rate on action effective |

Also references incident/detector series when those exporters are scraped:

- `open_incidents`, `incidents_created_total` (incident-manager `:8002/metrics`)
- `detector_anomalies_emitted_total` (anomaly-detector `:8001/metrics`)

### Scraping AIOps exporters

LGTM already scrapes OTLP app metrics. For **prometheus_client** `/metrics` endpoints, merge:

`observability/prometheus/scrape-aiops.yml`

into the Prometheus scrape config (or use a sidecar scrape job). For a quick laptop check without scrape config:

```bash
curl -s http://localhost:8005/metrics | findstr feedback
python scripts/suggest_threshold.py
```

### Threshold tuning from false positives

```bash
# Text report
python scripts/suggest_threshold.py

# JSON
python scripts/suggest_threshold.py --json

# Or API
curl http://localhost:8005/tuning/report
```

Streamlit on-call UI: **http://localhost:8502** (profile `day2`).
