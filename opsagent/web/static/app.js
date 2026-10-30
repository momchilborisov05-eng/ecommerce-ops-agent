// E-commerce Ops Agent dashboard - vanilla JS, hash routing, no build step.
// GSAP (from a CDN) only adds motion: without it, or with reduced motion, everything renders and works the same.
"use strict";

const view = document.getElementById("view");
const titleEl = document.getElementById("page-title");
let state = { mode: "demo" };
// ?motion=off also disables animation (handy for screenshots and demos on slow machines).
const motion = typeof gsap !== "undefined" && !matchMedia("(prefers-reduced-motion: reduce)").matches && !/[?&]motion=off/.test(location.search);

// ---------- helpers ----------
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => Number(n || 0).toLocaleString("en-US");
const money = (n, d = 4) => "$" + Number(n || 0).toFixed(d);
const ms = (n) => (n == null ? "–" : n < 1000 ? `${Math.round(n)} ms` : `${(n / 1000).toFixed(1)} s`);
const time = (iso) => (iso ? new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z").toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "–");

const ICONS = {
  runs: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  tool: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17v3h3l5.3-5.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z"/>',
  shield: '<path d="M12 3l7 3v5c0 4.5-3 8.5-7 10-4-1.5-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/>',
  plug: '<path d="M7 7h11l-3-3M17 17H6l3 3"/>',
  coin: '<circle cx="12" cy="12" r="8.5"/><path d="M14.5 9.5c-.4-.9-1.4-1.5-2.5-1.5-1.5 0-2.5.8-2.5 2s1 1.6 2.5 2 2.5.8 2.5 2-1 2-2.5 2c-1.1 0-2.1-.6-2.5-1.5M12 6.5v11"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v4h1"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  play: '<path d="M7 5l11 7-11 7z"/>',
  back: '<path d="M15 6l-6 6 6 6"/>',
  inbox: '<path d="M3 13l3-8h12l3 8v6H3z"/><path d="M3 13h5l1 2h6l1-2h5"/>',
};
const icon = (name, cls = "i") => `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ""}</svg>`;

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

function toast(msg, tone = "info") {
  const t = document.getElementById("toast");
  t.className = `toast ${tone}`;
  t.innerHTML = icon(tone === "ok" ? "check" : tone === "bad" ? "x" : "info") + `<span>${esc(msg)}</span>`;
  void t.offsetWidth;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 2800);
}

const STATUS = { completed: "ok", executed: "ok", resolved: "ok", ok: "ok", pending: "warn", running: "info", duplicate: "info",
  failed: "bad", error: "bad", blocked: "bad", stopped: "bad", incomplete: "warn", rejected: "neutral" };
const pill = (s) => `<span class="pill ${STATUS[s] || "neutral"}"><span class="dot"></span>${esc(s)}</span>`;
const wf = (w) => `<span class="wf ${esc(w)}">${esc(w)}</span>`;
const integ = (i) => `<span class="int ${esc(i)}">${esc(i)}</span>`;
const prio = (p) => `<span class="pill ${{ urgent: "bad", high: "warn", normal: "info", low: "neutral", medium: "warn" }[p] || "neutral"}">${esc(p)}</span>`;
const empty = (text, ico = "inbox") => `<div class="empty">${icon(ico)}${text}</div>`;

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

// ---------- motion ----------
const lastValue = {};
// Count a number up from its previous value. data-count holds the target, data-format how to print it.
const FORMATS = { int: (v) => fmt(Math.round(v)), pct: (v) => `${Math.round(v)}%`, usd2: (v) => money(v, 2), usd4: (v) => money(v, 4) };
function countUp(root) {
  root.querySelectorAll("[data-count]").forEach((el) => {
    const to = Number(el.dataset.count), f = FORMATS[el.dataset.format || "int"], key = el.dataset.key || "";
    const from = key && key in lastValue ? lastValue[key] : 0;
    if (key) lastValue[key] = to;
    el.textContent = f(to);
    if (!motion || from === to) return;
    const o = { v: from };
    el.textContent = f(from);
    gsap.to(o, { v: to, duration: 0.9, ease: "power2.out", onUpdate: () => (el.textContent = f(o.v)) });
  });
}
const num = (value, format = "int", key = "") => `<span data-count="${Number(value) || 0}" data-format="${format}" data-key="${esc(key)}">${FORMATS[format](Number(value) || 0)}</span>`;

