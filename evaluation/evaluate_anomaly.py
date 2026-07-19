#!/usr/bin/env python3
"""
Evaluate Hybrid Anomaly Detector against labeled mini/suite dataset.

Modes per scenario
------------------
  univariate (default): stream `values` into HybridDetector.force_score
  multivariate: stream `features_series` via evaluate_service (IsolationForest)

Metrics
-------
  TP/FP/FN/TN on the *final* observation only.
  Report overall + core/holdout when split=all.
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

from dataset_io import (  # noqa: E402
    load_scenarios,
    resolve_dataset_paths,
    split_counts,
)
from scoring import BinaryCounts, format_table  # noqa: E402


def evaluate(scenarios: list[dict[str, Any]]) -> tuple[BinaryCounts, list[dict]]:
    from app.detector import HybridDetector

    counts = BinaryCounts()
    rows: list[dict] = []

    for sc in scenarios:
        det = HybridDetector()
        service = sc.get("service") or "checkout-service"
        label = (sc.get("label") or "normal").lower()
        is_true_anomaly = label == "anomaly"
        mode = (sc.get("mode") or "univariate").lower()
        thr = float(sc.get("absolute_threshold") or 0.15)

        pred_anomaly = False
        last_score = 0.0
        last_methods: list[str] = []
        last_value: Any = None

        if mode == "multivariate" or sc.get("features_series"):
            series = list(sc.get("features_series") or [])
            for i, feat in enumerate(series):
                clean = {
                    k: float(v)
                    for k, v in (feat or {}).items()
                    if v is not None and k.startswith("http_")
                }
                results = det.evaluate_service(service, clean)
                if i == len(series) - 1:
                    pred_anomaly = any(r.is_anomaly for r in results)
                    if results:
                        best = max(results, key=lambda r: r.anomaly_score)
                        last_score = float(best.anomaly_score)
                        last_methods = list(best.winning_methods)
                        # collect IF wins from any result
                        for r in results:
                            for m in r.winning_methods:
                                if m not in last_methods:
                                    last_methods.append(m)
                    last_value = clean
        else:
            metric = sc.get("metric_name") or "http_error_rate"
            values = [float(v) for v in (sc.get("values") or [])]
            for i, v in enumerate(values):
                if i < len(values) - 1:
                    det._score_univariate(service, metric, v)
                else:
                    result = det.force_score(service, metric, v, thr)
                    pred_anomaly = bool(result.is_anomaly)
                    last_score = float(result.anomaly_score)
                    last_methods = list(result.winning_methods)
                    last_value = v

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
                "split": sc.get("split") or "core",
                "mode": mode,
                "label": label,
                "predicted_anomaly": pred_anomaly,
                "outcome": outcome,
                "score": round(last_score, 4),
                "methods": last_methods,
                "last_value": last_value,
            }
        )
        print(
            f"  [{sc.get('scenario_id')}] {outcome} split={sc.get('split')} "
            f"mode={mode} label={label} pred={pred_anomaly} "
            f"score={last_score:.2f} methods={last_methods}"
        )

    return counts, rows


def _counts_from_rows(rows: list[dict]) -> BinaryCounts:
    c = BinaryCounts()
    for r in rows:
        o = r["outcome"]
        if o == "TP":
            c.tp += 1
        elif o == "FP":
            c.fp += 1
        elif o == "FN":
            c.fn += 1
        elif o == "TN":
            c.tn += 1
    return c


def main() -> int:
    p = argparse.ArgumentParser(description="Evaluate anomaly detector")
    p.add_argument(
        "--dataset",
        type=Path,
        default=None,
    )
    p.add_argument(
        "--split",
        choices=("all", "core", "holdout", "hard"),
        default="all",
    )
    p.add_argument(
        "--extra-dataset",
        type=Path,
        action="append",
        default=None,
        help="Extra YAML (hard suite auto-loaded when present)",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=EVAL_DIR / "results" / "anomaly_latest.json",
    )
    args = p.parse_args()

    extra = list(args.extra_dataset or [])
    hard_path = EVAL_DIR / "anomaly_scenarios_hard.yaml"
    if hard_path.is_file() and args.dataset is None and hard_path not in extra:
        extra.append(hard_path)
    paths = resolve_dataset_paths(
        args.dataset,
        default_files=[EVAL_DIR / "anomaly_scenarios.yaml"],
        extra=extra,
    )
    scenarios = load_scenarios(paths, split=args.split)
    splits = split_counts(scenarios)
    print(
        f"=== Anomaly Detection Evaluation n={len(scenarios)} "
        f"split={args.split} core={splits.get('core', 0)} "
        f"holdout={splits.get('holdout', 0)} hard={splits.get('hard', 0)} ==="
    )
    counts, rows = evaluate(scenarios)

    by_split: dict[str, Any] = {}
    if args.split == "all":
        for name in ("core", "holdout", "hard"):
            sub = [r for r in rows if r.get("split") == name]
            if not sub:
                continue
            sc = _counts_from_rows(sub)
            by_split[name] = {
                "n": len(sub),
                "precision": round(sc.precision(), 4),
                "recall": round(sc.recall(), 4),
                "f1": round(sc.f1(), 4),
                "accuracy": round(sc.accuracy(), 4),
                "tp": sc.tp,
                "fp": sc.fp,
                "tn": sc.tn,
                "fn": sc.fn,
            }

    table = format_table(
        ["scenario_id", "split", "label", "pred", "out", "score"],
        [
            [
                str(r["scenario_id"]),
                str(r.get("split") or "core"),
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
    for name, sa in by_split.items():
        print(
            f"  [{name}] n={sa['n']} F1={sa['f1']:.1%} "
            f"P={sa['precision']:.1%} R={sa['recall']:.1%}"
        )

    # L0 = core+holdout (catalog regression); hard is reported separately
    l0_rows = [r for r in rows if r.get("split") in {"core", "holdout"}]
    l0 = _counts_from_rows(l0_rows) if l0_rows else counts
    hard_rows = [r for r in rows if r.get("split") == "hard"]
    hard_c = _counts_from_rows(hard_rows) if hard_rows else None

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": [str(x) for x in paths],
        "split_filter": args.split,
        "split_counts": splits,
        "honesty": (
            "Core/holdout = clean synthetic (L0). Hard = stats-only / noisy "
            "(absolute_threshold disabled via huge thr). Overall mixes both."
        ),
        "aggregate": {
            "n": len(rows),
            "precision": round(counts.precision(), 4),
            "recall": round(counts.recall(), 4),
            "f1": round(counts.f1(), 4),
            "accuracy": round(counts.accuracy(), 4),
            "tp": counts.tp,
            "fp": counts.fp,
            "tn": counts.tn,
            "fn": counts.fn,
        },
        "aggregate_l0": {
            "n": len(l0_rows),
            "precision": round(l0.precision(), 4),
            "recall": round(l0.recall(), 4),
            "f1": round(l0.f1(), 4),
            "accuracy": round(l0.accuracy(), 4),
            "tp": l0.tp,
            "fp": l0.fp,
            "tn": l0.tn,
            "fn": l0.fn,
        },
        "aggregate_hard": (
            {
                "n": len(hard_rows),
                "precision": round(hard_c.precision(), 4),
                "recall": round(hard_c.recall(), 4),
                "f1": round(hard_c.f1(), 4),
                "accuracy": round(hard_c.accuracy(), 4),
                "tp": hard_c.tp,
                "fp": hard_c.fp,
                "tn": hard_c.tn,
                "fn": hard_c.fn,
            }
            if hard_c is not None
            else None
        ),
        "by_split": by_split,
        "rows": rows,
        "table": table,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
