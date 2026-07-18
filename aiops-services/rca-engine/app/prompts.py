"""
High-quality Bedrock prompts for grounded RCA.

Design (LLM-for-ops)
--------------------
* **System** locks grounding rules + JSON schema (no free prose).
* **Few-shot** examples teach citation style and low-confidence honesty.
* **User** injects only the EvidencePack JSON (no untrusted free text as truth).
* **Temperature 0.1–0.2**: synthesis over fixed facts, not creative writing.

Example Converse shape
----------------------
system: [SYSTEM_PROMPT]
messages:
  - role=user: few-shot example 1 (evidence → JSON)
  - role=assistant: example JSON 1
  - role=user: few-shot example 2
  - role=assistant: example JSON 2
  - role=user: real incident + EVIDENCE
"""

from __future__ import annotations

import json

from app.models import EvidencePack

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a principal Site Reliability Engineer performing production root-cause analysis (RCA).

## Grounding rules (non-negotiable)
1. Use ONLY facts inside the user-provided EVIDENCE JSON (metrics_summary, neighbor_metrics,
   error_logs, neighbor_logs, traces, topology, change_events, incident fields).
2. NEVER invent deploys, git SHAs, IP addresses, error codes, services, or metric values not present in EVIDENCE.
3. Every claim in root_cause / why_root_cause must be supportable by at least one evidence[] citation.
4. evidence[] items MUST quote concrete observables, e.g.:
   - "metrics: http_error_rate last=0.42 max=0.55"
   - "neighbor_metrics[upstream]: payment-service error_rate=0.5"
   - "log: trace_id=abc... line contains payment timeout"
   - "topology: checkout-service upstream=[payment-service]"
   - "trace: id=... duration_ms=1200 root=checkout-service"
5. If sources_ok shows failures or gather_errors is non-empty, lower confidence and say what is missing.
6. Prefer the simplest causal chain that explains RED metrics + logs + traces + topology together.
7. If evidence is insufficient: set confidence ≤ 35, root_cause starts with "Insufficient evidence:", and still list what was observed.

## Topology rules (critical for multi-service RCA)
8. EVIDENCE.topology.upstream = services the ticket service *calls* (dependencies).
9. If an upstream neighbor has higher error_rate or latency (neighbor_metrics) and/or
   matching error logs (neighbor_logs), prefer that **dependency as root_cause** —
   the ticket service may only be showing cascade *symptoms*.
10. Do NOT blame the ticket service alone when topology + neighbor evidence points upstream.
11. Put both symptom and root services in affected_components when cascading.

## Explainability
- why_root_cause must answer: "Why is THIS the root cause (and not just a symptom)?"
- Reference at least two distinct evidence types when available (e.g. metric + trace, or metric + log).

## Output (strict)
Return ONE JSON object only. No markdown fences. No text before/after.
Schema:
{
  "root_cause": "string",
  "why_root_cause": "string — causal justification citing evidence",
  "confidence": 0,
  "affected_components": ["service names from evidence only"],
  "evidence": ["concrete citations from EVIDENCE"],
  "suggested_actions": ["safe operational steps"],
  "runbook_suggestion": "short runbook id or steps",
  "primary_trace_id": "trace id from EVIDENCE.traces or error_logs.trace_id, or null"
}
- confidence is integer 0–100.
- primary_trace_id must be copied from EVIDENCE when present; never invent a UUID.
- suggested_actions must be reversible/safe (no prod delete without confirmation).
"""

# ---------------------------------------------------------------------------
# Few-shot examples (teach citation + honesty)
# ---------------------------------------------------------------------------

FEW_SHOT_1_USER = """## Task
Produce grounded RCA JSON for this synthetic example.

## EVIDENCE
```json
{
  "incident": {
    "id": "ex-1",
    "service_name": "checkout-service",
    "metric_name": "http_error_rate",
    "metric_value": 0.48,
    "severity": "high",
    "explanation": "http_error_rate=0.48 is 3.1σ above EWMA baseline 0.02"
  },
  "metrics_summary": {
    "instant": {
      "http_error_rate": 0.48,
      "http_request_rate": 12.0,
      "http_latency_p95_seconds": 0.9
    }
  },
  "error_logs": [
    {
      "line": "payment call failed status=503 timeout",
      "trace_id": "aa11bb22cc33dd44ee55ff6677889900",
      "labels": {"service_name": "checkout-service"}
    }
  ],
  "traces": [
    {
      "trace_id": "aa11bb22cc33dd44ee55ff6677889900",
      "root_service": "checkout-service",
      "root_name": "POST /checkout",
      "duration_ms": 2100
    }
  ],
  "sources_ok": {"prometheus": true, "loki": true, "tempo": true},
  "gather_errors": []
}
```
"""

FEW_SHOT_1_ASSISTANT = json.dumps(
    {
        "root_cause": (
            "Checkout failures driven by payment dependency errors "
            "(elevated checkout http_error_rate with 503/timeout logs on payment path)"
        ),
        "why_root_cause": (
            "Metrics show checkout http_error_rate=0.48 with elevated latency p95=0.9s; "
            "error logs explicitly report payment call 503/timeout; the same trace_id "
            "aa11bb22cc33dd44ee55ff6677889900 appears in logs and Tempo with 2100ms duration. "
            "This ties the symptom (checkout errors) to the payment hop rather than a pure "
            "client-side issue."
        ),
        "confidence": 78,
        "affected_components": ["checkout-service", "payment-service"],
        "evidence": [
            "metrics: http_error_rate last=0.48 (incident metric_value=0.48)",
            "metrics: http_latency_p95_seconds=0.9",
            "detector: http_error_rate=0.48 is 3.1σ above EWMA baseline 0.02",
            "log: payment call failed status=503 timeout (trace_id=aa11bb22cc33dd44ee55ff6677889900)",
            "trace: id=aa11bb22cc33dd44ee55ff6677889900 root=checkout-service POST /checkout duration_ms=2100",
        ],
        "suggested_actions": [
            "Inspect payment-service health and recent error rate in Grafana",
            "Open primary trace aa11bb22cc33dd44ee55ff6677889900 in Tempo",
            "If demo chaos is enabled, reset payment/checkout error injection",
        ],
        "runbook_suggestion": "dependency-5xx-on-payment-path",
        "primary_trace_id": "aa11bb22cc33dd44ee55ff6677889900",
    },
    indent=2,
)

FEW_SHOT_2_USER = """## Task
Produce grounded RCA JSON for this synthetic example (thin evidence).

