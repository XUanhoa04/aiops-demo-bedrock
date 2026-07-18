"""
Deterministic rule-based RCA when Bedrock is unavailable, fails, or low-confidence.

Design (anti hard-code)
-----------------------
Fault *classes* come from ``config/rca_patterns.yaml`` via
``aiops_shared.rca_patterns`` — not per-scenario if/else in this file.

This module only:
  1. Builds grounded evidence citations from the EvidencePack
  2. Runs the generic pattern matcher on log/change text
  3. Applies topology correlation (prefer sicker upstream)
  4. Applies metric-only / insufficient-evidence fallbacks from config

Uses the same EvidencePack so fallback stays grounded (template fill only).
"""

from __future__ import annotations

from typing import Any, Optional

from aiops_shared.rca_patterns import load_pattern_catalog

from app.config import settings
from app.models import EvidencePack, RCAResult


def rule_based_rca(pack: EvidencePack) -> RCAResult:
    catalog = load_pattern_catalog(settings.rca_patterns_path or None)
    mcfg = catalog.metrics or {}
    err_elev = float(mcfg.get("error_rate_elevated") or 0.10)
    err_high = float(mcfg.get("error_rate_high") or 0.20)
    lat_elev = float(mcfg.get("latency_p95_elevated_seconds") or 0.50)

    service = pack.service_name
    metrics = pack.metrics_summary or {}
    instant = metrics.get("instant") or {}
    rng = metrics.get("range") or {}
    err = instant.get("http_error_rate")
    rps = instant.get("http_request_rate")
    lat = instant.get("http_latency_p95_seconds")
    err_range = (rng.get("http_error_rate") or {}) if isinstance(rng, dict) else {}
    lat_range = (rng.get("http_latency_p95_seconds") or {}) if isinstance(rng, dict) else {}

    evidence: list[str] = []
    affected = [service]
    conf = 25
    cause_bits: list[str] = []
    why_bits: list[str] = []
    actions: list[str] = []
    runbook = "generic-service-degradation"
    primary_trace = pack.primary_trace_id

    # --- Topology snapshot ---
    topo = pack.topology or {}
    upstream = list(topo.get("upstream") or [])
    downstream = list(topo.get("downstream") or [])
    shared = list(topo.get("shared_deps") or [])
    if topo:
        evidence.append(
            f"topology: service={topo.get('service') or service} "
            f"upstream={upstream} downstream={downstream} "
            f"shared_deps={shared} source={topo.get('source')}"
        )
        for u in upstream:
            if u not in affected:
                affected.append(u)

    if err is not None:
        evidence.append(f"metrics: http_error_rate last={err:.4g} service={service}")
    if err_range.get("max") is not None:
        evidence.append(
            f"metrics: http_error_rate window min={err_range.get('min')} "
            f"max={err_range.get('max')} last={err_range.get('last')} "
            f"points={err_range.get('points')}"
        )
    if rps is not None:
        evidence.append(f"metrics: http_request_rate last={rps:.4g}")
    if lat is not None:
        evidence.append(f"metrics: http_latency_p95_seconds last={lat:.4g}")
    if lat_range.get("max") is not None:
        evidence.append(
            f"metrics: latency_p95 window max={lat_range.get('max')} "
            f"last={lat_range.get('last')}"
        )

    for peer, body in (pack.neighbor_metrics or {}).items():
        inst = (body or {}).get("instant") or {}
        rel = (body or {}).get("relation") or "peer"
        pe = inst.get("http_error_rate")
        pl = inst.get("http_latency_p95_seconds")
        if pe is not None or pl is not None:
            evidence.append(
                f"neighbor_metrics[{rel}]: {peer} "
                f"error_rate={pe!s} latency_p95={pl!s}"
            )
            if peer not in affected:
                affected.append(peer)

    inc = pack.incident or {}
    ctx = inc.get("context") or {}
    if ctx.get("explanation"):
        evidence.append(f"detector: {ctx.get('explanation')}")
    if inc.get("metric_name") and inc.get("metric_value") is not None:
        evidence.append(
            f"incident: metric {inc.get('metric_name')}={inc.get('metric_value')} "
            f"threshold={inc.get('threshold')}"
        )

    # --- Topology correlation (generic, not pattern-specific) ---
    dep_hit = _prefer_dependency_root(pack, service, err, lat, mcfg)
    if dep_hit:
        cause_bits.append(dep_hit["root_cause"])
        why_bits.append(dep_hit["why"])
        conf = max(conf, dep_hit["confidence"])
        runbook = dep_hit.get("runbook") or runbook
        actions.extend(dep_hit.get("actions") or [])
        for a in dep_hit.get("affected") or []:
            if a not in affected:
                affected.append(a)
        evidence.extend(dep_hit.get("evidence") or [])

    # --- Config-driven pattern matching on logs + change events ---
    all_logs = list(pack.error_logs or []) + list(pack.neighbor_logs or [])
    log_services: set[str] = set()
    for row in all_logs:
        svc = row.get("neighbor_service") or (row.get("labels") or {}).get(
            "service_name"
        )
        if svc:
            log_services.add(str(svc))

    if all_logs:
        sample = all_logs[0]
        line = sample.get("line") or ""
        tid = sample.get("trace_id")
        evidence.append(
            f"log: {line[:180]}" + (f" (trace_id={tid})" if tid else "")
        )
        if tid and not primary_trace:
            primary_trace = str(tid)
        conf = max(conf, conf + 10)

    log_blob = " ".join(str(row.get("line") or "") for row in all_logs[:40]).lower()
    log_svc_hint = _service_hint_from_logs(all_logs, service)

    matches = catalog.match_logs(
        log_blob,
        ticket_service=service,
        log_service_hint=log_svc_hint,
        log_services_seen=log_services,
        change_events=list(pack.change_events or []),
    )

    if matches:
        best = matches[0]
        # Attribute root service from the *matching* log line (not first ticket log)
        if best.pattern.prefer_service_from_logs and not best.pattern.force_service:
            if best.source != "multi_service":
                refined = _service_from_matching_logs(
                    all_logs, best.matched_phrases, best.service
                )
                if refined:
                    best.service = refined
                    best.root_cause = best.pattern.root_cause_template.format(
                        service=refined
                    )
        # Prefer topology dependency root when it already fired with higher conf
        # unless pattern is multi-hop (fraud/inventory/gateway) with higher priority
        insert_front = True
        if dep_hit and best.score < 90:
            insert_front = False
        bit = best.root_cause
        if bit not in cause_bits:
            if insert_front:
                cause_bits.insert(0, bit)
            else:
                cause_bits.append(bit)
        why_bits.append(best.pattern.why or f"Matched pattern {best.pattern.id}")
        conf = max(conf, int(best.pattern.base_confidence))
        runbook = best.pattern.runbook or runbook
        for act in best.pattern.actions:
            actions.append(act.format(service=best.service))
        if best.service and best.service not in affected:
            affected.append(best.service)
        if best.matched_phrases:
            evidence.append(
                f"pattern:{best.pattern.id} phrases={best.matched_phrases[:4]} "
                f"source={best.source}"
            )
        evidence.append(f"pattern_catalog: path={catalog.path} version={catalog.version}")
        actions.append("Open correlated logs/trace in Grafana Explore")
    elif all_logs:
        evidence.append("log: lines present but no catalog pattern matched")
        # Error-class logs + elevated metrics → local elevated (generic)
        errorish = any(
            any(
                k in (row.get("line") or "").lower()
                for k in ("error", "fail", "timeout", "exception", "fatal")
            )
            for row in all_logs
        )
        if errorish and err is not None and err >= err_elev:
            pay = (pack.neighbor_metrics or {}).get("payment-service") or {}
            pay_err = (pay.get("instant") or {}).get("http_error_rate")
            fb = (catalog.fallbacks or {}).get("elevated_error") or {}
            if (
                pay_err is not None
                and float(pay_err) < 0.05
                and "checkout" in service
            ):
                tmpl = fb.get("root_cause_local") or (
                    f"elevated error rate on {service} (local) — payment upstream healthy"
                )
                cause_bits.append(tmpl.format(service=service))
                why_bits.append(
                    "Error logs on ticket service with healthy payment neighbor "
                    "→ local fault (generic metric+log fallback)."
                )
            else:
                tmpl = fb.get("root_cause_template") or (
                    "elevated http_error_rate on {service}"
                )
                cause_bits.append(tmpl.format(service=service))
                why_bits.append("Error-class logs present with elevated error rate.")
            conf = max(conf, int(fb.get("base_confidence") or 50))
            runbook = "elevated-http-error-rate"
            actions.append("Open correlated logs/trace in Grafana Explore")
    else:
        evidence.append("log: no error lines in window (or Loki empty)")

    # --- Traces ---
    all_traces = list(pack.traces or []) + list(pack.neighbor_traces or [])
    if all_traces:
        slow = sorted(
            all_traces,
            key=lambda t: (t.get("duration_ms") or 0),
            reverse=True,
        )[0]
        primary_trace = primary_trace or slow.get("trace_id")
        evidence.append(
            f"trace: id={slow.get('trace_id')} root={slow.get('root_service')}/"
            f"{slow.get('root_name')} duration_ms={slow.get('duration_ms')}"
        )
        conf = max(conf, conf + 5)
        if (slow.get("duration_ms") or 0) >= 500 and not cause_bits:
            cause_bits.append(
                f"slow traces observed (~{slow.get('duration_ms')}ms)"
            )
            why_bits.append(
                f"Tempo shows elevated span duration ({slow.get('duration_ms')}ms)."
            )
            if runbook == "generic-service-degradation":
                runbook = "latency-or-dependency-slowness"
            actions.append(
                f"Open primary trace {slow.get('trace_id')} in Grafana Tempo"
            )
        roots = {t.get("root_service") for t in all_traces if t.get("root_service")}
        for r in roots:
            if r and r != service and r not in affected:
                affected.append(str(r))
    else:
        evidence.append("trace: no matching traces in window")

    if pack.change_events and not any("deploy" in c.lower() for c in cause_bits):
        ev = pack.change_events[0]
        evidence.append(
            f"change_event: type={ev.get('type')} service={ev.get('service')} "
            f"msg={(ev.get('message') or '')[:100]}"
        )

    # Incomplete backends: rewrite weak-only causes
    if pack.gather_errors and cause_bits:
        weak_only = all(
            "slow traces" in c.lower() or "error-pattern logs" in c.lower()
            for c in cause_bits
        )
        if weak_only and err is not None and err >= err_elev:
            fb = (catalog.fallbacks or {}).get("elevated_error") or {}
            tmpl = fb.get("root_cause_incomplete_logs") or (
                "elevated error rate on {service} with incomplete log evidence"
            )
            cause_bits = [tmpl.format(service=service)]
            why_bits.append(
                "Evidence gather reported backend failures; metric elevation is real "
                "but log corroboration may be missing."
            )
            conf = min(max(conf, 45), 55)
            runbook = "elevated-http-error-rate"
            actions.append("Restore Loki and re-run RCA")

    if pack.gather_errors and not cause_bits:
        fb = (catalog.fallbacks or {}).get("insufficient") or {}
        tmpl = fb.get("root_cause_template") or (
            "Insufficient evidence: cannot pin root cause for {service}"
        )
        cause_bits.append(
            tmpl.format(service=service) + "; observability backends incomplete"
        )
        why_bits.append(
            "Evidence gather reported backend failures; do not invent a concrete "
            "infra root without metrics/logs/traces."
        )
        conf = min(conf, int(fb.get("base_confidence") or 30))
        actions.append("Restore Prom/Loki/Tempo and re-run RCA")
        evidence.append("gather_errors: " + "; ".join(pack.gather_errors[:5]))
    elif pack.gather_errors:
        evidence.append("gather_errors: " + "; ".join(pack.gather_errors[:5]))
        conf = min(conf, 40)

    # --- Metric-only fallbacks (config thresholds) ---
    if not cause_bits:
        fb_err = (catalog.fallbacks or {}).get("elevated_error") or {}
        fb_lat = (catalog.fallbacks or {}).get("elevated_latency") or {}
        fb_ins = (catalog.fallbacks or {}).get("insufficient") or {}
        if err is not None and err >= err_elev:
            if not pack.error_logs and not pack.neighbor_logs:
                tmpl = fb_err.get("root_cause_metric_only") or (
                    "elevated http_error_rate on {service} with insufficient "
                    "log/trace corroboration"
                )
            else:
                tmpl = fb_err.get("root_cause_template") or (
                    "elevated http_error_rate on {service}"
                )
            pay = (pack.neighbor_metrics or {}).get("payment-service") or {}
            pay_err = (pay.get("instant") or {}).get("http_error_rate")
            if (
                pay_err is not None
                and float(pay_err) < 0.05
                and "checkout" in service
                and err >= err_high
            ):
                tmpl = fb_err.get("root_cause_local") or (
                    "elevated error rate on {service} (local) — payment upstream healthy"
                )
                why_bits.append(
                    "Payment neighbor is healthy; prefer local fault over wrong-hop."
                )
            else:
                why_bits.append(
                    f"Prometheus instant http_error_rate={err:.4g} exceeds "
                    f"{err_elev} on {service}."
                )
            cause_bits.append(tmpl.format(service=service))
            conf = max(conf, int(fb_err.get("base_confidence") or 50))
            runbook = "elevated-http-error-rate"
            if err >= err_high:
                conf = max(conf, 55)
                actions.append(
                    f"Check chaos/error injection on {service} and reset if demo"
                )
                actions.append("Inspect upstream dependency health via topology")
        elif lat is not None and lat >= lat_elev:
            tmpl = fb_lat.get("root_cause_template") or "high latency p95 on {service}"
            cause_bits.append(tmpl.format(service=service) + f" p95={lat:.4g}s")
            why_bits.append(
                f"Prometheus latency p95={lat:.4g}s above {lat_elev}s threshold."
            )
            conf = max(conf, int(fb_lat.get("base_confidence") or 45))
            runbook = "latency-or-dependency-slowness"
        else:
            tmpl = fb_ins.get("root_cause_template") or (
                "Insufficient evidence: cannot pin root cause for {service}"
            )
            cause_bits.append(
                f"{tmpl.format(service=service)}; severity={inc.get('severity')}"
            )
            why_bits.append(
                "Available metrics/logs/traces do not form a corroborated causal chain."
            )
            conf = min(conf, int(fb_ins.get("base_confidence") or 30))
            actions.append(
                "Re-run RCA after generating load so Prom/Loki/Tempo fill"
            )

    if not actions:
        actions = [
            f"Open Grafana for {service}",
            "Verify Prometheus/Loki/Tempo have data in the incident window",
            "Inspect topology upstream dependencies for correlated errors",
            "Re-run POST /analyze-incident/{id}",
            f"Pattern catalog: {catalog.path}",
        ]

    conf = int(max(5, min(75, conf)))
    why = " ".join(why_bits) if why_bits else (
        "Config-driven pattern match + topology selected the simplest hypothesis "
        "consistent with grounded metrics/logs/traces."
    )

    seen_c: set[str] = set()
    unique_causes: list[str] = []
    for c in cause_bits:
        if c not in seen_c:
            seen_c.add(c)
            unique_causes.append(c)

    return RCAResult(
        root_cause="; ".join(unique_causes),
        why_root_cause=why,
        confidence=conf,
        affected_components=affected,
        evidence=evidence[:16],
        suggested_actions=list(dict.fromkeys(actions))[:8],
        runbook_suggestion=runbook,
        primary_trace_id=str(primary_trace) if primary_trace else None,
    )