// If frames are throttled (background tab, headless capture), finish entry animations so nothing is left half-faded.
function settleSoon(msDelay = 1600) {
  if (!motion) return;
  clearTimeout(settleSoon.t);
  settleSoon.t = setTimeout(() => gsap.globalTimeline.getChildren(false, true, true).forEach((t) => { if (!t.vars.repeat) t.progress(1); }), msDelay);
}

let firstPaint = true;
function enter() {
  countUp(view);
  if (!motion) return;
  const tl = gsap.timeline({ defaults: { ease: "power3.out", clearProps: "opacity,transform" } });
  if (firstPaint) {
    tl.from(".sidebar", { x: -12, opacity: 0, duration: 0.5 }, 0).from(".topbar", { y: -12, opacity: 0, duration: 0.5 }, 0.05);
    gsap.to(".glow.a", { x: 120, y: 40, duration: 26, ease: "sine.inOut", yoyo: true, repeat: -1 });
    gsap.to(".glow.b", { x: -100, y: 30, duration: 22, ease: "sine.inOut", yoyo: true, repeat: -1 });
  }
  const blocks = view.querySelectorAll(":scope > .banner, :scope > .toolbar, :scope > .run-head, .kpis > .card, :scope > .metrics, :scope > .final, .two > *, .approvals > .card, .wf-cards > .card, :scope > .card");
  tl.from(blocks, { y: 16, opacity: 0, duration: 0.5, stagger: 0.05 }, firstPaint ? 0.12 : 0);
  const rows = [...view.querySelectorAll("tbody tr")].slice(0, 25);
  if (rows.length) tl.from(rows, { opacity: 0, y: 8, duration: 0.35, stagger: 0.03 }, firstPaint ? 0.35 : 0.15);
  const fills = view.querySelectorAll(".bar-fill");
  if (fills.length) gsap.from(fills, { scaleX: 0, duration: 0.9, ease: "power3.out", stagger: 0.05, delay: 0.25, clearProps: "transform" });
  firstPaint = false;
  settleSoon();
}

function revealTimeline() {
  if (!motion) return;
  const rail = view.querySelector(".timeline .rail");
  const steps = view.querySelectorAll(".timeline .step");
  const tl = gsap.timeline({ delay: 0.15 });
  if (rail) tl.from(rail, { scaleY: 0, duration: Math.min(1.4, 0.25 + steps.length * 0.06), ease: "power2.inOut", clearProps: "transform" }, 0);
  tl.from(view.querySelectorAll(".timeline .marker"), { scale: 0.4, opacity: 0, duration: 0.35, stagger: 0.06, ease: "back.out(2.2)", clearProps: "opacity,transform" }, 0.05);
  tl.from(view.querySelectorAll(".timeline .step-card"), { x: 12, opacity: 0, duration: 0.4, stagger: 0.06, ease: "power3.out", clearProps: "opacity,transform" }, 0.1);
  settleSoon(2400);
}

// ---------- theme & mobile nav ----------
document.getElementById("theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("opsagent-theme", next); } catch (e) { /* storage blocked: still switches */ }
  if (motion) gsap.fromTo("#theme-toggle svg", { rotate: -60, scale: 0.6 }, { rotate: 0, scale: 1, duration: 0.45, ease: "back.out(2)" });
});
const menuBtn = document.getElementById("menu-btn"), scrim = document.getElementById("scrim");
function setNav(open) {
  document.body.classList.toggle("nav-open", open);
  menuBtn.setAttribute("aria-expanded", String(open));
  scrim.hidden = !open;
}
menuBtn.addEventListener("click", () => setNav(!document.body.classList.contains("nav-open")));
scrim.addEventListener("click", () => setNav(false));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") setNav(false); });

function moveNavIndicator() {
  const ind = document.querySelector(".nav-ind"), a = document.querySelector("#nav a.active");
  if (!ind) return;
  ind.classList.toggle("on", !!a);
  if (a) { ind.style.height = a.offsetHeight + "px"; ind.style.transform = `translateY(${a.offsetTop}px)`; }
}
window.addEventListener("resize", moveNavIndicator);

