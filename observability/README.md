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
