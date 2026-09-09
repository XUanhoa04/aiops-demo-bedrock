"""Unit tests for 4-service demo topology (no Docker)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared"))


def _client(app_mod_path: str, package_root: Path):
    sys.path.insert(0, str(package_root))
    # Isolate package name `app` per service by loading via path injection
    import importlib

    # Ensure fresh app package from package_root
    for key in list(sys.modules):
        if key == "app" or key.startswith("app."):
            del sys.modules[key]
    sys.path.insert(0, str(package_root))
    mod = importlib.import_module(app_mod_path)
    return TestClient(mod.app), mod


def test_inventory_chaos_and_reserve_ok():
    client, _ = _client("app.main", ROOT / "apps" / "inventory-service")
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "inventory-service"

    r = client.post(
        "/chaos",
        json={"error_rate": 0.0, "fault_mode": "stock_lock", "extra_latency_ms": 0},
    )
    assert r.status_code == 200
    assert r.json()["fault_mode"] == "stock_lock"

    r = client.post("/reserve", json={"order_id": "o1", "sku": "SKU-1", "qty": 1})
    assert r.status_code == 200
    assert r.json()["status"] == "reserved"


def test_inventory_fault_mode_errors():
    client, mod = _client("app.main", ROOT / "apps" / "inventory-service")
    client.post("/chaos", json={"error_rate": 1.0, "fault_mode": "stock_lock"})
    r = client.post("/reserve", json={"order_id": "o2", "sku": "SKU-1", "qty": 1})
    assert r.status_code == 503
    assert "stock lock" in r.json()["detail"].lower() or "lock" in r.json()["detail"].lower()


def test_fraud_score_and_fault():
    client, _ = _client("app.main", ROOT / "apps" / "fraud-service")
    client.post("/chaos", json={"error_rate": 0.0, "fault_mode": "none"})
    r = client.post("/score", json={"order_id": "o1", "amount": 10.0})
    assert r.status_code == 200
    assert r.json()["status"] == "scored"

    client.post("/chaos", json={"error_rate": 1.0, "fault_mode": "scoring_timeout"})
    r = client.post("/score", json={"order_id": "o2", "amount": 10.0})
    assert r.status_code == 503
    assert "fraud" in r.json()["detail"].lower() or "scoring" in r.json()["detail"].lower()


def test_topology_catalog_four_services():
    from aiops_shared.topology import load_topology_catalog

    cat = load_topology_catalog(str(ROOT / "config" / "service_topology.yaml"))
    co = cat.neighborhood("checkout")
    assert "payment-service" in co.upstream
    assert "inventory-service" in co.upstream
    pay = cat.neighborhood("payment")
    assert "fraud-service" in pay.upstream
    assert "checkout-service" in pay.downstream
    inv = cat.neighborhood("inventory-service")
    assert "checkout-service" in inv.downstream


def test_checkout_and_payment_persistent_client_reuse():
    # Checkout service persistent client
    _, chk_mod = _client("app.main", ROOT / "apps" / "checkout-service")
    c1 = chk_mod.get_http_client()
    c2 = chk_mod.get_http_client()
    assert c1 is c2
    assert not c1.is_closed

    # Payment service persistent client
    _, pay_mod = _client("app.main", ROOT / "apps" / "payment-service")
    p1 = pay_mod.get_http_client()
    p2 = pay_mod.get_http_client()
    assert p1 is p2
    assert not p1.is_closed


def test_checkout_e2e_with_downstream_hops():
    client, chk_mod = _client("app.main", ROOT / "apps" / "checkout-service")
    client.post("/chaos", json={"error_rate": 0.0, "extra_latency_ms": 0})

    mock_client = AsyncMock()
    mock_inv_resp = MagicMock()
    mock_inv_resp.is_success = True
    mock_inv_resp.json.return_value = {"status": "reserved", "sku": "SKU-DEMO"}
    mock_pay_resp = MagicMock()
    mock_pay_resp.is_success = True
    mock_pay_resp.json.return_value = {"status": "captured", "payment_id": "p-123"}

    mock_client.post.side_effect = [mock_inv_resp, mock_pay_resp]

    with patch.object(chk_mod, "get_http_client", return_value=mock_client):
        r = client.post("/checkout", json={"order_id": "test-order-99", "amount": 99.0})
        assert r.status_code == 200
        data = r.json()
        assert data["order_id"] == "test-order-99"
        assert data["status"] == "confirmed"
        assert data["inventory"]["sku"] == "SKU-DEMO"
        assert data["payment"]["payment_id"] == "p-123"
        assert mock_client.post.call_count == 2


def test_inventory_and_fraud_telemetry_fallbacks():
    _, inv_mod = _client("app.main", ROOT / "apps" / "inventory-service")
    noop = inv_mod._Noop()
    # Ensure all telemetry methods on fallback are safe no-ops
    noop.add(1, {"route": "/reserve"})
    noop.record(45.0, {"route": "/reserve"})
    noop.set(10)

    # Test with mocked meter
    mock_meter = MagicMock()
    with patch.object(inv_mod, "_get_meter", return_value=mock_meter):
        inv_mod._init_metrics()
        mock_meter.create_counter.assert_any_call(
            "demo_http_requests_total",
            description="Inventory requests",
            unit="1",
        )
        mock_meter.create_histogram.assert_called_once_with(
            "demo_http_duration_ms",
            description="Inventory request duration",
            unit="ms",
        )

    _, fraud_mod = _client("app.main", ROOT / "apps" / "fraud-service")
    with patch.object(fraud_mod, "_get_meter", return_value=mock_meter):
        fraud_mod._init_metrics()
        mock_meter.create_counter.assert_any_call(
            "demo_http_requests_total",
            description="Fraud requests",
            unit="1",
        )
        mock_meter.create_histogram.assert_called_with(
            "demo_http_duration_ms",
            description="Fraud request duration",
            unit="ms",
        )

