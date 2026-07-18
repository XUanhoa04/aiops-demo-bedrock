# Report: Xây dựng AIOps Demo Bedrock

| Trường | Giá trị |
|--------|---------|
| **Project** | `aiops-demo-bedrock` |
| **Đường dẫn** | `D:\project\aiops-demo-bedrock` |
| **Ngày** | 2026-07-18 |
| **Vai trò** | Senior DevOps + Python Engineer (AIOps) |
| **Mục tiêu** | Demo production-like cho CV, chạy 1 lệnh Docker Compose |

---

## 1. Tóm tắt

Đã scaffold và implement đầy đủ monorepo **AIOps pipeline** (detect → queue → ticket), kèm observability backbone (Grafana LGTM), 2 demo apps phát traffic/metrics/traces, Redis queue, và stub Day-2 (RCA Bedrock / remediation / feedback). Stack đã **build, start, healthcheck OK**, và **demo end-to-end** (chaos + manual anomaly → incident ticket) đã chạy thành công trên máy local.

---

## 2. Yêu cầu ban đầu vs kết quả

| Yêu cầu | Kết quả |
|---------|---------|
| Tên project `aiops-demo-bedrock` | ✅ |
| Cấu trúc thư mục rõ ràng | ✅ |
| `docker-compose.yml` 1 lệnh | ✅ `docker compose up -d --build` |
| grafana/otel-lgtm (ports 3000, 4317, 4318, 9090, 3100, 3200) | ✅ |
| OpenTelemetry Demo hoặc service đơn giản | ✅ 2 service: checkout + payment (nhẹ hơn full OTel Demo) |
| Redis | ✅ redis:7-alpine + AOF volume |
| aiops-anomaly-detector (FastAPI) | ✅ port 8001 |
| aiops-incident-manager (FastAPI + SQLite) | ✅ port 8002 |
| `OTEL_EXPORTER_OTLP_ENDPOINT=http://lgtm:4318` | ✅ shared env anchor trong compose |
| healthcheck + depends_on hợp lý | ✅ |
| `.env.example` (AWS + BEDROCK_MODEL_ID) | ✅ |
| Code hoàn chỉnh + comment production choices | ✅ |
| README | ✅ (kèm report này) |

---

## 3. Cấu trúc đã tạo

```text
aiops-demo-bedrock/
├── docker-compose.yml
├── .env.example
├── .gitignore
├── README.md
├── REPORT.md                 # file này
├── apps/
│   ├── README.md
│   ├── checkout-service/     # :8080 — entry, gọi payment, chaos API
│   └── payment-service/      # :8081 — downstream
├── observability/
│   └── README.md             # ghi chú LGTM / OTLP
├── aiops-services/
│   ├── anomaly-detector/     # Day-1: PromQL + threshold/z-score → Redis
│   ├── incident-manager/     # Day-1: consume queue → SQLite tickets
│   ├── rca-engine/           # Day-2 stub (Bedrock) — profile day2
│   ├── remediation/          # Day-2 stub — profile day2
│   └── feedback-collector/   # Day-2 stub — profile day2
├── shared/
│   ├── requirements-base.txt
│   └── aiops_shared/         # models, OTEL, logging, Redis helpers
└── scripts/
    ├── load_test.py
    ├── chaos.py
    ├── demo_flow.py
    └── wait_for_stack.sh
```

---

## 4. Việc đã làm (chi tiết)

### 4.1 Infrastructure / Docker Compose

- Định nghĩa stack `name: aiops-demo-bedrock` với network `aiops-demo-net`.
- Service **lgtm** (`grafana/otel-lgtm:latest`) expose đủ port observability.
- Service **redis** (appendonly + volume `aiops-redis-data`).
- Build context monorepo: mỗi service Dockerfile copy `shared/` + app code.
- Anchor YAML `x-otel-env` tái sử dụng `OTEL_EXPORTER_OTLP_ENDPOINT=http://lgtm:4318`.
- Healthcheck:
  - Redis: `redis-cli ping`
  - Apps/AIOps: Python `urllib` gọi `/health`
  - LGTM: TCP/HTTP probe Grafana `:3000`
