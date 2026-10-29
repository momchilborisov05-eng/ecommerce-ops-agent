// E-commerce Ops Agent dashboard - vanilla JS, hash routing, no build step.
"use strict";

const view = document.getElementById("view");
const titleEl = document.getElementById("page-title");
let state = { mode: "demo" };

// ---------- helpers ----------
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => Number(n || 0).toLocaleString("en-US");
const money = (n, d = 4) => "$" + Number(n || 0).toFixed(d);
const ms = (n) => (n == null ? "–" : n < 1000 ? `${Math.round(n)} ms` : `${(n / 1000).toFixed(1)} s`);
const time = (iso) => (iso ? new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z").toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "–");

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 2600);
}

const STATUS = { completed: "ok", executed: "ok", resolved: "ok", ok: "ok", pending: "warn", running: "info", duplicate: "info",
  failed: "bad", error: "bad", blocked: "bad", stopped: "bad", incomplete: "warn", rejected: "neutral" };
const pill = (s) => `<span class="pill ${STATUS[s] || "neutral"}"><span class="dot"></span>${esc(s)}</span>`;
const wf = (w) => `<span class="wf ${esc(w)}">${esc(w)}</span>`;
const integ = (i) => `<span class="int ${esc(i)}">${esc(i)}</span>`;
const prio = (p) => `<span class="pill ${{ urgent: "bad", high: "warn", normal: "info", low: "neutral", medium: "warn" }[p] || "neutral"}">${esc(p)}</span>`;

function json(value) {
  // Tokenize the raw JSON, then escape each piece (outside strings JSON is only punctuation and whitespace).
  const raw = JSON.stringify(value, null, 2) ?? "null";
  return raw.replace(/("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/g, (m, str, colon, bool) => {
    if (str) return colon ? `<span class="k">${esc(str)}</span>${colon}` : `<span class="s">${esc(str)}</span>`;
    if (bool) return `<span class="b">${m}</span>`;
    return `<span class="n">${m}</span>`;
  });
}
const jsonBlock = (label, value, open = false) =>
  value == null ? "" : `<details ${open ? "open" : ""}><summary>${esc(label)}</summary><pre class="json">${json(value)}</pre></details>`;

