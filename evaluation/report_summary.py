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
    print("=" * 60)
    print(" SentinelLoop evaluation summary")
    print("=" * 60)

    anom = load("anomaly_latest.json")
    rca = load("rca_latest.json")
    base = load("baselines_latest.json")
    live = load("rca_live_e2e_latest.json")
    compare = load("rca_compare_latest.json")

    if anom:
        a = anom.get("aggregate") or {}
        print("\n[Anomaly offline]")
        print(
            f"  n={a.get('n')} F1={a.get('f1')} P={a.get('precision')} "
            f"R={a.get('recall')} Acc={a.get('accuracy')}"
        )
        for split, sa in (anom.get("by_split") or {}).items():
            print(
                f"  [{split}] n={sa.get('n')} F1={sa.get('f1')} "
                f"P={sa.get('precision')} R={sa.get('recall')}"
            )

    if rca:
        a = rca.get("aggregate") or {}
        print("\n[RCA offline — primary mode in file]")
        print(
            f"  n={a.get('n')} Acc={a.get('accuracy')} "
            f"P={a.get('precision')} R={a.get('recall')} F1={a.get('f1')}"
        )
        for split, sa in (rca.get("by_split") or {}).items():
            print(f"  [{split}] n={sa.get('n')} Acc={sa.get('accuracy')}")
        cat = rca.get("pattern_catalog") or {}
        if cat:
            print(
                f"  pattern_catalog: {cat.get('path')} "
                f"n_patterns={cat.get('n_patterns')} sha={cat.get('sha256', '')[:12]}"
            )

    if compare:
        print("\n[RCA rule vs bedrock compare]")
        for k, v in (compare.get("by_engine") or {}).items():
            print(f"  {k}: acc={v.get('accuracy')} n={v.get('n')}")
        print(f"  agreement: {compare.get('agreement_rate')}")

    if base:
        print("\n[Baselines]")
        for k, v in (base.get("results") or {}).items():
            print(f"  {k}: acc={v.get('accuracy')}")
        print(f"  system_beats_baselines: {base.get('system_beats_baselines')}")

    if live:
        a = live.get("aggregate") or {}
        print("\n[RCA live e2e]")
        print(
            f"  n={a.get('n')} Acc={a.get('accuracy')} "
            f"correct={a.get('correct')} skipped={a.get('skipped')}"
        )
        print(f"  note: {live.get('note', '')[:100]}")

    print("\n" + "=" * 60)
    print(" Honesty: offline RCA measures config-driven rules on synthetic")
    print(" evidence; live e2e uses real chaos+OTel; Bedrock is optional.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
