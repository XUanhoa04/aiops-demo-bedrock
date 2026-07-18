"""
Engine QA Streamlit UI — supervise the supervisors.

  streamlit run app/streamlit_app.py --server.port 8503
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx
import streamlit as st

API = os.getenv("ENGINE_QA_API_URL", "http://127.0.0.1:8007").rstrip("/")
DEFAULT_REVIEWER = os.getenv("ENGINE_QA_REVIEWER", "oncall-sre")


def api(method: str, path: str, **kwargs: Any) -> Any:
    url = f"{API}{path}"
    with httpx.Client(timeout=30.0) as client:
        r = client.request(method, url, **kwargs)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} → {r.status_code}: {r.text[:400]}")
        if not r.content:
            return None
        if "application/json" in r.headers.get("content-type", ""):
            return r.json()
        return r.text


st.set_page_config(
    page_title="AIOps Engine QA",
    page_icon="🛰️",
    layout="wide",
)

st.title("🛰️ AIOps Engine QA")
st.caption(
    "Giám sát người giám sát — đánh giá detector · confidence · RCA/LLM · decision. "
    f"API `{API}` · Prometheus `/metrics`"
)

with st.sidebar:
    st.header("Reviewer")
    reviewer = st.text_input("Handle", value=DEFAULT_REVIEWER)
    limit = st.slider("Queue size", 5, 40, 15)
    hide_reviewed = st.checkbox("Hide already reviewed", value=True)
    if st.button("↻ Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    try:
        health = api("GET", "/health")
        st.success(f"API: {health.get('status')}")
        q = api("GET", "/qa/metrics")
        st.metric("Reviews", q.get("total_reviews", 0))
        st.metric("Engine health", f"{100 * float(q.get('overall_engine_health') or 0):.0f}%")
        st.metric("Precision ≈", f"{100 * float(q.get('precision_estimate') or 0):.0f}%")
        st.metric("FP rate", f"{100 * float(q.get('false_positive_rate') or 0):.0f}%")
        st.metric("Hallucination", f"{100 * float(q.get('hallucination_rate') or 0):.0f}%")
        st.metric("Mean iterations", f"{float(q.get('mean_decision_iterations') or 0):.2f}")
    except Exception as exc:
        st.error(f"API error: {exc}")
        st.stop()


tab_review, tab_metrics, tab_tuning, tab_history = st.tabs(
    ["📋 Review queue", "📊 Quality metrics", "🎛️ Tuning advice", "🗂 History"]
)


@st.cache_data(ttl=8)
def load_queue(n: int) -> list[dict]:
    return api("GET", f"/qa/review-queue?limit={n}")


with tab_review:
    st.subheader("On-call meta-review")
    st.markdown(
        """
Đánh giá 4 lớp:

