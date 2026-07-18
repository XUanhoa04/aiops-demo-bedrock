#!/usr/bin/env python3
"""
Toggle OpenTelemetry Demo (flagd) chaos flags for live AIOps demos.

Flags (from demo.flagd.json):
  paymentFailure, paymentUnreachable, productCatalogFailure, cartFailure,
  adFailure, adHighCpu, recommendationCacheFailure, emailMemoryLeak,
  kafkaQueueProblems, imageSlowLoad, intlShippingSlowdown, ...

Usage:
  python scripts/astronomy/set_flag.py --list
  python scripts/astronomy/set_flag.py paymentFailure on
  python scripts/astronomy/set_flag.py cartFailure 50%
  python scripts/astronomy/set_flag.py paymentFailure off
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Common boolean-style flags
BOOL_FLAGS = {
    "adFailure",
    "adHighCpu",
    "adManualGc",
    "failedReadinessProbe",
    "paymentFailure",
    "paymentUnreachable",
    "productCatalogFailure",
    "recommendationCacheFailure",
    "kafkaQueueProblems",
}

# Percent / multi-variant flags (pass variant name as second arg)
MULTI_FLAGS = {
    "cartFailure": ["off", "10%", "25%", "50%", "75%", "90%", "100%"],
    "emailMemoryLeak": ["off", "1x", "10x", "100x", "1000x", "10000x"],
    "imageSlowLoad": ["off", "5sec", "10sec"],
    "intlShippingSlowdown": ["off"],  # may have more in newer demos
    "loadGeneratorVUs": ["off"],
}


def http_json(method: str, url: str, body: Any = None, timeout: float = 10) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else None


def patch_flag_file(demo_dir: Path, flag: str, variant: str) -> bool:
    """Fallback: rewrite demo.flagd.json defaultVariant and restart flagd."""
    flag_path = demo_dir / "src" / "flagd" / "demo.flagd.json"
    if not flag_path.is_file():
        # also try container-copied paths
        return False
    data = json.loads(flag_path.read_text(encoding="utf-8"))
    flags = data.get("flags") or {}
    if flag not in flags:
        print(f"Unknown flag in file: {flag}", file=sys.stderr)
        return False
    flags[flag]["defaultVariant"] = variant
    flag_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Updated {flag_path} defaultVariant={variant}")
    # restart flagd for reload
    import subprocess

    try:
        subprocess.run(
            ["docker", "restart", "flagd"],
            check=False,
            capture_output=True,
            text=True,
        )
        print("Restarted flagd container")
    except Exception as exc:
        print(f"Could not restart flagd: {exc}", file=sys.stderr)
    return True


def try_flagd_ui(flag: str, variant: str, base: str) -> bool:
    """Best-effort Flagd UI / management endpoints (version-dependent)."""
    candidates = [
        (f"{base.rstrip('/')}/api/flags/{flag}", {"defaultVariant": variant}),
        (f"{base.rstrip('/')}/flags/{flag}", {"defaultVariant": variant}),
    ]
    for url, body in candidates:
        try:
            http_json("PUT", url, body)
            print(f"OK via UI API {url}")
            return True
        except Exception:
            continue
    return False


def main() -> int:
    p = argparse.ArgumentParser(description="Set Astronomy Shop chaos feature flags")
    p.add_argument("flag", nargs="?", help="Flag name")
    p.add_argument(
        "variant",
        nargs="?",
        default="on",
        help="Variant: on|off|50%|... (default on)",
    )
    p.add_argument("--list", action="store_true")
    p.add_argument("--flagd-ui", default="http://localhost:4000")
    p.add_argument(
        "--demo-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "third_party"
        / "opentelemetry-demo",
    )
    args = p.parse_args()

    if args.list or not args.flag:
        print("Boolean-ish flags:", ", ".join(sorted(BOOL_FLAGS)))
        print("Multi-variant flags:")
        for k, v in MULTI_FLAGS.items():
            print(f"  {k}: {', '.join(v)}")
        print("\nExamples:")
        print("  python scripts/astronomy/set_flag.py paymentFailure on")
        print("  python scripts/astronomy/set_flag.py cartFailure 50%")
        print("  python scripts/astronomy/set_flag.py productCatalogFailure off")
        return 0

    flag = args.flag
    variant = args.variant
    if flag in BOOL_FLAGS and variant in ("true", "1", "yes"):
        variant = "on"
    if flag in BOOL_FLAGS and variant in ("false", "0", "no"):
        variant = "off"

    # 1) try UI
    if try_flagd_ui(flag, variant, args.flagd_ui):
        return 0

    # 2) patch file + restart flagd
    if patch_flag_file(args.demo_dir, flag, variant):
        return 0

    print(
        "Could not set flag automatically. Open Flagd UI: http://localhost:4000\n"
        f"  Manually set {flag} = {variant}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
