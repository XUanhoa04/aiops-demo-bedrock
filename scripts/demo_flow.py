#!/usr/bin/env python3
"""
End-to-end demo flow (no Bedrock required for Day-1):

1. Inject chaos (high error rate on checkout)
2. Optionally fire a manual anomaly (fast path for live talks)
3. Wait for incident-manager to open a ticket
4. Print Grafana / API links

Usage:
  python scripts/demo_flow.py
  python scripts/demo_flow.py --manual-only
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def http_json(method: str, url: str, body: dict | None = None) -> dict | list:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--checkout", default="http://localhost:8080")
    p.add_argument("--detector", default="http://localhost:8001")
    p.add_argument("--incidents", default="http://localhost:8002")
    p.add_argument("--manual-only", action="store_true")
    p.add_argument("--wait", type=int, default=20)
    args = p.parse_args()

    print("== AIOps demo flow ==")

    if not args.manual_only:
        print("[1] chaos: checkout error_rate=0.45")
        print(
            http_json(
                "POST",
                f"{args.checkout}/chaos",
                {"error_rate": 0.45, "extra_latency_ms": 200},
            )
        )

    print("[2] manual anomaly inject (guarantees a ticket within seconds)")
    anomaly = http_json(
        "POST",
        f"{args.detector}/detect",
        {
            "service_name": "checkout-service",
            "metric_name": "http_error_rate",
            "metric_value": 0.45,
            "threshold": 0.15,
        },
    )
    print("  anomaly_id=", anomaly.get("id"))

    print(f"[3] wait up to {args.wait}s for incident...")
    deadline = time.time() + args.wait
    found = None
    while time.time() < deadline:
        try:
            items = http_json("GET", f"{args.incidents}/incidents?limit=5")
            if isinstance(items, list) and items:
                found = items[0]
                break
        except urllib.error.URLError as exc:
            print("  waiting for incident-manager...", exc)
        time.sleep(2)

    if not found:
        print("No incident yet. Check: docker compose logs -f aiops-anomaly-detector aiops-incident-manager")
        return 1

    print("[4] latest incident:")
    print(json.dumps(found, indent=2, default=str))
    print()
    print("Links:")
    print("  Grafana:           http://localhost:3000")
    print("  Anomaly detector:  http://localhost:8001/docs")
    print("  Incident manager:  http://localhost:8002/docs")
    print("  Incidents API:     http://localhost:8002/incidents")
    return 0


if __name__ == "__main__":
    sys.exit(main())