// ---------- shell ----------
async function refreshShell() {
  try {
    const o = await api("/api/overview");
    state = o;
    const pending = o.approvals.pending || 0;
    document.getElementById("pending-count").textContent = pending || "";
    document.getElementById("mode").innerHTML = o.mode === "demo"
      ? `<span class="pill demo"><span class="dot"></span>Demo mode · scripted LLM, no API calls</span>`
      : `<span class="pill ok"><span class="dot"></span>Live · ${esc(o.model)}</span>`;
    document.getElementById("mode").innerHTML += `<span class="pill neutral">Store date ${esc(o.store_date)}</span>`;
    document.getElementById("backends").innerHTML = `<div style="margin-bottom:4px;font-weight:600;color:#e2e8f0">Integrations</div>` +
      Object.entries(o.backends).map(([k, v]) => `<div class="row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("");
    return o;
  } catch (e) {
    view.innerHTML = `<div class="empty">Could not load data: ${esc(e.message)}</div>`;
  }
}

// ---------- pages ----------
async function pageOverview() {
  const o = await refreshShell();
  if (!o) return;
  const t = o.totals;
  const rate = t.runs ? Math.round((t.completed / t.runs) * 100) : 0;
  const calls = o.integrations.reduce((a, r) => a + r.calls, 0);
  const retries = o.integrations.reduce((a, r) => a + (r.retries || 0), 0);
  const maxTriage = Math.max(1, ...o.triage.map((r) => r.n));
  const est = t.estimated ? `<span class="pill demo" title="Demo mode: token counts are estimated from text length">estimated</span>` : "";
  view.innerHTML = `
    ${o.mode === "demo" ? `<div class="banner"><b>Demo mode.</b> Model responses come from a deterministic script (labelled <span class="mono">[demo]</span>), so everything runs offline. Tools, guardrails, approvals and integrations run the real code; Slack, ClickUp and Gmail use sandbox adapters.</div>` : ""}
    <div class="grid kpis">
      <div class="card kpi"><div class="label">Agent runs</div><div class="value">${fmt(t.runs)}</div><div class="sub">${fmt(t.llm_calls)} model calls</div></div>
      <div class="card kpi"><div class="label">Success rate</div><div class="value">${rate}%</div><div class="sub">${fmt(t.failed || 0)} failed or stopped</div></div>
      <div class="card kpi"><div class="label">Tool calls</div><div class="value">${fmt(t.tool_calls)}</div><div class="sub">avg run ${ms(t.avg_ms)}</div></div>
      <div class="card kpi"><div class="label">Approvals</div><div class="value">${fmt(o.approvals.pending || 0)}</div><div class="sub"><a href="#/approvals">pending review →</a></div></div>
      <div class="card kpi"><div class="label">API calls</div><div class="value">${fmt(calls)}</div><div class="sub">${fmt(retries)} retried</div></div>
      <div class="card kpi"><div class="label">LLM cost</div><div class="value">${money(t.cost_usd, 2)}</div><div class="sub">${est ? est + " " : ""}${fmt(t.input_tokens + t.output_tokens)} tokens</div></div>
    </div>
    <div class="grid two">
      <div class="card"><h2>Workflows</h2>
        <table><thead><tr><th>Workflow</th><th class="num">Runs</th><th class="num">OK</th><th class="num">Tool calls</th><th class="num">Avg time</th><th class="num">Cost</th><th></th></tr></thead><tbody>
        ${o.by_workflow.map((w) => `<tr><td>${wf(w.workflow)}</td><td class="num">${w.runs}</td><td class="num">${w.completed}</td><td class="num">${w.tool_calls}</td><td class="num">${ms(w.avg_ms)}</td><td class="num">${money(w.cost_usd)}</td>
          <td class="num"><button class="sm" data-run="${esc(w.workflow)}">Run now</button></td></tr>`).join("") || `<tr><td colspan="7" class="empty">No runs yet. Run a workflow from the Workflows page.</td></tr>`}
        </tbody></table></div>
      <div class="card"><h2>Inbox triage <span class="muted small">${fmt(o.triage.reduce((a, r) => a + r.n, 0))} emails</span></h2>
        <div class="card-body bars">${o.triage.map((r) => `<div class="bar-row"><span>${esc(r.category.replace("_", " "))}</span><div class="bar-track"><div class="bar-fill" style="width:${(r.n / maxTriage) * 100}%"></div></div><span class="num">${r.n}</span></div>`).join("") || `<div class="muted">No emails triaged yet.</div>`}</div></div>
    </div>
    <div class="grid two">
      <div class="card"><h2>Recent runs <a class="small" href="#/runs">all runs →</a></h2>${runTable(o.recent_runs, true)}</div>
      <div class="card"><h2>Tool usage</h2>
        <table class="tight"><thead><tr><th>Tool</th><th class="num">Calls</th><th class="num">Dupes</th><th class="num">Errors</th></tr></thead><tbody>
        ${o.tools.map((r) => `<tr><td class="mono">${esc(r.name)}</td><td class="num">${r.n}</td><td class="num">${r.duplicates || 0}</td><td class="num">${r.errors || 0}</td></tr>`).join("")}
        </tbody></table></div>
    </div>`;
  bindRunButtons();
}

function runTable(runs, compact = false) {
  if (!runs.length) return `<div class="empty">No runs yet.</div>`;
  return `<table class="${compact ? "compact" : ""}"><thead><tr><th>#</th><th>Workflow</th><th>Subject</th><th>Status</th>${compact ? "" : `<th class="num">Model calls</th><th class="num">Tools</th><th class="num">Tokens</th><th class="num">Cost</th>`}<th class="num">Time</th>${compact ? "" : "<th>Started</th>"}</tr></thead><tbody>
    ${runs.map((r) => `<tr class="click" data-href="#/runs/${r.id}"><td class="muted">${r.id}</td><td>${wf(r.workflow)}</td><td class="subject" title="${esc(r.subject)}">${esc(r.subject)}</td><td>${pill(r.status)}</td>
      ${compact ? "" : `<td class="num">${r.llm_calls}</td><td class="num">${r.tool_calls}</td><td class="num">${fmt(r.input_tokens + r.cache_read_tokens + r.cache_write_tokens + r.output_tokens)}</td><td class="num">${money(r.cost_usd)}</td>`}
      <td class="num">${ms(r.duration_ms)}</td>${compact ? "" : `<td class="muted small">${time(r.started_at)}</td>`}</tr>`).join("")}
  </tbody></table>`;
}

async function pageRuns(params) {
  await refreshShell();
  const qs = new URLSearchParams();
  if (params.get("workflow")) qs.set("workflow", params.get("workflow"));
  if (params.get("status")) qs.set("status", params.get("status"));
  const runs = await api("/api/runs?" + qs);
  const opt = (v, cur) => `<option value="${v}" ${v === (cur || "") ? "selected" : ""}>${v || "all"}</option>`;
  view.innerHTML = `
    <div class="toolbar">
      <label class="muted small">Workflow</label>
      <select id="f-wf">${["", "inbox-triage", "daily-digest", "review-monitor"].map((v) => opt(v, params.get("workflow"))).join("")}</select>
      <label class="muted small">Status</label>
      <select id="f-st">${["", "completed", "failed", "stopped", "incomplete"].map((v) => opt(v, params.get("status"))).join("")}</select>
      <span class="muted small" style="margin-left:auto">${runs.length} runs</span>
    </div>
    <div class="card">${runTable(runs)}</div>`;
  const go = () => {
    const q = new URLSearchParams();
    const w = document.getElementById("f-wf").value, s = document.getElementById("f-st").value;
    if (w) q.set("workflow", w);
    if (s) q.set("status", s);
    location.hash = "#/runs" + (q.toString() ? "?" + q : "");
  };
  document.getElementById("f-wf").onchange = go;
  document.getElementById("f-st").onchange = go;
}

function stepView(s) {
  const kindLabel = { llm: "M", tool: "T", guardrail: "!", approval: "A", retry: "R", error: "×" }[s.kind] || "·";
  let body = "";
  if (s.kind === "llm" && s.output) {
    const blocks = s.output.content || [];
    const says = blocks.filter((b) => b.type === "text").map((b) => `<div class="say">${esc(b.text)}</div>`).join("");
    const calls = blocks.filter((b) => b.type === "tool_use");
    body = says + (calls.length ? `<div class="calls">${calls.map((c) => `<span class="call-chip">${esc(c.name)}()</span>`).join("")}</div>` : "");
    body += jsonBlock("Requested tool calls", calls.length ? calls : null);
  } else {
    body = jsonBlock("Input", s.input) + jsonBlock("Output", s.output, s.status !== "ok");
  }
  const tokens = s.input_tokens != null ? `<span>${fmt(s.input_tokens)} in · ${fmt(s.output_tokens)} out</span>` : "";
  const title = s.kind === "llm" ? `model · ${esc(s.name)}` : esc(s.name);
  return `<div class="step ${esc(s.kind)}"><div class="marker">${kindLabel}</div><div class="step-card">
    <div class="step-top"><span class="name">${title}</span>${pill(s.status)}<span class="pill neutral">${esc(s.kind)}</span>
      <span class="meta">${tokens}<span>${ms(s.duration_ms)}</span><span>#${s.seq}</span></span></div>
    ${body ? `<div class="step-body">${body}</div>` : ""}</div></div>`;
}

async function pageRun(id) {
  await refreshShell();
  const r = await api(`/api/runs/${id}`);
  titleEl.textContent = `Run #${r.id}`;
  const est = r.usage_estimated ? ` <span class="pill demo">estimated</span>` : "";
  view.innerHTML = `
    <div class="run-head"><div><h2>${esc(r.subject)}</h2>
      <div style="display:flex;gap:8px;align-items:center">${wf(r.workflow)} ${pill(r.status)} <span class="muted small">${esc(r.mode)} · ${esc(r.model)} · started ${time(r.started_at)}</span></div></div>
      <a href="#/runs" class="btn">← All runs</a></div>
    <div class="card metrics">
      <div><div class="label">Model calls</div><div class="value">${r.llm_calls}</div></div>
      <div><div class="label">Tool calls</div><div class="value">${r.tool_calls}</div></div>
      <div><div class="label">Input tokens</div><div class="value">${fmt(r.input_tokens + r.cache_read_tokens + r.cache_write_tokens)}</div></div>
      <div><div class="label">Cached</div><div class="value">${fmt(r.cache_read_tokens)}</div></div>
      <div><div class="label">Output tokens</div><div class="value">${fmt(r.output_tokens)}</div></div>
      <div><div class="label">Cost${est}</div><div class="value">${money(r.cost_usd)}</div></div>
    </div>
    ${r.final_text ? `<div class="final"><b>Result:</b> ${esc(r.final_text)}</div>` : ""}
    ${r.error ? `<div class="final error"><b>Error:</b> ${esc(r.error)}</div>` : ""}
    <div class="grid two">
      <div><div class="card-head" style="padding:0 0 10px;border:0">Step-by-step trace <span class="muted small">${r.steps.length} steps · ${ms(r.duration_ms)}</span></div>
        <div class="timeline">${r.steps.map(stepView).join("")}</div></div>
      <div>
        <div class="card" style="margin-bottom:16px"><h2>Approvals from this run</h2><div class="side-list">
          ${r.approvals.map((a) => `<div class="item"><div style="display:flex;gap:8px;align-items:center;justify-content:space-between"><b>${esc(a.title)}</b>${pill(a.status)}</div><div class="muted small">${esc(a.action)} · <a href="#/approvals?status=all">open queue</a></div></div>`).join("") || `<div class="item muted">None</div>`}
        </div></div>
        <div class="card"><h2>Integration calls</h2><div class="side-list">
          ${r.integration_calls.map((c) => `<div class="item"><div style="display:flex;gap:8px;align-items:center">${integ(c.integration)}<span class="mono">${esc(c.operation)}</span>${pill(c.status)}<span class="muted small" style="margin-left:auto">${esc(c.backend)} · ${c.attempts} attempt${c.attempts > 1 ? "s" : ""}</span></div>${jsonBlock("Request", c.request)}${jsonBlock("Response", c.response)}</div>`).join("") || `<div class="item muted">None</div>`}
        </div></div>
      </div>
    </div>`;
}

function approvalBody(a) {
  const p = a.payload || {};
  if (a.action === "send_email_reply") {
    return `<div class="mail"><div class="hdr"><div><span>To</span>${esc(p.to)}</div><div><span>Subject</span><b>${esc(p.subject)}</b></div><div><span>Draft</span><span class="mono" style="width:auto">${esc(p.draft_id)}</span></div></div><div class="msg">${esc(p.body)}</div></div>`;
  }
  if (a.action === "issue_refund") {
    return `<div class="amount">${money(p.amount, 2)}</div><div class="kv" style="margin-top:8px">
      <div class="k">Order</div><div class="mono">${esc(p.order_id)}</div><div class="k">Order total</div><div>${money(p.order_total, 2)}</div>
      <div class="k">Customer</div><div>${esc(p.customer)}</div><div class="k">Marketplace</div><div>${esc(p.marketplace)}</div>
      <div class="k">Reason</div><div>${esc(p.reason)}</div></div>`;
  }
  return `<div class="kv"><div class="k">Severity</div><div>${prio(p.severity)}</div><div class="k">Source</div><div class="mono">${esc(p.source_ref)}</div><div class="k">Workflow</div><div>${wf(p.workflow)}</div></div>`;
}

async function pageApprovals(params) {
  await refreshShell();
  const status = params.get("status") || "pending";
  const items = await api(`/api/approvals?status=${status}`);
  const label = { send_email_reply: "Send email", issue_refund: "Refund", human_review: "Human review" };
  const tone = { send_email_reply: "info", issue_refund: "warn", human_review: "bad" };
  view.innerHTML = `
    <div class="toolbar"><div class="tabs">${["pending", "executed", "rejected", "resolved", "all"].map((s) => `<button class="${s === status ? "active" : ""}" data-status="${s}">${s}</button>`).join("")}</div>
      <span class="muted small" style="margin-left:auto">Risky actions wait here until a person decides. Approving runs the action; rejecting discards it.</span></div>
    <div class="approvals">${items.map((a) => `
      <div class="card ap" id="ap-${a.id}">
        <div class="ap-top"><span class="pill ${tone[a.action]}">${label[a.action] || esc(a.action)}</span><span class="ap-title" title="${esc(a.title)}">${esc(a.title)}</span>${pill(a.status)}</div>
        <div class="ap-body"><div class="reason">${esc(a.reason)}</div>${approvalBody(a)}
          ${a.result ? jsonBlock("Result", a.result) : ""}${a.note ? `<div class="small muted" style="margin-top:6px">Note: ${esc(a.note)}</div>` : ""}</div>
        <div class="ap-foot">${a.status === "pending" ? `<input type="text" placeholder="Note (optional)" id="note-${a.id}">
          <button class="reject" data-act="reject" data-id="${a.id}">Reject</button>
          <button class="approve" data-act="approve" data-id="${a.id}">${a.kind === "review" ? "Mark resolved" : "Approve"}</button>`
          : `<span class="muted small">${esc(a.decided_by || "")} · ${time(a.decided_at)}</span>`}
          <a class="small" style="margin-left:auto" href="#/runs/${a.run_id}">run #${a.run_id}</a></div>
      </div>`).join("") || `<div class="card empty">Nothing ${status === "all" ? "" : status} here.</div>`}</div>`;
  view.querySelectorAll("[data-status]").forEach((b) => (b.onclick = () => (location.hash = `#/approvals?status=${b.dataset.status}`)));
  view.querySelectorAll("[data-act]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    const id = b.dataset.id;
    try {
      const res = await api(`/api/approvals/${id}/${b.dataset.act}`, { method: "POST", body: JSON.stringify({ note: document.getElementById(`note-${id}`).value }) });
      toast(`#${id} ${res.status}`);
      route();
    } catch (e) { toast(e.message); b.disabled = false; }
  }));
}

