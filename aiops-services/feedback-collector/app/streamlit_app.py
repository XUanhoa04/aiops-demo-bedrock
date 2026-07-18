"""
Streamlit on-call review console.

  streamlit run app/streamlit_app.py --server.port 8502
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx
import streamlit as st

API = os.getenv("FEEDBACK_API_URL", "http://127.0.0.1:8005").rstrip("/")
DEFAULT_REVIEWER = os.getenv("FEEDBACK_REVIEWER", "oncall")


def api(method: str, path: str, **kwargs: Any) -> Any:
    url = f"{API}{path}"
    with httpx.Client(timeout=30.0) as client:
        r = client.request(method, url, **kwargs)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} → {r.status_code}: {r.text[:400]}")
        if not r.content:
            return None
        ctype = r.headers.get("content-type", "")
        if "application/json" in ctype:
            return r.json()
        return r.text


st.set_page_config(
    page_title="AIOps Feedback",
    page_icon="📝",
    layout="wide",
)

st.title("📝 AIOps On-call Feedback")
st.caption(
    f"API `{API}` · Rate anomaly / RCA / action · metrics on `/metrics` · "
    "tuning report on `/tuning/report`"
)

with st.sidebar:
    st.header("Reviewer")
    reviewer = st.text_input("Your name / handle", value=DEFAULT_REVIEWER)
    limit = st.slider("Incidents", 5, 50, 20)
    only_unreviewed = st.checkbox("Only unreviewed", value=False)
    if st.button("↻ Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    try:
        health = api("GET", "/health")
        st.success(f"API: {health.get('status')}")
        stats = api("GET", "/stats")
        st.metric("Feedback total", stats.get("total", 0))
        st.metric("FP count", stats.get("false_positive_count", 0))
        st.metric("Positive rate", f"{100 * float(stats.get('feedback_positive_rate') or 0):.0f}%")
        st.metric("RCA accuracy", f"{100 * float(stats.get('rca_accuracy_estimate') or 0):.0f}%")
    except Exception as exc:
        st.error(f"API error: {exc}")
        st.stop()

    st.divider()
    if st.button("Show threshold tuning", use_container_width=True):
        st.session_state["show_tuning"] = True


@st.cache_data(ttl=6)
def load_incidents(n: int) -> list[dict]:
    return api("GET", f"/incidents?limit={n}")


if st.session_state.get("show_tuning"):
    st.subheader("Threshold tuning suggestion")
    try:
        report = api("GET", "/tuning/report")
        st.code(report, language="text")
        suggestion = api("GET", "/tuning/suggestions")
        st.json(suggestion)
    except Exception as exc:
        st.error(str(exc))
    if st.button("Close tuning"):
        st.session_state["show_tuning"] = False
        st.rerun()
    st.divider()

try:
    rows = load_incidents(limit)
except Exception as exc:
    st.error(f"Load failed: {exc}")
    st.stop()

if only_unreviewed:
    rows = [r for r in rows if not r.get("reviewed")]

if not rows:
    st.info("No incidents to review.")
    st.stop()


def thumb_to_bool(label: str) -> Optional[bool]:
    """Map radio choice → bool | None."""
    if label.startswith("👍"):
        return True
    if label.startswith("👎"):
        return False
    return None


labels = []
for r in rows:
    inc = r.get("incident") or {}
    mark = "✓" if r.get("reviewed") else "·"
    labels.append(
        f"{mark} {str(inc.get('severity', '?')).upper()} · {inc.get('status')} · "
        f"{inc.get('service_name')} · {(inc.get('title') or '')[:50]} · {str(inc.get('id', ''))[:8]}"
    )

idx = st.selectbox("Select incident", range(len(labels)), format_func=lambda i: labels[i])
row = rows[idx]
inc = row.get("incident") or {}
iid = inc.get("id") or ""

col_l, col_r = st.columns([1.15, 1])

with col_l:
    st.subheader("Incident")
    st.markdown(
        f"**{inc.get('title')}**  \n"
        f"`{iid}` · **{inc.get('service_name')}** · "
        f"severity=`{inc.get('severity')}` · status=`{inc.get('status')}`"
    )
    if inc.get("root_cause"):
        conf = inc.get("rca_confidence")
        conf_s = f"{float(conf)*100:.0f}%" if conf is not None else "n/a"
        st.info(f"**RCA** ({conf_s}): {inc.get('root_cause')}")
    if inc.get("description"):
        with st.expander("Description / anomaly details"):
            st.text(inc.get("description"))
    if inc.get("remediation_notes"):
        with st.expander("Remediation notes"):
            st.text(str(inc.get("remediation_notes"))[:3000])
    if inc.get("human_feedback"):
        st.caption(f"Prior human_feedback: {inc.get('human_feedback')}")

    st.markdown("#### Prior reviews")
    prior = row.get("feedback") or []
    if not prior:
        st.caption("No feedback yet.")
    else:
        for f in prior:
            def t(v):
                if v is True:
                    return "👍"
                if v is False:
                    return "👎"
                return "—"
            st.write(
                f"- {f.get('created_at', '')[:19]} · {f.get('reviewer')} · "
                f"A:{t(f.get('anomaly_correct'))} R:{t(f.get('rca_useful'))} "
                f"X:{t(f.get('action_effective'))} · {f.get('comment') or ''}"
            )

with col_r:
    st.subheader("Your review")
    st.markdown("Thumbs for each signal (Skip allowed):")

    a_choice = st.radio(
        "Anomaly đúng? (not a false positive)",
        ["👍 Yes — real anomaly", "👎 No — false positive", "⏭ Skip"],
        index=0,
        key=f"a_{iid}",
    )
    r_choice = st.radio(
        "RCA hữu ích?",
        ["👍 Yes — useful", "👎 No — not useful", "⏭ Skip"],
        index=0,
        key=f"r_{iid}",
    )
    x_choice = st.radio(
        "Action hiệu quả?",
        ["👍 Yes — helped", "👎 No — no effect", "⏭ Skip"],
        index=2,
        key=f"x_{iid}",
    )
    comment = st.text_area("Comment", placeholder="Context for future tuning…", height=100)
    corrected = st.text_input("Corrected root cause (optional)", "")

    if st.button("Submit feedback", type="primary", use_container_width=True):
        payload = {
            "incident_id": iid,
            "anomaly_correct": thumb_to_bool(a_choice),
            "rca_useful": thumb_to_bool(r_choice),
            "action_effective": thumb_to_bool(x_choice),
            "comment": comment or "",
            "reviewer": reviewer,
            "corrected_root_cause": corrected or None,
        }
        try:
            saved = api("POST", "/feedback", json=payload)
            st.success(f"Saved feedback `{saved.get('id', '')[:8]}`")
            st.cache_data.clear()
            st.balloons()
        except Exception as exc:
            st.error(str(exc))

st.divider()
st.caption(
    "Metrics: `feedback_positive_rate`, `rca_accuracy_estimate`, `false_positive_count` · "
    "Grafana: import observability/grafana/dashboards/aiops-engine-health.json"
)
