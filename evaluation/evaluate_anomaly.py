#!/usr/bin/env python3
"""
Evaluate Hybrid Anomaly Detector against labeled mini dataset.

Each scenario streams values into HybridDetector.force_score / evaluate path
and labels the *final* observation as anomaly vs normal.

Metrics
-------
  TP: predicted anomaly AND label=anomaly
  FP: predicted anomaly AND label=normal
  FN: predicted normal  AND label=anomaly
  TN: predicted normal  AND label=normal
  Precision / Recall / F1 / Accuracy

Why offline series?
-------------------
Live Prometheus is non-deterministic for CI. Fixed series make precision/recall
reproducible when tuning zscore_threshold or contamination.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "aiops-services" / "anomaly-detector"))
sys.path.insert(0, str(EVAL_DIR))

from scoring import BinaryCounts, format_table  # noqa: E402

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        if yaml is None:
            raise SystemExit("PyYAML required: pip install pyyaml")
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return list(data.get("scenarios") or [])


def evaluate(scenarios: list[dict[str, Any]]) -> tuple[BinaryCounts, list[dict]]:
    from app.detector import HybridDetector

    counts = BinaryCounts()
    rows: list[dict] = []

    for sc in scenarios:
        det = HybridDetector()  # fresh state per scenario
        service = sc.get("service") or "checkout-service"
        metric = sc.get("metric_name") or "http_error_rate"
        values = [float(v) for v in (sc.get("values") or [])]
        thr = float(sc.get("absolute_threshold") or 0.15)
        label = (sc.get("label") or "normal").lower()
        is_true_anomaly = label == "anomaly"

        pred_anomaly = False
        last_score = 0.0
        last_methods: list[str] = []
        for i, v in enumerate(values):
            # Univariate path + threshold via force_score on last points
            if i < len(values) - 1:
                det._score_univariate(service, metric, v)
            else:
                result = det.force_score(service, metric, v, thr)
                pred_anomaly = bool(result.is_anomaly)
                last_score = float(result.anomaly_score)
                last_methods = list(result.winning_methods)

        if is_true_anomaly and pred_anomaly:
            counts.tp += 1
            outcome = "TP"
        elif not is_true_anomaly and pred_anomaly:
            counts.fp += 1
            outcome = "FP"
        elif is_true_anomaly and not pred_anomaly:
            counts.fn += 1
            outcome = "FN"
        else:
            counts.tn += 1
            outcome = "TN"

        rows.append(
            {
                "scenario_id": sc.get("scenario_id"),
                "label": label,
                "predicted_anomaly": pred_anomaly,
                "outcome": outcome,
                "score": round(last_score, 4),
                "methods": last_methods,
                "last_value": values[-1] if values else None,
            }
        )
        print(
            f"  [{sc.get('scenario_id')}] {outcome} label={label} "
            f"pred={pred_anomaly} score={last_score:.2f} methods={last_methods}"
        )

    return counts, rows


def main() -> int:
    p = argparse.ArgumentParser(description="Evaluate anomaly detector")
    p.add_argument(
        "--dataset",
        type=Path,
        default=EVAL_DIR / "anomaly_scenarios.yaml",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=EVAL_DIR / "results" / "anomaly_latest.json",
    )
    args = p.parse_args()

    scenarios = load_scenarios(args.dataset)
    print(f"=== Anomaly Detection Evaluation n={len(scenarios)} ===")
    counts, rows = evaluate(scenarios)

    table = format_table(
        ["scenario_id", "label", "pred", "out", "score"],
        [
            [
                str(r["scenario_id"]),
                str(r["label"]),
                "Y" if r["predicted_anomaly"] else "N",
                r["outcome"],
                f"{r['score']:.2f}",
            ]
            for r in rows
        ],
    )
    print()
    print(table)
    print()
    print("--- Aggregate ---")
    print(f"Precision: {counts.precision():.1%}")
    print(f"Recall:    {counts.recall():.1%}")
    print(f"F1:        {counts.f1():.1%}")
    print(f"Accuracy:  {counts.accuracy():.1%}")
    print(f"TP={counts.tp} FP={counts.fp} TN={counts.tn} FN={counts.fn}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(args.dataset),
        "aggregate": {
            "precision": round(counts.precision(), 4),
            "recall": round(counts.recall(), 4),
            "f1": round(counts.f1(), 4),
            "accuracy": round(counts.accuracy(), 4),
            "tp": counts.tp,
            "fp": counts.fp,
            "tn": counts.tn,
            "fn": counts.fn,
        },
        "rows": rows,
        "table": table,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