async function pageIntegrations(params) {
  await refreshShell();
  const which = params.get("integration") || "";
  const calls = await api("/api/integrations" + (which ? `?integration=${which}` : ""));
  view.innerHTML = `
    <div class="toolbar"><div class="tabs">${["", "slack", "clickup", "gmail"].map((s) => `<button class="${s === which ? "active" : ""}" data-int="${s}">${s || "all"}</button>`).join("")}</div>
      <span class="muted small" style="margin-left:auto">Every call to Slack, ClickUp and Gmail, with retries. Personal data is redacted before it is stored.</span></div>
    <div class="card"><table><thead><tr><th>#</th><th>Time</th><th>Integration</th><th>Backend</th><th>Operation</th><th>Status</th><th class="num">Attempts</th><th>Run</th><th>Request</th></tr></thead><tbody>
      ${calls.map((c) => `<tr><td class="muted">${c.id}</td><td class="muted small nowrap">${time(c.created_at)}</td><td>${integ(c.integration)}</td><td><span class="pill ${c.backend === "live" ? "ok" : "neutral"}">${esc(c.backend)}</span></td>
        <td class="mono">${esc(c.operation)}</td><td>${pill(c.status)}</td><td class="num">${c.attempts}</td><td>${c.run_id ? `<a href="#/runs/${c.run_id}">#${c.run_id}</a>` : "–"}</td>
        <td style="max-width:330px"><div class="subject small" style="max-width:330px">${esc(c.request.name || c.request.subject || c.request.channel || c.request.draft_id || "")}${c.request.text ? " · " + esc(c.request.text.split("\n")[0]) : ""}</div>${jsonBlock("details", { request: c.request, response: c.response })}</td></tr>`).join("") || `<tr><td colspan="9" class="empty">No integration calls yet.</td></tr>`}
    </tbody></table></div>`;
  view.querySelectorAll("[data-int]").forEach((b) => (b.onclick = () => (location.hash = "#/integrations" + (b.dataset.int ? `?integration=${b.dataset.int}` : ""))));
}

