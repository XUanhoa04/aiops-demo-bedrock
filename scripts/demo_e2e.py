#!/usr/bin/env python3
"""
Full-pipeline end-to-end demo (Anomaly → Incident → RCA → Remediation → Feedback).

  python scripts/demo_e2e.py
  python scripts/demo_e2e.py --skip-chaos
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def http_json(method: str, url: str, body: dict | None = None, timeout: int = 60):
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


def wait_health(urls: list[str], tries: int = 40) -> bool:
    for i in range(tries):
        ok = 0
        for u in urls:
            try:
                urllib.request.urlopen(u, timeout=3)
                ok += 1
            except Exception:
                pass
        print(f"  health {ok}/{len(urls)} (try {i+1}/{tries})")
        if ok == len(urls):
            return True
        time.sleep(3)
    return False


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--skip-chaos", action="store_true")
    p.add_argument("--wait-rca", type=int, default=90)
    args = p.parse_args()

    base = {
        "checkout": "http://localhost:8080",
        "detector": "http://localhost:8001",
        "incidents": "http://localhost:8002",
        "rca": "http://localhost:8003",
        "remediation": "http://localhost:8004",
        "feedback": "http://localhost:8005",
    }

    print("== AIOps E2E demo ==")
    print("[0] wait for stack health…")
    healthy = wait_health(
        [
            f"{base['checkout']}/health",
            f"{base['detector']}/health",
            f"{base['incidents']}/health",
            f"{base['rca']}/health",
            f"{base['remediation']}/health",
            f"{base['feedback']}/health",
        ]
    )
    if not healthy:
        print("Stack not healthy. Run: docker compose up -d --build", file=sys.stderr)
        return 1

    if not args.skip_chaos:
        print("[1] chaos inject")
        print(
            http_json(
                "POST",
                f"{base['checkout']}/chaos",
                {"error_rate": 0.4, "extra_latency_ms": 150},
            )
        )

    print("[2] anomaly → incident")
    det = http_json(
        "POST",
        f"{base['detector']}/detect",
        {
            "service_name": "checkout-service",
            "metric_name": "http_error_rate",
            "metric_value": 0.42,
            "threshold": 0.15,
        },
    )
    print("  detect:", (det.get("event") or {}).get("id") if isinstance(det, dict) else det)

    incident = None
    for _ in range(20):
        items = http_json("GET", f"{base['incidents']}/incidents?limit=3")
        if isinstance(items, list) and items:
            incident = items[0]
            break
        time.sleep(2)
    if not incident:
        print("No incident created", file=sys.stderr)
        return 1
    iid = incident["id"]
    print("  incident:", iid, incident.get("title"))

    print("[3] RCA (Bedrock or rule fallback)")
    try:
        rca = http_json(
            "POST",
            f"{base['rca']}/analyze-incident/{iid}?force=true&persist=true",
            timeout=args.wait_rca,
        )
        print("  status=", rca.get("status"), "mode=", rca.get("mode"))
        root = (rca.get("result") or {}).get("root_cause")
        print("  root_cause=", (root or "")[:180])
    except Exception as exc:
        print("  RCA error:", exc)

    print("[4] remediation propose")
    try:
        actions = http_json(
            "POST",
            f"{base['remediation']}/remediate/propose",
            {"incident_id": iid, "actions": []},
        )
        if isinstance(actions, list):
            for a in actions:
                print(
                    f"  [{a.get('risk_level')}] {a.get('action_type')} "
                    f"status={a.get('status')} text={(a.get('action_text') or '')[:60]}"
                )
        else:
            print(" ", actions)
    except Exception as exc:
        print("  remediation error:", exc)

    print("[5] feedback sample")
    try:
        fb = http_json(
            "POST",
            f"{base['feedback']}/feedback",
            {
                "incident_id": iid,
                "anomaly_correct": True,
                "rca_useful": True,
                "action_effective": True,
                "comment": "E2E demo auto-feedback",
                "reviewer": "demo-bot",
            },
        )
        print("  feedback_id=", fb.get("id"))
        print("  stats=", http_json("GET", f"{base['feedback']}/stats"))
    except Exception as exc:
        print("  feedback error:", exc)

    print("[6] reset chaos")
    try:
        http_json("POST", f"{base['checkout']}/chaos", {"error_rate": 0.01, "extra_latency_ms": 0})
    except Exception:
        pass

    print()
    print("Pipeline complete. Open:")
    print("  Incidents:    http://localhost:8002/")
    print("  Remediation:  http://localhost:8501")
    print("  Feedback:     http://localhost:8502")
    print("  Grafana:      http://localhost:3000")
    print("  Ticket:       http://localhost:8002/incidents/" + iid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
