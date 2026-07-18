#!/usr/bin/env python3
"""
Multi-stage dynamic load + chaos injection for realistic telemetry.

Stages (default profile `demo`)
--------------------------------
  1. normal   — low RPS, healthy chaos
  2. spike    — high RPS, still healthy
  3. errors   — inject error_rate / fault_mode on target service
  4. latency  — inject extra_latency_ms
  5. recovery — reset chaos, moderate RPS

Why multi-stage?
----------------
Static metrics cannot exercise EWMA/STL/IsolationForest or fill Loki/Tempo.
Time-varying RED signals + fault-mode log lines create *detectable* anomalies
and grounded RCA evidence.

Usage
-----
  python scripts/dynamic_load.py --profile demo
  python scripts/dynamic_load.py --profile demo --checkout http://localhost:8080
  python scripts/dynamic_load.py --stage-seconds 20 --profile full
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Stats:
    ok: int = 0
    err: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, success: bool) -> None:
        with self.lock:
            if success:
                self.ok += 1
            else:
                self.err += 1


def http_json(method: str, url: str, body: Optional[dict] = None, timeout: float = 8.0) -> Any:
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


def set_chaos(base: str, payload: dict) -> None:
    try:
        out = http_json("POST", f"{base.rstrip('/')}/chaos", payload)
        print(f"  chaos {base} → {out}")
    except Exception as exc:
        print(f"  chaos failed {base}: {exc}", file=sys.stderr)


def one_checkout(url: str, stats: Stats) -> None:
    payload = {
        "order_id": f"ord-{random.randint(10000, 99999)}",
        "amount": round(random.uniform(5, 200), 2),
        "currency": "USD",
    }
    try:
        http_json("POST", f"{url.rstrip('/')}/checkout", payload, timeout=12)
        stats.add(True)
    except Exception:
        stats.add(False)


def load_for(url: str, rps: float, duration: float, workers: int, stats: Stats) -> None:
    stop_at = time.time() + duration
    interval = max(0.01, workers / max(rps, 0.1))
    threads = []

    def worker() -> None:
        while time.time() < stop_at:
            one_checkout(url, stats)
            time.sleep(interval + random.uniform(0, interval * 0.2))

    for _ in range(workers):
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=duration + 5)


PROFILES: dict[str, list[dict[str, Any]]] = {
    "demo": [
        {
            "name": "normal",
            "rps": 8,
            "seconds": 25,
            "checkout_chaos": {"error_rate": 0.02, "extra_latency_ms": 0, "fault_mode": "none"},
            "payment_chaos": {"error_rate": 0.01, "extra_latency_ms": 0, "fault_mode": "none"},
        },
        {
            "name": "spike",
            "rps": 35,
            "seconds": 20,
            "checkout_chaos": {"error_rate": 0.02, "extra_latency_ms": 0, "fault_mode": "none"},
            "payment_chaos": {"error_rate": 0.01, "extra_latency_ms": 0, "fault_mode": "none"},
        },
        {
            "name": "error_injection",
            "rps": 15,
            "seconds": 30,
            "checkout_chaos": {
                "error_rate": 0.05,
                "extra_latency_ms": 100,
                "fault_mode": "dependency_timeout",
            },
            "payment_chaos": {
                "error_rate": 0.4,
                "extra_latency_ms": 300,
                "fault_mode": "db_pool",
            },
        },
        {
            "name": "latency_injection",
            "rps": 12,
            "seconds": 25,
            "checkout_chaos": {
                "error_rate": 0.08,
                "extra_latency_ms": 1200,
                "fault_mode": "cache_miss",
            },
            "payment_chaos": {
                "error_rate": 0.05,
                "extra_latency_ms": 800,
                "fault_mode": "redis_cache_miss",
            },
        },
        {
            "name": "recovery",
            "rps": 10,
            "seconds": 20,
            "checkout_chaos": {"error_rate": 0.02, "extra_latency_ms": 0, "fault_mode": "none"},
            "payment_chaos": {"error_rate": 0.01, "extra_latency_ms": 0, "fault_mode": "none"},
        },
    ],
    "full": [],  # filled as longer demo
}

# full = demo with longer stages
PROFILES["full"] = [
    {**s, "seconds": int(s["seconds"] * 1.5)} for s in PROFILES["demo"]
]


def main() -> int:
    p = argparse.ArgumentParser(description="Dynamic multi-stage load + chaos")
    p.add_argument("--checkout", default="http://localhost:8080")
    p.add_argument("--payment", default="http://localhost:8081")
    p.add_argument("--profile", choices=list(PROFILES), default="demo")
    p.add_argument(
        "--stage-seconds",
        type=int,
        default=0,
        help="Override every stage duration (0=use profile defaults)",
    )
    p.add_argument("--workers", type=int, default=10)
    args = p.parse_args()

    stages = PROFILES[args.profile]
    print(f"=== Dynamic load profile={args.profile} stages={len(stages)} ===")
    print(f"checkout={args.checkout} payment={args.payment}")

    grand = Stats()
    for stage in stages:
        name = stage["name"]
        seconds = args.stage_seconds or int(stage["seconds"])
        rps = float(stage["rps"])
        print(f"\n--- stage={name} rps={rps} seconds={seconds} ---")
        set_chaos(args.checkout, stage.get("checkout_chaos") or {})
        set_chaos(args.payment, stage.get("payment_chaos") or {})
        st = Stats()
        load_for(args.checkout, rps, seconds, args.workers, st)
        print(f"  stage done ok={st.ok} err={st.err}")
        grand.ok += st.ok
        grand.err += st.err

    print(f"\n=== done total ok={grand.ok} err={grand.err} ===")
    print("Tip: watch Grafana :3000 and anomaly-detector :8001/results")
    return 0


if __name__ == "__main__":
    sys.exit(main())
