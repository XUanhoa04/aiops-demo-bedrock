#!/usr/bin/env python3
"""
Print detector threshold tuning suggestions from Feedback Collector.

Uses false-positive on-call labels to recommend ZSCORE / ERROR_RATE changes.

Usage:
  python scripts/suggest_threshold.py
  python scripts/suggest_threshold.py --api http://localhost:8005
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def http_get(url: str) -> str:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode()


def main() -> int:
    p = argparse.ArgumentParser(description="AIOps threshold tuning from feedback FPs")
    p.add_argument("--api", default="http://localhost:8005", help="Feedback collector base URL")
    p.add_argument("--json", action="store_true", help="Print JSON suggestion instead of text report")
    args = p.parse_args()
    base = args.api.rstrip("/")

    try:
        if args.json:
            raw = http_get(f"{base}/tuning/suggestions")
            data = json.loads(raw)
            print(json.dumps(data, indent=2))
        else:
            print(http_get(f"{base}/tuning/report"))
    except urllib.error.URLError as exc:
        print(f"Failed to reach feedback-collector at {base}: {exc}", file=sys.stderr)
        print("Hint: docker compose up -d aiops-feedback-collector", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
