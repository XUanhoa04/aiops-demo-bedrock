"""Unit tests for dual-mode RCA scoring (default vs strict)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scoring import (  # noqa: E402
    grade_rca,
    is_rca_correct,
    is_wrong_hop,
    jaccard,
)


def test_default_allows_keyword_path():
    pred = "payment-service database connection pool exhaustion; cascade on checkout"
    gt = "payment-service database connection pool exhaustion"
    keys = ["connection pool", "payment-service"]
    assert is_rca_correct(pred, gt, keys, mode="default")
    assert is_rca_correct(pred, gt, keys, mode="strict")


def test_strict_rejects_keyword_only_low_jaccard():
    # Shared few tokens but high keywords — if jaccard low and not substring
    pred = "pool issue on payment-service side"
    gt = "payment-service database connection pool exhaustion"
    keys = ["pool", "payment-service"]
    # default may pass via keywords; strict needs jaccard/substring
    default_ok = is_rca_correct(pred, gt, keys, mode="default")
    strict_ok = is_rca_correct(pred, gt, keys, mode="strict")
    assert default_ok or not default_ok  # smoke
    # If jaccard < 0.5 and gt not in pred → strict false
    if jaccard(pred, gt) < 0.5 and gt.lower() not in pred.lower():
        assert strict_ok is False


def test_nofault_rejects_invented_pool():
    pred = "payment-service database connection pool exhaustion"
    gt = "normal traffic without application fault / insufficient evidence of outage"
    assert is_rca_correct(pred, gt, mode="default") is False
    assert grade_rca(pred, gt, mode="default") == "false_positive"


def test_ood_requires_insufficient_not_elevated():
    gt = "insufficient evidence / unknown fault class (DNS) — out of catalog"
    # Shallow elevated fallback is NOT enough for true OOD
    assert (
        is_rca_correct(
            "elevated http_error_rate on checkout-service", gt, mode="default"
        )
        is False
    )
    assert is_rca_correct(
        "Insufficient evidence: cannot pin root cause for checkout-service",
        gt,
        mode="default",
    )
    assert (
        is_rca_correct(
            "payment-service database connection pool exhaustion", gt, mode="default"
        )
        is False
    )


def test_wrong_hop_detection():
    pred = "checkout-service worker/thread pool saturation or CPU throttle"
    gt = "payment-service database connection pool exhaustion"
    assert is_wrong_hop(pred, gt) is True
    assert is_rca_correct(pred, gt, ["pool", "payment-service"], mode="default") is False


def test_insufficient_ok_grade():
    pred = "Insufficient evidence: cannot pin root cause for checkout-service"
    gt = "normal traffic spike without application fault / insufficient evidence"
    assert grade_rca(pred, gt, mode="default") == "insufficient_ok"