// ---------- shell ----------
async function refreshShell() {
  try {
    const o = await api("/api/overview");
    state = o;
    const pending = o.approvals.pending || 0;
    const pc = document.getElementById("pending-count");
    if (pc.textContent !== String(pending || "")) {
      pc.textContent = pending || "";
      if (motion && pending) gsap.fromTo(pc, { scale: 1.5 }, { scale: 1, duration: 0.5, ease: "back.out(3)", clearProps: "transform" });
    }
    document.getElementById("mode").innerHTML = (o.mode === "demo"
      ? `<span class="pill demo" title="Model responses come from a deterministic script; no API calls"><span class="dot"></span><span>Demo mode<span class="long"> · scripted LLM</span></span></span>`
      : `<span class="pill ok"><span class="dot live"></span>Live · ${esc(o.model)}</span>`) + `<span class="pill neutral">Store date ${esc(o.store_date)}</span>`;
    document.getElementById("backends").innerHTML = `<div class="title">Integrations</div>` +
      Object.entries(o.backends).map(([k, v]) => `<div class="row"><span>${esc(k)}</span><b class="${esc(v)}">${esc(v)}</b></div>`).join("");
    return o;
  } catch (e) {
    view.innerHTML = `<div class="card">${empty(`Could not load data: ${esc(e.message)}`, "x")}</div>`;
  }
}

// ---------- pages ----------
function kpi(label, ico, valueHtml, sub, cls = "") {
  return `<div class="card kpi lift ${cls}"><div class="label"><span class="spark">${icon(ico)}</span><span class="t">${esc(label)}</span></div><div class="value">${valueHtml}</div><div class="sub">${sub}</div></div>`;
}

async function pageOverview() {
  const o = await refreshShell();
  if (!o) return;
  const t = o.totals;
  const rate = t.runs ? Math.round((t.completed / t.runs) * 100) : 0;
  const calls = o.integrations.reduce((a, r) => a + r.calls, 0);
  const retries = o.integrations.reduce((a, r) => a + (r.retries || 0), 0);
  const maxTriage = Math.max(1, ...o.triage.map((r) => r.n));
  const est = t.estimated ? `<span class="pill demo" title="Demo mode: token counts are estimated from text length">estimated</span>` : "";
  const pending = o.approvals.pending || 0;
  view.innerHTML = `
    ${o.mode === "demo" ? `<div class="banner">${icon("info")}<div><b>Demo mode.</b> Model responses come from a deterministic script (labelled <span class="mono">[demo]</span>), so everything runs offline. Tools, guardrails, approvals and integrations run the real code; Slack, ClickUp and Gmail use sandbox adapters.</div></div>` : ""}
    <div class="grid kpis">
      ${kpi("Agent runs", "runs", num(t.runs, "int", "runs"), `${fmt(t.llm_calls)} model calls`)}
      ${kpi("Success rate", "check", num(rate, "pct", "rate"), `${fmt(t.failed || 0)} failed or stopped`)}
      ${kpi("Tool calls", "tool", num(t.tool_calls, "int", "tools"), `avg run ${ms(t.avg_ms)}`)}
      ${kpi("Approvals", "shield", num(pending, "int", "pending"), `<a href="#/approvals">pending review →</a>`, pending ? "attention" : "")}
      ${kpi("API calls", "plug", num(calls, "int", "calls"), `${fmt(retries)} retried`)}
      ${kpi("LLM cost", "coin", num(t.cost_usd, "usd2", "cost"), `${est ? est + " " : ""}${fmt(t.input_tokens + t.output_tokens)} tokens`)}
    </div>
    <div class="grid two">
      <div class="card"><h2>Workflows <a class="small" href="#/workflows">definitions →</a></h2><div class="table-wrap">
        <table><thead><tr><th>Workflow</th><th class="num">Runs</th><th class="num">OK</th><th class="num">Tool calls</th><th class="num">Avg time</th><th class="num">Cost</th><th></th></tr></thead><tbody>
        ${o.by_workflow.map((w) => `<tr><td>${wf(w.workflow)}</td><td class="num">${w.runs}</td><td class="num">${w.completed}</td><td class="num">${w.tool_calls}</td><td class="num">${ms(w.avg_ms)}</td><td class="num">${money(w.cost_usd)}</td>
          <td class="num"><button class="sm" data-run="${esc(w.workflow)}" title="Run ${esc(w.workflow)} now">${icon("play")}Run</button></td></tr>`).join("") || `<tr><td colspan="7">${empty("No runs yet. Run a workflow from the Workflows page.")}</td></tr>`}
        </tbody></table></div></div>
      <div class="card"><h2>Inbox triage <span class="muted small">${fmt(o.triage.reduce((a, r) => a + r.n, 0))} emails</span></h2>
        <div class="card-body bars">${o.triage.map((r) => `<div class="bar-row"><span title="${esc(r.category)}">${esc(r.category.replace("_", " "))}</span><div class="bar-track"><div class="bar-fill" style="width:${(r.n / maxTriage) * 100}%"></div></div><span class="num">${r.n}</span></div>`).join("") || `<div class="muted">No emails triaged yet.</div>`}</div></div>
    </div>
    <div class="grid two">
      <div class="card"><h2>Recent runs <a class="small" href="#/runs">all runs →</a></h2>${runTable(o.recent_runs, true)}</div>
      <div class="card"><h2>Tool usage <span class="muted small">incl. blocked duplicates</span></h2><div class="table-wrap">
        <table class="tight"><thead><tr><th>Tool</th><th class="num">Calls</th><th class="num">Dupes</th><th class="num">Errors</th></tr></thead><tbody>
        ${o.tools.map((r) => `<tr><td class="mono">${esc(r.name)}</td><td class="num">${r.n}</td><td class="num">${r.duplicates ? `<span class="pill info">${r.duplicates}</span>` : 0}</td><td class="num">${r.errors ? `<span class="pill bad">${r.errors}</span>` : 0}</td></tr>`).join("") || `<tr><td colspan="4">${empty("No tool calls yet.")}</td></tr>`}
        </tbody></table></div></div>
    </div>`;
  bindRunButtons();
  enter();
}

