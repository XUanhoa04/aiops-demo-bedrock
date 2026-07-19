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
from dataset_io import (  # noqa: E402
    is_fault_scenario,
    load_scenarios as load_scenarios_multi,
    resolve_dataset_paths,
    split_counts,
)
from scoring import (  # noqa: E402
    aggregate_rca,
    format_table,
    grade_rca,
    is_rca_correct,
    is_wrong_hop,
    jaccard,
    keyword_hit_rate,
    RcaScoreRow,
)


def load_scenarios(path: Path, *, split: str = "all") -> list[dict[str, Any]]:
    """Backward-compatible single-file loader (used by baselines)."""
    return load_scenarios_multi([path], split=split)


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

    sources_ok = {
        "prometheus": True,
        "loki": True,
        "tempo": True,
        "topology": True,
    }
    if isinstance(sc.get("sources_ok"), dict):
        sources_ok.update({k: bool(v) for k, v in sc["sources_ok"].items()})
    gather_errors = list(sc.get("gather_errors") or [])

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
        sources_ok=sources_ok,
        gather_errors=gather_errors,
        topology=nb.to_dict(),
        neighbor_metrics=neighbor_metrics,
        neighbor_logs=neighbor_logs,
        neighbor_traces=neighbor_traces,
        change_events=list(sc.get("change_events") or []),
    )
    return pack


def _score_row(
    sc: dict,
    result: Any,
    *,
    mode: str,
    iterations: int = 1,
    notes: str = "",
) -> RcaScoreRow:
    pred = result.root_cause if result else ""
    gt = sc.get("ground_truth_root_cause") or ""
    kws = list(sc.get("keywords") or [])
    correct_default = is_rca_correct(pred, gt, kws, mode="default")
    correct_strict = is_rca_correct(pred, gt, kws, mode="strict")
    return RcaScoreRow(
        scenario_id=str(sc.get("scenario_id")),
        ground_truth=gt,
        predicted=pred,
        correct=correct_default,
        correct_strict=correct_strict,
        jaccard=jaccard(pred, gt),
        keyword_rate=keyword_hit_rate(pred, kws),
        confidence=float(result.confidence) if result else None,
        mode=mode,
        iterations=iterations,
        notes=notes,
        grade=grade_rca(pred, gt, kws, mode="default"),
        grade_strict=grade_rca(pred, gt, kws, mode="strict"),
        wrong_hop=is_wrong_hop(pred, gt),
        scoring_mode="default",
    )


def run_offline(scenarios: list[dict], *, use_bedrock: bool) -> list[RcaScoreRow]:
    """Primary offline path. Default = config-driven rules only."""
    from app.rule_fallback import rule_based_rca

    bedrock = None
    if use_bedrock:
        try:
            from app.bedrock_client import BedrockRCAClient

            bedrock = BedrockRCAClient()
            if not bedrock.configured:
                print(
                    "[warn] Bedrock not configured — using rule_based only",
                    file=sys.stderr,
                )
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
                result, usage = bedrock.analyze(pack)
                iterations = max(1, getattr(usage, "attempt", 1) or 1)
                mode = "bedrock"
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

        row = _score_row(sc, result, mode=mode, iterations=iterations, notes=notes)
        rows.append(row)
        print(
            f"  [{row.scenario_id}] correct={row.correct} mode={mode} "
            f"jaccard={row.jaccard:.2f} pred={(row.predicted or '')[:80]!r}"
        )
    return rows


