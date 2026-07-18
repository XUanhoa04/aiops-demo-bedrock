# aiops-demo-bedrock

**Production-like AIOps demo** for CV / workshops: detect → ticket → (Day-2) RCA on Amazon Bedrock → remediate → human feedback.

One command:

```bash
cp .env.example .env
docker compose up -d --build
```

## Architecture

```text
[checkout] ──► [payment]          demo apps (metrics + traces)
     │               │
     └──── OTLP ─────┴──► [grafana/otel-lgtm]  Prometheus/Loki/Tempo/Grafana
                                ▲
                                │ PromQL
                    [anomaly-detector] ──Redis queue──► [incident-manager/SQLite]
                                                              │
                         Day-2 (profile):  RCA(Bedrock) · remediation · feedback
```

| Service | Port | Role |
|---------|------|------|
| Grafana LGTM | 3000, 4317, 4318, 9090, 3100, 3200 | Observability backbone |
| checkout-service | 8080 | Demo traffic + chaos |
| payment-service | 8081 | Downstream dependency |
| Redis | 6379 | Anomaly / incident queues |
| aiops-anomaly-detector | 8001 | Threshold + z-score → Redis |
| aiops-incident-manager | 8002 | Tickets in SQLite |
| rca / remediation / feedback | 8003–8005 | Day-2 (`--profile day2`) |

## Quick demo

```bash
# wait for health (or open http://localhost:8002/health)
# Windows: use `py -3` if `python` is not on PATH
python scripts/demo_flow.py

# generate load
python scripts/load_test.py --rps 15 --duration 60

# inject failures
python scripts/chaos.py --service checkout --error-rate 0.5
python scripts/chaos.py --reset
```

> **Ports:** host ports `3000` and `9090` must be free (stop other Grafana/Prometheus stacks first if bind fails).

- **Grafana**: http://localhost:3000  
- **Detector API**: http://localhost:8001/docs  
- **Incidents API**: http://localhost:8002/docs  

## Day-2 (Bedrock)

```bash
# set AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, BEDROCK_MODEL_ID in .env
docker compose --profile day2 up -d --build
```

## Project layout

```text
aiops-demo-bedrock/
├── docker-compose.yml
├── .env.example
├── apps/                    # checkout + payment (OTel demo stand-in)
├── observability/           # notes for LGTM / future dashboards
├── aiops-services/
│   ├── anomaly-detector/
│   ├── incident-manager/
│   ├── rca-engine/          # Day-2
│   ├── remediation/         # Day-2
│   └── feedback-collector/  # Day-2
├── shared/aiops_shared/     # models, OTEL bootstrap, Redis helpers
└── scripts/                 # load test, chaos, demo flow
```

## Production choices (interview talking points)

- **OTLP HTTP `:4318`** — simpler than gRPC across language SDKs for demos.
- **Healthchecks + `depends_on: condition: service_healthy`** — ordered startup without racey sleep scripts.
- **Redis LIST queue** — teach detect→act pipeline; swap for SQS/Kafka in production.
- **SQLite + WAL on a volume** — zero-ops tickets for laptop demos; Postgres in prod.
- **Incident correlation window** — reduce alert noise (same service+metric).
- **Non-root containers, log rotation, fail-open telemetry** — baseline hardened defaults.
- **Amazon Bedrock** — RCA without self-hosting LLMs; keys only in `.env` (never committed).

## License

MIT — demo / portfolio use.
