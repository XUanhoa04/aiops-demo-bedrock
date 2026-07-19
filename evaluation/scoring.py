"""
Scoring helpers for RCA / anomaly evaluation.

Why custom metrics (not only sklearn)?
--------------------------------------
Ops datasets are tiny (10–50 scenarios). We need transparent, commentable
formulas that interviewers and SREs can audit:

  Accuracy  = correct / N
  Precision = TP / (TP + FP)   where "positive" = system claims a specific fault
  Recall    = TP / (TP + FN)
  F1        = 2PR / (P+R)
  Semantic  = Jaccard token overlap

Two RCA scoring modes
---------------------
  default — regression-friendly (Jaccard ≥ 0.40 OR GT⊂pred OR keywords+class)
  strict  — CV-honest (class + service + Jaccard ≥ 0.50; keywords alone insufficient)

Grades (always computed under both modes when reporting):
  exact | partial | wrong_hop | insufficient_ok | false_positive | miss
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Literal, Optional

ScoringMode = Literal["default", "strict"]
RcaGrade = Literal[
    "exact",
    "partial",
    "wrong_hop",
    "insufficient_ok",
    "false_positive",
    "miss",
]

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)?", re.I)

# Concrete infra classes that must NOT be invented on no-fault GT
_CONCRETE_FAULT_CLASSES = frozenset(
    {
        "pool",
        "cache",
        "gateway",
        "cpu",
        "fraud",
        "inventory",
        "change",
        "catalog",
        "cart",
    }
)

_RELATED_CLASSES = frozenset(
    {
        ("fraud", "gateway"),
        ("gateway", "fraud"),
        ("inventory", "error_rate"),
        ("error_rate", "inventory"),
        ("change", "error_rate"),
        ("error_rate", "change"),
    }
)


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
        "fraud-service": ("fraud-service", "fraud service"),
        "redis-cache": ("redis-cache", "shared redis", "redis cache"),
    }
    for canon, aliases in mapping.items():
        if any(a in blob for a in aliases):
            found.add(canon)
    if "payment" in blob and "payment-service" not in found and "gateway" not in blob:
        found.add("payment-service")
    if "checkout" in blob and "checkout-service" not in found:
        found.add("checkout-service")
    if "fraud" in blob and "fraud-service" not in found:
        found.add("fraud-service")
    if "inventory" in blob and "inventory-service" not in found:
        found.add("inventory-service")
    return found


def primary_fault_class(text: str) -> Optional[str]:
    """Coarse fault class for matching (pool / cache / gateway / cpu / …)."""
    t = (text or "").lower()
    if any(x in t for x in ("connection pool", "pool exhaust", "pool exhausted", "db_pool", "hikari", "too many connections")):
        return "pool"
    if any(
        x in t
        for x in (
            "cache miss",
            "cold redis",
            "redis keyspace",
            "shared redis",
            "redis-cache",
        )
    ):
        return "cache"
    if any(x in t for x in ("fraud-service", "scoring saturation", "fraud dependency", "scoring timeout")):
        return "fraud"
    if any(x in t for x in ("inventory", "stock lock")):
        return "inventory"
    if any(x in t for x in ("deploy", "release", "rollback", "post-deploy", "regression")):
        return "change"
    if any(
        x in t
        for x in (
            "gateway timeout",
            "payment gateway",
            "dependency timeout",
            "dependency failure",
            "psp",
            "deadline exceeded",
            "client timeout waiting for upstream",
        )
    ):
        return "gateway"
    if any(
        x in t
        for x in (
            "cpu",
            "thread pool",
            "throttl",
            "worker",
            "run queue",
            "scheduling delayed",
        )
    ):
        return "cpu"
    if any(x in t for x in ("product catalog", "productcatalog", "catalog failure")):
        return "catalog"
    if any(x in t for x in ("cart failure", "cart service", "valkey")):
        return "cart"
    if any(
        x in t
        for x in (
            "without application fault",
            "normal traffic",
            "traffic spike",
            "insufficient evidence",
            "cannot pin",
            "no real fault",
            "unknown fault class",
            "out of catalog",
        )
    ):
        return "nofault"
    if "error rate" in t or "elevated" in t:
        return "error_rate"
    if "slow traces" in t:
        return "latency"
    return None


def is_nofault_gt(ground_truth: str) -> bool:
    gt_s = (ground_truth or "").strip()
    gt_class = primary_fault_class(gt_s)
    if gt_class == "nofault":
        return True
    return any(
        x in gt_s.lower()
        for x in (
            "without application fault",
            "normal traffic",
            "insufficient evidence",
            "no real fault",
            "unknown fault",
            "out of catalog",
            "ood",
        )
    )


def is_ood_unknown_gt(ground_truth: str) -> bool:
    """True OOD: correct answer is 'I don't know', not generic elevated error."""
    low = (ground_truth or "").lower()
    return any(
        x in low
        for x in (
            "unknown fault class",
            "out of catalog",
            "ood",
        )
    )


