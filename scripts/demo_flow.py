#!/usr/bin/env python3
"""
Legacy quick demo (anomaly → incident). Prefer scripts/demo_e2e.py for full pipeline.

  python scripts/demo_flow.py
  python scripts/demo_flow.py --manual-only
  python scripts/demo_flow.py --full   # delegates to generate_incident --full
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def http_json(method: str, url: str, body: dict | None = None) -> dict | list:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--checkout", default="http://localhost:8080")
    p.add_argument("--detector", default="http://localhost:8001")
    p.add_argument("--incidents", default="http://localhost:8002")
    p.add_argument("--manual-only", action="store_true")
    p.add_argument("--full", action="store_true", help="Full pipeline via generate_incident.py")
    p.add_argument("--wait", type=int, default=25)
    args = p.parse_args()

    if args.full:
        script = Path(__file__).with_name("generate_incident.py")
        return subprocess.call([sys.executable, str(script), "--full"])

    print("== AIOps demo flow (quick) ==")
    print("Tip: use python scripts/demo_e2e.py for RCA + remediation + feedback")

    if not args.manual_only:
        print("[1] chaos: checkout error_rate=0.45")
        print(
            http_json(
                "POST",
                f"{args.checkout}/chaos",
                {"error_rate": 0.45, "extra_latency_ms": 200},
            )
        )

    print("[2] manual anomaly inject")
    detect_resp = http_json(
        "POST",
        f"{args.detector}/detect",
        {
            "service_name": "checkout-service",
            "metric_name": "http_error_rate",
            "metric_value": 0.45,
            "threshold": 0.15,
        },
    )
    anomaly = detect_resp.get("event") if isinstance(detect_resp, dict) else detect_resp
    if not isinstance(anomaly, dict):
        anomaly = detect_resp if isinstance(detect_resp, dict) else {}
    print("  anomaly_id=", anomaly.get("id") or detect_resp.get("id"))

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
    print("  Incident manager:  http://localhost:8002/")
    print("  Remediation UI:    http://localhost:8501")
    print("  Feedback UI:       http://localhost:8502")
    print("  RCA:               http://localhost:8003/docs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
