#!/usr/bin/env python3
"""
Compare RCA system against naive baselines.

Why baselines?
--------------
Offline accuracy alone can look inflated if rules share keywords with the
dataset. Baselines prove the system is better than:
  - random root-cause strings
  - always "elevated http_error_rate" (metric-only shallow answer)
  - keyword-free empty string

CI fails if system accuracy does not strictly beat the best baseline.
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

from scoring import is_rca_correct, jaccard, keyword_hit_rate  # noqa: E402

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

# Import evaluate_rca helpers without running main
from evaluate_rca import load_scenarios, run_offline, scenario_to_evidence_pack  # noqa: E402


RANDOM_CAUSES = [
    "kubernetes node disk pressure",
    "DNS resolution failure in mesh",
    "certificate expiry on ingress",
    "bad deploy of inventory-service",
    "message queue backlog on kafka",
]


def score_predictions(
    scenarios: list[dict[str, Any]], predictions: dict[str, str]
) -> dict[str, Any]:
    correct = 0
    jacs = []
    kws = []
    for sc in scenarios:
        sid = str(sc["scenario_id"])
        pred = predictions.get(sid, "")
        gt = sc.get("ground_truth_root_cause") or ""
        keys = list(sc.get("keywords") or [])
        ok = is_rca_correct(pred, gt, keys)
        correct += int(ok)
        jacs.append(jaccard(pred, gt))
        kws.append(keyword_hit_rate(pred, keys))
    n = len(scenarios) or 1
    return {
        "n": len(scenarios),
        "correct": correct,
        "accuracy": round(correct / n, 4),
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


def system_predictions(scenarios: list[dict]) -> dict[str, str]:
    rows = run_offline(scenarios, use_bedrock=False)
    return {r.scenario_id: r.predicted for r in rows}


def main() -> int:
    p = argparse.ArgumentParser(description="RCA baseline comparison")
    p.add_argument("--dataset", type=Path, default=EVAL_DIR / "rca_scenarios.yaml")
    p.add_argument(
        "--output",
        type=Path,
        default=EVAL_DIR / "results" / "baselines_latest.json",
    )
    p.add_argument(
        "--require-beats-baselines",
        action="store_true",
        help="Exit 1 if system accuracy ≤ best baseline accuracy",
    )
    args = p.parse_args()

    scenarios = load_scenarios(args.dataset)
    print(f"=== RCA baselines n={len(scenarios)} ===")

    results = {
        "system_rule_based": score_predictions(scenarios, system_predictions(scenarios)),
        "baseline_random": score_predictions(scenarios, baseline_random(scenarios)),
        "baseline_always_error_rate": score_predictions(
            scenarios, baseline_always_error_rate(scenarios)
        ),
        "baseline_empty": score_predictions(scenarios, baseline_empty(scenarios)),
    }

    for name, agg in results.items():
        print(
            f"  {name:28s} accuracy={agg['accuracy']:.1%}  "
            f"jaccard={agg['mean_jaccard']:.3f}  kw={agg['mean_keyword_rate']:.3f}"
        )

    system_acc = results["system_rule_based"]["accuracy"]
    best_base = max(
        results["baseline_random"]["accuracy"],
        results["baseline_always_error_rate"]["accuracy"],
        results["baseline_empty"]["accuracy"],
    )
    beats = system_acc > best_base
    print(f"\nSystem beats best baseline? {beats} ({system_acc:.1%} > {best_base:.1%})")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(args.dataset),
        "results": results,
        "system_beats_baselines": beats,
        "best_baseline_accuracy": best_base,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {args.output}")

    if args.require_beats_baselines and not beats:
        print("FAIL: system did not beat baselines", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
