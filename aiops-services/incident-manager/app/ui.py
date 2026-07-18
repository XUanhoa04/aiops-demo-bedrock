"""
Production-minded Incident Console (single-page HTML).

Design goals (SRE / AIOps interview bar)
---------------------------------------
1. **Time-to-evidence**: one click from ticket → Grafana Tempo/Loki (no copy-paste).
2. **Explainability**: surface the detector's human sentence ("3.2σ above EWMA…"),
   not only a severity badge.
3. **Storytelling for demos**: RCA root_cause + suggested actions visible without
   digging into raw JSON.
4. **Fail soft**: if RCA hasn't run yet, still offer service-scoped Explore links.

Why not only Streamlit?
- Incident Manager is the system of record; keeping a zero-dep UI on the same
  process avoids another hop and another failure domain for the demo path.
"""

from __future__ import annotations

UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AIOps Incident Console</title>
  <style>
    :root {
      --bg: #070b14;
      --panel: #0f1628;
      --panel2: #141e33;
      --border: #243049;
      --text: #e8eefc;
      --muted: #9fb0d0;
      --accent: #5b9dff;
      --accent2: #7c5cff;
      --critical: #ff5c7a;
      --high: #ff9f43;
      --medium: #f6c945;
      --low: #3dd68c;
      --ok: #3dd68c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background:
        radial-gradient(900px 480px at 8% -10%, #1a2748 0%, transparent 55%),
        radial-gradient(700px 400px at 100% 0%, #2a1650 0%, transparent 40%),
        var(--bg);
      color: var(--text);
      min-height: 100vh;
    }
    header {
      display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
      justify-content: space-between;
      padding: 16px 22px; border-bottom: 1px solid var(--border);
      background: rgba(8, 12, 22, 0.82); backdrop-filter: blur(10px);
      position: sticky; top: 0; z-index: 2;
    }
    h1 { font-size: 1.1rem; margin: 0; letter-spacing: 0.02em; }
    .sub { color: var(--muted); font-size: 0.82rem; margin-top: 2px; }
    .story {
      margin: 14px 22px 0; padding: 12px 14px; border-radius: 12px;
      border: 1px solid color-mix(in srgb, var(--accent2) 35%, var(--border));
      background: linear-gradient(90deg, rgba(124,92,255,0.12), rgba(91,157,255,0.08));
      color: var(--muted); font-size: 0.88rem; line-height: 1.45;
    }
    .story strong { color: var(--text); }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    select, button, input {
      background: var(--panel); color: var(--text);
      border: 1px solid var(--border); border-radius: 8px;
      padding: 8px 12px; font: inherit;
    }
    button { cursor: pointer; background: #1a2b4a; }
    button.primary { background: linear-gradient(135deg, #2a4a8a, #3a2a7a); border-color: #4a6ab0; }
    button:hover { border-color: var(--accent); }
    a.btn {
      display: inline-flex; align-items: center; gap: 6px;
      text-decoration: none; color: var(--text);
      background: #1a2b4a; border: 1px solid var(--border);
      border-radius: 8px; padding: 8px 12px; font-size: 0.9rem;
    }
    a.btn.trace {
      background: linear-gradient(135deg, #1e3a5f, #3b1f6e);
      border-color: color-mix(in srgb, var(--accent2) 50%, var(--border));
      font-weight: 600;
    }
    a.btn:hover { border-color: var(--accent); }
    main { padding: 16px 22px 48px; max-width: 1180px; margin: 0 auto; }
    .stats {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 10px; margin: 14px 0 16px;
    }
    .stat {
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 12px; padding: 12px 14px;
    }
    .stat .k { color: var(--muted); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; }
    .stat .v { font-size: 1.35rem; font-weight: 650; margin-top: 4px; }
    table {
      width: 100%; border-collapse: collapse; background: var(--panel);
      border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
    }
    th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
    th { color: var(--muted); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: rgba(91, 157, 255, 0.06); cursor: pointer; }
    .badge {
      display: inline-block; padding: 2px 8px; border-radius: 999px;
      font-size: 0.72rem; font-weight: 600; border: 1px solid var(--border);
      background: #0f1728;
    }
    .sev-critical { color: var(--critical); }
    .sev-high { color: var(--high); }
    .sev-medium { color: var(--medium); }
    .sev-low { color: var(--low); }
    .empty, .error { color: var(--muted); padding: 28px; text-align: center; }
    .error { color: var(--critical); }
    .muted { color: var(--muted); font-size: 0.85rem; }
    .explain {
      max-width: 420px; color: var(--muted); font-size: 0.8rem; line-height: 1.35;
    }
    dialog {
      border: 1px solid var(--border); border-radius: 14px; background: var(--panel);
      color: var(--text); width: min(860px, 94vw); padding: 0; max-height: 90vh;
    }
    dialog::backdrop { background: rgba(0,0,0,0.6); }
    .dlg-h {
      padding: 14px 16px; border-bottom: 1px solid var(--border);
      display:flex; justify-content:space-between; gap:12px; align-items:flex-start; flex-wrap: wrap;
    }
    .dlg-b { padding: 14px 16px 18px; overflow: auto; max-height: calc(90vh - 70px); }
    .card {
      background: var(--panel2); border: 1px solid var(--border);
      border-radius: 12px; padding: 12px 14px; margin-bottom: 12px;
    }
    .card h3 { margin: 0 0 8px; font-size: 0.85rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
    .card p { margin: 0; line-height: 1.45; }
    .row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
    pre {
      white-space: pre-wrap; word-break: break-word; background: #0a101c;
      border: 1px solid var(--border); border-radius: 10px; padding: 12px;
      font-size: 0.78rem; color: #c9d7f5; max-height: 28vh; overflow: auto; margin: 0;
    }
    .links { font-size: 0.85rem; color: var(--muted); }
    code { font-size: 0.85em; color: #b8c9f0; }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>AIOps · Incident Console</h1>
      <div class="sub">Detect → Ticket → Grounded RCA → Gated Remediation → Feedback · Trace-first evidence</div>
    </div>
    <div class="actions">
      <select id="statusFilter">
        <option value="">all statuses</option>
        <option value="open">open</option>
        <option value="acknowledged">acknowledged</option>
        <option value="investigating">investigating</option>
        <option value="remediating">remediating</option>
        <option value="resolved">resolved</option>
        <option value="closed">closed</option>
        <option value="false_positive">false_positive</option>
      </select>
      <input id="serviceFilter" placeholder="service filter" />
      <button id="refreshBtn" type="button">Refresh</button>
      <button id="createBtn" type="button">+ Manual</button>
      <a class="btn" href="http://localhost:8501" target="_blank" rel="noopener">Remediation UI</a>
      <a class="btn" href="http://localhost:8502" target="_blank" rel="noopener">Feedback UI</a>
    </div>
  </header>

  <div class="story">
    <strong>Demo story:</strong>
    Checkout order latency/errors spike → hybrid detector explains the sigma/EWMA breach →
    ticket opens with correlation → RCA grounds on Prom/Loki/Tempo →
    <strong>🔍 Xem Trace</strong> jumps into Grafana Tempo → remediation proposes scale/reset with risk gates → on-call feedback closes the loop.
  </div>

  <main>
    <div class="stats" id="stats"></div>
    <div class="links" style="margin-bottom:12px">
      API <a href="/docs" target="_blank">/docs</a> ·
      Metrics <a href="/metrics" target="_blank">/metrics</a> ·
      Health <a href="/health" target="_blank">/health</a> ·
      Grafana <a href="http://localhost:3000" target="_blank">:3000</a>
    </div>
    <div id="tableWrap"><div class="empty">Loading…</div></div>
  </main>

  <dialog id="detailDlg">
    <div class="dlg-h">
      <div>
        <strong id="dlgTitle">Incident</strong>
        <div class="muted" id="dlgMeta"></div>
      </div>
      <div class="row" id="dlgActions"></div>
    </div>
    <div class="dlg-b">
      <div class="card" id="cardExplain">
        <h3>Explainability — why this fired</h3>
        <p id="explainText" class="muted">—</p>
      </div>
      <div class="card" id="cardRca">
        <h3>Root cause analysis</h3>
        <p id="rcaText" class="muted">RCA pending…</p>
        <div id="rcaActions" class="muted" style="margin-top:8px"></div>
      </div>
      <div class="card">
        <h3>Evidence links</h3>
        <div class="row" id="obsLinks"></div>
        <p class="muted" style="margin-top:8px" id="traceHint"></p>
      </div>
      <div class="card">
        <h3>Raw ticket JSON</h3>
        <pre id="dlgBody"></pre>
      </div>
      <div class="row" style="justify-content:flex-end">
        <button type="button" id="dlgClose">Close</button>
      </div>
    </div>
  </dialog>

  <script>
    const $ = (id) => document.getElementById(id);

    function sevClass(sev) {
      return "badge sev-" + (sev || "medium");
    }

    function fmtTime(iso) {
      if (!iso) return "—";
      try { return new Date(iso).toLocaleString(); } catch { return iso; }
    }

    function esc(s) {
      return String(s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
    }

    function parseNotes(inc) {
      const raw = inc.remediation_notes;
      if (!raw) return {};
      if (typeof raw === "object") return raw;
      try {
        if (String(raw).trim().startsWith("{")) return JSON.parse(raw);
      } catch (e) {}
      return { raw: raw };
    }

    function explanationOf(inc) {
      const ctx = inc.context || {};
      return (
        ctx.explanation ||
        (ctx.explainability && ctx.explainability.summary) ||
        (ctx.anomaly_details && ctx.anomaly_details.message) ||
        inc.description ||
        "No detector explanation stored yet."
      );
    }

    async function fetchJSON(url, opts) {
      const res = await fetch(url, opts);
      if (!res.ok) throw new Error(res.status + " " + (await res.text()));
      return res.json();
    }

    async function loadStats() {
      try {
        const s = await fetchJSON("/stats");
        const by = s.by_status || {};
        const open = (by.open || 0) + (by.acknowledged || 0) + (by.investigating || 0) + (by.remediating || 0);
        const total = Object.values(by).reduce((a, b) => a + b, 0);
        $("stats").innerHTML = `
          <div class="stat"><div class="k">Open</div><div class="v">${open}</div></div>
          <div class="stat"><div class="k">Total</div><div class="v">${total}</div></div>
          <div class="stat"><div class="k">Investigating</div><div class="v">${by.investigating || 0}</div></div>
          <div class="stat"><div class="k">Resolved</div><div class="v">${by.resolved || 0}</div></div>
          <div class="stat"><div class="k">Consumer</div><div class="v">${(s.consumer && s.consumer.processed) || 0}</div></div>
        `;
      } catch (e) {
        $("stats").innerHTML = `<div class="stat"><div class="k">Stats</div><div class="v error">err</div></div>`;
      }
    }

    async function loadIncidents() {
      const status = $("statusFilter").value;
      const service = $("serviceFilter").value.trim();
      const qs = new URLSearchParams({ limit: "50" });
      if (status) qs.set("status", status);
      if (service) qs.set("service_name", service);
      const wrap = $("tableWrap");
      try {
        const items = await fetchJSON("/incidents?" + qs.toString());
        if (!items.length) {
          wrap.innerHTML = `<div class="empty">No incidents yet. Run <code>python scripts/demo_story.py</code> or inject chaos.</div>`;
          return;
        }
        const rows = items.map(i => {
          const expl = explanationOf(i);
          const short = expl.length > 140 ? expl.slice(0, 140) + "…" : expl;
          const hasRca = !!i.root_cause;
          return `
          <tr data-id="${i.id}">
            <td><span class="${sevClass(i.severity)}">${esc(i.severity)}</span></td>
            <td><span class="badge">${esc(i.status)}</span></td>
            <td>${esc(i.service_name)}</td>
            <td>
              <div>${esc(i.title)}</div>
              <div class="explain">${esc(short)}</div>
              ${hasRca ? `<div class="muted" style="margin-top:4px">RCA: ${esc((i.root_cause || "").slice(0,100))}</div>` : ""}
            </td>
            <td>${fmtTime(i.created_at)}</td>
          </tr>`;
        }).join("");
        wrap.innerHTML = `
          <table>
            <thead><tr>
              <th>Severity</th><th>Status</th><th>Service</th><th>Title / Why it fired</th><th>Created</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>`;
        wrap.querySelectorAll("tr[data-id]").forEach(tr => {
          tr.addEventListener("click", () => openDetail(tr.dataset.id));
        });
      } catch (e) {
        wrap.innerHTML = `<div class="error">Failed to load: ${esc(e.message)}</div>`;
      }
    }

    async function openDetail(id) {
      try {
        const inc = await fetchJSON("/incidents/" + encodeURIComponent(id));
        let obs = null;
        try {
          obs = await fetchJSON("/incidents/" + encodeURIComponent(id) + "/observability-links");
        } catch (e) { obs = null; }

        const notes = parseNotes(inc);
        const expl = explanationOf(inc);
        $("dlgTitle").textContent = inc.title || id;
        $("dlgMeta").textContent = `${inc.id} · ${inc.service_name} · ${inc.severity} · ${inc.status}`;
        $("explainText").textContent = expl;

        if (inc.root_cause) {
          const conf = inc.rca_confidence != null ? Math.round(Number(inc.rca_confidence) * 100) + "%" : "n/a";
          $("rcaText").innerHTML = `<strong>${esc(inc.root_cause)}</strong><br/><span class="muted">confidence ${esc(conf)} · mode ${esc(notes.rca_mode || "—")}</span>`;
          const acts = notes.suggested_actions || [];
          $("rcaActions").innerHTML = acts.length
            ? "<strong>Suggested actions:</strong><br/>" + acts.map(a => "• " + esc(a)).join("<br/>")
            : "";
        } else {
          $("rcaText").textContent = "RCA pending — consumer/webhook will fill root_cause, or POST /analyze-incident/{id} on RCA engine.";
          $("rcaActions").textContent = "";
        }

        // Action buttons: Trace first
        const actions = [];
        const primaryUrl = (obs && obs.primary_trace_url) || notes.grafana_trace_url;
        const svcTraces = (obs && obs.service_traces_url) || notes.grafana_service_traces_url;
        const logsUrl = (obs && obs.service_logs_url) || notes.grafana_logs_url;
        const tid = (obs && obs.primary_trace_id) || notes.primary_trace_id;

        if (primaryUrl) {
          actions.push(`<a class="btn trace" href="${primaryUrl}" target="_blank" rel="noopener">🔍 Xem Trace</a>`);
        } else if (svcTraces) {
          actions.push(`<a class="btn trace" href="${svcTraces}" target="_blank" rel="noopener">🔍 Xem Traces (service)</a>`);
        }
        if (logsUrl) {
          actions.push(`<a class="btn" href="${logsUrl}" target="_blank" rel="noopener">📜 Logs</a>`);
        }
        actions.push(`<a class="btn" href="http://localhost:8501" target="_blank" rel="noopener">🛠️ Remediate</a>`);
        actions.push(`<a class="btn" href="http://localhost:8502" target="_blank" rel="noopener">📝 Feedback</a>`);
        actions.push(`<button type="button" id="dlgCloseTop">Close</button>`);
        $("dlgActions").innerHTML = actions.join("");
        const closeTop = document.getElementById("dlgCloseTop");
        if (closeTop) closeTop.addEventListener("click", () => $("detailDlg").close());

        const links = [];
        if (primaryUrl) links.push(`<a class="btn trace" href="${primaryUrl}" target="_blank" rel="noopener">🔍 Primary trace ${tid ? tid.slice(0,12)+"…" : ""}</a>`);
        if (svcTraces) links.push(`<a class="btn" href="${svcTraces}" target="_blank" rel="noopener">Service error traces</a>`);
        if (logsUrl) links.push(`<a class="btn" href="${logsUrl}" target="_blank" rel="noopener">Error logs (Loki)</a>`);
        $("obsLinks").innerHTML = links.join("") || `<span class="muted">No links yet — RCA will attach Tempo IDs when traces exist.</span>`;
        $("traceHint").textContent = tid
          ? `primary_trace_id=${tid} (from RCA evidence pack / Tempo search)`
          : `No specific trace id yet. Service Explore still works for ${inc.service_name}.`;

        $("dlgBody").textContent = JSON.stringify(inc, null, 2);
        $("detailDlg").showModal();
      } catch (e) {
        alert("Load failed: " + e.message);
      }
    }

    async function createManual() {
      const service = prompt("Service name", "checkout-service");
      if (!service) return;
      const title = prompt("Title", "[MANUAL] Demo incident");
      if (!title) return;
      try {
        await fetchJSON("/incidents", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title,
            description: "Created from Incident Console",
            service_name: service,
            severity: "medium",
          }),
        });
        await refresh();
      } catch (e) {
        alert("Create failed: " + e.message);
      }
    }

    async function refresh() {
      await Promise.all([loadStats(), loadIncidents()]);
    }

    $("refreshBtn").addEventListener("click", refresh);
    $("createBtn").addEventListener("click", createManual);
    $("statusFilter").addEventListener("change", loadIncidents);
    $("serviceFilter").addEventListener("change", loadIncidents);
    $("dlgClose").addEventListener("click", () => $("detailDlg").close());
    refresh();
    setInterval(refresh, 10000);
  </script>
</body>
</html>
"""
