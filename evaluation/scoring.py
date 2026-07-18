"""
Scoring helpers for RCA / anomaly evaluation.

Why custom metrics (not only sklearn)?
--------------------------------------
Ops datasets are tiny (10–20 scenarios). We need transparent, commentable
formulas that interviewers and SREs can audit:

  Accuracy  = correct / N
  Precision = TP / (TP + FP)   where "positive" = system claims a specific fault
  Recall    = TP / (TP + FN)
  F1        = 2PR / (P+R)
  Semantic  = Jaccard token overlap or optional embedding cosine

Token Jaccard is the default "semantic similarity" — zero ML deps, stable.
Optional sentence-transformers/openai embeddings can be plugged later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional


_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)?", re.I)


def tokenize(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) > 1}


def jaccard(a: str, b: str) -> float:
    ta, tb = tokenize(a), tokenize(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def keyword_hit_rate(predicted: str, keywords: Iterable[str]) -> float:
    """Fraction of ground-truth keywords found in prediction (case-insensitive)."""
    keys = [k for k in keywords if k and str(k).strip()]
    if not keys:
        return 1.0 if (predicted or "").strip() else 0.0
    blob = (predicted or "").lower()
    hits = sum(1 for k in keys if str(k).lower() in blob)
    return hits / len(keys)


def is_rca_correct(
    predicted: str,
    ground_truth: str,
    keywords: Optional[list[str]] = None,
    *,
    jaccard_threshold: float = 0.35,
    keyword_threshold: float = 0.5,
) -> bool:
    """
    A prediction is "correct" if:
      - Jaccard(pred, GT) ≥ threshold, OR
      - ≥ keyword_threshold of curated keywords appear in pred
    """
    pred = predicted or ""
    gt = ground_truth or ""
    if jaccard(pred, gt) >= jaccard_threshold:
        return True
    if keywords and keyword_hit_rate(pred, keywords) >= keyword_threshold:
        return True
    # Substring either way (handles "X; Y" multi-cause rule output).
    # NOTE: empty pred must NOT match — in Python `"" in "foo"` is True.
    pred_s = pred.strip()
    gt_s = gt.strip()
    if pred_s and gt_s and (gt_s.lower() in pred_s.lower() or pred_s.lower() in gt_s.lower()):
        return True
    # "No real fault" scenarios: accept insufficient-evidence style outputs
    no_fault = any(
        x in gt.lower()
        for x in ("without application fault", "normal traffic", "insufficient")
    )
    if no_fault and pred_s:
        pl = pred_s.lower()
        if any(
            x in pl
            for x in (
                "insufficient evidence",
                "cannot pin",
                "no error",
                "normal",
                "traffic spike",
            )
        ):
            return True
    return False


@dataclass
class BinaryCounts:
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0

    def precision(self) -> float:
        d = self.tp + self.fp
        return self.tp / d if d else 0.0

    def recall(self) -> float:
        d = self.tp + self.fn
        return self.tp / d if d else 0.0

    def f1(self) -> float:
        p, r = self.precision(), self.recall()
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def accuracy(self) -> float:
        n = self.tp + self.fp + self.tn + self.fn
        return (self.tp + self.tn) / n if n else 0.0


@dataclass
class RcaScoreRow:
    scenario_id: str
    ground_truth: str
    predicted: str
    correct: bool
    jaccard: float
    keyword_rate: float
    confidence: Optional[float] = None
    mode: str = "rule_based"
    iterations: int = 1
    notes: str = ""


@dataclass
class RcaAggregate:
    n: int = 0
    correct: int = 0
    mean_jaccard: float = 0.0
    mean_keyword_rate: float = 0.0
    mean_iterations: float = 0.0
    # Treat "correct RCA" as positive class for P/R when GT is a real fault.
    # Scenarios labeled normal/spike use positive=False for "has fault".
    binary: BinaryCounts = field(default_factory=BinaryCounts)
    rows: list[RcaScoreRow] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.n if self.n else 0.0


def aggregate_rca(
    rows: list[RcaScoreRow],
    *,
    is_fault: dict[str, bool] | None = None,
) -> RcaAggregate:
    """
    is_fault[scenario_id]: True if scenario is a real fault (default True).
    For binary P/R we define:
      predicted_positive = correct match to a fault GT  OR  (if not is_fault) 
      Actually simpler ops definition:
        - For fault scenarios: TP if correct else FN
        - For normal scenarios: TN if "correct" (recognized as normal) else FP
    """
    is_fault = is_fault or {}
    agg = RcaAggregate(n=len(rows), rows=rows)
    if not rows:
        return agg
    agg.correct = sum(1 for r in rows if r.correct)
    agg.mean_jaccard = sum(r.jaccard for r in rows) / len(rows)
    agg.mean_keyword_rate = sum(r.keyword_rate for r in rows) / len(rows)
    agg.mean_iterations = sum(r.iterations for r in rows) / len(rows)

    for r in rows:
        fault = is_fault.get(r.scenario_id, True)
        if fault:
            if r.correct:
                agg.binary.tp += 1
            else:
                agg.binary.fn += 1
        else:
            # Normal / no-fault scenario
            if r.correct:
                agg.binary.tn += 1
            else:
                # Model invented a serious fault → false positive diagnosis
                agg.binary.fp += 1
    return agg


def format_table(headers: list[str], rows: list[list[str]], widths: Optional[list[int]] = None) -> str:
    if not widths:
        widths = [
            max(len(headers[i]), max((len(r[i]) for r in rows), default=0))
            for i in range(len(headers))
        ]
    def fmt(cells: list[str]) -> str:
        return " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    sep = "-+-".join("-" * w for w in widths)
    lines = [fmt(headers), sep]
    for r in rows:
        lines.append(fmt(r))
    return "\n".join(lines)
