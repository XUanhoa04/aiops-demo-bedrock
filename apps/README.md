# Demo applications

Laptop-friendly 4-service commerce topology:

```text
checkout-service (:8080)
  ├─► inventory-service (:8082)   stock reserve
  └─► payment-service   (:8081)
        └─► fraud-service (:8083) scoring
```

| Service | Port | Chaos fault modes (examples) |
|---------|------|------------------------------|
| checkout | 8080 | db_pool, cache_miss, dependency_timeout, cpu_throttle |
| payment | 8081 | db_pool, gateway_timeout, redis_cache_miss |
| inventory | 8082 | stock_lock, db_pool, cache_miss |
| fraud | 8083 | scoring_timeout, rule_engine_saturated, cpu_throttle |

All export OpenTelemetry to `lgtm:4318` and expose:

- `GET /health`
- `GET|POST /chaos` — runtime error rate / latency / fault_mode

```bash
python scripts/chaos.py --service inventory --error-rate 0.5 --fault-mode stock_lock
python scripts/chaos.py --service fraud --error-rate 0.6 --fault-mode scoring_timeout
python scripts/chaos.py --reset
```

For the full OpenTelemetry Astronomy Shop (~12 services), see `docs/OTEL_DEMO.md`.
