"""Config-driven RCA patterns — no per-scenario hard-code."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiops_shared.rca_patterns import clear_pattern_cache, load_pattern_catalog  # noqa: E402
from app.models import EvidencePack  # noqa: E402
from app.rule_fallback import rule_based_rca  # noqa: E402


def test_catalog_loads_from_repo_yaml():
    clear_pattern_cache()
    cat = load_pattern_catalog(str(ROOT / "config" / "rca_patterns.yaml"))
    ids = {p.id for p in cat.patterns}
    assert "db_pool" in ids
    assert "fraud_scoring" in ids
    assert cat.path.endswith("rca_patterns.yaml")


def test_pattern_match_jdbc_not_literal_pool_string():
    clear_pattern_cache()
    cat = load_pattern_catalog(str(ROOT / "config" / "rca_patterns.yaml"))
    matches = cat.match_logs(
        "error could not obtain jdbc connection maxpoolsize reached",
        ticket_service="checkout-service",
        log_service_hint="payment-service",
    )
    assert matches
    assert matches[0].pattern.fault_class == "pool"
    assert "payment" in matches[0].root_cause.lower()


def test_rule_uses_catalog_not_scenario_id():
    """RCA must work from evidence alone — no scenario_id branching."""
    clear_pattern_cache()
    pack = EvidencePack(
        incident_id="x",
        service_name="checkout-service",
        window_minutes=15,
        window_start_iso="2020-01-01T00:00:00+00:00",
        window_end_iso="2020-01-01T00:15:00+00:00",
        incident={"service_name": "checkout-service", "metric_value": 0.4},
        metrics_summary={"instant": {"http_error_rate": 0.4}},
        error_logs=[],
        neighbor_logs=[
            {
                "line": "ERROR PSP provider deadline exceeded after 2000ms",
                "neighbor_service": "payment-service",
                "labels": {"service_name": "payment-service"},
            }
        ],
        neighbor_metrics={
            "payment-service": {
                "relation": "upstream",
                "instant": {"http_error_rate": 0.5, "http_latency_p95_seconds": 2.0},
            }
        },
        topology={
            "service": "checkout-service",
            "upstream": ["payment-service"],
            "downstream": [],
            "shared_deps": [],
            "rca_hints": {"prefer_dependency_root_when_correlated": True},
        },
        sources_ok={"topology": True},
    )
    result = rule_based_rca(pack)
    assert "gateway" in result.root_cause.lower() or "payment" in result.root_cause.lower()
