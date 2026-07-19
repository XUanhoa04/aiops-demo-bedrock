#!/usr/bin/env python3
"""Print a compact multi-suite evaluation summary from results/*.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"


def load(name: str) -> dict:
    p = RESULTS / name
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> int:
    print("=" * 64)
    print(" SentinelLoop evaluation summary (honest layers)")
    print("=" * 64)

    anom = load("anomaly_latest.json")
    rca = load("rca_latest.json")
    base = load("baselines_latest.json")
    live = load("rca_live_e2e_latest.json")
    compare = load("rca_compare_latest.json")

    if anom:
        a = anom.get("aggregate") or {}
        l0 = anom.get("aggregate_l0") or {}
        hard = anom.get("aggregate_hard") or {}
        print("\n[Anomaly]")
        print(
            f"  overall  n={a.get('n')} F1={a.get('f1')} "
            f"P={a.get('precision')} R={a.get('recall')}"
        )
        if l0:
            print(
                f"  L0 clean n={l0.get('n')} F1={l0.get('f1')} "
                f"P={l0.get('precision')} R={l0.get('recall')}"
            )
        if hard:
            print(
                f"  L1 hard  n={hard.get('n')} F1={hard.get('f1')} "
                f"P={hard.get('precision')} R={hard.get('recall')}"
            )
        for split, sa in (anom.get("by_split") or {}).items():
            print(
                f"  [{split}] n={sa.get('n')} F1={sa.get('f1')} "
                f"P={sa.get('precision')} R={sa.get('recall')}"
            )

    if rca:
        a = rca.get("aggregate") or {}
        print("\n[RCA offline]")
        print(
            f"  default acc={a.get('accuracy')}  strict acc={a.get('accuracy_strict')}  "
            f"wrong_hop={a.get('wrong_hop_rate')}  n={a.get('n')}"
        )
        print(
            f"  mean_jaccard={a.get('mean_jaccard')}  "
            f"grades={a.get('grade_counts')}"
        )
        for split, sa in (rca.get("by_split") or {}).items():
            print(
                f"  [{split}] n={sa.get('n')} acc={sa.get('accuracy')} "
                f"strict={sa.get('accuracy_strict')} wh={sa.get('wrong_hop_rate')}"
            )
        cat = rca.get("pattern_catalog") or {}
        if cat:
            print(
                f"  pattern_catalog: n={cat.get('n_patterns')} "
                f"sha={str(cat.get('sha256') or '')[:12]}"
            )
        if rca.get("honesty"):
            print(f"  note: {rca['honesty'][:120]}…")

    if compare:
        print("\n[RCA rule vs bedrock compare]")
        for k, v in (compare.get("by_engine") or {}).items():
            print(f"  {k}: acc={v.get('accuracy')} n={v.get('n')}")
        print(f"  agreement: {compare.get('agreement_rate')}")

    if base:
        print("\n[Baselines]")
        for k, v in (base.get("results") or {}).items():
            print(
                f"  {k}: acc={v.get('accuracy')} strict={v.get('accuracy_strict')}"
            )
        print(f"  beats_weak:   {base.get('system_beats_baselines')}")
        print(f"  beats_strong: {base.get('system_beats_strong_baselines')}")

    if live:
        a = live.get("aggregate") or {}
        print("\n[RCA live e2e]")
        print(
            f"  n={a.get('n')} acc={a.get('accuracy')} strict={a.get('accuracy_strict')} "
            f"completeness={a.get('mean_evidence_completeness')} "
            f"seed={live.get('seed_context')}"
        )
        if live.get("note"):
            print(f"  note: {live['note'][:140]}")

    print("\n" + "=" * 64)
    print(" Layers: L0 = catalog/clean synthetic · L1 hard/OOD · L2 live e2e")
    print(" CV tip: report strict + live + hard; not only offline default 100%.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