def classes_compatible(gt_class: Optional[str], pred_class: Optional[str]) -> bool:
    if not gt_class or not pred_class:
        return True
    if gt_class == pred_class:
        return True
    if gt_class == "error_rate" or pred_class == "error_rate":
        return True
    return (gt_class, pred_class) in _RELATED_CLASSES


def service_alignment_ok(predicted: str, ground_truth: str) -> bool:
    """True if pred mentions the primary service named in GT (when GT is specific)."""
    gt_s = ground_truth or ""
    pred_s = predicted or ""
    gt_services = extract_service_mentions(gt_s)
    pred_services = extract_service_mentions(pred_s)
    pl = pred_s.lower()

    if "payment-service" in gt_services and "checkout-service" not in gt_services:
        if "payment-service" not in pred_services and "payment-gateway" not in pred_services:
            if "payment" not in pl:
                return False
    if "checkout-service" in gt_services and "payment-service" not in gt_services:
        if "checkout-service" not in pred_services and "checkout" not in pl:
            return False
    if "fraud-service" in gt_services and "fraud" not in pl:
        return False
    if "inventory-service" in gt_services and "inventory" not in pl:
        return False
    return True


def is_wrong_hop(predicted: str, ground_truth: str) -> bool:
    """
    True when GT pins a root service and pred primarily blames a different app service.
    """
    gt_s = (ground_truth or "").strip()
    pred_s = (predicted or "").strip()
    if not pred_s or is_nofault_gt(gt_s):
        return False
    if not service_alignment_ok(pred_s, gt_s):
        return True
    gt_svc = extract_service_mentions(gt_s)
    pred_svc = extract_service_mentions(pred_s)
    # GT only payment, pred only checkout (common cascade miss)
    roots = gt_svc - {"payment-gateway", "redis-cache"}
    if not roots:
        return False
    blamed = pred_svc - {"payment-gateway", "redis-cache"}
    if not blamed:
        return False
    # Wrong hop if none of GT roots appear in pred services
    if roots.isdisjoint(blamed) and not any(
        r.replace("-service", "") in pred_s.lower() for r in roots
    ):
        return True
    return False


def is_insufficient_prediction(predicted: str) -> bool:
    pl = (predicted or "").lower()
    return any(
        x in pl
        for x in (
            "insufficient evidence",
            "cannot pin",
            "no error",
            "normal",
            "traffic spike",
            "without application fault",
            "incomplete",
            "unknown fault",
            "out of catalog",
        )
    )


def is_rca_correct(
    predicted: str,
    ground_truth: str,
    keywords: Optional[list[str]] = None,
    *,
    mode: ScoringMode = "default",
    jaccard_threshold: Optional[float] = None,
    keyword_threshold: float = 0.6,
) -> bool:
    """
    Correctness under default (catalog regression) or strict (CV-honest) mode.

    default:
      1. No-fault GT → insufficient/normal language, no concrete fault invent
      2. Fault GT → class + service guards
      3. Soft OR: Jaccard ≥ 0.40, or GT⊂pred, or keywords≥0.6 with class match

    strict:
      1. Same no-fault rules
      2. Fault: class compatible AND service OK AND
         (Jaccard ≥ 0.50 OR GT fully contained in pred)
      3. Keywords alone are never enough
    """
    pred = predicted or ""
    gt = ground_truth or ""
    pred_s = pred.strip()
    gt_s = gt.strip()
    if not pred_s:
        return False

    jac_thr = jaccard_threshold
    if jac_thr is None:
        jac_thr = 0.50 if mode == "strict" else 0.40

    gt_class = primary_fault_class(gt_s)
    pred_class = primary_fault_class(pred_s)
    no_fault = is_nofault_gt(gt_s)

    if no_fault:
        if pred_class in _CONCRETE_FAULT_CLASSES:
            return False
        # True OOD (unknown fault class): only "insufficient / cannot pin" counts.
        # Generic elevated is a shallow metric fallback — not credit for OOD.
        if is_ood_unknown_gt(gt_s):
            return is_insufficient_prediction(pred_s) and not any(
                x in pred_s.lower()
                for x in ("elevated http_error_rate", "high latency p95", "slow traces")
            )
        # Classic no-fault / normal traffic: insufficient OR generic elevated/latency
        if is_insufficient_prediction(pred_s):
            return True
        pl = pred_s.lower()
        if any(
            x in pl
            for x in (
                "elevated http_error_rate",
                "elevated error rate",
                "high latency",
                "slow traces",
                "incomplete log",
                "insufficient log",
            )
        ):
            return True
        return False

    # Fault scenarios
    if not service_alignment_ok(pred_s, gt_s):
        return False

    if gt_class and pred_class and not classes_compatible(gt_class, pred_class):
        if jaccard(pred_s, gt_s) < (0.50 if mode == "strict" else 0.50):
            return False

    jac = jaccard(pred_s, gt_s)
    if jac >= jac_thr:
        return True

    if gt_s.lower() in pred_s.lower():
        return True

    if mode == "strict":
        # Strict: no keyword-only path
        return False

    # default keyword path with class alignment
    if keywords and keyword_hit_rate(pred_s, keywords) >= keyword_threshold:
        if gt_class and pred_class and not classes_compatible(gt_class, pred_class):
            if gt_class not in {"error_rate", "nofault"}:
                return False
        return True

    return False