async function pageWorkflows() {
  await refreshShell();
  const wfs = await api("/api/workflows");
  view.innerHTML = `<div class="grid wf-cards">${wfs.map((w) => `
    <div class="card"><h2>${esc(w.title)} ${wf(w.name)}</h2><div class="card-body">
      <p style="margin-top:0">${esc(w.description)}</p>
      <div class="kv"><div class="k">Trigger</div><div>${esc(w.trigger)}</div><div class="k">Max steps</div><div>${w.max_steps} model calls</div><div class="k">Max tool calls</div><div>${w.max_tool_calls}</div></div>
      <div class="small muted" style="margin-top:12px;font-weight:600">Allowed tools</div><div class="chips">${w.allowed_tools.map((t) => `<span class="chip">${esc(t)}</span>`).join("")}</div>
      <div style="margin-top:14px"><button class="primary" data-run="${esc(w.name)}">Run now</button></div>
    </div></div>`).join("")}</div>`;
  bindRunButtons();
}

function bindRunButtons() {
  view.querySelectorAll("[data-run]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    b.textContent = "Running…";
    try {
      const res = await api(`/api/workflows/${b.dataset.run}/run`, { method: "POST", body: "{}" });
      toast(res.runs.length ? `${res.runs.length} run(s) finished` : "Nothing new to process");
      location.hash = res.runs.length === 1 ? `#/runs/${res.runs[0].run_id}` : `#/runs?workflow=${b.dataset.run}`;
    } catch (e) { toast(e.message); b.disabled = false; b.textContent = "Run now"; }
  }));
}

// ---------- router ----------
const TITLES = { overview: "Overview", runs: "Runs", approvals: "Approvals", integrations: "Integrations", workflows: "Workflows" };

async function route() {
  const [path, query] = (location.hash.replace(/^#/, "") || "/overview").split("?");
  const params = new URLSearchParams(query || "");
  const parts = path.split("/").filter(Boolean);
  const page = parts[0] || "overview";
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  titleEl.textContent = TITLES[page] || "Overview";
  try {
    if (page === "runs" && parts[1]) await pageRun(parts[1]);
    else if (page === "runs") await pageRuns(params);
    else if (page === "approvals") await pageApprovals(params);
    else if (page === "integrations") await pageIntegrations(params);
    else if (page === "workflows") await pageWorkflows();
    else await pageOverview();
  } catch (e) {
    view.innerHTML = `<div class="card empty">${esc(e.message)}</div>`;
  }
}

view.addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-href]");
  if (row && !e.target.closest("a,button,details")) location.hash = row.dataset.href;
});
window.addEventListener("hashchange", route);
route();