1. **Anomaly đúng không?** → precision / FP  
2. **Confidence hợp lý?** → calibration scorer  
3. **RCA hữu ích?** → LLM quality (+ hallucination nếu bịa)  
4. **Decision đúng không?** → routing auto / RCA / escalate  
"""
    )
    try:
        queue = load_queue(limit)
    except Exception as exc:
        st.error(str(exc))
        queue = []

    if hide_reviewed:
        queue = [x for x in queue if not x.get("already_reviewed")]

    if not queue:
        st.info("Hàng đợi trống — tạo incident (chaos / POST /detect) rồi refresh.")
    else:
        labels = []
        for item in queue:
            inc = item.get("incident") or {}
            iid = str(inc.get("id") or "")[:8]
            svc = inc.get("service_name") or "?"
            title = (inc.get("title") or inc.get("metric_name") or "")[:40]
            conf = item.get("engine_confidence")
            conf_s = f" conf={conf:.0f}" if isinstance(conf, (int, float)) else ""
            labels.append(f"{iid} · {svc} · {title}{conf_s}")

        idx = st.selectbox(
            "Incident",
            range(len(queue)),
            format_func=lambda i: labels[i],
        )
        item = queue[idx]
        inc = item.get("incident") or {}
        dec = item.get("decision") or {}

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Incident**")
            st.json(
                {
                    "id": inc.get("id"),
                    "service": inc.get("service_name"),
                    "severity": inc.get("severity"),
                    "status": inc.get("status"),
                    "metric": inc.get("metric_name"),
                    "value": inc.get("metric_value"),
                    "root_cause": inc.get("root_cause"),
                    "rca_confidence": inc.get("rca_confidence"),
                }
            )
        with c2:
            st.markdown("**Decision / confidence snapshot**")
            st.json(
                {
                    "action": dec.get("action"),
                    "band": dec.get("band"),
                    "confidence_score": item.get("engine_confidence")
                    or dec.get("confidence_score"),
                    "iterations": dec.get("iteration_count"),
                    "missing_context": item.get("missing_context"),
                    "reason": (dec.get("reason") or "")[:300],
                    "llm_confidence": dec.get("llm_confidence"),
                }
            )

        st.markdown("---")
        st.markdown("### Votes")
        v1, v2, v3, v4 = st.columns(4)

        def _vote(col, label: str, key: str) -> Optional[bool]:
            with col:
                choice = st.radio(
                    label,
                    ["skip", "👍 yes", "👎 no"],
                    key=key,
                    horizontal=True,
                )
            if choice.startswith("👍"):
                return True
            if choice.startswith("👎"):
                return False
            return None

        anomaly_correct = _vote(v1, "Anomaly đúng?", "a")
        confidence_reasonable = _vote(v2, "Confidence hợp lý?", "c")
        rca_useful = _vote(v3, "RCA hữu ích?", "r")
        decision_correct = _vote(v4, "Decision đúng?", "d")

        hallu = st.radio(
            "LLM hallucination? (bịa evidence / root cause)",
            ["skip", "yes", "no"],
            horizontal=True,
            key="hallu",
        )
        llm_hallucinated = (
            True if hallu == "yes" else False if hallu == "no" else None
        )

        expected_confidence = st.slider(
            "Expected confidence (0 = skip)",
            0,
            100,
            0,
            help="What score would you assign? Used for calibration error.",
        )
        corrected = st.text_area("Corrected root cause (if RCA wrong)", height=80)
        comment = st.text_area("Comment", height=80)

        if st.button("Submit Engine QA review", type="primary"):
            payload = {
                "incident_id": inc.get("id"),
                "anomaly_id": inc.get("source_anomaly_id"),
                "decision_id": dec.get("id"),
                "anomaly_correct": anomaly_correct,
                "confidence_reasonable": confidence_reasonable,
                "rca_useful": rca_useful,
                "decision_correct": decision_correct,
                "llm_hallucinated": llm_hallucinated,
                "expected_confidence": expected_confidence or None,
                "corrected_root_cause": corrected or None,
                "comment": comment or "",
                "reviewer": reviewer,
                "decision_action": dec.get("action"),
                "decision_iterations": dec.get("iteration_count"),
                "engine_confidence": item.get("engine_confidence")
                or dec.get("confidence_score"),
                "llm_confidence": dec.get("llm_confidence"),
            }
            try:
                saved = api("POST", "/qa/reviews", json=payload)
                st.success(f"Saved review `{saved.get('id')}`")
                st.cache_data.clear()
            except Exception as exc:
                st.error(str(exc))


with tab_metrics:
    st.subheader("Engine + LLM quality")
    try:
        q = api("GET", "/qa/metrics")
    except Exception as exc:
        st.error(str(exc))
        q = {}

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Precision ≈", f"{100 * float(q.get('precision_estimate') or 0):.1f}%")
    m2.metric("Recall ≈", f"{100 * float(q.get('recall_estimate') or 0):.1f}%")
    m3.metric("FP rate", f"{100 * float(q.get('false_positive_rate') or 0):.1f}%")
    m4.metric("Overall health", f"{100 * float(q.get('overall_engine_health') or 0):.1f}%")

    n1, n2, n3, n4 = st.columns(4)
    n1.metric("Confidence OK", f"{100 * float(q.get('confidence_reasonable_rate') or 0):.1f}%")
    n2.metric("RCA useful", f"{100 * float(q.get('rca_useful_rate') or 0):.1f}%")
    n3.metric("Hallucination", f"{100 * float(q.get('hallucination_rate') or 0):.1f}%")
    n4.metric("Decision OK", f"{100 * float(q.get('decision_correct_rate') or 0):.1f}%")

    st.metric(
        "Mean decision iterations (before handoff/terminal)",
        f"{float(q.get('mean_decision_iterations') or 0):.2f}",
    )
    st.metric("Overconfidence count (FP + conf≥70)", q.get("overconfidence_count", 0))

    if q.get("notes"):
        st.info("\n".join(f"• {n}" for n in q["notes"]))

    with st.expander("Raw metrics JSON"):
        st.json(q)


with tab_tuning:
    st.subheader("Suggested knob changes (advisory only)")
    st.caption(
        "Không auto-apply — copy .env → restart anomaly-detector / decision-engine."
    )
    try:
        report = api("GET", "/qa/tuning/report")
        st.code(report, language="text")
        advice = api("GET", "/qa/tuning")
        if advice.get("env_snippet"):
            st.markdown("**Copy-paste env**")
            st.code(advice["env_snippet"], language="bash")
        if advice.get("suggested_confidence_weights"):
            st.markdown("**Confidence weights**")
            st.json(advice["suggested_confidence_weights"])
    except Exception as exc:
        st.error(str(exc))


with tab_history:
    st.subheader("Recent QA reviews")
    try:
        rows = api("GET", "/qa/reviews?limit=50")
        st.dataframe(rows, use_container_width=True)
    except Exception as exc:
        st.error(str(exc))