def grade_rca(
    predicted: str,
    ground_truth: str,
    keywords: Optional[list[str]] = None,
    *,
    mode: ScoringMode = "default",
) -> RcaGrade:
    """Assign a fine-grained grade for reporting (not only binary correct)."""
    pred_s = (predicted or "").strip()
    gt_s = (ground_truth or "").strip()
    if not pred_s:
        return "miss"

    no_fault = is_nofault_gt(gt_s)
    jac = jaccard(pred_s, gt_s)

    if no_fault:
        if primary_fault_class(pred_s) in _CONCRETE_FAULT_CLASSES:
            return "false_positive"
        if is_rca_correct(pred_s, gt_s, keywords, mode=mode):
            return "insufficient_ok"
        return "miss"

    if is_wrong_hop(pred_s, gt_s):
        return "wrong_hop"

    ok = is_rca_correct(pred_s, gt_s, keywords, mode=mode)
    if not ok:
        # Invented concrete fault when GT expected elevated/insufficient style
        if is_insufficient_prediction(pred_s) and not is_nofault_gt(gt_s):
            return "miss"
        return "miss"

    if jac >= 0.75 or gt_s.lower() in pred_s.lower():
        return "exact"
    if jac >= 0.40 or (keywords and keyword_hit_rate(pred_s, keywords) >= 0.6):
        return "partial"
    return "partial"


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
    grade: RcaGrade = "miss"
    correct_strict: bool = False
    grade_strict: RcaGrade = "miss"
    wrong_hop: bool = False
    scoring_mode: ScoringMode = "default"


@dataclass
class RcaAggregate:
    n: int = 0
    correct: int = 0
    correct_strict: int = 0
    mean_jaccard: float = 0.0
    mean_keyword_rate: float = 0.0
    mean_iterations: float = 0.0
    wrong_hop_count: int = 0
    grade_counts: dict[str, int] = field(default_factory=dict)
    grade_strict_counts: dict[str, int] = field(default_factory=dict)
    binary: BinaryCounts = field(default_factory=BinaryCounts)
    binary_strict: BinaryCounts = field(default_factory=BinaryCounts)
    rows: list[RcaScoreRow] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.n if self.n else 0.0

    @property
    def accuracy_strict(self) -> float:
        return self.correct_strict / self.n if self.n else 0.0

    @property
    def wrong_hop_rate(self) -> float:
        return self.wrong_hop_count / self.n if self.n else 0.0


def aggregate_rca(
    rows: list[RcaScoreRow],
    *,
    is_fault: dict[str, bool] | None = None,
) -> RcaAggregate:
    """
    is_fault[scenario_id]: True if scenario is a real fault (default True).
      - For fault scenarios: TP if correct else FN
      - For normal scenarios: TN if correct else FP
    """
    is_fault = is_fault or {}
    agg = RcaAggregate(n=len(rows), rows=rows)
    if not rows:
        return agg
    agg.correct = sum(1 for r in rows if r.correct)
    agg.correct_strict = sum(1 for r in rows if r.correct_strict)
    agg.mean_jaccard = sum(r.jaccard for r in rows) / len(rows)
    agg.mean_keyword_rate = sum(r.keyword_rate for r in rows) / len(rows)
    agg.mean_iterations = sum(r.iterations for r in rows) / len(rows)
    agg.wrong_hop_count = sum(1 for r in rows if r.wrong_hop)

    for r in rows:
        g = r.grade or "miss"
        agg.grade_counts[g] = agg.grade_counts.get(g, 0) + 1
        gs = r.grade_strict or "miss"
        agg.grade_strict_counts[gs] = agg.grade_strict_counts.get(gs, 0) + 1

        fault = is_fault.get(r.scenario_id, True)
        if fault:
            if r.correct:
                agg.binary.tp += 1
            else:
                agg.binary.fn += 1
            if r.correct_strict:
                agg.binary_strict.tp += 1
            else:
                agg.binary_strict.fn += 1
        else:
            if r.correct:
                agg.binary.tn += 1
            else:
                agg.binary.fp += 1
            if r.correct_strict:
                agg.binary_strict.tn += 1
            else:
                agg.binary_strict.fp += 1
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