function runTable(runs, compact = false) {
  if (!runs.length) return empty("No runs yet.", "runs");
  return `<div class="table-wrap ${compact ? "" : "scroll"}"><table class="${compact ? "compact" : "tight"}"><thead><tr><th>#</th><th>Workflow</th><th>Subject</th><th>Status</th>${compact ? "" : `<th class="num">Model calls</th><th class="num">Tools</th><th class="num">Tokens</th><th class="num">Cost</th>`}<th class="num">Time</th>${compact ? "" : "<th>Started</th>"}</tr></thead><tbody>
    ${runs.map((r) => `<tr class="click" data-href="#/runs/${r.id}"><td class="id">${r.id}</td><td>${wf(r.workflow)}</td><td class="subject" title="${esc(r.subject)}">${esc(r.subject)}</td><td>${pill(r.status)}</td>
      ${compact ? "" : `<td class="num">${r.llm_calls}</td><td class="num">${r.tool_calls}</td><td class="num">${fmt(r.input_tokens + r.cache_read_tokens + r.cache_write_tokens + r.output_tokens)}</td><td class="num">${money(r.cost_usd)}</td>`}
      <td class="num">${ms(r.duration_ms)}</td>${compact ? "" : `<td class="muted small nowrap">${time(r.started_at)}</td>`}</tr>`).join("")}
  </tbody></table></div>`;
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
      <label class="muted small" for="f-wf">Workflow</label>
      <select id="f-wf">${["", "inbox-triage", "daily-digest", "review-monitor"].map((v) => opt(v, params.get("workflow"))).join("")}</select>
      <label class="muted small" for="f-st">Status</label>
      <select id="f-st">${["", "completed", "failed", "stopped", "incomplete"].map((v) => opt(v, params.get("status"))).join("")}</select>
      <span class="muted small hint"><b style="color:var(--text)">${num(runs.length, "int", "runs-count")}</b> runs</span>
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
  enter();
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
  const r = await api(`/api/runs/${encodeURIComponent(id)}`);
  titleEl.textContent = `Run #${r.id}`;
  const est = r.usage_estimated ? ` <span class="pill demo">estimated</span>` : "";
  view.innerHTML = `
    <div class="run-head"><div><h2>${esc(r.subject)}</h2>
      <div class="tags">${wf(r.workflow)} ${pill(r.status)} <span class="muted small">${esc(r.mode)} · ${esc(r.model)} · started ${time(r.started_at)}</span></div></div>
      <a href="#/runs" class="btn">${icon("back")}All runs</a></div>
    <div class="card metrics">
      <div><div class="label">Model calls</div><div class="value">${num(r.llm_calls)}</div></div>
      <div><div class="label">Tool calls</div><div class="value">${num(r.tool_calls)}</div></div>
      <div><div class="label">Input tokens</div><div class="value">${num(r.input_tokens + r.cache_read_tokens + r.cache_write_tokens)}</div></div>
      <div><div class="label">Cached</div><div class="value">${num(r.cache_read_tokens)}</div></div>
      <div><div class="label">Output tokens</div><div class="value">${num(r.output_tokens)}</div></div>
      <div><div class="label">Cost${est}</div><div class="value">${num(r.cost_usd, "usd4")}</div></div>
    </div>
    ${r.final_text ? `<div class="final"><b>Result:</b> ${esc(r.final_text)}</div>` : ""}
    ${r.error ? `<div class="final error"><b>Error:</b> ${esc(r.error)}</div>` : ""}
    <div class="grid two">
      <div><div class="trace-head">Step-by-step trace <span class="muted small">${r.steps.length} steps · ${ms(r.duration_ms)}</span></div>
        <div class="timeline"><span class="rail" aria-hidden="true"></span>${r.steps.map(stepView).join("")}</div></div>
      <div>
        <div class="card" style="margin-bottom:14px"><h2>Approvals from this run</h2><div class="side-list">
          ${r.approvals.map((a) => `<div class="item"><div class="line" style="justify-content:space-between"><b>${esc(a.title)}</b>${pill(a.status)}</div><div class="muted small">${esc(a.action)} · <a href="#/approvals?status=all">open queue</a></div></div>`).join("") || `<div class="item muted">None</div>`}
        </div></div>
        <div class="card"><h2>Integration calls</h2><div class="side-list">
          ${r.integration_calls.map((c) => `<div class="item"><div class="line">${integ(c.integration)}<span class="mono">${esc(c.operation)}</span>${pill(c.status)}<span class="muted small" style="margin-left:auto">${esc(c.backend)} · ${c.attempts} attempt${c.attempts > 1 ? "s" : ""}</span></div>${jsonBlock("Request", c.request)}${jsonBlock("Response", c.response)}</div>`).join("") || `<div class="item muted">None</div>`}
        </div></div>
      </div>
    </div>`;
  enter();
  revealTimeline();
}

