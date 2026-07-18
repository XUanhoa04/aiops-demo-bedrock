#!/usr/bin/env python3
"""
One-shot CV demo path (no video — human records separately).

Assumes: docker compose up -d --build already ran and stack is healthy.

Flow
----
  1. Health-check critical services
  2. Reset chaos → short normal load
  3. Inject payment db_pool fault + load
  4. Manual detect (reliable for demos) + wait for incident
  5. Trigger RCA + Decision Engine dry score
  6. Print operator URLs

Usage:
  python scripts/demo_one_shot.py
  python scripts/demo_one_shot.py --skip-load
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional


def http_json(
    method: str,
    url: str,
    body: Optional[dict] = None,
    timeout: int = 60,
) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def ok(name: str, url: str) -> bool:
    try:
        http_json("GET", url, timeout=5)
        print(f"  ✓ {name}")
        return True
    except Exception as exc:
        print(f"  ✗ {name}: {exc}", file=sys.stderr)
        return False


def main() -> int:
    p = argparse.ArgumentParser(description="One-shot AIOps demo runner")
    p.add_argument("--checkout", default="http://localhost:8080")
    p.add_argument("--payment", default="http://localhost:8081")
    p.add_argument("--detector", default="http://localhost:8001")
    p.add_argument("--incidents", default="http://localhost:8002")
    p.add_argument("--rca", default="http://localhost:8003")
    p.add_argument("--decision", default="http://localhost:8006")
    p.add_argument("--remediation", default="http://localhost:8004")
    p.add_argument("--wait-incident", type=int, default=45)
    p.add_argument("--skip-load", action="store_true")
    args = p.parse_args()

    print("=" * 60)
    print(" AIOps one-shot demo")
    print("=" * 60)

    print("\n[1] Health checks")
    healthy = True
    for name, url in [
        ("checkout", f"{args.checkout}/health"),
        ("payment", f"{args.payment}/health"),
        ("detector", f"{args.detector}/health"),
        ("incidents", f"{args.incidents}/health"),
        ("rca", f"{args.rca}/health"),
        ("decision", f"{args.decision}/health"),
        ("remediation", f"{args.remediation}/health"),
    ]:
        if not ok(name, url):
            healthy = False
    if not healthy:
        print("\nStart stack first: docker compose up -d --build", file=sys.stderr)
        print("Then: bash scripts/wait_for_stack.sh", file=sys.stderr)
        return 1

    print("\n[2] Reset chaos → healthy baseline")
    for base, body in [
        (args.checkout, {"error_rate": 0.02, "extra_latency_ms": 0, "fault_mode": "none"}),
        (args.payment, {"error_rate": 0.01, "extra_latency_ms": 0, "fault_mode": "none"}),
    ]:
        http_json("POST", f"{base}/chaos", body)
        print(f"  reset {base}")

    if not args.skip_load:
        print("\n[3] Short normal load (10s)")
        end = time.time() + 10
        n = 0
        while time.time() < end:
            try:
                http_json(
                    "POST",
                    f"{args.checkout}/checkout",
                    {"order_id": f"warm-{n}", "amount": 10.0, "currency": "USD"},
                    timeout=8,
                )
            except Exception:
                pass
            n += 1
            time.sleep(0.25)
        print(f"  sent ~{n} checkouts")

    print("\n[4] Inject payment DB pool exhaustion + load")
    http_json(
        "POST",
        f"{args.payment}/chaos",
        {"error_rate": 0.45, "extra_latency_ms": 400, "fault_mode": "db_pool"},
    )
    if not args.skip_load:
        end = time.time() + 15
        while time.time() < end:
            try:
                http_json(
                    "POST",
                    f"{args.checkout}/checkout",
                    {"order_id": f"fault-{int(time.time()*1000)}", "amount": 42.0},
                    timeout=8,
                )
            except Exception:
                pass
            time.sleep(0.2)

    print("\n[5] Manual detect (demo-reliable path)")
    detect = http_json(
        "POST",
        f"{args.detector}/detect",
        {
            "service_name": "payment-service",
            "metric_name": "http_error_rate",
            "metric_value": 0.45,
            "threshold": 0.15,
            "gather_context": True,
        },
        timeout=90,
    )
    event = detect.get("event") or {}
    decision = detect.get("decision") or {}
    print(f"  anomaly_id={event.get('id')}")
    print(f"  confidence={decision.get('confidence_score')} "
          f"completeness={decision.get('context_completeness')}")
    print(f"  explanation={(decision.get('explanation') or event.get('message') or '')[:120]}")

    print(f"\n[6] Wait up to {args.wait_incident}s for incident ticket")
    deadline = time.time() + args.wait_incident
    incident = None
    while time.time() < deadline:
        try:
            items = http_json("GET", f"{args.incidents}/incidents?limit=5", timeout=10)
            if isinstance(items, list) and items:
                incident = items[0]
                break
        except Exception:
            pass
        time.sleep(2)
    if not incident:
        print("  No incident yet — check detector/IM logs", file=sys.stderr)
        return 1
    iid = incident.get("id")
    print(f"  incident_id={iid} status={incident.get('status')}")

    print("\n[7] RCA analyze (force, wait)")
    try:
        rca = http_json(
            "POST",
            f"{args.rca}/analyze-incident/{iid}?force=true&persist=true",
            timeout=120,
        )
        root = ((rca or {}).get("result") or {}).get("root_cause")
        print(f"  mode={(rca or {}).get('mode')} root_cause={(root or '')[:140]}")
    except Exception as exc:
        print(f"  RCA call failed (non-fatal): {exc}")

    print("\n[8] Decision Engine score (policy table)")
    try:
        ctx = (event.get("context") or {}) if isinstance(event, dict) else {}
        dec = http_json(
            "POST",
            f"{args.decision}/decide",
            {
                "service_name": "payment-service",
                "metric_name": "http_error_rate",
                "metric_value": 0.45,
                "confidence_score": float(
                    decision.get("confidence_score")
                    or ctx.get("confidence_score")
                    or 70
                ),
                "confidence_breakdown": decision.get("confidence_breakdown")
                or ctx.get("confidence_breakdown")
                or {},
                "missing_context": decision.get("missing_context")
                or ctx.get("missing_context")
                or [],
                "context_completeness": float(
                    decision.get("context_completeness")
                    or ctx.get("context_completeness")
                    or 0.5
                ),
                "explanation": decision.get("explanation")
                or event.get("message")
                or "",
                "incident_id": iid,
                "anomaly_id": event.get("id"),
                "skip_side_effects": False,
            },
            timeout=90,
        )
        d = (dec or {}).get("decision") or {}
        print(
            f"  action={d.get('action')} band={d.get('band')} "
            f"reason={(d.get('reason') or '')[:100]}"
        )
    except Exception as exc:
        print(f"  Decision engine call failed (non-fatal): {exc}")

    print("\n" + "=" * 60)
    print(" Operator URLs (record your video from here)")
    print("=" * 60)
    print(f"  Console:      http://localhost:8500")
    print(f"  Incidents:    http://localhost:8002/")
    print(f"  Incident API: http://localhost:8002/incidents/{iid}")
    print(f"  Grafana:      http://localhost:3000")
    print(f"  Decision:     http://localhost:8006/decision-table")
    print(f"  Engine QA:    http://localhost:8503")
    print(f"  Remediation:  http://localhost:8501")
    print("\n  Reset chaos:  python scripts/chaos.py --reset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