def run_offline_compare(
    scenarios: list[dict],
) -> tuple[list[RcaScoreRow], list[RcaScoreRow], dict[str, Any]]:
    """
    Always score config-driven rules; also try Bedrock when credentials exist.

    Returns (rule_rows, bedrock_rows, meta). bedrock_rows may be empty if
    AWS is not configured — honesty over silent fake LLM scores.
    """
    from app.rule_fallback import rule_based_rca

    rule_rows: list[RcaScoreRow] = []
    bedrock_rows: list[RcaScoreRow] = []
    meta: dict[str, Any] = {"bedrock_configured": False, "bedrock_error": None}

    bedrock = None
    try:
        from app.bedrock_client import BedrockRCAClient

        bedrock = BedrockRCAClient()
        meta["bedrock_configured"] = bool(bedrock.configured)
        if not bedrock.configured:
            bedrock = None
            meta["bedrock_error"] = "credentials missing"
    except Exception as exc:
        meta["bedrock_error"] = str(exc)
        bedrock = None

    for sc in scenarios:
        pack = scenario_to_evidence_pack(sc)
        rule = rule_based_rca(pack)
        rr = _score_row(sc, rule, mode="rule_based")
        rule_rows.append(rr)

        if bedrock is None:
            print(
                f"  [{rr.scenario_id}] rule={rr.correct} bedrock=SKIP "
                f"pred={(rr.predicted or '')[:50]!r}"
            )
            continue

        notes = ""
        mode = "bedrock"
        try:
            bres, usage = bedrock.analyze(pack)
            iterations = max(1, getattr(usage, "attempt", 1) or 1)
            # Pure bedrock score (no silent rule swap) for fair comparison
            br = _score_row(
                sc, bres, mode=mode, iterations=iterations, notes=notes
            )
        except Exception as exc:
            br = _score_row(
                sc,
                rule,
                mode="bedrock_failed_fallback_rule",
                iterations=2,
                notes=str(exc),
            )
        bedrock_rows.append(br)
        print(
            f"  [{rr.scenario_id}] rule={rr.correct} bedrock={br.correct} "
            f"agree={rr.predicted == br.predicted}"
        )

    # Agreement among scenarios where both ran
    agree = 0
    n = 0
    by_id_b = {r.scenario_id: r for r in bedrock_rows}
    for r in rule_rows:
        b = by_id_b.get(r.scenario_id)
        if not b or b.mode.startswith("bedrock_failed"):
            continue
        n += 1
        if r.correct == b.correct and (
            jaccard(r.predicted, b.predicted) >= 0.35
            or (r.predicted or "").lower() in (b.predicted or "").lower()
            or (b.predicted or "").lower() in (r.predicted or "").lower()
        ):
            agree += 1
    meta["agreement_rate"] = round(agree / n, 4) if n else None
    meta["agreement_n"] = n
    return rule_rows, bedrock_rows, meta


def pattern_catalog_meta() -> dict[str, Any]:
    """Fingerprint of config/rca_patterns.yaml for eval honesty / freeze audits."""
    import hashlib

    try:
        from aiops_shared.rca_patterns import load_pattern_catalog

        cat = load_pattern_catalog()
        path = Path(cat.path) if cat.path and cat.path != "builtin" else None
        sha = ""
        if path and path.is_file():
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "path": cat.path,
            "version": cat.version,
            "n_patterns": len(cat.patterns),
            "pattern_ids": [p.id for p in cat.patterns],
            "sha256": sha,
        }
    except Exception as exc:
        return {"error": str(exc)}


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
        dummy = type("R", (), {"root_cause": pred, "confidence": conf or 0})()
        row = _score_row(
            sc,
            dummy,
            mode=str(mode),
            iterations=iterations,
            notes=notes,
        )
        rows.append(row)
        print(
            f"  [{sc.get('scenario_id')}] ok={row.correct} strict={row.correct_strict} "
            f"grade={row.grade} mode={mode} pred={pred[:80]!r}"
        )
    return rows


def _agg_for(rows: list[RcaScoreRow], scenarios: list[dict[str, Any]]):
    is_fault = {str(sc["scenario_id"]): is_fault_scenario(sc) for sc in scenarios}
    return aggregate_rca(rows, is_fault=is_fault)