function approvalBody(a) {
  const p = a.payload || {};
  if (a.action === "send_email_reply") {
    return `<div class="mail"><div class="hdr"><div><span>To</span>${esc(p.to)}</div><div><span>Subject</span><b>${esc(p.subject)}</b></div><div><span>Draft</span><span class="mono">${esc(p.draft_id)}</span></div></div><div class="msg">${esc(p.body)}</div></div>`;
  }
  if (a.action === "issue_refund") {
    return `<div class="amount">${money(p.amount, 2)}</div><div class="kv" style="margin-top:10px">
      <div class="k">Order</div><div class="mono">${esc(p.order_id)}</div><div class="k">Order total</div><div>${money(p.order_total, 2)}</div>
      <div class="k">Customer</div><div>${esc(p.customer)}</div><div class="k">Marketplace</div><div>${esc(p.marketplace)}</div>
      <div class="k">Reason</div><div>${esc(p.reason)}</div></div>`;
  }
  return `<div class="kv"><div class="k">Severity</div><div>${prio(p.severity)}</div><div class="k">Source</div><div class="mono">${esc(p.source_ref)}</div><div class="k">Workflow</div><div>${wf(p.workflow)}</div></div>`;
}

async function pageApprovals(params) {
  await refreshShell();
  const status = params.get("status") || "pending";
  const items = await api(`/api/approvals?status=${encodeURIComponent(status)}`);
  const label = { send_email_reply: "Send email", issue_refund: "Refund", human_review: "Human review" };
  const tone = { send_email_reply: "info", issue_refund: "warn", human_review: "bad" };
  view.innerHTML = `
    <div class="toolbar"><div class="tabs">${["pending", "executed", "rejected", "resolved", "all"].map((s) => `<button class="${s === status ? "active" : ""}" data-status="${s}">${s}</button>`).join("")}</div>
      <span class="muted small hint">Risky actions wait here until a person decides. Approving runs the action; rejecting discards it.</span></div>
    <div class="approvals">${items.map((a) => `
      <div class="card ap" id="ap-${a.id}">
        <div class="ap-top"><span class="pill ${tone[a.action] || "neutral"}">${label[a.action] || esc(a.action)}</span><span class="ap-title" title="${esc(a.title)}">${esc(a.title)}</span>${pill(a.status)}</div>
        <div class="ap-body"><div class="reason">${esc(a.reason)}</div>${approvalBody(a)}
          ${a.result ? jsonBlock("Result", a.result) : ""}${a.note ? `<div class="small muted" style="margin-top:8px">Note: ${esc(a.note)}</div>` : ""}</div>
        <div class="ap-foot">${a.status === "pending" ? `<input type="text" placeholder="Note (optional)" id="note-${a.id}" aria-label="Note for #${a.id}">
          <button class="reject" data-act="reject" data-id="${a.id}">${icon("x")}Reject</button>
          <button class="approve" data-act="approve" data-id="${a.id}">${icon("check")}${a.kind === "review" ? "Mark resolved" : "Approve"}</button>`
          : `<span class="muted small">${esc(a.decided_by || "")} · ${time(a.decided_at)}</span>`}
          <a class="small run-link" href="#/runs/${a.run_id}">run #${a.run_id}</a></div>
      </div>`).join("") || `<div class="card">${empty(`Nothing ${status === "all" ? "" : esc(status)} here.`, "shield")}</div>`}</div>`;
  view.querySelectorAll("[data-status]").forEach((b) => (b.onclick = () => (location.hash = `#/approvals?status=${b.dataset.status}`)));
  view.querySelectorAll("[data-act]").forEach((b) => (b.onclick = async () => {
    const id = b.dataset.id, act = b.dataset.act;
    const card = document.getElementById(`ap-${id}`);
    const buttons = card.querySelectorAll("[data-act]");
    buttons.forEach((x) => (x.disabled = true));
    const original = b.innerHTML;
    b.innerHTML = `<span class="spin"></span>${act === "approve" ? "Working…" : "Rejecting…"}`;
    try {
      const res = await api(`/api/approvals/${id}/${act}`, { method: "POST", body: JSON.stringify({ note: document.getElementById(`note-${id}`).value }) });
      const ok = act === "approve";
      toast(`#${id} ${res.status}`, ok ? "ok" : "info");
      card.classList.add(ok ? "done-ok" : "done-no");
      const p = card.querySelector(".ap-top .pill:last-child");
      if (p) p.outerHTML = pill(res.status);
      b.innerHTML = `${icon(ok ? "check" : "x")}${esc(res.status)}`;
      if (motion) {
        const tl = gsap.timeline({ onComplete: route });
        if (ok) tl.fromTo(card, { scale: 1 }, { scale: 1.015, duration: 0.18, ease: "power2.out" }).to(card, { scale: 1, duration: 0.25, ease: "power2.inOut" });
        else tl.to(card, { x: -6, duration: 0.08 }).to(card, { x: 0, duration: 0.2, ease: "power2.out" });
        tl.to(card, { opacity: 0, y: ok ? -10 : 10, scale: 0.98, duration: 0.25, ease: "power2.in", delay: 0.45 });
      } else setTimeout(route, 600);
    } catch (e) {
      toast(e.message, "bad");
      b.innerHTML = original;
      buttons.forEach((x) => (x.disabled = false));
      if (motion) gsap.fromTo(card, { x: -6 }, { x: 0, duration: 0.6, ease: "elastic.out(1, .3)", clearProps: "transform" });
    }
  }));
  enter();
}