## EVIDENCE
```json
{
  "incident": {
    "id": "ex-2",
    "service_name": "payment-service",
    "metric_name": "http_latency_p95_seconds",
    "metric_value": 0.2,
    "severity": "medium"
  },
  "metrics_summary": {
    "instant": {
      "http_error_rate": null,
      "http_request_rate": null,
      "http_latency_p95_seconds": null
    }
  },
  "error_logs": [],
  "traces": [],
  "sources_ok": {"prometheus": false, "loki": true, "tempo": true},
  "gather_errors": ["prometheus: connection refused"]
}
```
"""

FEW_SHOT_2_ASSISTANT = json.dumps(
    {
        "root_cause": (
            "Insufficient evidence: Prometheus metrics unavailable; cannot validate "
            "latency anomaly for payment-service"
        ),
        "why_root_cause": (
            "sources_ok.prometheus=false and gather_errors report connection refused. "
            "No error_logs or traces were collected. Ticket claims latency metric_value=0.2 "
            "but there is no corroborating time-series, so a confident root cause would be "
            "speculation."
        ),
        "confidence": 25,
        "affected_components": ["payment-service"],
        "evidence": [
            "gather_errors: prometheus: connection refused",
            "sources_ok: prometheus=false",
            "incident: metric_name=http_latency_p95_seconds metric_value=0.2 (uncorroborated)",
            "error_logs: empty",
            "traces: empty",
        ],
        "suggested_actions": [
            "Restore Prometheus/LGTM scrape path and re-run RCA",
            "Generate load against payment-service to populate RED metrics",
        ],
        "runbook_suggestion": "observability-backend-degraded",
        "primary_trace_id": None,
    },
    indent=2,
)

# ---------------------------------------------------------------------------
# Live user prompt
# ---------------------------------------------------------------------------

USER_PROMPT_TEMPLATE = """## Task
Analyze the REAL incident below. Output grounded RCA JSON only (same schema as examples).

## Incident summary
- id: {incident_id}
- service: {service_name}
- title: {title}
- severity: {severity}
- metric_name: {metric_name}
- metric_value: {metric_value}
- threshold: {threshold}
- status: {status}
- created_at: {created_at}
- detector_explanation: {detector_explanation}

## EVIDENCE (grounded — only source of truth)
Time window: last {window_minutes} minutes ({window_start} → {window_end})

```json
{evidence_json}
```

## Checklist before you answer
1. Metrics: cite instant/range values actually present.
2. Logs: cite line text + trace_id when present.
3. Traces: cite trace_id, duration_ms, root_service.
4. Fill why_root_cause with causal reasoning (why not just a symptom).
5. Set primary_trace_id from EVIDENCE only (or null).
6. Output ONLY the JSON object.
"""


def build_user_prompt(pack: EvidencePack) -> str:
    inc = pack.incident or {}
    ctx = inc.get("context") or {}
    return USER_PROMPT_TEMPLATE.format(
        incident_id=pack.incident_id,
        service_name=pack.service_name,
        title=inc.get("title") or "",
        severity=inc.get("severity") or "",
        metric_name=inc.get("metric_name") or "",
        metric_value=inc.get("metric_value"),
        threshold=inc.get("threshold"),
        status=inc.get("status") or "",
        created_at=inc.get("created_at") or "",
        detector_explanation=ctx.get("explanation") or inc.get("description") or "",
        window_minutes=pack.window_minutes,
        window_start=pack.window_start_iso,
        window_end=pack.window_end_iso,
        evidence_json=pack.to_prompt_block(),
    )


def build_messages(pack: EvidencePack) -> list[dict]:
    """
    Bedrock Converse messages: few-shot turns + live user task.

    Few-shot as multi-turn improves schema compliance and citation quality
    vs stuffing examples only into the system prompt.
    """
    return [
        {"role": "user", "content": [{"text": FEW_SHOT_1_USER}]},
        {"role": "assistant", "content": [{"text": FEW_SHOT_1_ASSISTANT}]},
        {"role": "user", "content": [{"text": FEW_SHOT_2_USER}]},
        {"role": "assistant", "content": [{"text": FEW_SHOT_2_ASSISTANT}]},
        {"role": "user", "content": [{"text": build_user_prompt(pack)}]},
    ]
