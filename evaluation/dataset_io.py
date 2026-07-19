"""Load evaluation scenario YAML files (single or multi-file, core/holdout)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore


def load_yaml_or_json(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        if yaml is None:
            raise SystemExit("PyYAML required: pip install pyyaml")
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise SystemExit(f"dataset root must be a mapping: {path}")
    return data


def load_scenario_file(path: Path) -> list[dict[str, Any]]:
    data = load_yaml_or_json(path)
    scenarios = list(data.get("scenarios") or [])
    for sc in scenarios:
        sc.setdefault("_source_file", str(path.name))
        # Default split for older scenarios without the field
        sc.setdefault("split", "core")
    return scenarios


def load_scenarios(
    paths: Iterable[Path],
    *,
    split: str = "all",
) -> list[dict[str, Any]]:
    """
    Load one or more dataset files and optionally filter by split.

    split: all | core | holdout
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        path = Path(path)
        if not path.is_file():
            raise SystemExit(f"dataset not found: {path}")
        for sc in load_scenario_file(path):
            sid = str(sc.get("scenario_id") or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            sc_split = str(sc.get("split") or "core").lower()
            if split != "all" and sc_split != split.lower():
                continue
            out.append(sc)
    return out


def resolve_dataset_paths(
    dataset: Optional[Path],
    *,
    default_files: list[Path],
    extra: Optional[list[Path]] = None,
) -> list[Path]:
    """
    If --dataset points at a file, use it (plus optional --extra-dataset).
    If None, load default_files that exist.
    """
    paths: list[Path] = []
    if dataset is not None:
        paths.append(Path(dataset))
    else:
        for p in default_files:
            if Path(p).is_file():
                paths.append(Path(p))
    for p in extra or []:
        if p and Path(p).is_file():
            paths.append(Path(p))
    # de-dupe preserving order
    uniq: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        key = str(p.resolve()) if p.exists() else str(p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    if not uniq:
        raise SystemExit("no dataset files found")
    return uniq


def is_fault_scenario(sc: dict[str, Any]) -> bool:
    """Whether the scenario is a real application fault (for P/R)."""
    if "is_fault" in sc:
        return bool(sc["is_fault"])
    gt = (sc.get("ground_truth_root_cause") or "").lower()
    if any(
        x in gt
        for x in (
            "without application fault",
            "normal traffic",
            "insufficient evidence",
            "no real fault",
            "monitor only",
        )
    ):
        return False
    return True


def split_counts(scenarios: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"core": 0, "holdout": 0, "hard": 0, "other": 0}
    for sc in scenarios:
        s = str(sc.get("split") or "core").lower()
        if s in counts:
            counts[s] += 1
        else:
            counts["other"] += 1
    return counts
