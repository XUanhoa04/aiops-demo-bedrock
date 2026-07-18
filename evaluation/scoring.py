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


def extract_service_mentions(text: str) -> set[str]:
    """Canonical service tokens found in free text (for wrong-hop checks)."""
    blob = (text or "").lower()
    found: set[str] = set()
    mapping = {
        "payment-service": ("payment-service", "payment service"),
        "checkout-service": ("checkout-service", "checkout service"),
        "payment-gateway": ("payment gateway", "payment-gateway", "gateway timeout"),
        "inventory-service": ("inventory-service", "inventory service"),
    }
    for canon, aliases in mapping.items():
        if any(a in blob for a in aliases):
            found.add(canon)
    # bare names without -service suffix
    if "payment" in blob and "payment-service" not in found and "gateway" not in blob:
        found.add("payment-service")
    if "checkout" in blob and "checkout-service" not in found:
        found.add("checkout-service")
    return found


def primary_fault_class(text: str) -> Optional[str]:
    """Coarse fault class for matching (pool / cache / gateway / cpu / spike / other)."""
    t = (text or "").lower()
    if any(x in t for x in ("connection pool", "pool exhaust", "pool exhausted", "db_pool")):
        return "pool"
    if any(x in t for x in ("cache miss", "cold redis", "redis keyspace")):
        return "cache"
    if any(x in t for x in ("gateway timeout", "payment gateway", "dependency timeout")):
        return "gateway"
    if any(x in t for x in ("cpu", "thread pool", "throttl", "worker")):
        return "cpu"
    if any(
        x in t
        for x in (
            "without application fault",
            "normal traffic",
            "traffic spike",
            "insufficient evidence",
        )
    ):
        return "nofault"
    if "error rate" in t or "elevated" in t:
        return "error_rate"
    return None


def is_rca_correct(
    predicted: str,
    ground_truth: str,
    keywords: Optional[list[str]] = None,
    *,
    jaccard_threshold: float = 0.40,
    keyword_threshold: float = 0.6,
) -> bool:
    """
    Stricter correctness (anti-overfit):

    1. No-fault GT → must look like insufficient/normal (not invent pool/gateway).
    2. Fault GT → must match *fault class* (pool/cache/gateway/…) AND not
       primarily blame the wrong service when GT names a specific service.
    3. Soft OR: Jaccard ≥ threshold, or high keyword hit *with* class match,
       or GT substring in prediction (not the reverse alone — stops kitchen-sink
       preds matching via tiny GT fragments).

    Keywords alone are **not** enough if the fault class disagrees.
    """
    pred = predicted or ""
    gt = ground_truth or ""
    pred_s = pred.strip()
    gt_s = gt.strip()
    if not pred_s:
        return False

    gt_class = primary_fault_class(gt_s)
    pred_class = primary_fault_class(pred_s)
    no_fault = gt_class == "nofault" or any(
        x in gt_s.lower()
        for x in ("without application fault", "normal traffic", "insufficient")
    )

    if no_fault:
        pl = pred_s.lower()
        # Reject inventing a concrete infra fault on normal scenarios
        if pred_class in {"pool", "cache", "gateway", "cpu"}:
            return False
        return any(
            x in pl
            for x in (
                "insufficient evidence",
                "cannot pin",
                "no error",
                "normal",
                "traffic spike",
                "without application fault",
            )
        )

    # Fault scenarios: class must agree when both sides have a class
    if gt_class and pred_class and gt_class != pred_class and gt_class != "error_rate":
        # allow error_rate GT to match gateway/pool when keywords strong + service ok
        if not (gt_class == "error_rate" and pred_class in {"gateway", "pool", "error_rate"}):
            if jaccard(pred_s, gt_s) < 0.55:
                return False

    # Wrong-hop guard: if GT clearly names payment vs checkout, pred must mention it
    gt_services = extract_service_mentions(gt_s)
    pred_services = extract_service_mentions(pred_s)
    if "payment-service" in gt_services and "checkout-service" not in gt_services:
        # Prefer payment root — fail if only checkout is blamed
        if "payment-service" not in pred_services and "payment-gateway" not in pred_services:
            if "payment" not in pred_s.lower():
                return False
    if "checkout-service" in gt_services and "payment-service" not in gt_services:
        if "checkout-service" not in pred_services and "checkout" not in pred_s.lower():
            return False

    if jaccard(pred_s, gt_s) >= jaccard_threshold:
        return True

    # GT fully contained in prediction (multi-cause rule output)
    if gt_s.lower() in pred_s.lower():
        return True

    if keywords and keyword_hit_rate(pred_s, keywords) >= keyword_threshold:
        # Require class alignment when class is known
        if gt_class and pred_class and gt_class != pred_class and gt_class not in {
            "error_rate",
            "nofault",
        }:
            return False
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
