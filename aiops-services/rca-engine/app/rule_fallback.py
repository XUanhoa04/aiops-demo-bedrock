"""
Deterministic rule-based RCA when Bedrock is unavailable, fails, or low-confidence.

Uses the same EvidencePack so fallback stays grounded (template fill only).
"""

from __future__ import annotations

from app.models import EvidencePack, RCAResult


def rule_based_rca(pack: EvidencePack) -> RCAResult:
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

    inc = pack.incident or {}
    ctx = inc.get("context") or {}
    if ctx.get("explanation"):
        evidence.append(f"detector: {ctx.get('explanation')}")
    if inc.get("metric_name") and inc.get("metric_value") is not None:
        evidence.append(
            f"incident: metric {inc.get('metric_name')}={inc.get('metric_value')} "
            f"threshold={inc.get('threshold')}"
        )
        if "error" in str(inc.get("metric_name")):
            try:
                mv = float(inc["metric_value"])
                if mv >= 0.2:
                    conf = max(conf, 55)
                    cause_bits.append(
                        f"elevated error rate on {service} "
                        f"({inc.get('metric_name')}={mv})"
                    )
                    why_bits.append(
                        f"Ticket metric {inc.get('metric_name')}={mv} is well above "
                        f"threshold {inc.get('threshold')}; treating this as a real "
                        f"error-path regression on {service}."
                    )
                    actions.append(
                        f"Check chaos/error injection on {service} (POST /chaos) and reset if demo"
                    )
                    actions.append("Inspect upstream dependency health (payment vs checkout)")
                    runbook = "elevated-http-error-rate"
            except (TypeError, ValueError):
                pass

    if pack.error_logs:
        sample = pack.error_logs[0]
        line = sample.get("line") or ""
        tid = sample.get("trace_id")
        evidence.append(
            f"log: {line[:180]}" + (f" (trace_id={tid})" if tid else "")
        )
        if tid and not primary_trace:
            primary_trace = str(tid)
        conf = max(conf, conf + 10)
        # Keyword → production-like root causes (also used by offline evaluation dataset)
        log_blob = " ".join(
            str(row.get("line") or "") for row in pack.error_logs[:20]
        ).lower()
        if "connection pool" in log_blob or "db_pool" in log_blob or "pool exhausted" in log_blob:
            cause_bits.append(
                f"{service} database connection pool exhaustion"
            )
            why_bits.append(
                "Error logs mention connection pool exhaustion — classic DB pool "
                "saturation under load or leaked connections."
            )
            conf = max(conf, 65)
            runbook = "db-connection-pool"
            actions.append(f"Increase pool size / fix connection leak on {service}")
            actions.append("Reset demo chaos fault_mode=db_pool if injected")
        elif "cache miss" in log_blob or "redis" in log_blob and "cache" in log_blob:
            cause_bits.append(
                f"{service} high latency due to cache miss / cold redis keyspace"
            )
            why_bits.append(
                "Logs indicate cache miss storm; origin/DB path is hit repeatedly."
            )
            conf = max(conf, 60)
            runbook = "cache-miss-latency"
            actions.append("Warm cache / check redis availability")
        elif "gateway timeout" in log_blob or "payment gateway" in log_blob:
            cause_bits.append("payment gateway timeout / dependency failure")
            why_bits.append(
                "Logs show payment gateway timeouts cascading into checkout errors."
            )
            conf = max(conf, 62)
            if "payment-service" not in affected:
                affected.append("payment-service")
            runbook = "dependency-timeout"
            actions.append("Check payment-service chaos / gateway health")
        elif "cpu" in log_blob or "thread pool" in log_blob or "throttl" in log_blob:
            cause_bits.append(f"{service} worker/thread pool saturation or CPU throttle")
            why_bits.append(
                "Logs indicate worker saturation; latency rises before hard errors."
            )
            conf = max(conf, 55)
            runbook = "cpu-saturation"
        else:
            cause_bits.append("error-pattern logs present in Loki window")
            why_bits.append(
                "Loki returned error-class log lines in the evidence window, supporting "
                "that the service is actively failing rather than a pure metric glitch."
            )
        actions.append("Open correlated logs/trace in Grafana Explore")
    else:
        evidence.append("log: no error lines in window (or Loki empty)")

    if pack.traces:
        slow = sorted(
            pack.traces,
            key=lambda t: (t.get("duration_ms") or 0),
            reverse=True,
        )[0]
        primary_trace = primary_trace or slow.get("trace_id")
        evidence.append(
            f"trace: id={slow.get('trace_id')} root={slow.get('root_service')}/"
            f"{slow.get('root_name')} duration_ms={slow.get('duration_ms')}"
        )
        conf = max(conf, conf + 5)
        if (slow.get("duration_ms") or 0) >= 500:
            cause_bits.append(
                f"slow traces observed (~{slow.get('duration_ms')}ms)"
            )
            why_bits.append(
                f"Tempo shows elevated span duration ({slow.get('duration_ms')}ms) on "
                f"{slow.get('root_service')}, consistent with a dependency or internal "
                f"slow path rather than a pure client retry storm."
            )
            runbook = "latency-or-dependency-slowness"
            actions.append(
                f"Open primary trace {slow.get('trace_id')} in Grafana Tempo"
            )
        roots = {t.get("root_service") for t in pack.traces if t.get("root_service")}
        for r in roots:
            if r and r != service and r not in affected:
                affected.append(str(r))
    else:
        evidence.append("trace: no matching traces in window")

    if pack.gather_errors:
        evidence.append("gather_errors: " + "; ".join(pack.gather_errors[:5]))
        conf = min(conf, 40)
        why_bits.append(
            "One or more evidence backends failed; confidence is capped because "
            "corroboration is incomplete."
        )

    if not cause_bits:
        if err is not None and err >= 0.1:
            cause_bits.append(f"elevated http_error_rate={err:.4g} on {service}")
            why_bits.append(
                f"Prometheus instant http_error_rate={err:.4g} exceeds 0.1 on {service}."
            )
            conf = max(conf, 50)
            runbook = "elevated-http-error-rate"
        elif lat is not None and lat >= 0.5:
            cause_bits.append(f"high latency p95={lat:.4g}s on {service}")
            why_bits.append(
                f"Prometheus instant latency p95={lat:.4g}s is above 0.5s safety threshold."
            )
            conf = max(conf, 45)
            runbook = "latency-or-dependency-slowness"
        else:
            cause_bits.append(
                f"Insufficient evidence: cannot pin root cause for {service}; "
                f"severity={inc.get('severity')}"
            )
            why_bits.append(
                "Available metrics/logs/traces do not form a corroborated causal chain."
            )
            conf = min(conf, 30)
            actions.append("Re-run RCA after generating load so Prom/Loki/Tempo fill")

    if not actions:
        actions = [
            f"Open Grafana for {service}",
            "Verify Prometheus/Loki/Tempo have data in the incident window",
            "Re-run POST /analyze-incident/{id}",
        ]

    conf = int(max(5, min(70, conf)))
    why = " ".join(why_bits) if why_bits else (
        "Rule-based heuristic selected the simplest hypothesis consistent with "
        "available grounded metrics/logs/traces."
    )

    return RCAResult(
        root_cause="; ".join(cause_bits),
        why_root_cause=why,
        confidence=conf,
        affected_components=affected,
        evidence=evidence[:12],
        suggested_actions=actions[:8],
        runbook_suggestion=runbook,
        primary_trace_id=str(primary_trace) if primary_trace else None,
    )
