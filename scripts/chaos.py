#!/usr/bin/env python3
"""
Chaos injection for the AIOps demo.

Examples:
  python scripts/chaos.py --service checkout --error-rate 0.4
  python scripts/chaos.py --service payment --extra-latency-ms 1200
  python scripts/chaos.py --reset
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request


SERVICES = {
    "checkout": "http://localhost:8080",
    "payment": "http://localhost:8081",
    "inventory": "http://localhost:8082",
    "fraud": "http://localhost:8083",
}


def post_chaos(base: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chaos",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--service", choices=list(SERVICES), default="checkout")
    p.add_argument("--error-rate", type=float, default=None)
    p.add_argument("--base-latency-ms", type=float, default=None)
    p.add_argument("--extra-latency-ms", type=float, default=None)
    p.add_argument(
        "--fault-mode",
        default=None,
        help=(
            "none|db_pool|cache_miss|dependency_timeout|cpu_throttle|"
            "gateway_timeout|redis_cache_miss|stock_lock|scoring_timeout"
        ),
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="Reset all demo services to healthy baseline",
    )
    args = p.parse_args()

    if args.reset:
        defaults = {
            "checkout": 0.02,
            "payment": 0.01,
            "inventory": 0.01,
            "fraud": 0.01,
        }
        for name, url in SERVICES.items():
            body = {
                "error_rate": defaults.get(name, 0.01),
                "extra_latency_ms": 0,
                "fault_mode": "none",
            }
            print(name, post_chaos(url, body))
        return 0

    payload = {}
    if args.error_rate is not None:
        payload["error_rate"] = args.error_rate
    if args.base_latency_ms is not None:
        payload["base_latency_ms"] = args.base_latency_ms
    if args.extra_latency_ms is not None:
        payload["extra_latency_ms"] = args.extra_latency_ms
    if args.fault_mode is not None:
        payload["fault_mode"] = args.fault_mode
    if not payload:
        print(
            "nothing to do; pass --error-rate / --extra-latency-ms / --fault-mode or --reset",
            file=sys.stderr,
        )
        return 2

    result = post_chaos(SERVICES[args.service], payload)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
