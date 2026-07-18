#!/usr/bin/env python3
"""
Cross-platform incident generator (Windows-friendly alternative to .sh).

  python scripts/generate_incident.py
  python scripts/generate_incident.py --full
  python scripts/generate_incident.py --reset
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def http_json(method: str, url: str, body: dict | None = None, timeout: int = 30):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def main() -> int:
    p = argparse.ArgumentParser(description="Generate a demo AIOps incident")
    p.add_argument("--checkout", default="http://localhost:8080")
    p.add_argument("--payment", default="http://localhost:8081")
    p.add_argument("--detector", default="http://localhost:8001")
    p.add_argument("--incidents", default="http://localhost:8002")
    p.add_argument("--rca", default="http://localhost:8003")
    p.add_argument("--remediation", default="http://localhost:8004")
    p.add_argument("--wait", type=int, default=45)
    p.add_argument("--full", action="store_true", help="Wait for RCA + propose remediation")
    p.add_argument("--reset", action="store_true", help="Reset chaos only")
    args = p.parse_args()

    if args.reset:
        print("[reset] chaos")
        for base in (args.checkout, args.payment):
            try:
                http_json("POST", f"{base}/chaos", {"error_rate": 0.01, "extra_latency_ms": 0})
            except Exception as exc:
                print(" ", base, exc)
        return 0

    print("== generate_incident ==")
    print("[1] chaos checkout error_rate=0.45")
    print(http_json("POST", f"{args.checkout}/chaos", {"error_rate": 0.45, "extra_latency_ms": 200}))

    print("[2] light load burst")
    for i in range(25):
        try:
            http_json(
                "POST",
                f"{args.checkout}/checkout",
                {"order_id": f"demo-{i}", "amount": 12.5},
                timeout=5,
            )
        except Exception:
            pass
        time.sleep(0.15)

    print("[3] manual anomaly inject")
    detect = http_json(
        "POST",
        f"{args.detector}/detect",
        {
            "service_name": "checkout-service",
            "metric_name": "http_error_rate",
            "metric_value": 0.45,
            "threshold": 0.15,
        },
    )
    event = detect.get("event") if isinstance(detect, dict) else detect
    print("  anomaly_id=", (event or {}).get("id") if isinstance(event, dict) else detect)

    print(f"[4] wait ≤{args.wait}s for incident")
    deadline = time.time() + args.wait
    found = None
    while time.time() < deadline:
        try:
            items = http_json("GET", f"{args.incidents}/incidents?limit=5")
            if isinstance(items, list) and items:
                found = items[0]
                break
        except urllib.error.URLError as exc:
            print("  waiting…", exc)
        time.sleep(2)
    if not found:
        print("No incident. Check docker compose logs.", file=sys.stderr)
        return 1

    iid = found["id"]
    print("  incident_id=", iid)
    print(json.dumps(found, indent=2, default=str)[:1500])

    if args.full:
        print("[full] RCA analyze (sync wait)")
        try:
            rca = http_json(
                "POST",
                f"{args.rca}/analyze-incident/{iid}?force=true&persist=true",
                timeout=90,
            )
            print("  rca status=", rca.get("status"), "mode=", rca.get("mode"))
            if rca.get("result"):
                print("  root_cause=", (rca["result"].get("root_cause") or "")[:200])
        except Exception as exc:
            print("  RCA call failed:", exc)

        # Refresh ticket
        try:
            found = http_json("GET", f"{args.incidents}/incidents/{iid}")
        except Exception:
            pass

        print("[full] remediation propose")
        try:
            actions = http_json(
                "POST",
                f"{args.remediation}/remediate/propose",
                {"incident_id": iid, "actions": []},
                timeout=30,
            )
            print(f"  proposed {len(actions) if isinstance(actions, list) else actions} action(s)")
        except Exception as exc:
            print("  remediation failed:", exc)

    print()
    print("Links:")
    print("  Incident:     ", f"{args.incidents}/incidents/{iid}")
    print("  IM UI:        ", f"{args.incidents}/")
    print("  Remediation:  ", "http://localhost:8501")
    print("  Feedback:     ", "http://localhost:8502")
    print("  Grafana:      ", "http://localhost:3000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
