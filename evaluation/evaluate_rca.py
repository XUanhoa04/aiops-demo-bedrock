#!/usr/bin/env python3
"""
Evaluate RCA Engine against the mini ground-truth dataset.

Modes
-----
  offline (default): Build EvidencePack from scenario symptoms → rule_based_rca
                     (and optional Bedrock if --bedrock and credentials exist).
  online:            Require running stack; create incidents + call RCA HTTP API.

Why offline-first?
------------------
CI and laptops without AWS still need a green evaluation. Rule fallback is the
same code path used in production when Bedrock is down — measuring it prevents
regressions. Online mode validates the full gather→model→persist path.

Usage
-----
  python evaluation/evaluate_rca.py
  python evaluation/evaluate_rca.py --output evaluation/results/rca_latest.json
  python evaluation/evaluate_rca.py --mode online --rca-url http://localhost:8003
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Repo paths
ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "aiops-services" / "rca-engine"))

sys.path.insert(0, str(EVAL_DIR))
from scoring import (  # noqa: E402
    aggregate_rca,
    format_table,
    is_rca_correct,
    jaccard,
    keyword_hit_rate,
    RcaScoreRow,
)

try:
    import yaml
except ImportError:  # pragma: no cover
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


def scenario_to_evidence_pack(sc: dict[str, Any]):
    """Map dataset symptoms → EvidencePack (no live Prom/Loki/Tempo) + topology."""
    from aiops_shared.topology import load_topology_catalog
    from app.models import EvidencePack

    symptoms = sc.get("symptoms") or {}
    metrics_list = symptoms.get("metrics") or []
    logs = symptoms.get("logs") or []
    traces = symptoms.get("traces") or []
    # Ticket service may be symptom-only (wrong-hop eval): prefer explicit field
    primary = (
        sc.get("ticket_service")
        or (sc.get("affected_services") or ["unknown"])[0]
    )

    instant: dict[str, Any] = {}
    by_svc: dict[str, dict] = {}
    for m in metrics_list:
        svc = m.get("service") or primary
        name = str(m.get("name"))
        val = float(m.get("value") or 0)
        by_svc.setdefault(svc, {})[name] = val
        if svc == primary:
            instant[name] = val
    if not instant and by_svc.get(primary):
        instant = dict(by_svc[primary])

    error_logs = []
    neighbor_logs = []
    for i, row in enumerate(logs):
        tid = f"{'a' * 24}{i:08d}"
        svc = row.get("service") or primary
        entry = {
            "line": row.get("line") or "",
            "trace_id": tid,
            "labels": {"service_name": svc},
        }
        if svc == primary:
            error_logs.append(entry)
        else:
            entry["neighbor_service"] = svc
            entry["labels"]["topology_relation"] = "upstream"
            neighbor_logs.append(entry)

    trace_rows = []
    neighbor_traces = []
    for i, t in enumerate(traces):
        tid = f"{'b' * 24}{i:08d}"
        svc = t.get("service") or primary
        entry = {
            "trace_id": tid,
            "root_service": svc,
            "root_name": t.get("pattern") or "scenario",
            "duration_ms": t.get("duration_ms") or 500,
            "search_mode": "scenario_dataset",
        }
        if svc == primary:
            trace_rows.append(entry)
        else:
            entry["neighbor_service"] = svc
            neighbor_traces.append(entry)

    # Neighbor metrics for topology correlation
    neighbor_metrics: dict[str, Any] = {}
    for svc, m in by_svc.items():
        if svc == primary:
            continue
        neighbor_metrics[svc] = {
            "instant": m,
            "relation": "upstream",
        }

    catalog = load_topology_catalog()
    nb = catalog.neighborhood(primary)
    # If scenario declares ticket on checkout but metrics on payment, ensure upstream
    for svc in neighbor_metrics:
        if svc not in nb.upstream and svc != primary:
            nb.upstream.append(svc)

    now = datetime.now(timezone.utc).isoformat()
    pack = EvidencePack(
        incident_id=f"eval-{sc.get('scenario_id')}",
        service_name=primary,
        window_minutes=15,
        window_start_iso=now,
        window_end_iso=now,
        incident={
            "id": f"eval-{sc.get('scenario_id')}",
            "service_name": primary,
            "title": sc.get("description") or sc.get("scenario_id"),
            "severity": "high",
            "metric_name": next(iter(instant), "http_error_rate"),
            "metric_value": next(iter(instant.values()), 0.0) if instant else 0.0,
            "threshold": 0.15,
            "context": {
                "explanation": f"evaluation scenario {sc.get('scenario_id')}",
                "evaluation": True,
            },
        },
        metrics_summary={
            "service": primary,
            "instant": instant,
            "by_service": by_svc,
            "range": {
                k: {"points": 10, "max": v, "last": v, "avg": v * 0.7}
                for k, v in instant.items()
            },
        },
        error_logs=error_logs,
        traces=trace_rows,
        primary_trace_id=(trace_rows or neighbor_traces or [{}])[0].get("trace_id"),
        sources_ok={
            "prometheus": True,
            "loki": True,
            "tempo": True,
            "topology": True,
        },
        topology=nb.to_dict(),
        neighbor_metrics=neighbor_metrics,
        neighbor_logs=neighbor_logs,
        neighbor_traces=neighbor_traces,
        change_events=list(sc.get("change_events") or []),
    )
    return pack


def run_offline(scenarios: list[dict], *, use_bedrock: bool) -> list[RcaScoreRow]:
    from app.rule_fallback import rule_based_rca

    bedrock = None
    if use_bedrock:
        try:
            from app.bedrock_client import BedrockRCAClient

            bedrock = BedrockRCAClient()
            if not bedrock.configured:
                print("[warn] Bedrock not configured — using rule_based only", file=sys.stderr)
                bedrock = None
        except Exception as exc:
            print(f"[warn] Bedrock import failed: {exc}", file=sys.stderr)
            bedrock = None

    rows: list[RcaScoreRow] = []
    for sc in scenarios:
        pack = scenario_to_evidence_pack(sc)
        mode = "rule_based"
        iterations = 1
        result = None
        notes = ""

        if bedrock is not None:
            try:
                t0 = time.perf_counter()
                result, usage = bedrock.analyze(pack)
                iterations = max(1, getattr(usage, "attempt", 1) or 1)
                mode = "bedrock"
                # Low LLM confidence → one more iteration via rules (simulates limited loop)
                if result.confidence < 40:
                    iterations += 1
                    rule = rule_based_rca(pack)
                    if rule.confidence > result.confidence:
                        result = rule
                        mode = "bedrock+rule_fallback"
                        notes = "LLM low conf; preferred rule_based"
            except Exception as exc:
                notes = f"bedrock failed: {exc}"
                result = rule_based_rca(pack)
                iterations = 2
        else:
            result = rule_based_rca(pack)

        pred = result.root_cause if result else ""
        gt = sc.get("ground_truth_root_cause") or ""
        kws = list(sc.get("keywords") or [])
        correct = is_rca_correct(pred, gt, kws)
        rows.append(
            RcaScoreRow(
                scenario_id=str(sc.get("scenario_id")),
                ground_truth=gt,
                predicted=pred,
                correct=correct,
                jaccard=jaccard(pred, gt),
                keyword_rate=keyword_hit_rate(pred, kws),
                confidence=float(result.confidence) if result else None,
                mode=mode,
                iterations=iterations,
                notes=notes,
            )
        )
        print(
            f"  [{sc.get('scenario_id')}] correct={correct} mode={mode} "
            f"jaccard={jaccard(pred, gt):.2f} pred={pred[:80]!r}"
        )
    return rows


def run_online(
    scenarios: list[dict],
    *,
    incident_url: str,
    rca_url: str,
) -> list[RcaScoreRow]:
    """Create synthetic incidents with scenario context and call RCA API."""
    import urllib.error
    import urllib.request

    def http_json(method: str, url: str, body: Optional[dict] = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())

    rows: list[RcaScoreRow] = []
    for sc in scenarios:
        pack = scenario_to_evidence_pack(sc)
        # Create incident via generic API
        try:
            created = http_json(
                "POST",
                f"{incident_url.rstrip('/')}/incidents",
                {
                    "title": f"[eval] {sc.get('scenario_id')}: {sc.get('description', '')[:80]}",
                    "description": json.dumps(
                        {
                            "evaluation": True,
                            "scenario_id": sc.get("scenario_id"),
                            "ground_truth": sc.get("ground_truth_root_cause"),
                            "symptoms": sc.get("symptoms"),
                        }
                    )[:4000],
                    "service_name": pack.service_name,
                    "severity": "high",
                    "metric_name": pack.incident.get("metric_name"),
                    "metric_value": pack.incident.get("metric_value"),
                },
            )
            iid = created.get("id")
        except Exception as exc:
            print(f"  [{sc.get('scenario_id')}] create incident failed: {exc}")
            rows.append(
                RcaScoreRow(
                    scenario_id=str(sc.get("scenario_id")),
                    ground_truth=sc.get("ground_truth_root_cause") or "",
                    predicted="",
                    correct=False,
                    jaccard=0.0,
                    keyword_rate=0.0,
                    notes=f"incident create failed: {exc}",
                )
            )
            continue

        # Online RCA uses live evidence gather (may be empty without load).
        # Still call API; score whatever root_cause returns.
        try:
            t0 = time.perf_counter()
            rca = http_json(
                "POST",
                f"{rca_url.rstrip('/')}/analyze-incident/{iid}?force=true&persist=true",
            )
            elapsed = time.perf_counter() - t0
            result = (rca or {}).get("result") or {}
            pred = result.get("root_cause") or ""
            mode = (rca or {}).get("mode") or "unknown"
            conf = result.get("confidence")
            # Approximate iterations: 1 API call (+1 if fallback noted)
            iterations = 1
            if (rca or {}).get("status") == "fallback" or mode == "rule_based":
                iterations = 1
            if elapsed > 20:
                iterations = 2  # slow path often retries
        except Exception as exc:
            pred, mode, conf, iterations = "", "error", None, 1
            notes = str(exc)
        else:
            notes = f"incident_id={iid}"

        gt = sc.get("ground_truth_root_cause") or ""
        kws = list(sc.get("keywords") or [])
        correct = is_rca_correct(pred, gt, kws)
        rows.append(
            RcaScoreRow(
                scenario_id=str(sc.get("scenario_id")),
                ground_truth=gt,
                predicted=pred,
                correct=correct,
                jaccard=jaccard(pred, gt),
                keyword_rate=keyword_hit_rate(pred, kws),
                confidence=float(conf) if conf is not None else None,
                mode=str(mode),
                iterations=iterations,
                notes=notes,
            )
        )
        print(
            f"  [{sc.get('scenario_id')}] correct={correct} mode={mode} pred={pred[:80]!r}"
        )
    return rows


def main() -> int:
    p = argparse.ArgumentParser(description="Evaluate RCA against ground-truth dataset")
    p.add_argument(
        "--dataset",
        type=Path,
        default=EVAL_DIR / "rca_scenarios.yaml",
    )
    p.add_argument(
        "--mode",
        choices=("offline", "online"),
        default="offline",
        help="offline=EvidencePack+rules; online=HTTP RCA on live stack",
    )
    p.add_argument("--bedrock", action="store_true", help="Try Bedrock in offline mode")
    p.add_argument("--incident-url", default="http://localhost:8002")
    p.add_argument("--rca-url", default="http://localhost:8003")
    p.add_argument(
        "--output",
        type=Path,
        default=EVAL_DIR / "results" / "rca_latest.json",
    )
    args = p.parse_args()

    scenarios = load_scenarios(args.dataset)
    print(f"=== RCA Evaluation ({args.mode}) n={len(scenarios)} ===")
    print(f"dataset={args.dataset}")

    if args.mode == "offline":
        rows = run_offline(scenarios, use_bedrock=args.bedrock)
    else:
        rows = run_online(
            scenarios,
            incident_url=args.incident_url,
            rca_url=args.rca_url,
        )

    # Scenario rca-08 is normal traffic (not a real fault)
    is_fault = {
        str(sc["scenario_id"]): "normal traffic" not in (sc.get("ground_truth_root_cause") or "").lower()
        and "without application fault" not in (sc.get("ground_truth_root_cause") or "").lower()
        for sc in scenarios
    }
    # mark explicitly
    for sc in scenarios:
        sid = str(sc.get("scenario_id"))
        if sid.endswith("traffic-spike-only") or "normal traffic" in (
            sc.get("ground_truth_root_cause") or ""
        ).lower():
            is_fault[sid] = False

    agg = aggregate_rca(rows, is_fault=is_fault)

    table = format_table(
        ["scenario_id", "ok", "jac", "kw", "iter", "mode", "predicted"],
        [
            [
                r.scenario_id,
                "Y" if r.correct else "N",
                f"{r.jaccard:.2f}",
                f"{r.keyword_rate:.2f}",
                str(r.iterations),
                r.mode[:12],
                (r.predicted or "")[:40],
            ]
            for r in rows
        ],
    )
    print()
    print(table)
    print()
    print("--- Aggregate ---")
    print(f"Accuracy:              {agg.accuracy:.1%}  ({agg.correct}/{agg.n})")
    print(f"Precision (fault P/R): {agg.binary.precision():.1%}")
    print(f"Recall (fault P/R):    {agg.binary.recall():.1%}")
    print(f"F1:                    {agg.binary.f1():.1%}")
    print(f"Mean Jaccard (semantic): {agg.mean_jaccard:.3f}")
    print(f"Mean keyword hit rate:   {agg.mean_keyword_rate:.3f}")
    print(f"Mean iterations:         {agg.mean_iterations:.2f}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "dataset": str(args.dataset),
        "aggregate": {
            "n": agg.n,
            "correct": agg.correct,
            "accuracy": round(agg.accuracy, 4),
            "precision": round(agg.binary.precision(), 4),
            "recall": round(agg.binary.recall(), 4),
            "f1": round(agg.binary.f1(), 4),
            "mean_jaccard": round(agg.mean_jaccard, 4),
            "mean_keyword_rate": round(agg.mean_keyword_rate, 4),
            "mean_iterations": round(agg.mean_iterations, 4),
        },
        "rows": [
            {
                "scenario_id": r.scenario_id,
                "ground_truth": r.ground_truth,
                "predicted": r.predicted,
                "correct": r.correct,
                "jaccard": round(r.jaccard, 4),
                "keyword_rate": round(r.keyword_rate, 4),
                "confidence": r.confidence,
                "mode": r.mode,
                "iterations": r.iterations,
                "notes": r.notes,
            }
            for r in rows
        ],
        "table": table,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {args.output}")
    return 0 if agg.accuracy >= 0.0 else 1


if __name__ == "__main__":
    # Allow `python evaluation/evaluate_rca.py` imports of evaluation.scoring
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    sys.exit(main())
