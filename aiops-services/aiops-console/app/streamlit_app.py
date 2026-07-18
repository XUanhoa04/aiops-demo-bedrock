"""
AIOps Console — production-style Streamlit control plane.

Tabs
----
1. Incidents   — pipeline workbench (list → detail with Trace/Logs/RCA/Feedback)
2. AIOps Health — self-monitoring of the engine (quality + service health)

Run:
  streamlit run app/streamlit_app.py --server.port 8500
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote

import pandas as pd
import streamlit as st

from app import clients as api

st.set_page_config(
    page_title="AIOps Console",
    page_icon="🛰️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Theme / chrome
# ---------------------------------------------------------------------------

st.markdown(
    """
<style>
  /* Enterprise dark ops console */
  .block-container { padding-top: 1.2rem; padding-bottom: 2rem; max-width: 1400px; }
  div[data-testid="stMetric"] {
    background: linear-gradient(180deg, #121a2b 0%, #0d1422 100%);
    border: 1px solid #243049;
    border-radius: 12px;
    padding: 12px 14px;
  }
  div[data-testid="stMetric"] label { color: #9fb0d0 !important; }
  .aiops-banner {
    background: linear-gradient(90deg, rgba(91,157,255,0.14), rgba(124,92,255,0.12));
    border: 1px solid #2a3a5c;
    border-radius: 14px;
    padding: 14px 18px;
    margin-bottom: 14px;
    color: #c9d7f5;
    font-size: 0.95rem;
    line-height: 1.45;
  }
  .aiops-banner strong { color: #e8eefc; }
  .sev-critical { color: #ff5c7a; font-weight: 700; }
  .sev-high { color: #ff9f43; font-weight: 700; }
  .sev-medium { color: #f6c945; font-weight: 700; }
  .sev-low { color: #3dd68c; font-weight: 700; }
  .pill {
    display: inline-block; padding: 2px 10px; border-radius: 999px;
    border: 1px solid #334155; background: #0f172a; font-size: 0.78rem;
    margin-right: 6px;
  }
  .section-card {
    border: 1px solid #243049; border-radius: 14px;
    background: #0f1628; padding: 16px 18px; margin-bottom: 12px;
  }
  .muted { color: #9fb0d0; font-size: 0.88rem; }
  .pipeline {
    display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    font-size: 0.85rem; color: #9fb0d0; margin: 8px 0 4px;
  }
  .pipeline span.step {
    background: #141e33; border: 1px solid #2a3a5c; border-radius: 8px;
    padding: 6px 10px; color: #e8eefc;
  }
  .pipeline span.arrow { color: #5b9dff; }
</style>
""",
    unsafe_allow_html=True,
)


def _sev_html(sev: str) -> str:
    s = (sev or "medium").lower()
    return f'<span class="sev-{s}">{s.upper()}</span>'


def _fmt_ts(iso: Any) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except Exception:
        return str(iso)[:19]


def _thumb_choice(label: str) -> Optional[bool]:
    if label.startswith("👍"):
        return True
    if label.startswith("👎"):
        return False
    return None


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### 🛰️ AIOps Console")
    st.caption("Internal control plane · detect → ticket → RCA → remediate → feedback")
    reviewer = st.text_input("Operator", value=os.getenv("DEFAULT_REVIEWER", "oncall"))
    st.divider()
    st.markdown("**Pipeline**")
    st.markdown(
        """
<div class="pipeline">
  <span class="step">1 Detect</span><span class="arrow">→</span>
  <span class="step">2 Ticket</span><span class="arrow">→</span>
  <span class="step">3 RCA</span><span class="arrow">→</span>
  <span class="step">4 Action</span><span class="arrow">→</span>
  <span class="step">5 Feedback</span>
</div>
""",
        unsafe_allow_html=True,
    )
    st.divider()
    if st.button("↻ Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.caption("Backends")
    for name, base in [
        ("Incident Manager", api.IM),
        ("RCA Engine", api.RCA),
        ("Remediation", api.REM),
        ("Feedback", api.FB),
        ("Detector", api.DET),
    ]:
        h = api.health(base)
        ok = h.get("status") in ("ok", "degraded")
        st.write(f"{'🟢' if ok else '🔴'} {name}")


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_incidents, tab_health = st.tabs(["📋 Incidents", "📈 AIOps Health"])


# ========================= INCIDENTS =======================================

with tab_incidents:
    st.markdown(
        """
<div class="aiops-banner">
  <strong>Operator workbench.</strong>
  Select an incident to inspect detector explainability, Bedrock RCA evidence,
  one-click Grafana Tempo/Logs, suggested actions, and submit on-call feedback.
</div>
""",
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns([1.2, 1, 1])
    with c1:
        status_f = st.selectbox(
            "Status filter",
            [
                "",
                "open",
                "acknowledged",
                "investigating",
                "remediating",
                "resolved",
                "closed",
                "false_positive",
            ],
            format_func=lambda x: "all statuses" if x == "" else x,
        )
    with c2:
        service_f = st.text_input("Service contains", "")
    with c3:
        limit = st.slider("Limit", 10, 100, 40)

    try:
        incidents = api.list_incidents(limit=limit, status=status_f or None)
    except Exception as exc:
        st.error(f"Failed to load incidents: {exc}")
        incidents = []

    if service_f.strip():
        q = service_f.strip().lower()
        incidents = [
            i for i in incidents if q in str(i.get("service_name") or "").lower()
        ]

    if not incidents:
        st.info(
            "No incidents yet. Run `python scripts/demo_story.py` or inject chaos + detect."
        )
    else:
        # Summary strip
        m1, m2, m3, m4 = st.columns(4)
        openish = sum(
            1
            for i in incidents
            if i.get("status")
            in ("open", "acknowledged", "investigating", "remediating")
        )
        with_rca = sum(1 for i in incidents if i.get("root_cause"))
        crit = sum(1 for i in incidents if i.get("severity") in ("critical", "high"))
        m1.metric("Loaded", len(incidents))
        m2.metric("Open-ish", openish)
        m3.metric("With RCA", with_rca)
        m4.metric("High/Critical", crit)

        # Table
        rows = []
        for i in incidents:
            rows.append(
                {
                    "id": i.get("id"),
                    "severity": (i.get("severity") or "").upper(),
                    "status": i.get("status"),
                    "service": i.get("service_name"),
                    "title": (i.get("title") or "")[:80],
                    "RCA": "✓" if i.get("root_cause") else "—",
                    "created": _fmt_ts(i.get("created_at")),
                }
            )
        df = pd.DataFrame(rows)
        st.dataframe(
            df.drop(columns=["id"]),
            use_container_width=True,
            hide_index=True,
            height=min(420, 48 + 35 * len(df)),
        )

        labels = [
            f"{(i.get('severity') or '?').upper()} · {i.get('status')} · "
            f"{i.get('service_name')} · {(i.get('title') or '')[:55]} · {str(i.get('id',''))[:8]}"
            for i in incidents
        ]
        idx = st.selectbox(
            "Open incident detail",
            options=list(range(len(labels))),
            format_func=lambda i: labels[i],
        )
        selected = incidents[idx]
        iid = selected.get("id") or ""

        # ---- Detail ----
        st.markdown("---")
        try:
            inc = api.get_incident(iid)
        except Exception as exc:
            st.error(f"Load incident failed: {exc}")
            st.stop()

        try:
            links = api.observability_links(iid)
        except Exception:
            links = {}

        notes = api.parse_notes(inc.get("remediation_notes"))
        expl = api.explanation_of(inc)

        # Header
        h_l, h_r = st.columns([2.2, 1])
        with h_l:
            st.markdown(
                f"### {inc.get('title') or iid}\n"
                f"<span class='pill'>{_sev_html(inc.get('severity') or '')}</span>"
                f"<span class='pill'>{inc.get('status')}</span>"
                f"<span class='pill'>{inc.get('service_name')}</span>"
                f"<span class='muted'> · {iid}</span>",
                unsafe_allow_html=True,
            )
            st.caption(f"Created {_fmt_ts(inc.get('created_at'))} · Updated {_fmt_ts(inc.get('updated_at'))}")
        with h_r:
            trace_url = links.get("primary_trace_url") or notes.get("grafana_trace_url")
            logs_url = links.get("service_logs_url") or notes.get("grafana_logs_url")
            svc_traces = links.get("service_traces_url") or notes.get(
                "grafana_service_traces_url"
            )
            if not trace_url and svc_traces:
                trace_url = svc_traces
            if not trace_url:
                # Last resort: build classic Explore left= link
                tid = links.get("primary_trace_id") or notes.get("primary_trace_id")
                if tid:
                    left = quote(
                        '{"datasource":"Tempo","queries":[{"refId":"A","queryType":"traceql","query":"'
                        + tid
                        + '"}]}',
                        safe="",
                    )
                    trace_url = f"http://localhost:3000/explore?orgId=1&left={left}"

            b1, b2 = st.columns(2)
            if trace_url:
                b1.link_button(
                    "🔍 Xem Full Trace trong Grafana",
                    trace_url,
                    use_container_width=True,
                    type="primary",
                )
            else:
                b1.button("🔍 Trace (unavailable)", disabled=True, use_container_width=True)
            if logs_url:
                b2.link_button(
                    "📜 Xem Logs liên quan",
                    logs_url,
                    use_container_width=True,
                )
            else:
                b2.button("📜 Logs (unavailable)", disabled=True, use_container_width=True)

            tid = links.get("primary_trace_id") or notes.get("primary_trace_id")
            if tid:
                st.caption(f"primary_trace_id=`{tid}`")

        # Three-column body: Anomaly | RCA | Actions/Feedback
        col_a, col_b = st.columns(2)

        with col_a:
            st.markdown("#### 1 · Anomaly & explainability")
            st.markdown(
                f"""
<div class="section-card">
  <div class="muted">Why the detector fired</div>
  <p style="margin:8px 0 0; line-height:1.5; color:#e8eefc">{expl}</p>
</div>
""",
                unsafe_allow_html=True,
            )
            ctx = inc.get("context") or {}
            mcols = st.columns(3)
            mcols[0].metric("Metric", str(inc.get("metric_name") or "—")[:22])
            mcols[1].metric(
                "Value",
                f"{inc.get('metric_value'):.4g}"
                if inc.get("metric_value") is not None
                else "—",
            )
            mcols[2].metric(
                "Threshold",
                f"{inc.get('threshold')}" if inc.get("threshold") is not None else "—",
            )
            with st.expander("Method details / anomaly context", expanded=False):
                st.json(
                    {
                        "detector": ctx.get("detector"),
                        "winning_methods": ctx.get("winning_methods"),
                        "anomaly_score": ctx.get("anomaly_score"),
                        "features": ctx.get("features"),
                        "method_details": ctx.get("method_details"),
                        "anomaly_details": ctx.get("anomaly_details"),
                    }
                )

            st.markdown("#### 3 · Suggested actions & runbook")
            actions = notes.get("suggested_actions") or []
            runbook = notes.get("runbook_suggestion") or "—"
            if actions:
                for a in actions:
                    st.markdown(f"- {a}")
            else:
                st.caption("No suggested actions yet — run RCA.")
            st.info(f"**Runbook:** {runbook}")

            st.markdown("##### Remediation history")
            try:
                hist = api.list_actions(iid)
            except Exception:
                hist = []
            if hist:
                hdf = pd.DataFrame(
                    [
                        {
                            "risk": h.get("risk_level"),
                            "type": h.get("action_type"),
                            "status": h.get("status"),
                            "by": h.get("executed_by"),
                            "text": (h.get("action_text") or "")[:60],
                        }
                        for h in hist
                    ]
                )
                st.dataframe(hdf, use_container_width=True, hide_index=True)
            else:
                st.caption("No remediation actions recorded.")

            r1, r2 = st.columns(2)
            with r1:
                if st.button("▶ Run / refresh RCA", use_container_width=True):
                    with st.spinner("RCA (Bedrock / rules)…"):
                        try:
                            out = api.run_rca(iid, force=True)
                            st.success(
                                f"RCA {out.get('mode')} · conf="
                                f"{(out.get('result') or {}).get('confidence')}"
                            )
                            st.cache_data.clear()
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
            with r2:
                if st.button("🛠 Propose remediation", use_container_width=True):
                    try:
                        created = api.propose_remediation(iid)
                        st.success(f"Proposed {len(created)} action(s)")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))

        with col_b:
            st.markdown("#### 2 · RCA (Bedrock / rules)")
            if inc.get("root_cause"):
                conf = inc.get("rca_confidence")
                conf_s = (
                    f"{float(conf) * 100:.0f}%" if conf is not None else "n/a"
                )
                why = notes.get("why_root_cause") or ""
                # If why embedded in root_cause after " | Why: "
                if not why and " | Why: " in str(inc.get("root_cause")):
                    parts = str(inc.get("root_cause")).split(" | Why: ", 1)
                    root_display = parts[0]
                    why = parts[1] if len(parts) > 1 else ""
                else:
                    root_display = inc.get("root_cause")
                st.markdown(
                    f"""
<div class="section-card">
  <div class="muted">Root cause · confidence {conf_s} · mode {notes.get('rca_mode') or '—'}</div>
  <p style="margin:8px 0; font-weight:600; color:#e8eefc; line-height:1.45">{root_display}</p>
  {"<div class='muted'><strong>Why:</strong> " + why + "</div>" if why else ""}
</div>
""",
                    unsafe_allow_html=True,
                )
                evidence = notes.get("evidence") or []
                if evidence:
                    st.markdown("**Evidence citations**")
                    for e in evidence:
                        st.markdown(f"- `{e}`" if len(str(e)) < 120 else f"- {e}")
                affected = notes.get("affected_components") or []
                if affected:
                    st.caption("Affected: " + ", ".join(map(str, affected)))
            else:
                st.warning(
                    "RCA pending. Click **Run / refresh RCA** or wait for the async pipeline."
                )

            st.markdown("#### 4 · On-call feedback")
            with st.form(f"fb_{iid}"):
                a = st.radio(
                    "Anomaly đúng?",
                    ["👍 Yes", "👎 No (false positive)", "⏭ Skip"],
                    horizontal=True,
                    index=0,
                )
                r = st.radio(
                    "RCA hữu ích?",
                    ["👍 Yes", "👎 No", "⏭ Skip"],
                    horizontal=True,
                    index=0,
                )
                x = st.radio(
                    "Action hiệu quả?",
                    ["👍 Yes", "👎 No", "⏭ Skip"],
                    horizontal=True,
                    index=2,
                )
                comment = st.text_area("Comment", placeholder="Context for tuning…")
                submitted = st.form_submit_button(
                    "Submit feedback", type="primary", use_container_width=True
                )
                if submitted:
                    try:
                        fb = api.submit_feedback(
                            {
                                "incident_id": iid,
                                "anomaly_correct": _thumb_choice(a),
                                "rca_useful": _thumb_choice(r),
                                "action_effective": _thumb_choice(x),
                                "comment": comment or "",
                                "reviewer": reviewer,
                            }
                        )
                        st.success(f"Saved feedback `{str(fb.get('id',''))[:8]}`")
                    except Exception as exc:
                        st.error(str(exc))

            try:
                prior = api.feedback_list(incident_id=iid, limit=5)
            except Exception:
                prior = []
            if prior:
                st.caption("Prior reviews")
                for f in prior:
                    def t(v):
                        return "👍" if v is True else ("👎" if v is False else "—")

                    st.write(
                        f"- {str(f.get('created_at',''))[:19]} · {f.get('reviewer')} · "
                        f"A:{t(f.get('anomaly_correct'))} R:{t(f.get('rca_useful'))} "
                        f"X:{t(f.get('action_effective'))} · {f.get('comment') or ''}"
                    )

        with st.expander("Raw incident JSON", expanded=False):
            st.json(inc)


# ========================= HEALTH ==========================================

with tab_health:
    st.markdown(
        """
<div class="aiops-banner">
  <strong>AIOps Engine Health</strong> — self-monitoring of the control plane
  (not just app RED). Quality gauges come from on-call feedback; service cards
  from each component <code>/health</code>.
</div>
""",
        unsafe_allow_html=True,
    )

    # Quality metrics from feedback-collector
    try:
        fstats = api.feedback_stats()
    except Exception as exc:
        fstats = {}
        st.warning(f"Feedback stats unavailable: {exc}")

    try:
        istats = api.im_stats()
    except Exception:
        istats = {}

    st.subheader("Quality gauges (from on-call feedback)")
    q1, q2, q3, q4, q5 = st.columns(5)
    q1.metric("Feedback total", fstats.get("total", 0))
    q2.metric(
        "Positive rate",
        f"{100 * float(fstats.get('feedback_positive_rate') or 0):.0f}%",
    )
    q3.metric(
        "RCA accuracy est.",
        f"{100 * float(fstats.get('rca_accuracy_estimate') or 0):.0f}%",
    )
    q4.metric(
        "Anomaly precision",
        f"{100 * float(fstats.get('anomaly_precision_estimate') or 0):.0f}%",
    )
    q5.metric("False positives", fstats.get("false_positive_count", 0))

    st.subheader("Incident pipeline")
    by = istats.get("by_status") or {}
    open_n = sum(
        by.get(k, 0)
        for k in ("open", "acknowledged", "investigating", "remediating")
    )
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Open-ish incidents", open_n)
    p2.metric("Resolved", by.get("resolved", 0))
    p3.metric("False positive status", by.get("false_positive", 0))
    p4.metric(
        "Consumer processed",
        (istats.get("consumer") or {}).get("processed", "—"),
    )
    if by:
        st.bar_chart(pd.DataFrame({"count": by}))

    st.subheader("Component health")
    comps = {
        "anomaly-detector": api.DET,
        "incident-manager": api.IM,
        "rca-engine": api.RCA,
        "remediation": api.REM,
        "feedback-collector": api.FB,
    }
    cols = st.columns(len(comps))
    for col, (name, base) in zip(cols, comps.items()):
        h = api.health(base)
        status = h.get("status", "down")
        with col:
            st.markdown(f"**{name}**")
            if status == "ok":
                st.success(status)
            elif status == "degraded":
                st.warning(status)
            else:
                st.error(status)
            details = h.get("details") or {}
            # Highlight LLM usage if RCA
            if name == "rca-engine":
                b = details.get("bedrock") or {}
                st.caption(
                    f"inv={b.get('invocations', 0)} fail={b.get('failures', 0)}\n"
                    f"avg_lat={b.get('avg_latency_ms')}ms\n"
                    f"tok_in={b.get('total_input_tokens')} "
                    f"tok_out={b.get('total_output_tokens')}"
                )
            elif name == "feedback-collector":
                s = details.get("stats") or fstats
                st.caption(f"fp={s.get('false_positive_count', 0)}")
            elif name == "incident-manager":
                st.caption(f"open={details.get('open_incidents', '—')}")

    st.subheader("Threshold tuning suggestion")
    try:
        tune = api.tuning_suggestions()
        st.write(tune.get("recommendation"))
        t1, t2, t3 = st.columns(3)
        t1.metric("FP rate", f"{100 * float(tune.get('false_positive_rate') or 0):.1f}%")
        t2.metric(
            "Suggested ZSCORE",
            tune.get("suggested_zscore_threshold")
            or tune.get("current_zscore_threshold"),
        )
        t3.metric(
            "Suggested ERR_RATE",
            tune.get("suggested_error_rate_threshold")
            or tune.get("current_error_rate_threshold"),
        )
        with st.expander("Details"):
            st.json(tune)
    except Exception as exc:
        st.caption(f"Tuning API unavailable: {exc}")

    st.subheader("Prometheus metrics (links)")
    st.markdown(
        """
- Detector: http://localhost:8001/metrics  
- Incident Manager: http://localhost:8002/metrics  
- Feedback: http://localhost:8005/metrics  
  (`feedback_positive_rate`, `rca_accuracy_estimate`, `false_positive_count`)
- Grafana dashboard JSON: `observability/grafana/dashboards/aiops-engine-health.json`
"""
    )

st.caption(
    "AIOps Console · Streamlit · "
    f"IM={api.IM} · RCA={api.RCA} · FB={api.FB}"
)
