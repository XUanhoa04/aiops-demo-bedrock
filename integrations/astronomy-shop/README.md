# Astronomy Shop bridge assets

Files used by `scripts/astronomy/start.*` to connect
[OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo) to SentinelLoop LGTM.

| File | Purpose |
|------|---------|
| `otelcol-config-aiops-export.yml` | Collector exporters → `aiops-lgtm:4318` |
| `compose.aiops-bridge.yaml` | Attach `otel-collector` to AIOps Docker network |

Full operator guide: [`docs/OTEL_DEMO.md`](../../docs/OTEL_DEMO.md).