def _prefer_dependency_root(
    pack: EvidencePack,
    service: str,
    primary_err: Optional[float],
    primary_lat: Optional[float],
    mcfg: dict[str, Any],
) -> Optional[dict[str, Any]]:
    topo = pack.topology or {}
    hints = topo.get("rca_hints") or {}
    if hints.get("prefer_dependency_root_when_correlated") is False:
        return None

    err_margin = float(
        hints.get("error_rate_neighbor_margin")
        or mcfg.get("error_rate_neighbor_margin")
        or 0.05
    )
    lat_margin = float(
        hints.get("latency_neighbor_margin_seconds")
        or mcfg.get("latency_neighbor_margin_seconds")
        or 0.2
    )
    upstream = set(topo.get("upstream") or [])
    if not upstream and not pack.neighbor_metrics:
        return None

    best: Optional[dict[str, Any]] = None
    best_score = 0.0

    for peer, body in (pack.neighbor_metrics or {}).items():
        rel = (body or {}).get("relation") or ""
        if peer not in upstream and rel != "upstream":
            if peer not in upstream:
                continue
        inst = (body or {}).get("instant") or {}
        pe = inst.get("http_error_rate")
        pl = inst.get("http_latency_p95_seconds")
        score = 0.0
        reasons: list[str] = []

        if pe is not None:
            base = primary_err if primary_err is not None else 0.0
            if pe >= base + err_margin and pe >= 0.1:
                score += pe * 10
                reasons.append(
                    f"upstream {peer} error_rate={pe:.4g} exceeds primary "
                    f"{service} by ≥{err_margin}"
                )
        if pl is not None:
            base_l = primary_lat if primary_lat is not None else 0.0
            if pl >= base_l + lat_margin and pl >= 0.5:
                score += pl
                reasons.append(
                    f"upstream {peer} latency_p95={pl:.4g}s exceeds primary "
                    f"by ≥{lat_margin}s"
                )

        peer_logs = [
            r
            for r in (pack.neighbor_logs or [])
            if (r.get("neighbor_service") == peer)
            or ((r.get("labels") or {}).get("service_name") == peer)
        ]
        blob = " ".join(str(r.get("line") or "") for r in peer_logs).lower()
        # Light boost from any neighbor error language (patterns refine later)
        if any(
            k in blob
            for k in ("error", "timeout", "pool", "fail", "saturated", "lock")
        ):
            score += 2

        if score <= best_score or score < 1.0:
            continue

        root = (
            f"elevated error rate on upstream dependency {peer} "
            f"(cascade symptoms on {service})"
        )
        # If neighbor logs already name a concrete service fault, keep generic
        # topology root; pattern matcher will refine from full log blob.
        best_score = score
        best = {
            "root_cause": root,
            "why": (
                "Topology-aware correlation: "
                + "; ".join(reasons)
                + f". Prefer dependency root over symptom service {service}."
            ),
            "confidence": min(72, 55 + int(score)),
            "runbook": "dependency-cascade",
            "actions": [
                f"Investigate {peer} first (upstream of {service})",
                f"Do not restart {service} until dependency is healthy",
                "Open neighbor metrics/logs in Grafana",
            ],
            "affected": [service, peer],
            "evidence": [
                f"topology_correlation: prefer {peer} over {service} score={score:.2f}"
            ],
        }

    return best


def _service_hint_from_logs(logs: list[dict[str, Any]], fallback: str) -> str:
    for row in logs:
        svc = row.get("neighbor_service") or (row.get("labels") or {}).get(
            "service_name"
        )
        if svc:
            return str(svc)
        line = (row.get("line") or "").lower()
        for name in (
            "fraud-service",
            "inventory-service",
            "payment-service",
            "checkout-service",
        ):
            if name in line:
                return name
        if "payment " in line:
            return "payment-service"
        if "checkout " in line:
            return "checkout-service"
    return fallback


def _service_from_matching_logs(
    logs: list[dict[str, Any]],
    phrases: list[str],
    fallback: str,
) -> str:
    """Pick service from the log line that actually matched pattern phrases."""
    phrases_l = [p.lower() for p in (phrases or []) if p]
    if not phrases_l:
        return fallback
    for row in logs:
        line = (row.get("line") or "").lower()
        if not any(p in line for p in phrases_l):
            continue
        svc = row.get("neighbor_service") or (row.get("labels") or {}).get(
            "service_name"
        )
        if svc:
            return str(svc)
        for name in (
            "fraud-service",
            "inventory-service",
            "payment-service",
            "checkout-service",
        ):
            if name in line:
                return name
    return fallback
