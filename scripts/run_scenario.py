#!/usr/bin/env python3
"""
Inject a single evaluation scenario's live_chaos profile + generate load.

  python scripts/run_scenario.py --scenario rca-01-payment-db-pool
  python scripts/run_scenario.py --list
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "evaluation" / "rca_scenarios.yaml"

try:
    import yaml
except ImportError:
    yaml = None


def load_scenarios() -> list[dict]:
    if yaml is None:
        raise SystemExit("pip install pyyaml")
    data = yaml.safe_load(EVAL.read_text(encoding="utf-8"))
    return list(data.get("scenarios") or [])


def post_json(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", default="")
    p.add_argument("--list", action="store_true")
    p.add_argument("--checkout", default="http://localhost:8080")
    p.add_argument("--payment", default="http://localhost:8081")
    p.add_argument("--duration", type=int, default=40)
    p.add_argument("--rps", type=float, default=12)
    args = p.parse_args()

    scenarios = load_scenarios()
    if args.list:
        for sc in scenarios:
            print(f"{sc['scenario_id']}: {sc.get('description', '')[:70]}")
        return 0

    sc = next((s for s in scenarios if s.get("scenario_id") == args.scenario), None)
    if not sc:
        print("Unknown scenario; use --list", file=sys.stderr)
        return 2

    chaos = sc.get("live_chaos") or {}
    svc = chaos.get("service", "checkout")
    base = args.checkout if svc == "checkout" else args.payment
    payload = {
        k: chaos[k]
        for k in ("error_rate", "extra_latency_ms", "base_latency_ms", "fault_mode")
        if k in chaos
    }
    print(f"Inject {sc['scenario_id']} on {svc}: {payload}")
    print(post_json(f"{base.rstrip('/')}/chaos", payload))

    # drive traffic through checkout (creates traces across both services)
    stop = time.time() + args.duration
    ok = err = 0
    interval = 1.0 / max(args.rps, 0.1)
    print(f"Load checkout for {args.duration}s @ ~{args.rps} rps")
    while time.time() < stop:
        body = {
            "order_id": f"ord-{random.randint(1000, 9999)}",
            "amount": 42.0,
            "currency": "USD",
        }
        try:
            post_json(f"{args.checkout.rstrip('/')}/checkout", body)
            ok += 1
        except Exception:
            err += 1
        time.sleep(interval)
    print(f"done ok={ok} err={err}")
    print("Ground truth:", sc.get("ground_truth_root_cause"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
