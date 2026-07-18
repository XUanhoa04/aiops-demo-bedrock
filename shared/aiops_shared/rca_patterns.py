"""
Config-driven RCA pattern catalog loader + generic matcher.

This replaces hard-coded if/else chains for fault classes. Ops extend
``config/rca_patterns.yaml`` instead of editing Python for each demo scenario.
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
class RcaPattern:
    id: str
    fault_class: str
    priority: int = 50
    log_any: list[str] = field(default_factory=list)
    change_event_types: list[str] = field(default_factory=list)
    force_service: str = ""
    prefer_service_from_logs: bool = True
    prefer_upstream_if_sicker: bool = False
    multi_service_shared_root: str = ""
    root_cause_template: str = ""
    why: str = ""
    runbook: str = "generic"
    actions: list[str] = field(default_factory=list)
    base_confidence: int = 50


@dataclass
class PatternMatch:
    pattern: RcaPattern
    service: str
    root_cause: str
    score: float
    matched_phrases: list[str] = field(default_factory=list)
    source: str = "logs"  # logs | change_event | multi_service


@dataclass
class PatternCatalog:
    version: str
    metrics: dict[str, Any]
    patterns: list[RcaPattern]
    fallbacks: dict[str, Any]
    path: str = ""

    def match_logs(
        self,
        log_blob: str,
        *,
        ticket_service: str,
        log_service_hint: str = "",
        log_services_seen: Optional[set[str]] = None,
        change_events: Optional[list[dict[str, Any]]] = None,
    ) -> list[PatternMatch]:
        """Return all matching patterns sorted by (priority, score) desc."""
        blob = (log_blob or "").lower()
        seen_svcs = {s for s in (log_services_seen or set()) if s}
        matches: list[PatternMatch] = []

        for pat in self.patterns:
            phrases = [p for p in pat.log_any if p.lower() in blob]
            change_hit = False
            if pat.change_event_types and change_events:
                types = {
                    str(e.get("type") or "").lower() for e in change_events
                }
                change_hit = bool(types & {t.lower() for t in pat.change_event_types})

            if not phrases and not change_hit:
                continue

            svc = ticket_service
            if pat.force_service:
                svc = pat.force_service
            elif pat.prefer_service_from_logs and log_service_hint:
                svc = log_service_hint

            # Multi-service shared root (e.g. redis blast)
            root = pat.root_cause_template.format(service=svc)
            source = "logs" if phrases else "change_event"
            if pat.multi_service_shared_root and len(seen_svcs) >= 2:
                # checkout+payment both present with cache phrases
                interesting = {
                    s
                    for s in seen_svcs
                    if "checkout" in s or "payment" in s or "inventory" in s
                }
                if len(interesting) >= 2 or (
                    "checkout" in blob and "payment" in blob
                ):
                    root = pat.multi_service_shared_root
                    source = "multi_service"

            if change_hit and not phrases:
                # Prefer service on the change event
                for e in change_events or []:
                    if str(e.get("type") or "").lower() in {
                        t.lower() for t in pat.change_event_types
                    }:
                        if e.get("service"):
                            svc = str(e["service"])
                            root = pat.root_cause_template.format(service=svc)
                        break

            score = float(pat.priority) + 5.0 * len(phrases)
            if change_hit:
                score += 8.0
            matches.append(
                PatternMatch(
                    pattern=pat,
                    service=svc,
                    root_cause=root,
                    score=score,
                    matched_phrases=phrases[:8],
                    source=source,
                )
            )

        matches.sort(key=lambda m: m.score, reverse=True)
        return matches


def default_pattern_paths() -> list[Path]:
    env = os.getenv("RCA_PATTERNS_PATH") or os.getenv("RCA_PATTERN_CATALOG_PATH")
    paths: list[Path] = []
    if env:
        paths.append(Path(env))
    here = Path(__file__).resolve()
    paths.extend(
        [
            here.parents[2] / "config" / "rca_patterns.yaml",
            Path("/app/config/rca_patterns.yaml"),
            Path.cwd() / "config" / "rca_patterns.yaml",
        ]
    )
    return paths


def _builtin_catalog() -> dict[str, Any]:
    """Minimal built-in if YAML missing — keeps RCA bootable."""
    return {
        "version": "builtin-1.0",
        "metrics": {
            "error_rate_elevated": 0.10,
            "error_rate_high": 0.20,
            "latency_p95_elevated_seconds": 0.50,
            "error_rate_neighbor_margin": 0.05,
            "latency_neighbor_margin_seconds": 0.20,
        },
        "patterns": [
            {
                "id": "db_pool",
                "fault_class": "pool",
                "priority": 100,
                "log_any": [
                    "connection pool",
                    "pool exhausted",
                    "pool exhaustion",
                    "maxpoolsize",
                    "jdbc",
                ],
                "root_cause_template": "{service} database connection pool exhaustion",
                "why": "Pool saturation in logs.",
                "runbook": "db-connection-pool",
                "actions": ["Increase pool size / fix leak on {service}"],
                "base_confidence": 68,
            },
            {
                "id": "cache_miss",
                "fault_class": "cache",
                "priority": 90,
                "log_any": ["cache miss", "redis miss", "falling back to origin"],
                "root_cause_template": (
                    "{service} high latency due to cache miss / cold redis keyspace"
                ),
                "why": "Cache miss storm.",
                "runbook": "cache-miss-latency",
                "actions": ["Warm cache / check redis"],
                "base_confidence": 60,
            },
            {
                "id": "payment_gateway",
                "fault_class": "gateway",
                "priority": 95,
                "log_any": [
                    "gateway timeout",
                    "payment gateway",
                    "psp",
                    "deadline exceeded",
                ],
                "force_service": "payment-service",
                "root_cause_template": "payment gateway timeout / dependency failure",
                "why": "Gateway/dependency timeout.",
                "runbook": "dependency-timeout",
                "actions": ["Check payment gateway"],
                "base_confidence": 62,
            },
        ],
        "fallbacks": {
            "elevated_error": {
                "root_cause_template": "elevated http_error_rate on {service}",
                "root_cause_metric_only": (
                    "elevated http_error_rate on {service} with insufficient "
                    "log/trace corroboration"
                ),
                "base_confidence": 50,
            },
            "insufficient": {
                "root_cause_template": (
                    "Insufficient evidence: cannot pin root cause for {service}"
                ),
                "base_confidence": 30,
            },
        },
    }


def _parse_catalog(raw: dict[str, Any], path: str = "") -> PatternCatalog:
    patterns: list[RcaPattern] = []
    for body in raw.get("patterns") or []:
        body = body or {}
        patterns.append(
            RcaPattern(
                id=str(body.get("id") or "unknown"),
                fault_class=str(body.get("fault_class") or "other"),
                priority=int(body.get("priority") or 50),
                log_any=[str(x).lower() for x in (body.get("log_any") or [])],
                change_event_types=[
                    str(x).lower() for x in (body.get("change_event_types") or [])
                ],
                force_service=str(body.get("force_service") or ""),
                prefer_service_from_logs=bool(
                    body.get("prefer_service_from_logs", True)
                ),
                prefer_upstream_if_sicker=bool(
                    body.get("prefer_upstream_if_sicker", False)
                ),
                multi_service_shared_root=str(
                    body.get("multi_service_shared_root") or ""
                ),
                root_cause_template=str(body.get("root_cause_template") or "{service}"),
                why=str(body.get("why") or "").strip(),
                runbook=str(body.get("runbook") or "generic"),
                actions=[str(a) for a in (body.get("actions") or [])],
                base_confidence=int(body.get("base_confidence") or 50),
            )
        )
    return PatternCatalog(
        version=str(raw.get("version") or "1.0"),
        metrics=dict(raw.get("metrics") or {}),
        patterns=patterns,
        fallbacks=dict(raw.get("fallbacks") or {}),
        path=path,
    )


@lru_cache(maxsize=4)
def load_pattern_catalog(path: Optional[str] = None) -> PatternCatalog:
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    candidates.extend(default_pattern_paths())

    for p in candidates:
        try:
            if p.is_file():
                if yaml is None:
                    logger.warning("PyYAML missing — using built-in RCA patterns")
                    break
                raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                logger.info("rca pattern catalog loaded path=%s n=%s", p, len(raw.get("patterns") or []))
                return _parse_catalog(raw, path=str(p))
        except Exception as exc:
            logger.warning("rca patterns load failed path=%s err=%s", p, exc)

    logger.warning("rca pattern catalog not found — using built-in")
    return _parse_catalog(_builtin_catalog(), path="builtin")


def clear_pattern_cache() -> None:
    load_pattern_catalog.cache_clear()