async function pageIntegrations(params) {
  await refreshShell();
  const which = params.get("integration") || "";
  const calls = await api("/api/integrations" + (which ? `?integration=${encodeURIComponent(which)}` : ""));
  view.innerHTML = `
    <div class="toolbar"><div class="tabs">${["", "slack", "clickup", "gmail"].map((s) => `<button class="${s === which ? "active" : ""}" data-int="${s}">${s || "all"}</button>`).join("")}</div>
      <span class="muted small hint">Every call to Slack, ClickUp and Gmail, with retries. Personal data is redacted before it is stored.</span></div>
    <div class="card"><div class="table-wrap scroll"><table class="tight"><thead><tr><th>#</th><th>Time</th><th>Integration</th><th>Backend</th><th>Operation</th><th>Status</th><th class="num">Attempts</th><th>Run</th><th>Request</th></tr></thead><tbody>
      ${calls.map((c) => `<tr><td class="id">${c.id}</td><td class="muted small nowrap">${time(c.created_at)}</td><td>${integ(c.integration)}</td><td><span class="pill ${c.backend === "live" ? "ok" : "accent"}">${esc(c.backend)}</span></td>
        <td class="mono">${esc(c.operation)}</td><td>${pill(c.status)}</td><td class="num">${c.attempts > 1 ? `<span class="pill warn">${c.attempts}</span>` : c.attempts}</td><td>${c.run_id ? `<a href="#/runs/${c.run_id}">#${c.run_id}</a>` : "–"}</td>
        <td style="max-width:250px"><div class="subject small" style="max-width:250px">${esc(c.request.name || c.request.subject || c.request.channel || c.request.draft_id || "")}${c.request.text ? " · " + esc(c.request.text.split("\n")[0]) : ""}</div>${jsonBlock("details", { request: c.request, response: c.response })}</td></tr>`).join("") || `<tr><td colspan="9">${empty("No integration calls yet.", "plug")}</td></tr>`}
    </tbody></table></div></div>`;
  view.querySelectorAll("[data-int]").forEach((b) => (b.onclick = () => (location.hash = "#/integrations" + (b.dataset.int ? `?integration=${b.dataset.int}` : ""))));
  enter();
}

