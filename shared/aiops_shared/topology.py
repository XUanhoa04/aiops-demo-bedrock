"""
Service topology catalog loader + neighborhood resolution.

Used by RCA evidence gatherer and offline evaluation so root-cause analysis
can distinguish *symptom services* from *dependency roots*.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore


@dataclass
class ServiceNode:
    name: str
    display_name: str = ""
    depends_on: list[str] = field(default_factory=list)
    shared_deps: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)


@dataclass
class ServiceNeighborhood:
    """Resolved view around one service for RCA evidence expansion."""

    service: str
    # Services this service calls (dependencies / upstream of the request path)
    upstream: list[str] = field(default_factory=list)
    # Services that call this service (dependents / downstream callers)
    downstream: list[str] = field(default_factory=list)
    shared_deps: list[str] = field(default_factory=list)
    # Edges inferred at runtime (e.g. from Tempo root_service)
    inferred_edges: list[dict[str, str]] = field(default_factory=list)
    source: str = "static_catalog"
    catalog_version: str = "1.0"
    rca_hints: dict[str, Any] = field(default_factory=dict)

    def all_neighbors(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for s in self.upstream + self.downstream:
            if s and s not in seen and s != self.service:
                seen.add(s)
                out.append(s)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "upstream": list(self.upstream),
            "downstream": list(self.downstream),
            "shared_deps": list(self.shared_deps),
            "inferred_edges": list(self.inferred_edges),
            "source": self.source,
            "catalog_version": self.catalog_version,
            "rca_hints": dict(self.rca_hints),
            "note": (
                "upstream = services this service depends_on (calls); "
                "downstream = services that call this service"
            ),
        }


class TopologyCatalog:
    def __init__(self, raw: dict[str, Any], path: str = "") -> None:
        self.path = path
        self.version = str(raw.get("version") or "1.0")
        self.rca_hints = dict(raw.get("rca_hints") or {})
        self.shared_infra = dict(raw.get("shared_infrastructure") or {})
        self._nodes: dict[str, ServiceNode] = {}
        self._alias: dict[str, str] = {}

        for name, body in (raw.get("services") or {}).items():
            body = body or {}
            node = ServiceNode(
                name=name,
                display_name=str(body.get("display_name") or name),
                depends_on=[str(x) for x in (body.get("depends_on") or [])],
                shared_deps=[str(x) for x in (body.get("shared_deps") or [])],
                aliases=[str(x) for x in (body.get("aliases") or [])],
            )
            self._nodes[name] = node
            self._alias[name.lower()] = name
            for a in node.aliases:
                self._alias[a.lower()] = name

        # Invert depends_on → downstream callers
        self._downstream: dict[str, list[str]] = {n: [] for n in self._nodes}
        for name, node in self._nodes.items():
            for dep in node.depends_on:
                self._downstream.setdefault(dep, []).append(name)

    def resolve_name(self, service: str) -> str:
        if not service:
            return service
        return self._alias.get(service.lower().strip(), service)

    def neighborhood(self, service: str) -> ServiceNeighborhood:
        canonical = self.resolve_name(service)
        node = self._nodes.get(canonical)
        if not node:
            # Unknown service — empty static neighborhood (runtime edges may fill later)
            return ServiceNeighborhood(
                service=canonical or service,
                source="unknown_service",
                catalog_version=self.version,
                rca_hints=dict(self.rca_hints),
            )
        return ServiceNeighborhood(
            service=canonical,
            upstream=list(node.depends_on),
            downstream=list(self._downstream.get(canonical) or []),
            shared_deps=list(node.shared_deps),
            source="static_catalog",
            catalog_version=self.version,
            rca_hints=dict(self.rca_hints),
        )

    def with_inferred_edges(
        self,
        service: str,
        edges: list[dict[str, str]],
    ) -> ServiceNeighborhood:
        nb = self.neighborhood(service)
        if not edges:
            return nb
        nb.inferred_edges = list(edges)
        # Merge unknown peers from edges into upstream/downstream heuristically
        for e in edges:
            src = self.resolve_name(e.get("from") or e.get("caller") or "")
            dst = self.resolve_name(e.get("to") or e.get("callee") or "")
            if not src or not dst:
                continue
            if src == nb.service and dst not in nb.upstream and dst != nb.service:
                nb.upstream.append(dst)
            if dst == nb.service and src not in nb.downstream and src != nb.service:
                nb.downstream.append(src)
        if edges:
            nb.source = "static_catalog+runtime_traces"
        return nb


def default_topology_paths() -> list[Path]:
    """Search order for the YAML catalog (repo root, container paths, CWD)."""
    env = os.getenv("TOPOLOGY_PATH") or os.getenv("SERVICE_TOPOLOGY_PATH")
    paths: list[Path] = []
    if env:
        paths.append(Path(env))
    # Repo root relative to this file: shared/aiops_shared/topology.py → ../../config
    here = Path(__file__).resolve()
    paths.extend(
        [
            here.parents[2] / "config" / "service_topology.yaml",
            Path("/app/config/service_topology.yaml"),
            Path.cwd() / "config" / "service_topology.yaml",
            Path.cwd() / "service_topology.yaml",
        ]
    )
    return paths


@lru_cache(maxsize=4)
def load_topology_catalog(path: Optional[str] = None) -> TopologyCatalog:
    """
    Load catalog once (cached). Falls back to built-in checkout→payment if
    YAML missing so RCA never crashes without the file.
    """
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    candidates.extend(default_topology_paths())

    for p in candidates:
        try:
            if p.is_file():
                if yaml is None:
                    logger.warning("PyYAML missing — using built-in topology")
                    break
                raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                logger.info("topology catalog loaded path=%s", p)
                return TopologyCatalog(raw, path=str(p))
        except Exception as exc:
            logger.warning("topology load failed path=%s err=%s", p, exc)

    logger.warning("topology catalog not found — using built-in demo graph")
    return TopologyCatalog(_builtin_catalog(), path="builtin")


def _builtin_catalog() -> dict[str, Any]:
    return {
        "version": "builtin-1.1",
        "rca_hints": {
            "prefer_dependency_root_when_correlated": True,
            "error_rate_neighbor_margin": 0.05,
            "latency_neighbor_margin_seconds": 0.2,
        },
        "services": {
            "checkout-service": {
                "depends_on": ["payment-service", "inventory-service"],
                "shared_deps": ["redis-cache", "postgres-orders"],
                "aliases": ["checkout", "aiops-checkout"],
            },
            "payment-service": {
                "depends_on": ["fraud-service"],
                "shared_deps": [
                    "redis-cache",
                    "postgres-payments",
                    "payment-gateway",
                ],
                "aliases": ["payment", "aiops-payment"],
            },
            "inventory-service": {
                "depends_on": [],
                "shared_deps": ["postgres-inventory", "redis-cache"],
                "aliases": ["inventory", "aiops-inventory"],
            },
            "fraud-service": {
                "depends_on": [],
                "shared_deps": ["redis-cache"],
                "aliases": ["fraud", "aiops-fraud"],
            },
        },
        "shared_infrastructure": {
            "redis-cache": {"kind": "cache"},
            "postgres-orders": {"kind": "database"},
            "postgres-payments": {"kind": "database"},
            "postgres-inventory": {"kind": "database"},
            "payment-gateway": {"kind": "external"},
        },
    }


def infer_edges_from_traces(
    service: str, traces: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """
    Build coarse call edges from Tempo search hits.
    root_service often equals entry service; still useful when multi-service.
    """
    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for tr in traces or []:
        root = str(tr.get("root_service") or "")
        name = str(tr.get("root_name") or tr.get("search_mode") or "")
        if root and root != service:
            key = (service, root)
            if key not in seen:
                seen.add(key)
                # If another service is root of a trace related to us, treat as peer edge
                edges.append({"from": service, "to": root, "via": "tempo_root"})
        # Pattern strings sometimes contain "checkout → payment"
        if "→" in name or "->" in name:
            parts = name.replace("->", "→").split("→")
            if len(parts) >= 2:
                a = parts[0].strip().split()[0]
                b = parts[1].strip().split()[0]
                if a and b:
                    key = (a, b)
                    if key not in seen:
                        seen.add(key)
                        edges.append({"from": a, "to": b, "via": "trace_pattern"})
    return edges