- `depends_on`:
  - Redis → `service_healthy`
  - LGTM → `service_started` (apps fail-open khi OTLP chưa ready)
  - checkout chờ payment healthy
  - incident-manager chờ anomaly-detector healthy
- Day-2 services gắn `profiles: ["day2"]` để không bắt buộc khi demo Day-1.
- Logging json-file rotation (`max-size: 10m`).

### 4.2 Shared library (`shared/aiops_shared`)

| Module | Nội dung |
|--------|----------|
| `models.py` | `AnomalyEvent`, `Incident`, severity/status enums (Pydantic) |
| `otel.py` | Tracer/Meter OTLP HTTP + FastAPI instrumentation (fail-open) |
| `redis_client.py` | LPUSH / BRPOP queue helpers |
| `logging_config.py` | Structured stdout logs cho container |
| `requirements-base.txt` | FastAPI, uvicorn, redis, OTEL SDK pins |

**Production choice:** package nội bộ copy vào image (tránh private PyPI cho monorepo demo).

### 4.3 Demo apps (`apps/`)

- **checkout-service**: `POST /checkout` → gọi payment; metrics `demo_http_*`; `POST /chaos` inject error_rate / latency.
- **payment-service**: `POST /pay`; chaos tương tự.
- Lý do không dùng full OpenTelemetry Demo: nặng RAM/disk; 2 service đủ RED metrics + distributed trace cho AIOps.

### 4.4 Anomaly detector (`aiops-services/anomaly-detector`)

- Background worker poll Prometheus (LGTM `:9090`) theo interval env.
- Thuật toán: **threshold** + **z-score** sliding window; cooldown 60s chống spam.
- Publish `AnomalyEvent` JSON lên Redis list `aiops:anomalies`.
- API:
  - `GET /health`, `GET /ready`, `GET /anomalies`, `GET /status`
  - `POST /detect` — inject anomaly cho live demo
- Watch services: `checkout-service`, `payment-service`.

### 4.5 Incident manager (`aiops-services/incident-manager`)

- Consumer BRPOP Redis → map anomaly → incident.
- **SQLite** (path `/data/incidents.db`, volume `aiops-incident-data`), WAL mode.
- **Correlation**: cùng `service_name` + `metric_name` trong cửa sổ 10 phút → update ticket cũ (giảm noise).
- API REST: list/get/create/patch incidents, create-from-anomaly, `/stats` (kèm flag Bedrock configured).
- Env Bedrock/AWS sẵn sàng cho Day-2.

### 4.6 Day-2 stubs

| Service | Port | Chức năng stub |
|---------|------|----------------|
| rca-engine | 8003 | Mock hypothesis; chỗ gắn Bedrock Converse |
| remediation | 8004 | Gọi `/chaos` reset error_rate trên demo apps |
| feedback-collector | 8005 | PATCH `human_feedback` lên incident-manager |

### 4.7 Scripts

- `load_test.py` — concurrent load checkout (stdlib).
- `chaos.py` — inject/reset chaos runtime.
- `demo_flow.py` — E2E: chaos → manual detect → chờ incident.
- `wait_for_stack.sh` — poll health endpoints (bash).

### 4.8 Tài liệu & secrets hygiene

- `.env.example`: AWS keys, `BEDROCK_MODEL_ID`, OTEL, Redis, threshold anomaly, chaos knobs.
- `.gitignore`: `.env`, venv, `*.db`, IDE…
- `README.md`: architecture, ports, Day-2, talking points phỏng vấn.

---

## 5. Sửa lỗi phát sinh khi verify

| Vấn đề | Nguyên nhân | Cách xử lý |
|--------|-------------|------------|
| Bind port 3000/9090 fail | `mlops-grafana` / `mlops-prometheus` đang chiếm | Stop tạm container conflict; ghi chú trong README |
| anomaly-detector **unhealthy** | Poll PromQL timeout dài (10s × nhiều query) block thread pool → healthcheck timeout | Timeout httpx ngắn (1–2s), fail-fast khi host unreachable, `/health` không gọi Prometheus deep |
| FastAPI OTEL warning | `instrument_app` sau khi app already started | Gọi `setup_otel(app=...)` ngay sau tạo `FastAPI()`, không trong lifespan |
| `python` không có trên PATH (Windows) | Alias Microsoft Store | Dùng `py -3 scripts/...` |

