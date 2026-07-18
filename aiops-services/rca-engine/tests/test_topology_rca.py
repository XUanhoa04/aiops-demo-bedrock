"""Topology catalog + topology-aware rule RCA (offline, no network)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiops_shared.topology import TopologyCatalog, load_topology_catalog  # noqa: E402
from app.models import EvidencePack  # noqa: E402
from app.rule_fallback import rule_based_rca  # noqa: E402


def test_catalog_checkout_upstream_payment():
    cat = load_topology_catalog(str(ROOT / "config" / "service_topology.yaml"))
    nb = cat.neighborhood("checkout")
    assert nb.service == "checkout-service"
    assert "payment-service" in nb.upstream
    assert "inventory-service" in nb.upstream
    pay = cat.neighborhood("payment-service")
    assert "checkout-service" in pay.downstream
    assert "fraud-service" in pay.upstream
    fraud = cat.neighborhood("fraud-service")
    assert "payment-service" in fraud.downstream


def test_wrong_hop_prefers_payment_pool():
    """Ticket on checkout, sicker payment + pool logs → payment root."""
    cat = load_topology_catalog(str(ROOT / "config" / "service_topology.yaml"))
    nb = cat.neighborhood("checkout-service")
    pack = EvidencePack(
        incident_id="t1",
        service_name="checkout-service",
        window_minutes=15,
        window_start_iso="2020-01-01T00:00:00+00:00",
        window_end_iso="2020-01-01T00:15:00+00:00",
        incident={
            "service_name": "checkout-service",
            "metric_name": "http_error_rate",
            "metric_value": 0.3,
            "severity": "high",
        },
        metrics_summary={
            "instant": {"http_error_rate": 0.3, "http_latency_p95_seconds": 0.8}
        },
        error_logs=[
            {
                "line": "ERROR payment failed: 502 cascade",
                "labels": {"service_name": "checkout-service"},
            }
        ],
        neighbor_metrics={
            "payment-service": {
                "relation": "upstream",
                "instant": {
                    "http_error_rate": 0.55,
                    "http_latency_p95_seconds": 1.6,
                },
            }
        },
        neighbor_logs=[
            {
                "line": "ERROR payment-service database connection pool exhaustion",
                "neighbor_service": "payment-service",
                "labels": {"service_name": "payment-service"},
            }
        ],
        topology=nb.to_dict(),
        sources_ok={"topology": True},
    )
    result = rule_based_rca(pack)
    assert "payment" in result.root_cause.lower()
    assert "pool" in result.root_cause.lower() or "connection" in result.root_cause.lower()
    assert "payment-service" in result.affected_components


def test_local_checkout_pool_not_blamed_on_healthy_payment():
    cat = load_topology_catalog(str(ROOT / "config" / "service_topology.yaml"))
    nb = cat.neighborhood("checkout-service")
    pack = EvidencePack(
        incident_id="t2",
        service_name="checkout-service",
        window_minutes=15,
        window_start_iso="2020-01-01T00:00:00+00:00",
        window_end_iso="2020-01-01T00:15:00+00:00",
        incident={
            "service_name": "checkout-service",
            "metric_name": "http_error_rate",
            "metric_value": 0.5,
        },
        metrics_summary={"instant": {"http_error_rate": 0.5}},
        error_logs=[
            {
                "line": "ERROR database connection pool exhausted (no free connections)",
                "labels": {"service_name": "checkout-service"},
            }
        ],
        neighbor_metrics={
            "payment-service": {
                "relation": "upstream",
                "instant": {"http_error_rate": 0.01},
            }
        },
        topology=nb.to_dict(),
    )
    result = rule_based_rca(pack)
    assert "checkout" in result.root_cause.lower()
    assert "payment-service database" not in result.root_cause.lower()


def test_builtin_catalog_without_file():
    # empty path that does not exist falls back
    cat = TopologyCatalog(
        {
            "version": "t",
            "services": {
                "a": {"depends_on": ["b"], "aliases": []},
                "b": {"depends_on": [], "aliases": []},
            },
            "rca_hints": {"prefer_dependency_root_when_correlated": True},
        }
    )
    assert cat.neighborhood("a").upstream == ["b"]
    assert cat.neighborhood("b").downstream == ["a"]
