# Demo applications

Lightweight stand-ins for a full microservices commerce stack:

- `checkout-service` (`:8080`) — entrypoint, calls payment, chaos API
- `payment-service` (`:8081`) — downstream dependency

Both export OpenTelemetry traces/metrics to `lgtm:4318` and expose:

- `GET /health`
- `GET|POST /chaos` — runtime error rate / latency injection
