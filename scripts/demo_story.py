#!/usr/bin/env python3
"""
Demo Story — "Slow checkout → payment pressure → AIOps closed loop"

Narrative (say this while the script runs)
-----------------------------------------
1. A customer places an order; checkout calls payment.
2. We inject chaos (errors + latency) — the *customer* feels pain.
3. Hybrid detector fires with an *explainable* sentence
   (e.g. error_rate 3.2σ above EWMA baseline), not a black-box score.
4. Incident Manager opens a correlated ticket.
5. RCA grounds on Prom/Loki/Tempo evidence + Bedrock (or rules).
6. From the Incident Console you click **🔍 Xem Trace** → Grafana Tempo
   with the primary slow/error trace pre-loaded.
7. Remediation proposes actions: low-risk auto, high-risk needs approval.
8. Feedback thumbs train the quality gauges / threshold advisor.

Usage:
  python scripts/demo_story.py
  python scripts/demo_story.py --fast
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def http_json(method: str, url: str, body: dict | None = None, timeout: int = 90):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def say(step: str, msg: str) -> None:
    print(f"\n{'='*60}\n[{step}] {msg}\n{'='*60}")


def main() -> int:
    p = argparse.ArgumentParser(description="AIOps CV demo story runner")
    p.add_argument("--fast", action="store_true", help="Skip long sleeps / load burst")
    p.add_argument("--checkout", default="http://localhost:8080")
    p.add_argument("--detector", default="http://localhost:8001")
    p.add_argument("--incidents", default="http://localhost:8002")
    p.add_argument("--rca", default="http://localhost:8003")
    p.add_argument("--remediation", default="http://localhost:8004")
    p.add_argument("--feedback", default="http://localhost:8005")
    args = p.parse_args()

    say("0", "Health check — full stack must be up (docker compose up -d --build)")
    for name, url in [
        ("checkout", f"{args.checkout}/health"),
        ("detector", f"{args.detector}/health"),
        ("incidents", f"{args.incidents}/health"),
        ("rca", f"{args.rca}/health"),
        ("remediation", f"{args.remediation}/health"),
        ("feedback", f"{args.feedback}/health"),
    ]:
        try:
            http_json("GET", url, timeout=5)
            print(f"  ✓ {name}")
        except Exception as exc:
            print(f"  ✗ {name}: {exc}", file=sys.stderr)
            print("Start stack: docker compose up -d --build", file=sys.stderr)
            return 1

    say(
        "1",
        "STORY: Customer checkout starts failing — inject chaos on checkout-service\n"
        "       (error_rate↑ + latency↑). Production: this is a real dependency blip.",
    )
    print(
        http_json(
            "POST",
            f"{args.checkout}/chaos",
            {"error_rate": 0.45, "extra_latency_ms": 250},
        )
    )

    if not args.fast:
        say("1b", "Generate a few checkout requests so OTLP metrics/traces land in LGTM")
        for i in range(20):
            try:
                http_json(
                    "POST",
                    f"{args.checkout}/checkout",
                    {"order_id": f"story-{i}", "amount": 49.0},
                    timeout=5,
                )
            except Exception:
                pass
            time.sleep(0.1)

    say(
        "2",
        "STORY: AIOps hybrid detector fires — explainable sigma/EWMA (or manual inject).\n"
        "       Why hybrid? Z/EWMA are auditable; IsolationForest catches joint outliers.",
    )
    det = http_json(
        "POST",
        f"{args.detector}/detect",
        {
            "service_name": "checkout-service",
            "metric_name": "http_error_rate",
            "metric_value": 0.45,
            "threshold": 0.15,
        },
    )
    event = det.get("event") if isinstance(det, dict) else det
    if isinstance(event, dict):
        print("  anomaly_id:", event.get("id"))
        print("  explanation:", event.get("message"))
        print("  context.explanation:", (event.get("context") or {}).get("explanation"))

    say("3", "STORY: Incident Manager opens a correlated ticket (noise reduction window)")
    incident = None
    for _ in range(20):
        items = http_json("GET", f"{args.incidents}/incidents?limit=5")
        if isinstance(items, list) and items:
            incident = items[0]
            break
        time.sleep(1.5)
    if not incident:
        print("No incident created", file=sys.stderr)
        return 1
    iid = incident["id"]
    print("  incident_id:", iid)
    print("  title:", incident.get("title"))
    print("  why:", (incident.get("context") or {}).get("explanation") or incident.get("description", "")[:200])

    say(
        "4",
        "STORY: RCA grounds on Prometheus + Loki + Tempo, then Bedrock JSON\n"
        "       (rule fallback if no AWS keys). No free-form hallucination allowed.",
    )
    rca = http_json(
        "POST",
        f"{args.rca}/analyze-incident/{iid}?force=true&persist=true",
        timeout=120,
    )
    print("  mode:", rca.get("mode"), "status:", rca.get("status"))
    root = (rca.get("result") or {}).get("root_cause")
    print("  root_cause:", root)
    print("  suggested_actions:", (rca.get("result") or {}).get("suggested_actions"))

    say(
        "5",
        "STORY: One-click TRACE EXPERIENCE — open Grafana Tempo from the ticket API",
    )
    links = http_json("GET", f"{args.incidents}/incidents/{iid}/observability-links")
    print("  primary_trace_id:", links.get("primary_trace_id"))
    print("  🔍 Xem Trace URL:")
    print("   ", links.get("primary_trace_url") or links.get("service_traces_url"))
    print("  Logs Explore:")
    print("   ", links.get("service_logs_url"))
    print("  explanation:", (links.get("explanation") or "")[:220])

    say(
        "6",
        "STORY: Remediation proposes actions — low risk may auto-run; restart/scale gated",
    )
    actions = http_json(
        "POST",
        f"{args.remediation}/remediate/propose",
        {"incident_id": iid, "actions": []},
    )
    if isinstance(actions, list):
        for a in actions:
            print(
                f"  [{a.get('risk_level')}] {a.get('action_type')} "
                f"status={a.get('status')} → {(a.get('action_text') or '')[:70]}"
            )

    say("7", "STORY: On-call feedback closes the loop (precision / RCA quality metrics)")
    fb = http_json(
        "POST",
        f"{args.feedback}/feedback",
        {
            "incident_id": iid,
            "anomaly_correct": True,
            "rca_useful": True,
            "action_effective": True,
            "comment": "Demo story: payment path pressure after chaos; RCA useful",
            "reviewer": "demo-sre",
        },
    )
    print("  feedback_id:", fb.get("id"))
    print("  quality:", http_json("GET", f"{args.feedback}/stats"))

    say("8", "Reset chaos so the environment is clean for the next take")
    try:
        http_json("POST", f"{args.checkout}/chaos", {"error_rate": 0.01, "extra_latency_ms": 0})
    except Exception:
        pass

    print(
        f"""
{'='*60}
OPEN THESE WHILE YOU NARRATE
{'='*60}
  AIOps Console:     http://localhost:8500   ← primary operator UI
  Incident API UI:   {args.incidents}/
  Ticket JSON:       {args.incidents}/incidents/{iid}
  Observability API: {args.incidents}/incidents/{iid}/observability-links
  🔍 Trace button:   Console → select incident → "Xem Full Trace trong Grafana"
  Remediation UI:    http://localhost:8501
  Feedback UI:       http://localhost:8502
  Grafana:           http://localhost:3000
  Detector metrics:  {args.detector}/metrics
  Feedback metrics:  {args.feedback}/metrics

Talking points for senior interviewers
--------------------------------------
• Explainability first: sigma/EWMA sentences beat opaque ML-only scores.
• Grounded RCA: evidence pack only; structured JSON; rule fallback for safety.
• Trace-first UX: deep-link removes swivel-chair between ticket and Tempo.
• Risk gates: never auto-restart prod without policy / approval.
• Feedback loop: FP rate drives threshold advice — AIOps must observe itself.
"""
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