---

## 6. Kết quả kiểm thử (smoke)

Thực hiện trên máy dev sau `docker compose up -d --build`:

| Check | Kết quả |
|-------|---------|
| `docker compose config` | OK |
| Build 4 image Python core | OK |
| Tất cả container Day-1 `healthy` | OK |
| `GET :8001/health` | 200 |
| `GET :8002/health` | 200 |
| `GET :8080/health`, `:8081/health` | 200 |
| `GET :3000/api/health` (Grafana) | 200 |
| `py -3 scripts/demo_flow.py` | Tạo incident `open` severity high cho checkout |

Ví dụ incident (rút gọn):

```json
{
  "title": "[HIGH] checkout-service: http_error_rate",
  "status": "open",
  "service_name": "checkout-service",
  "metric_name": "http_error_rate",
  "metric_value": 0.45,
  "threshold": 0.15
}
```

---

## 7. Production choices (tóm tắt interview)

1. **OTLP HTTP 4318** — dễ demo, ít friction hơn gRPC.
2. **Health vs ready** — liveness rẻ; dependency (Prom) không chặn health.
3. **Redis LIST queue** — dạy pattern detect→act; prod thay SQS/Kafka/Streams.
4. **SQLite + volume** — zero-ops laptop; prod Postgres + migration.
5. **Incident correlation window** — giảm alert noise.
6. **Non-root container user (uid 10001)**.
7. **Log rotation** json-file driver.
8. **Telemetry fail-open** — app vẫn chạy khi collector chưa up.
9. **Bedrock env-only secrets** — không commit key; RCA Day-2 không self-host LLM.
10. **Compose profiles** — tách Day-1 / Day-2 rõ ràng.

---

## 8. Cách chạy lại

```bash
cd D:\project\aiops-demo-bedrock
copy .env.example .env          # hoặc cp trên Linux/macOS
docker compose up -d --build

# Demo (Windows)
py -3 scripts/demo_flow.py
py -3 scripts/load_test.py --rps 15 --duration 60
py -3 scripts/chaos.py --service checkout --error-rate 0.5
py -3 scripts/chaos.py --reset

# Day-2 (sau khi điền AWS keys trong .env)
docker compose --profile day2 up -d --build
```

| URL | Mục đích |
|-----|----------|
| http://localhost:3000 | Grafana (LGTM) |
| http://localhost:8001/docs | Anomaly Detector OpenAPI |
| http://localhost:8002/docs | Incident Manager OpenAPI |
| http://localhost:8002/incidents | Danh sách ticket |

---

## 9. Việc **chưa** làm (ngoài scope Day-1 / để Day-2)

- [ ] Gọi thật Amazon Bedrock Converse API trong `rca-engine`
- [ ] Evidence pack (metrics/logs/traces từ Prom/Loki/Tempo) cho prompt RCA
- [ ] Dashboard Grafana as-code / provisioning
- [ ] Full OpenTelemetry Demo submodule (optional)
- [ ] CI (GitHub Actions: compose config, lint, smoke test)
- [ ] Unit tests / pytest cho detector & repository
- [ ] Authn/authz trên API control-plane
- [ ] Helm/K8s manifest (compose-only hiện tại)

---

## 10. Kết luận

Đã giao một **AIOps demo production-like**, monorepo rõ ràng, **một lệnh Docker Compose**, pipeline **anomaly → Redis → incident** chạy được, observability LGTM sẵn, stub Bedrock Day-2 có chỗ cắm. Phù hợp đưa vào CV/workshop với talking points DevOps + Python + AIOps rõ ràng.

---

*File report được ghi lại sau khi implement và smoke-test thành công trên môi trường Windows + Docker.*