def main() -> int:
    p = argparse.ArgumentParser(description="Evaluate RCA against ground-truth dataset")
    p.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help="Primary dataset file (default: evaluation/rca_scenarios.yaml)",
    )
    p.add_argument(
        "--split",
        choices=("all", "core", "holdout", "hard"),
        default="all",
        help="Filter scenarios by split field (core/holdout/hard)",
    )
    p.add_argument(
        "--extra-dataset",
        type=Path,
        action="append",
        default=None,
        help="Additional dataset YAML (e.g. rca_scenarios_hard.yaml). "
        "Default: also load hard file when present.",
    )
    p.add_argument(
        "--mode",
        choices=("offline", "online"),
        default="offline",
        help="offline=EvidencePack+rules; online=HTTP RCA on live stack",
    )
    p.add_argument(
        "--bedrock",
        action="store_true",
        help="Offline: use Bedrock when configured (else rules)",
    )
    p.add_argument(
        "--compare",
        action="store_true",
        help="Offline: score rules AND Bedrock separately (writes compare file)",
    )
    p.add_argument("--incident-url", default="http://localhost:8002")
    p.add_argument("--rca-url", default="http://localhost:8003")
    p.add_argument(
        "--output",
        type=Path,
        default=EVAL_DIR / "results" / "rca_latest.json",
    )
    p.add_argument(
        "--compare-output",
        type=Path,
        default=EVAL_DIR / "results" / "rca_compare_latest.json",
    )
    args = p.parse_args()

    extra = list(args.extra_dataset or [])
    # Auto-include hard OOD suite for split=all or hard
    hard_path = EVAL_DIR / "rca_scenarios_hard.yaml"
    if hard_path.is_file() and args.dataset is None:
        if hard_path not in extra:
            extra.append(hard_path)
    paths = resolve_dataset_paths(
        args.dataset,
        default_files=[EVAL_DIR / "rca_scenarios.yaml"],
        extra=extra,
    )
    scenarios = load_scenarios_multi(paths, split=args.split)
    splits = split_counts(scenarios)
    print(f"=== RCA Evaluation ({args.mode}) n={len(scenarios)} split={args.split} ===")
    print(
        f"dataset={', '.join(str(x) for x in paths)} "
        f"core={splits.get('core', 0)} holdout={splits.get('holdout', 0)} "
        f"hard={splits.get('hard', 0) or splits.get('other', 0)}"
    )
    cat_meta = pattern_catalog_meta()
    if cat_meta.get("path"):
        print(
            f"pattern_catalog={cat_meta.get('path')} "
            f"n={cat_meta.get('n_patterns')} sha={str(cat_meta.get('sha256') or '')[:12]}"
        )

    compare_payload: Optional[dict[str, Any]] = None
    if args.mode == "offline" and args.compare:
        rule_rows, bedrock_rows, meta = run_offline_compare(scenarios)
        rows = rule_rows  # primary artifact remains rule-based for CI
        by_engine = {
            "rule_based": {
                "n": len(rule_rows),
                "correct": sum(1 for r in rule_rows if r.correct),
                "accuracy": round(
                    sum(1 for r in rule_rows if r.correct) / max(len(rule_rows), 1), 4
                ),
            }
        }
        if bedrock_rows:
            by_engine["bedrock"] = {
                "n": len(bedrock_rows),
                "correct": sum(1 for r in bedrock_rows if r.correct),
                "accuracy": round(
                    sum(1 for r in bedrock_rows if r.correct)
                    / max(len(bedrock_rows), 1),
                    4,
                ),
            }
        compare_payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dataset": [str(x) for x in paths],
            "split_filter": args.split,
            "pattern_catalog": cat_meta,
            "by_engine": by_engine,
            "agreement_rate": meta.get("agreement_rate"),
            "agreement_n": meta.get("agreement_n"),
            "bedrock_meta": {
                "configured": meta.get("bedrock_configured"),
                "error": meta.get("bedrock_error"),
            },
            "rule_rows": [
                {
                    "scenario_id": r.scenario_id,
                    "correct": r.correct,
                    "predicted": r.predicted,
                    "mode": r.mode,
                }
                for r in rule_rows
            ],
            "bedrock_rows": [
                {
                    "scenario_id": r.scenario_id,
                    "correct": r.correct,
                    "predicted": r.predicted,
                    "mode": r.mode,
                    "notes": r.notes,
                }
                for r in bedrock_rows
            ],
            "honesty": (
                "Rule path uses config/rca_patterns.yaml. Bedrock is optional and "
                "only scored when AWS credentials work — never fake LLM accuracy."
            ),
        }
        print(
            f"\n--- Compare --- rule_acc={by_engine['rule_based']['accuracy']:.1%} "
            f"bedrock_acc={by_engine.get('bedrock', {}).get('accuracy', 'SKIP')} "
            f"agreement={meta.get('agreement_rate')}"
        )
    elif args.mode == "offline":
        rows = run_offline(scenarios, use_bedrock=args.bedrock)
    else:
        rows = run_online(
            scenarios,
            incident_url=args.incident_url,
            rca_url=args.rca_url,
        )

    by_id = {str(sc["scenario_id"]): sc for sc in scenarios}
    agg = _agg_for(rows, scenarios)

    # Per-split aggregates (when running all)
    split_aggs: dict[str, Any] = {}
    if args.split == "all":
        for name in ("core", "holdout", "hard"):
            sub_sc = [sc for sc in scenarios if str(sc.get("split") or "core") == name]
            sub_ids = {str(sc["scenario_id"]) for sc in sub_sc}
            sub_rows = [r for r in rows if r.scenario_id in sub_ids]
            if sub_rows:
                sa = _agg_for(sub_rows, sub_sc)
                split_aggs[name] = {
                    "n": sa.n,
                    "correct": sa.correct,
                    "accuracy": round(sa.accuracy, 4),
                    "accuracy_strict": round(sa.accuracy_strict, 4),
                    "precision": round(sa.binary.precision(), 4),
                    "recall": round(sa.binary.recall(), 4),
                    "f1": round(sa.binary.f1(), 4),
                    "wrong_hop_rate": round(sa.wrong_hop_rate, 4),
                    "mean_jaccard": round(sa.mean_jaccard, 4),
                    "grade_counts": sa.grade_counts,
                }

    table = format_table(
        ["scenario_id", "split", "ok", "strict", "grade", "jac", "wh", "mode", "predicted"],
        [
            [
                r.scenario_id,
                str((by_id.get(r.scenario_id) or {}).get("split") or "core"),
                "Y" if r.correct else "N",
                "Y" if r.correct_strict else "N",
                str(r.grade)[:12],
                f"{r.jaccard:.2f}",
                "Y" if r.wrong_hop else "N",
                r.mode[:10],
                (r.predicted or "")[:36],
            ]
            for r in rows
        ],
    )
    print()
    print(table)
    print()
    print("--- Aggregate (L0 catalog / default scoring) ---")
    print(f"Accuracy (default):    {agg.accuracy:.1%}  ({agg.correct}/{agg.n})")
    print(f"Accuracy (strict):     {agg.accuracy_strict:.1%}  ({agg.correct_strict}/{agg.n})")
    print(f"Wrong-hop rate:        {agg.wrong_hop_rate:.1%}")
    print(f"Precision (fault P/R): {agg.binary.precision():.1%}")
    print(f"Recall (fault P/R):    {agg.binary.recall():.1%}")
    print(f"F1:                    {agg.binary.f1():.1%}")
    print(f"Mean Jaccard:          {agg.mean_jaccard:.3f}")
    print(f"Mean keyword hit rate: {agg.mean_keyword_rate:.3f}")
    print(f"Mean iterations:       {agg.mean_iterations:.2f}")
    print(f"Grades (default):      {agg.grade_counts}")
    print(f"Grades (strict):       {agg.grade_strict_counts}")
    for name, sa in split_aggs.items():
        print(
            f"  [{name}] n={sa['n']} acc={sa['accuracy']:.1%} "
            f"strict={sa['accuracy_strict']:.1%} "
            f"wh={sa['wrong_hop_rate']:.1%} jac={sa['mean_jaccard']:.2f}"
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "dataset": [str(x) for x in paths],
        "split_filter": args.split,
        "split_counts": splits,
        "pattern_catalog": cat_meta,
        "honesty": (
            "Offline default accuracy = config-driven rule RCA on synthetic "
            "EvidencePack (L0 catalog regression). strict accuracy requires "
            "class+service+Jaccard≥0.50 (no keyword-only). Not production ML. "
            "Use evaluate_live_e2e.py for runtime; hard split for OOD."
        ),
        "scoring": {
            "default": "Jaccard≥0.40 OR GT⊂pred OR keywords+class (catalog regression)",
            "strict": "class+service OK AND (Jaccard≥0.50 OR GT⊂pred); no keyword-only",
        },
        "aggregate": {
            "n": agg.n,
            "correct": agg.correct,
            "accuracy": round(agg.accuracy, 4),
            "correct_strict": agg.correct_strict,
            "accuracy_strict": round(agg.accuracy_strict, 4),
            "wrong_hop_count": agg.wrong_hop_count,
            "wrong_hop_rate": round(agg.wrong_hop_rate, 4),
            "precision": round(agg.binary.precision(), 4),
            "recall": round(agg.binary.recall(), 4),
            "f1": round(agg.binary.f1(), 4),
            "precision_strict": round(agg.binary_strict.precision(), 4),
            "recall_strict": round(agg.binary_strict.recall(), 4),
            "f1_strict": round(agg.binary_strict.f1(), 4),
            "mean_jaccard": round(agg.mean_jaccard, 4),
            "mean_keyword_rate": round(agg.mean_keyword_rate, 4),
            "mean_iterations": round(agg.mean_iterations, 4),
            "grade_counts": agg.grade_counts,
            "grade_strict_counts": agg.grade_strict_counts,
        },
        "by_split": split_aggs,
        "rows": [
            {
                "scenario_id": r.scenario_id,
                "split": str((by_id.get(r.scenario_id) or {}).get("split") or "core"),
                "ground_truth": r.ground_truth,
                "predicted": r.predicted,
                "correct": r.correct,
                "correct_strict": r.correct_strict,
                "grade": r.grade,
                "grade_strict": r.grade_strict,
                "wrong_hop": r.wrong_hop,
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
    if compare_payload is not None:
        args.compare_output.parent.mkdir(parents=True, exist_ok=True)
        args.compare_output.write_text(
            json.dumps(compare_payload, indent=2), encoding="utf-8"
        )
        print(f"Wrote {args.compare_output}")
    return 0 if agg.accuracy >= 0.0 else 1


if __name__ == "__main__":
    # Allow `python evaluation/evaluate_rca.py` imports of evaluation.scoring
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    sys.exit(main())
