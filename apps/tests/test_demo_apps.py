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
