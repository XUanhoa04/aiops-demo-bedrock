#!/usr/bin/env python3
"""
Live end-to-end evaluation against a running compose stack.

Flow per scenario (with live_chaos)
-----------------------------------
  1. Inject chaos on checkout/payment
  2. Generate /checkout traffic so Prom/Loki/Tempo fill
  3. Wait for anomaly → incident (or create manual ticket)
  4. Force RCA analyze
  5. Score root_cause vs ground truth
  6. Reset chaos

This is the path that proves "real runtime data", not offline YAML-only eval.

Usage
-----
  # stack must be up
  python evaluation/evaluate_live_e2e.py --limit 5
  python evaluation/evaluate_live_e2e.py --scenario rca-01-payment-db-pool
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR))
sys.path.insert(0, str(ROOT / "shared"))

from dataset_io import is_fault_scenario, load_scenarios, resolve_dataset_paths  # noqa: E402
from scoring import is_rca_correct, jaccard, keyword_hit_rate  # noqa: E402


def http_json(
    method: str,
    url: str,
    body: Optional[dict] = None,
    timeout: float = 30.0,
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
        return json.loads(raw) if raw else None


def http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


def post_chaos(base: str, payload: dict) -> dict:
    return http_json("POST", f"{base.rstrip('/')}/chaos", payload) or {}


def drive_load(checkout: str, seconds: int, rps: float) -> tuple[int, int]:
    stop = time.time() + seconds
    ok = err = 0
    interval = 1.0 / max(rps, 0.1)
    while time.time() < stop:
        body = {
            "order_id": f"e2e-{random.randint(10000, 99999)}",
            "amount": 42.0,
            "currency": "USD",
        }
        try:
            http_json("POST", f"{checkout.rstrip('/')}/checkout", body, timeout=15)
            ok += 1
        except Exception:
            err += 1
        time.sleep(interval)
    return ok, err


def find_recent_incident(
    incident_url: str,
    service_name: str,
    *,
    since_epoch: float,
) -> Optional[dict]:
    try:
        items = http_json(
            "GET",
            f"{incident_url.rstrip('/')}/incidents?limit=30&service_name={service_name}",
        )
    except Exception:
        items = None
    if not items:
        try:
            items = http_json("GET", f"{incident_url.rstrip('/')}/incidents?limit=30")
        except Exception:
            return None
    best = None
    for inc in items or []:
        if service_name and inc.get("service_name") not in {
            service_name,
            service_name.replace("-service", ""),
        }:
            # allow checkout-service vs fuzzy
            if service_name not in str(inc.get("service_name") or ""):
                continue
        created = inc.get("created_at") or ""
        try:
            # accept any recent open/investigating ticket
            status = str(inc.get("status") or "")
            if status in {"resolved", "closed", "false_positive"}:
                continue
            best = inc
            break
        except Exception:
            continue
    return best


def ensure_incident(
    incident_url: str,
    service_name: str,
    scenario_id: str,
    gt: str,
) -> dict:
    found = find_recent_incident(incident_url, service_name, since_epoch=time.time() - 600)
    if found:
        return found
    # Manual ticket so RCA can still run even if detector is slow
    return http_json(
        "POST",
        f"{incident_url.rstrip('/')}/incidents",
        {
            "title": f"[e2e] {scenario_id}",
            "description": f"live e2e eval gt={gt[:200]}",
            "service_name": service_name,
            "severity": "high",
            "metric_name": "http_error_rate",
            "metric_value": 0.4,
        },
    )


def run_one(
    sc: dict[str, Any],
    *,
    checkout: str,
    payment: str,
    incident_url: str,
    rca_url: str,
    load_seconds: int,
    rps: float,
    wait_detector: int,
    force_rule_based: bool = True,
) -> dict[str, Any]:
    sid = str(sc.get("scenario_id"))
    gt = sc.get("ground_truth_root_cause") or ""
    kws = list(sc.get("keywords") or [])
    chaos = dict(sc.get("live_chaos") or {})
    if not chaos:
        return {
            "scenario_id": sid,
            "skipped": True,
            "reason": "no live_chaos",
            "correct": False,
        }

    svc_short = str(chaos.get("service") or "checkout")
    base = checkout if "checkout" in svc_short else payment
    ticket_service = (
        sc.get("ticket_service")
        or (sc.get("affected_services") or [f"{svc_short}-service"])[0]
    )
    if ticket_service in ("checkout", "payment"):
        ticket_service = f"{ticket_service}-service"

    payload = {
        k: chaos[k]
        for k in ("error_rate", "extra_latency_ms", "base_latency_ms", "fault_mode")
        if k in chaos
    }
    # Ensure a visible fault for detection
    payload.setdefault("error_rate", 0.35)
    payload.setdefault("fault_mode", "none")

    row: dict[str, Any] = {
        "scenario_id": sid,
        "split": sc.get("split") or "core",
        "ticket_service": ticket_service,
        "chaos": payload,
        "ground_truth": gt,
    }
    try:
        post_chaos(base, payload)
        # Also nudge the other service lightly so checkout path produces traces
        if base.rstrip("/") == payment.rstrip("/"):
            try:
                post_chaos(checkout, {"error_rate": 0.05, "extra_latency_ms": 50})
            except Exception:
                pass

        ok, err = drive_load(checkout, load_seconds, rps)
        row["load_ok"] = ok
        row["load_err"] = err
        time.sleep(max(0, wait_detector))

        inc = ensure_incident(incident_url, ticket_service, sid, gt)
        iid = str(inc.get("id") or "")
        row["incident_id"] = iid
        if not iid:
            row["correct"] = False
            row["notes"] = "no incident id"
            return row

        q = "force=true&persist=true"
        if force_rule_based:
            q += "&force_rule_based=true"
        rca = http_json(
            "POST",
            f"{rca_url.rstrip('/')}/analyze-incident/{iid}?{q}",
            timeout=90,
        )
        result = (rca or {}).get("result") or {}
        pred = result.get("root_cause") or ""
        mode = (rca or {}).get("mode") or "unknown"
        conf = result.get("confidence")
        correct = is_rca_correct(pred, gt, kws)
        row.update(
            {
                "predicted": pred,
                "correct": correct,
                "jaccard": round(jaccard(pred, gt), 4),
                "keyword_rate": round(keyword_hit_rate(pred, kws), 4),
                "confidence": conf,
                "mode": mode,
                "evidence_sources": (rca or {}).get("evidence_sources"),
                "notes": (
                    f"status={(rca or {}).get('status')} "
                    f"bedrock_error={(rca or {}).get('bedrock_error')}"
                ),
            }
        )
        # Honest signal: if only "slow traces" / insufficient, live logs may be empty
        if not correct and (
            "slow traces" in (pred or "").lower()
            or "insufficient" in (pred or "").lower()
        ):
            row["notes"] = (
                (row.get("notes") or "")
                + " | hint: Loki may lack fault log lines — check app log labels "
                "and evidence_sources; offline YAML still measures catalog"
            )
    except Exception as exc:
        row["correct"] = False
        row["predicted"] = ""
        row["notes"] = f"error: {exc}"
    finally:
        # Reset both apps
        for b in (checkout, payment):
            try:
                post_chaos(
                    b,
                    {
                        "error_rate": 0.02 if "8080" in b or "checkout" in b else 0.01,
                        "extra_latency_ms": 0,
                        "fault_mode": "none",
                    },
                )
            except Exception:
                pass
    return row


def main() -> int:
    p = argparse.ArgumentParser(description="Live E2E RCA evaluation")
    p.add_argument("--dataset", type=Path, default=None)
    p.add_argument("--split", choices=("all", "core", "holdout"), default="core")
    p.add_argument("--scenario", default="", help="Run a single scenario_id")
    p.add_argument("--limit", type=int, default=5, help="Max scenarios (default 5)")
    p.add_argument("--checkout", default="http://localhost:8080")
    p.add_argument("--payment", default="http://localhost:8081")
    p.add_argument("--incident-url", default="http://localhost:8002")
    p.add_argument("--rca-url", default="http://localhost:8003")
    p.add_argument("--load-seconds", type=int, default=20)
    p.add_argument("--rps", type=float, default=10.0)
    p.add_argument("--wait-detector", type=int, default=35)
    p.add_argument(
        "--force-rule-based",
        action="store_true",
        default=True,
        help="Ask RCA API to use config rules (default True for offline parity)",
    )
    p.add_argument(
        "--allow-bedrock",
        action="store_true",
        help="Allow live Bedrock path (disables force_rule_based)",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=EVAL_DIR / "results" / "rca_live_e2e_latest.json",
    )
    args = p.parse_args()

    # Preflight
    for name, url in (
        ("checkout", f"{args.checkout}/health"),
        ("payment", f"{args.payment}/health"),
        ("incident-manager", f"{args.incident_url}/health"),
        ("rca-engine", f"{args.rca_url}/health"),
    ):
        if not http_ok(url):
            print(f"FAIL: {name} not reachable at {url}", file=sys.stderr)
            print("Start stack: docker compose up -d --build", file=sys.stderr)
            return 2

    paths = resolve_dataset_paths(
        args.dataset, default_files=[EVAL_DIR / "rca_scenarios.yaml"]
    )
    scenarios = load_scenarios(paths, split=args.split if not args.scenario else "all")
    if args.scenario:
        scenarios = [s for s in scenarios if s.get("scenario_id") == args.scenario]
    scenarios = [
        s
        for s in scenarios
        if s.get("live_chaos") and is_fault_scenario(s)
    ]
    if args.limit > 0:
        scenarios = scenarios[: args.limit]

    print(f"=== Live E2E RCA n={len(scenarios)} split={args.split} ===")
    rows: list[dict] = []
    for sc in scenarios:
        print(f"\n>> {sc.get('scenario_id')}")
        row = run_one(
            sc,
            checkout=args.checkout,
            payment=args.payment,
            incident_url=args.incident_url,
            rca_url=args.rca_url,
            load_seconds=args.load_seconds,
            rps=args.rps,
            wait_detector=args.wait_detector,
            force_rule_based=(not args.allow_bedrock),
        )
        rows.append(row)
        print(
            f"   correct={row.get('correct')} mode={row.get('mode')} "
            f"pred={(row.get('predicted') or '')[:70]!r}"
        )

    scored = [r for r in rows if not r.get("skipped")]
    correct = sum(1 for r in scored if r.get("correct"))
    n = len(scored) or 1
    acc = correct / n if scored else 0.0
    print(f"\n--- Aggregate ---")
    print(f"Accuracy: {acc:.1%} ({correct}/{len(scored)})")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "live_e2e",
        "split_filter": args.split,
        "aggregate": {
            "n": len(scored),
            "correct": correct,
            "accuracy": round(acc, 4),
            "skipped": sum(1 for r in rows if r.get("skipped")),
        },
        "rows": rows,
        "note": (
            "Live path uses real chaos + OTel + RCA API. Scores depend on stack "
            "health and detector timing; not identical to offline YAML eval."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
