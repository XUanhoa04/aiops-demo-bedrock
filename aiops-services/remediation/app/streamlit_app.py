"""
Simple Streamlit ops console for remediation.

Run (inside container via start.sh):
  streamlit run app/streamlit_app.py --server.port 8501

Talks to the local FastAPI remediation API (REMEDIATION_API_URL).
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import streamlit as st

API = os.getenv("REMEDIATION_API_URL", "http://127.0.0.1:8004").rstrip("/")
OPERATOR = os.getenv("REMEDIATION_OPERATOR", "streamlit-operator")


def api(method: str, path: str, **kwargs: Any) -> Any:
    url = f"{API}{path}"
    with httpx.Client(timeout=30.0) as client:
        r = client.request(method, url, **kwargs)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} → {r.status_code}: {r.text[:400]}")
        if not r.content:
            return None
        return r.json()


st.set_page_config(
    page_title="AIOps Remediation",
    page_icon="🛠️",
    layout="wide",
)

st.title("🛠️ AIOps Remediation Console")
st.caption(
    f"API: `{API}` · Low-risk actions may auto-run · High-risk needs **Approve & Execute**"
)

# Sidebar
with st.sidebar:
    st.header("Controls")
    operator = st.text_input("Executed by", value=OPERATOR)
    limit = st.slider("Incidents to load", 5, 50, 15)
    if st.button("↻ Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    try:
        health = api("GET", "/health")
        st.success(f"API status: {health.get('status')}")
        st.json(health.get("details") or {})
    except Exception as exc:
        st.error(f"API unreachable: {exc}")
        st.stop()


@st.cache_data(ttl=8)
def load_bundles(n: int) -> list[dict]:
    return api("GET", f"/incidents?limit={n}")


try:
    bundles = load_bundles(limit)
except Exception as exc:
    st.error(f"Failed to load incidents: {exc}")
    st.stop()

if not bundles:
    st.info("No incidents yet. Create one via Anomaly Detector / Incident Manager.")
    st.stop()

# Summary metrics
c1, c2, c3 = st.columns(3)
openish = [
    b
    for b in bundles
    if (b.get("incident") or {}).get("status")
    in ("open", "acknowledged", "investigating", "remediating")
]
c1.metric("Loaded incidents", len(bundles))
c2.metric("Open-ish", len(openish))
with_rca = sum(1 for b in bundles if (b.get("rca") or {}).get("root_cause"))
c3.metric("With RCA", with_rca)

st.divider()

labels = []
for b in bundles:
    inc = b.get("incident") or {}
    labels.append(
        f"{inc.get('severity', '?').upper()} · {inc.get('status')} · "
        f"{inc.get('service_name')} · {(inc.get('title') or '')[:60]} · {inc.get('id', '')[:8]}"
    )

idx = st.selectbox("Select incident", range(len(labels)), format_func=lambda i: labels[i])
bundle = bundles[idx]
inc = bundle.get("incident") or {}
rca = bundle.get("rca") or {}
history = bundle.get("history") or []
iid = inc.get("id") or ""

left, right = st.columns([1.1, 1])

with left:
    st.subheader("Incident")
    st.markdown(
        f"**{inc.get('title')}**  \n"
        f"id=`{iid}`  ·  service=`{inc.get('service_name')}`  ·  "
        f"severity=`{inc.get('severity')}`  ·  status=`{inc.get('status')}`"
    )
    with st.expander("Raw incident JSON", expanded=False):
        st.json(inc)

    st.subheader("RCA")
    if rca.get("root_cause"):
        conf = rca.get("confidence_percent")
        if conf is None and rca.get("rca_confidence") is not None:
            try:
                conf = round(float(rca["rca_confidence"]) * 100)
            except Exception:
                conf = rca.get("rca_confidence")
        st.write(f"**Root cause:** {rca.get('root_cause')}")
        st.write(f"**Confidence:** {conf}% · mode=`{rca.get('rca_mode')}`")
        if rca.get("evidence"):
            st.markdown("**Evidence**")
            for e in rca["evidence"]:
                st.markdown(f"- {e}")
        if rca.get("runbook_suggestion"):
            st.info(f"Runbook: {rca.get('runbook_suggestion')}")
    else:
        st.warning("No RCA on this ticket yet. Run RCA engine `/analyze-incident/{id}`.")

    st.markdown("**Suggested actions (from RCA)**")
    suggested = bundle.get("suggested_actions") or rca.get("suggested_actions") or []
    if suggested:
        for s in suggested:
            st.markdown(f"- {s}")
    else:
        st.caption("_None stored — Propose will synthesize demo defaults._")

with right:
    st.subheader("Actions")
    b1, b2, b3 = st.columns(3)
    with b1:
        if st.button("Propose from RCA", type="primary", use_container_width=True):
            try:
                created = api(
                    "POST",
                    "/remediate/propose",
                    json={"incident_id": iid, "actions": []},
                )
                st.success(f"Created {len(created)} action(s)")
                st.cache_data.clear()
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    with b2:
        if st.button("Approve & Execute (all high-risk pending)", use_container_width=True):
            pending = [
                h
                for h in history
                if h.get("risk_level") == "high"
                and h.get("status") in ("proposed", "approved")
            ]
            if not pending:
                st.warning("No high-risk pending actions. Propose first.")
            else:
                ok, err = 0, 0
                for h in pending:
                    try:
                        api(
                            "POST",
                            f"/actions/{h['id']}/approve",
                            json={"executed_by": operator, "execute_now": True},
                        )
                        ok += 1
                    except Exception:
                        err += 1
                st.success(f"Approved/executed: {ok}, errors: {err}")
                st.cache_data.clear()
                st.rerun()
    with b3:
        if st.button("Mark as False Positive", use_container_width=True):
            try:
                api(
                    "POST",
                    f"/incidents/{iid}/false-positive",
                    json={
                        "executed_by": operator,
                        "note": f"False positive marked by {operator} via Streamlit",
                    },
                )
                st.success("Incident marked false_positive")
                st.cache_data.clear()
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    st.markdown("#### Action history")
    if not history:
        st.caption("No remediation history for this incident.")
    else:
        for h in history:
            risk = h.get("risk_level", "?")
            status = h.get("status", "?")
            color = {"low": "🟢", "high": "🔴"}.get(risk, "⚪")
            with st.expander(
                f"{color} [{risk}/{status}] {h.get('action_type')} — {(h.get('action_text') or '')[:80]}"
            ):
                st.write(f"id: `{h.get('id')}`")
                st.write(f"target: `{h.get('target_service')}`")
                st.write(f"executed_by: `{h.get('executed_by')}`")
                if h.get("command"):
                    st.code(h.get("command"), language="bash")
                if h.get("result"):
                    st.write(h.get("result"))
                st.json(h.get("payload") or {})

                col_a, col_b, col_c = st.columns(3)
                if status in ("proposed", "approved") and risk == "high":
                    if col_a.button("Approve & Execute", key=f"ap_{h['id']}"):
                        try:
                            api(
                                "POST",
                                f"/actions/{h['id']}/approve",
                                json={"executed_by": operator, "execute_now": True},
                            )
                            st.cache_data.clear()
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
                if status == "proposed":
                    if col_b.button("Execute (force)", key=f"ex_{h['id']}"):
                        try:
                            api(
                                "POST",
                                f"/actions/{h['id']}/execute",
                                json={"executed_by": operator, "force": True},
                            )
                            st.cache_data.clear()
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
                    if col_c.button("Reject", key=f"rj_{h['id']}"):
                        try:
                            api(
                                "POST",
                                f"/actions/{h['id']}/reject",
                                params={"executed_by": operator, "reason": "rejected in UI"},
                            )
                            st.cache_data.clear()
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))

st.divider()
st.caption(
    "Low risk → auto/simulate chaos reset & log-only · "
    "High risk → restart/scale need approval · History stored in SQLite"
)