async function pageWorkflows() {
  await refreshShell();
  const wfs = await api("/api/workflows");
  view.innerHTML = `<div class="grid wf-cards">${wfs.map((w) => `
    <div class="card lift"><h2>${esc(w.title)} ${wf(w.name)}</h2><div class="card-body">
      <p style="margin-top:0">${esc(w.description)}</p>
      <div class="kv"><div class="k">Trigger</div><div>${esc(w.trigger)}</div><div class="k">Max steps</div><div>${w.max_steps} model calls</div><div class="k">Max tool calls</div><div>${w.max_tool_calls}</div></div>
      <div class="small muted" style="margin-top:14px;font-weight:600;text-transform:uppercase;letter-spacing:.04em">Allowed tools</div><div class="chips">${w.allowed_tools.map((t) => `<span class="chip">${esc(t)}</span>`).join("")}</div>
      <div style="margin-top:16px"><button class="primary" data-run="${esc(w.name)}">${icon("play")}Run now</button></div>
    </div></div>`).join("")}</div>`;
  bindRunButtons();
  enter();
}

function bindRunButtons() {
  view.querySelectorAll("[data-run]").forEach((b) => (b.onclick = async () => {
    const original = b.innerHTML;
    b.disabled = true;
    b.innerHTML = `<span class="spin"></span>Running…`;
    try {
      const res = await api(`/api/workflows/${encodeURIComponent(b.dataset.run)}/run`, { method: "POST", body: "{}" });
      toast(res.runs.length ? `${res.runs.length} run(s) finished` : "Nothing new to process", res.runs.length ? "ok" : "info");
      location.hash = res.runs.length === 1 ? `#/runs/${res.runs[0].run_id}` : `#/runs?workflow=${b.dataset.run}`;
    } catch (e) {
      toast(e.message, "bad");
      b.disabled = false;
      b.innerHTML = original;
      if (motion) gsap.fromTo(b, { x: -6 }, { x: 0, duration: 0.6, ease: "elastic.out(1, .3)", clearProps: "transform" });
    }
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
  moveNavIndicator();
  setNav(false);
  titleEl.textContent = TITLES[page] || "Overview";
  try {
    if (page === "runs" && parts[1]) await pageRun(parts[1]);
    else if (page === "runs") await pageRuns(params);
    else if (page === "approvals") await pageApprovals(params);
    else if (page === "integrations") await pageIntegrations(params);
    else if (page === "workflows") await pageWorkflows();
    else await pageOverview();
  } catch (e) {
    view.innerHTML = `<div class="card">${empty(esc(e.message), "x")}</div>`;
  }
}

view.addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-href]");
  if (row && !e.target.closest("a,button,details")) location.hash = row.dataset.href;
});
window.addEventListener("hashchange", route);
if (document.fonts && document.fonts.ready) document.fonts.ready.then(moveNavIndicator);
route();
