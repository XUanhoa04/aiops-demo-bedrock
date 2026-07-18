# OpenTelemetry Demo (Astronomy Shop) + SentinelLoop

Integrate the official **[OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo)** (~10–12 microservices, full OTLP instrumentation, load generator, feature-flag chaos) with this AIOps control plane.

## Why

| Mini apps (default) | Astronomy Shop mode |
|---------------------|---------------------|
| 2 services | ~12 services + real dependency graph |
| Hand-rolled `/chaos` | OpenFeature **flagd** faults |
| Fine for CI / laptop | Best for topology + demo video |

Default `docker compose up` **still uses mini checkout/payment** so CI and weak laptops stay fast.

## Architecture

```text
                    load-generator / browser
                              │
                              ▼
              Astronomy Shop microservices
              (frontend, cart, checkout, payment, …)
                              │ OTLP
                              ▼
                    otel-collector (demo)
                              │ dual-export (bridge)
                              ▼
                 aiops-lgtm (Grafana LGTM :3000)
                     Prom / Loki / Tempo
                              │
                              ▼
              SentinelLoop AIOps control plane
         detector → incident → decision → RCA → remediate
```

Topology file: `config/service_topology_astronomy.yaml`.

## Prerequisites

- Docker Desktop with **≥ 8 GB RAM** recommended (16 GB better)
- Disk free **≥ 10 GB** for images
- Git
- AIOps repo already cloned

## One-command start (Windows)

```powershell
# From repo root
powershell -ExecutionPolicy Bypass -File scripts/astronomy/start.ps1
```

Linux/macOS:

```bash
bash scripts/astronomy/start.sh
```

What the script does:

1. Starts SentinelLoop with `docker-compose.astronomy.yml` (disables mini apps)
2. Shallow-clones `third_party/opentelemetry-demo` if missing
3. Installs collector export config → **AIOps LGTM**
4. Starts Astronomy Shop **without** its Grafana/Prometheus/Jaeger (avoids port fights)
5. Bridges `otel-collector` onto `aiops-demo-bedrock_aiops-net`

## URLs

| UI | URL |
|----|-----|
| Astronomy frontend | http://localhost:8080 |
| Flagd UI (faults) | http://localhost:4000 |
| Grafana LGTM | http://localhost:3000 |
| Incident console | http://localhost:8002 |
| AIOps console | http://localhost:8500 |

## Inject faults

```powershell
py -3.11 scripts/astronomy/set_flag.py --list
py -3.11 scripts/astronomy/set_flag.py paymentFailure on
py -3.11 scripts/astronomy/set_flag.py productCatalogFailure on
py -3.11 scripts/astronomy/set_flag.py cartFailure 50%
py -3.11 scripts/astronomy/set_flag.py paymentFailure off
```

Or use **Flagd UI** at http://localhost:4000.

Load generator is already part of the demo (traffic continuously hits the frontend).

## Verify telemetry lands in LGTM

```powershell
powershell -File scripts/astronomy/status.ps1
# Prometheus Explore:
#   count by (service_name) ({__name__=~".+"})
# Tempo:
#   {resource.service.name="checkout"}
```

Wait **1–2 minutes** after first start for metrics/traces to appear.

## AIOps behaviour in this mode

- `WATCHED_SERVICES` includes frontend, checkout, cart, payment, shipping, …
- RCA uses `service_topology_astronomy.yaml` (multi-hop neighbors)
- PromQL templates include **HTTP + gRPC** OTel metrics
- Remediation stays **SIMULATE_ONLY** by default (no mini-app `/chaos` reset)

## Stop

```powershell
powershell -File scripts/astronomy/stop.ps1
# AIOps stack remains up
```

Full teardown including AIOps:

```powershell
docker compose -f docker-compose.yml -f docker-compose.astronomy.yml down
# + astronomy stop.ps1
```

## Manual compose (advanced)

```bash
# Terminal A — AIOps
docker compose -f docker-compose.yml -f docker-compose.astronomy.yml up -d --build

# Terminal B — Demo (from third_party/opentelemetry-demo after clone)
docker compose --env-file .env --env-file .env.override \
  -f compose.yaml -f compose.extras.yaml \
  -f ../../integrations/astronomy-shop/compose.aiops-bridge.yaml \
  up -d
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Port 8080 busy | Stop mini checkout or set `ENVOY_PORT=18080` in demo `.env.override` |
| Port 3000/9090 conflict | Do **not** start demo `compose.observability.yaml` |
| No metrics in LGTM | Check `otel-collector` is on `aiops-demo-bedrock_aiops-net`; `docker logs otel-collector` |
| Collector cannot resolve `aiops-lgtm` | AIOps compose must be up first; network name matches bridge file |
| RAM thrash | Close other stacks; use Docker resource limits; stick to mini apps for daily work |
| Clone huge | Script uses `--depth 1`; images still multi-GB |

## Honest scope

- This integration **orchestrates** the upstream demo; we do not vendor its source.
- Offline RCA eval suite remains on synthetic YAML (CI-friendly).
- Live topology value is highest in Astronomy mode; mini apps remain the default for unit/CI.
