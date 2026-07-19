#!/usr/bin/env python3
"""
Compare RCA system against naive + SRE-style baselines.

Why baselines?
--------------
Offline accuracy alone can look inflated if rules share keywords with the
dataset. Baselines prove the system is better than:

  Weak (legacy)
  - random root-cause strings
  - always "elevated http_error_rate" (metric-only shallow answer)
  - keyword-free empty string

  Stronger (SRE heuristics)
  - always blame ticket service elevated error
  - blame highest-error neighbor in symptoms
  - always last deploy / post-deploy if any change_event
  - keyword/log phrase bag → first matching catalog-ish template

CI fails if system accuracy does not strictly beat the best *weak* baseline.
Optionally --require-beats-strong also requires beating SRE baselines.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "aiops-services" / "rca-engine"))
sys.path.insert(0, str(EVAL_DIR))
sys.path.insert(0, str(ROOT))

from scoring import (  # noqa: E402
    is_rca_correct,
    jaccard,
    keyword_hit_rate,
)

from evaluate_rca import run_offline  # noqa: E402

RANDOM_CAUSES = [
    "kubernetes node disk pressure",
    "DNS resolution failure in mesh",
    "certificate expiry on ingress",
    "bad deploy of inventory-service",
    "message queue backlog on kafka",
]

# Simple phrase → template for bag-of-words baseline (not the real catalog matcher)
_PHRASE_TEMPLATES: list[tuple[list[str], str]] = [
    (["connection pool", "pool exhaust", "hikari", "jdbc", "too many connections"],
     "{svc} database connection pool exhaustion"),
    (["cache miss", "cold redis", "redis keyspace", "redis cache"],
     "{svc} high latency due to cache miss / cold redis keyspace"),
    (["gateway timeout", "payment gateway", "psp", "deadline exceeded"],
     "payment gateway timeout / dependency failure"),
    (["cpu throttle", "thread pool", "worker pool", "scheduling delayed"],
     "{svc} worker/thread pool saturation or CPU throttle"),
    (["stock lock", "inventory reserve", "sku lock"],
     "inventory-service stock lock / DB contention causing checkout latency"),
    (["fraud", "scoring", "rule engine"],
     "fraud-service latency / scoring saturation cascading into payment and checkout"),
    (["deploy", "release", "rollback", "rolled out"],
     "{svc} post-deploy regression / bad release correlated with error spike"),
]


def score_predictions(
    scenarios: list[dict[str, Any]],
    predictions: dict[str, str],
    *,
    mode: str = "default",
) -> dict[str, Any]:
    correct = 0
    correct_strict = 0
    jacs = []
    kws = []
    for sc in scenarios:
        sid = str(sc["scenario_id"])
        pred = predictions.get(sid, "")
        gt = sc.get("ground_truth_root_cause") or ""
        keys = list(sc.get("keywords") or [])
        ok = is_rca_correct(pred, gt, keys, mode="default")
        ok_s = is_rca_correct(pred, gt, keys, mode="strict")
        correct += int(ok)
        correct_strict += int(ok_s)
        jacs.append(jaccard(pred, gt))
        kws.append(keyword_hit_rate(pred, keys))
    n = len(scenarios) or 1
    return {
        "n": len(scenarios),
        "correct": correct,
        "accuracy": round(correct / n, 4),
        "correct_strict": correct_strict,
        "accuracy_strict": round(correct_strict / n, 4),
        "mean_jaccard": round(sum(jacs) / n, 4),
        "mean_keyword_rate": round(sum(kws) / n, 4),
    }


def baseline_random(scenarios: list[dict], seed: int = 42) -> dict[str, str]:
    rng = random.Random(seed)
    return {
        str(sc["scenario_id"]): rng.choice(RANDOM_CAUSES) for sc in scenarios
    }


def baseline_always_error_rate(scenarios: list[dict]) -> dict[str, str]:
    out = {}
    for sc in scenarios:
        svc = (sc.get("affected_services") or ["service"])[0]
        out[str(sc["scenario_id"])] = f"elevated http_error_rate on {svc}"
    return out


def baseline_empty(scenarios: list[dict]) -> dict[str, str]:
    return {str(sc["scenario_id"]): "" for sc in scenarios}


def baseline_ticket_service(scenarios: list[dict]) -> dict[str, str]:
    """SRE heuristic: always blame the ticket / first affected service."""
    out = {}
    for sc in scenarios:
        svc = (
            sc.get("ticket_service")
            or (sc.get("affected_services") or ["unknown-service"])[0]
        )
        out[str(sc["scenario_id"])] = (
            f"elevated error rate on {svc} (ticket service heuristic)"
        )
    return out


def baseline_highest_error_neighbor(scenarios: list[dict]) -> dict[str, str]:
    """Blame the service with the highest http_error_rate in symptoms."""
    out = {}
    for sc in scenarios:
        metrics = (sc.get("symptoms") or {}).get("metrics") or []
        best_svc = sc.get("ticket_service") or (sc.get("affected_services") or ["service"])[0]
        best_err = -1.0
        for m in metrics:
            if str(m.get("name")) != "http_error_rate":
                continue
            try:
                val = float(m.get("value") or 0)
            except (TypeError, ValueError):
                continue
            if val > best_err:
                best_err = val
                best_svc = m.get("service") or best_svc
        out[str(sc["scenario_id"])] = (
            f"elevated error rate on upstream dependency {best_svc} "
            f"(highest error-rate heuristic)"
        )
    return out


def baseline_last_deploy(scenarios: list[dict]) -> dict[str, str]:
    """If change_events present → post-deploy; else elevated ticket service."""
    out = {}
    for sc in scenarios:
        svc = (
            sc.get("ticket_service")
            or (sc.get("affected_services") or ["service"])[0]
        )
        events = sc.get("change_events") or []
        if events:
            ev_svc = events[0].get("service") or svc
            out[str(sc["scenario_id"])] = (
                f"{ev_svc} post-deploy regression / bad release correlated with error spike"
            )
        else:
            out[str(sc["scenario_id"])] = f"elevated http_error_rate on {svc}"
    return out


def baseline_log_phrase_bag(scenarios: list[dict]) -> dict[str, str]:
    """First matching phrase bag over concatenated log lines (shallow catalog)."""
    out = {}
    for sc in scenarios:
        logs = (sc.get("symptoms") or {}).get("logs") or []
        blob = " ".join(str(row.get("line") or "") for row in logs).lower()
        svc = (
            sc.get("ticket_service")
            or (sc.get("affected_services") or ["service"])[0]
        )
        pred = f"elevated http_error_rate on {svc} with insufficient log/trace corroboration"
        for phrases, tmpl in _PHRASE_TEMPLATES:
            if any(p in blob for p in phrases):
                pred = tmpl.format(svc=svc)
                break
        # no-fault GT scenarios: prefer insufficient if logs lack errors
        gt = (sc.get("ground_truth_root_cause") or "").lower()
        if any(x in gt for x in ("without application fault", "normal traffic", "insufficient evidence")):
            if not any(k in blob for k in ("error", "fail", "timeout", "exception")):
                pred = (
                    f"Insufficient evidence: cannot pin root cause for {svc}; "
                    "severity=high"
                )
        out[str(sc["scenario_id"])] = pred
    return out


def system_predictions(scenarios: list[dict]) -> dict[str, str]:
    rows = run_offline(scenarios, use_bedrock=False)
    return {r.scenario_id: r.predicted for r in rows}


def main() -> int:
    p = argparse.ArgumentParser(description="RCA baseline comparison")
    p.add_argument("--dataset", type=Path, default=None)
    p.add_argument(
        "--extra-dataset",
        type=Path,
        action="append",
        default=None,
    )
    p.add_argument(
        "--output",
        type=Path,
        default=EVAL_DIR / "results" / "baselines_latest.json",
    )
    p.add_argument(
        "--require-beats-baselines",
        action="store_true",
        help="Exit 1 if system accuracy ≤ best weak baseline accuracy",
    )
    p.add_argument(
        "--require-beats-strong",
        action="store_true",
        help="Also require beating best SRE-style baseline",
    )
    p.add_argument(
        "--include-hard",
        action="store_true",
        default=True,
        help="Include hard OOD suite when present (default True)",
    )
    args = p.parse_args()

    from dataset_io import load_scenarios as load_multi, resolve_dataset_paths

    extra = list(args.extra_dataset or [])
    hard_path = EVAL_DIR / "rca_scenarios_hard.yaml"
    if args.include_hard and hard_path.is_file() and args.dataset is None:
        if hard_path not in extra:
            extra.append(hard_path)
    paths = resolve_dataset_paths(
        args.dataset,
        default_files=[EVAL_DIR / "rca_scenarios.yaml"],
        extra=extra,
    )
    scenarios = load_multi(paths, split="all")
    print(f"=== RCA baselines n={len(scenarios)} files={len(paths)} ===")

    sys_pred = system_predictions(scenarios)
    results = {
        "system_rule_based": score_predictions(scenarios, sys_pred),
        "baseline_random": score_predictions(scenarios, baseline_random(scenarios)),
        "baseline_always_error_rate": score_predictions(
            scenarios, baseline_always_error_rate(scenarios)
        ),
        "baseline_empty": score_predictions(scenarios, baseline_empty(scenarios)),
        "baseline_ticket_service": score_predictions(
            scenarios, baseline_ticket_service(scenarios)
        ),
        "baseline_highest_error_neighbor": score_predictions(
            scenarios, baseline_highest_error_neighbor(scenarios)
        ),
        "baseline_last_deploy": score_predictions(
            scenarios, baseline_last_deploy(scenarios)
        ),
        "baseline_log_phrase_bag": score_predictions(
            scenarios, baseline_log_phrase_bag(scenarios)
        ),
    }

    weak_keys = (
        "baseline_random",
        "baseline_always_error_rate",
        "baseline_empty",
    )
    strong_keys = (
        "baseline_ticket_service",
        "baseline_highest_error_neighbor",
        "baseline_last_deploy",
        "baseline_log_phrase_bag",
    )

    for name, agg in results.items():
        tag = "WEAK" if name in weak_keys else ("STRONG" if name in strong_keys else "SYS")
        print(
            f"  [{tag:6s}] {name:32s} acc={agg['accuracy']:.1%}  "
            f"strict={agg['accuracy_strict']:.1%}  "
            f"jac={agg['mean_jaccard']:.3f}"
        )

    system_acc = results["system_rule_based"]["accuracy"]
    best_weak = max(results[k]["accuracy"] for k in weak_keys)
    best_strong = max(results[k]["accuracy"] for k in strong_keys)
    beats_weak = system_acc > best_weak
    beats_strong = system_acc > best_strong
    print(
        f"\nSystem beats weak baselines?   {beats_weak} "
        f"({system_acc:.1%} > {best_weak:.1%})"
    )
    print(
        f"System beats strong baselines? {beats_strong} "
        f"({system_acc:.1%} > {best_strong:.1%})"
    )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": [str(x) for x in paths],
        "results": results,
        "system_beats_baselines": beats_weak,
        "system_beats_strong_baselines": beats_strong,
        "best_baseline_accuracy": best_weak,
        "best_strong_baseline_accuracy": best_strong,
        "honesty": (
            "Weak baselines are trivial. Strong baselines mimic SRE heuristics. "
            "System should beat weak; beating strong is desirable but not always true on OOD."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {args.output}")

    if args.require_beats_baselines and not beats_weak:
        print("FAIL: system did not beat weak baselines", file=sys.stderr)
        return 1
    if args.require_beats_strong and not beats_strong:
        print("FAIL: system did not beat strong SRE baselines", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
