#!/usr/bin/env python3
"""
Simple concurrent load generator against checkout-service.

Usage (host):
  python scripts/load_test.py --url http://localhost:8080 --rps 20 --duration 60

Production note: prefer k6/Locust/Vegeta for serious load tests; this script is
zero-dependency (stdlib + urllib) so it runs without a venv on the host.
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


@dataclass
class Stats:
    ok: int = 0
    err: int = 0
    latencies: list[float] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, success: bool, latency_ms: float) -> None:
        with self.lock:
            if success:
                self.ok += 1
            else:
                self.err += 1
            self.latencies.append(latency_ms)


def one_request(url: str, stats: Stats) -> None:
    payload = json.dumps(
        {
            "order_id": f"ord-{random.randint(1000, 9999)}",
            "amount": round(random.uniform(5, 200), 2),
            "currency": "USD",
        }
    ).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/checkout",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
            ok = 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        e.read()
        ok = False
    except Exception:
        ok = False
    elapsed = (time.perf_counter() - start) * 1000
    stats.record(ok, elapsed)


def worker(url: str, stats: Stats, stop_at: float, interval: float) -> None:
    while time.time() < stop_at:
        one_request(url, stats)
        time.sleep(interval)


def main() -> int:
    p = argparse.ArgumentParser(description="AIOps demo load test")
    p.add_argument("--url", default="http://localhost:8080")
    p.add_argument("--rps", type=float, default=10.0, help="approx requests/sec total")
    p.add_argument("--duration", type=int, default=45, help="seconds")
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args()

    stats = Stats()
    stop_at = time.time() + args.duration
    interval = max(args.workers / max(args.rps, 0.1), 0.01)

    print(
        f"load_test url={args.url} rps≈{args.rps} duration={args.duration}s workers={args.workers}"
    )
    threads = [
        threading.Thread(
            target=worker, args=(args.url, stats, stop_at, interval), daemon=True
        )
        for _ in range(args.workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = stats.ok + stats.err
    lats = sorted(stats.latencies)
    p95 = lats[int(len(lats) * 0.95)] if lats else 0
    err_rate = (stats.err / total) if total else 0
    print(
        f"done total={total} ok={stats.ok} err={stats.err} "
        f"error_rate={err_rate:.2%} p95_ms={p95:.1f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
