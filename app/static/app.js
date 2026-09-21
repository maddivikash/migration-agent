/* Migration Agent UI - vanilla JS, polls the API. */
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = v => v === null || v === undefined || v === "" ? '<span class="muted">—</span>' : esc(v);
let lastSeq = 0, state = {}, escalations = {open: [], resolved: []}, mappings = {}, records = [], schema = null, activeTab = "live";
const api = async (path, opts = {}) => {
  const r = await fetch(path, {headers: {"Content-Type": "application/json"}, ...opts});
  if (!r.ok) { const t = await r.text(); throw new Error(t || r.statusText); }
  return r.json();
};
const toast = msg => { const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), 3500); };

/* ---------------- tabs ---------------- */
document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => {
  document.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x.id === "tab-" + b.dataset.tab));
  activeTab = b.dataset.tab; render();
});
function go(tab) { document.querySelector(`.tabs button[data-tab=${tab}]`).click(); location.hash = tab; }
if (location.hash && document.querySelector(`.tabs button[data-tab=${location.hash.slice(1)}]`)) go(location.hash.slice(1));

/* ---------------- polling ---------------- */
async function poll() {
  try {
    state = await api("/api/state");
    const ev = await api(`/api/events?since=${lastSeq}`);
    if (ev.events.length) { appendEvents(ev.events); lastSeq = ev.events[ev.events.length - 1].seq; }
    if (lastSeq === 0 && state.events > 0) { /* fresh page load after run: reload all */ }
    if (state.status !== "running") {
      [escalations, mappings, records] = await Promise.all([api("/api/escalations"), api("/api/mappings").then(m => m.mappings), api("/api/records").then(r => r.records)]);
    }
    renderHeader(); render();
  } catch (e) { console.error(e); }
  setTimeout(poll, state.status === "running" ? 400 : 1500);
}
if (!schema) api("/api/schema").then(s => schema = s);

/* ---------------- header ---------------- */
function renderHeader() {
  $("#statusDot").className = "dot " + state.status;
  const s = state.stats || {}, bs = s.by_status || {};
  const stat = (v, l) => `<div class="stat"><b>${v ?? "–"}</b><span>${l}</span></div>`;
  $("#stats").innerHTML = state.run_no ? stat(s.source_rows, "source rows") + stat(s.unique_people, "people") + stat(s.auto_fixes, "auto fixes") +
    stat(bs.ready ?? 0, "ready") + stat(bs.held ?? 0, "held") + stat(bs.pushed ?? 0, "pushed") + stat(bs.failed ?? 0, "failed") + stat(state.target_count, "in target") : "";
  const n = escalations.open.length; const b = $("#queueBadge"); b.textContent = n; b.classList.toggle("zero", n === 0);
  $("#btnRun").disabled = state.status === "running"; $("#btnPush").disabled = state.status === "running" || !(bs.ready > 0);
  $("#btnRetry").disabled = !(bs.failed > 0); $("#btnRetryNoMgr").disabled = !(bs.failed > 0); $("#btnRollback").disabled = !(bs.pushed > 0);
  $("#fileList").innerHTML = (state.files || []).map(f => `<li>${esc(f)}</li>`).join("") || '<li class="muted">sample_data/*</li>';
  $("#llmInfo").textContent = state.llm ? (state.llm.available ? `✓ ${state.llm.detail}` : `○ ${state.llm.detail}`) : "";
  $("#memInfo").textContent = state.memory ? `${state.memory.column_mappings} column mapping(s), ${state.memory.enum_values} value mapping(s) remembered from consultant decisions` : "";
}

/* ---------------- live feed ---------------- */
function appendEvents(evs) {
  const feed = $("#feed"); if ($(".empty", feed)) feed.innerHTML = "";
  for (const e of evs) {
    const d = document.createElement("div"); d.className = `ev kind-${e.kind}`;
    const det = e.details && Object.keys(e.details).length ? `<details><summary>details</summary><pre>${esc(JSON.stringify(e.details, null, 1))}</pre></details>` : "";
    d.innerHTML = `<time>${e.ts.slice(11, 23)}</time><span class="kind">${esc(e.kind)}</span><span>${esc(e.message)}${det}</span>`;
    feed.appendChild(d);
  }
  feed.scrollTop = feed.scrollHeight;
}

/* ---------------- escalation queue ---------------- */
function ctxHtml(e) {
  const c = e.context || {}; const rows = [];
  const pills = a => (a || []).map(v => `<span class="pill">${esc(v)}</span>`).join("");
  if (e.type === "ambiguous_mapping") {
    rows.push(["File", esc(c.file)], ["Column", `<code>${esc(c.column)}</code>`], ["Sample values", pills(c.sample_values)]);
    if (c.llm_opinion) rows.push(["Model opinion", esc(c.llm_opinion)]);
  } else if (e.type === "date_order") {
    rows.push(["File", esc(c.file)], ["Column", `<code>${esc(c.column)}</code>`], ["Sample values", pills(c.sample_values)]);
  } else if (e.type === "unknown_value") {
    rows.push(["Field", `<code>${esc(c.field)}</code>`], ["Raw value", `<b class="mono">${esc(c.raw_value)}</b>`],
      ["Affected", (c.example_records || []).map(r => `${esc(r.first_name)} ${esc(r.last_name)} · ${esc(r.job_title || "")} · ${esc(r.employee_id)}`).join("<br>")]);
  } else if (e.type === "conflict") {
    rows.push(["Record", esc(c.record)], ["Field", `<code>${esc(c.field)}</code>`], ["Values", (c.values || []).map(v => `<b class="mono">${esc(v.value)}</b> <span class="muted">from ${esc(v.source)}</span>`).join("<br>")]);
  } else if (e.type === "validation_failed") {
    const p = c.record_preview || {};
    rows.push(["Record", `${esc(p.first_name || "")} ${esc(p.last_name || "")} <span class="muted">${esc(c.record)}</span>`]);
    if (c.raw_values && c.raw_values.length) rows.push(["Raw value", c.raw_values.map(r => `<b class="mono">${esc(r.before)}</b> <span class="muted">— ${esc(r.note)}</span>`).join("<br>")]);
    rows.push(["Context", Object.entries(p).filter(([k]) => !["first_name", "last_name"].includes(k)).map(([k, v]) => `${k}=<span class="mono">${esc(v ?? "—")}</span>`).join("  ")]);
    rows.push(["Sources", (c.sources || []).map(s => `${esc(s.file)} row ${s.row}`).join(", ")]);
  }
  return rows.length ? `<div class="ctx">${rows.map(([k, v]) => `<b>${k}</b><span>${v}</span>`).join("")}</div>` : "";
}
function dupHtml(e) {
  const a = e.context.primary, b = e.context.suspect; const keys = ["employee_id", "first_name", "last_name", "date_of_birth", "email", "hire_date"];
  const side = (x, y, title) => `<div><h5>${title}</h5>${keys.map(k => `<div class="${x[k] !== y[k] ? "diff" : ""}"><span class="muted">${k}</span> <span class="mono">${fmt(x[k])}</span></div>`).join("")}<div class="muted">${(x.sources || []).map(s => `${esc(s.file)}#${s.row}`).join(", ")}</div></div>`;
  return `<div class="compare">${side(a, b, "Existing record")}${side(b, a, "Suspected duplicate")}</div>`;
}
let queueSig = "", mapSig = "";
function renderQueue() {
  const q = $("#queue");
  // only rebuild when the set of escalations changed - otherwise a poll would wipe what the consultant is typing
  const sig = escalations.open.map(e => e.id).join("|") + "#" + escalations.resolved.length + "#" + state.run_no;
  if (sig === queueSig) return; queueSig = sig;
  if (!escalations.open.length) { q.innerHTML = `<div class="card"><div class="empty">${state.run_no ? "Nothing needs your attention. Everything else the agent handled on its own." : "Run the agent first."}</div></div>`; }
  else q.innerHTML = escalations.open.map(e => `
    <div class="card ${e.severity}" data-id="${esc(e.id)}">
      <div class="type">${esc(e.type.replace(/_/g, " "))} · ${e.severity}${e.affected && e.affected.length ? ` · ${e.affected.length} record(s)` : ""}</div>
      <h4>${esc(e.title)}</h4>
      <div class="why">${esc(e.summary)}</div>
      ${e.type === "possible_duplicate" ? dupHtml(e) : ctxHtml(e)}
      <div class="options">${(e.options || []).map(o => `<button data-v='${esc(JSON.stringify(o.value))}'>${esc(o.label)}${o.reasons && o.reasons.length ? `<span class="r">${esc(o.reasons[0])}</span>` : ""}</button>`).join("")}</div>
      ${e.custom ? `<div class="custom"><span class="muted small">${esc(e.custom.label)}:</span>${e.custom.fields.map(f => `<input data-f="${esc(f)}" placeholder="${esc(f)}">`).join("")}<button class="small" data-custom>Apply</button></div>` : ""}
      <div class="note"><input data-note placeholder="optional note for the audit trail (e.g. 'confirmed with client HR on call')"></div>
    </div>`).join("");
  q.querySelectorAll(".card").forEach(card => {
    const id = card.dataset.id, note = () => $("[data-note]", card).value;
    card.querySelectorAll(".options button").forEach(b => b.onclick = () => decide(id, {value: JSON.parse(b.dataset.v), note: note()}));
    const cb = $("[data-custom]", card); if (cb) cb.onclick = () => {
      const values = {}; card.querySelectorAll(".custom input").forEach(i => { if (i.value.trim()) values[i.dataset.f] = i.value.trim(); });
      if (!Object.keys(values).length) return toast("Enter a value first");
      decide(id, {values, value: Object.keys(values).length === 1 ? Object.values(values)[0] : null, note: note()});
    };
  });
  $("#resolvedCount").textContent = escalations.resolved.length ? `(${escalations.resolved.length})` : "";
  $("#resolved").innerHTML = escalations.resolved.slice().reverse().map(e => `<div class="item"><b>${esc(e.title)}</b><span>→ ${esc(JSON.stringify(e.resolution?.values || e.resolution?.value))}${e.resolution?.note ? ` · “${esc(e.resolution.note)}”` : ""}</span></div>`).join("") || '<div class="muted small">none yet</div>';
}
async function decide(id, body) {
  try { toast("Applying decision and re-running the agent…"); await api(`/api/escalations/${encodeURIComponent(id)}/decide`, {method: "POST", body: JSON.stringify(body)}); toast("Decision applied"); }
  catch (e) { toast("Failed: " + e.message); }
}

/* ---------------- mappings ---------------- */
function renderMappings() {
  const el = $("#mappings"); const files = Object.keys(mappings);
  const sig = JSON.stringify(Object.values(mappings).flat().map(m => [m.source_column, m.target, m.status])) + state.run_no;
  if (sig === mapSig) return; mapSig = sig;
  if (!files.length) return el.innerHTML = '<div class="card"><div class="empty">Run the agent first.</div></div>';
  const targets = schema ? Object.keys(schema.fields) : [];
  el.innerHTML = files.map(f => `<div class="filecard"><h3>${esc(f)} <small>${mappings[f].filter(m => m.target).length} of ${mappings[f].length} columns migrated</small></h3>
    <table><thead><tr><th>Source column</th><th>Sample values</th><th>→ Target field</th><th>Confidence</th><th>How decided</th><th>Why</th></tr></thead><tbody>
    ${mappings[f].map(m => `<tr><td><code>${esc(m.source_column)}</code></td><td class="muted small">${(m.samples || []).slice(0, 3).map(esc).join(" · ")}</td>
      <td><select class="map" data-file="${esc(f)}" data-col="${esc(m.source_column)}"><option value="">— don't migrate —</option>${targets.map(t => `<option ${t === m.target ? "selected" : ""}>${t}</option>`).join("")}</select></td>
      <td><span class="conf"><i style="width:${Math.round(m.confidence * 100)}%"></i></span><span class="small muted">${Math.round(m.confidence * 100)}%</span></td>
      <td><span class="tag ${m.status}">${m.status === "auto" ? "agent" : m.status === "human" ? "you" : m.status}</span></td><td class="small muted">${esc(m.note)}</td></tr>`).join("")}</tbody></table></div>`).join("");
  el.querySelectorAll("select.map").forEach(s => s.onchange = async () => {
    try { toast("Applying override…"); await api("/api/mappings/override", {method: "POST", body: JSON.stringify({file: s.dataset.file, column: s.dataset.col, target: s.value || null})}); toast("Mapping updated, agent re-ran"); }
    catch (e) { toast("Failed: " + e.message); }
  });
}

/* ---------------- records ---------------- */
function renderRecords() {
  const q = $("#recSearch").value.toLowerCase(), st = $("#recStatus").value;
  const rows = records.filter(r => (!st || r.status === st) && (!q || JSON.stringify(r.fields).toLowerCase().includes(q)));
  const cols = ["employee_id", "first_name", "last_name", "email", "department", "hire_date", "status"];
  $("#records").innerHTML = rows.length ? `<table><thead><tr><th>Status</th>${cols.map(c => `<th>${c}</th>`).join("")}<th>Fixes</th><th>Sources</th><th></th></tr></thead><tbody>
    ${rows.map(r => `<tr class="clickable" data-key="${esc(r.key)}"><td><span class="tag ${r.status}">${r.status}</span>${r.push && r.push.last_error ? `<div class="small" style="color:var(--bad)">${esc(r.push.last_error)}</div>` : ""}</td>
      ${cols.map(c => `<td>${fmt(r.fields[c])}</td>`).join("")}<td>${r.changes}${r.human_changes ? ` <span class="small" style="color:var(--brand)">(${r.human_changes} by you)</span>` : ""}</td>
      <td class="small muted">${r.sources.length}</td>
      <td>${r.status === "held" ? `<button class="small" data-go-queue>Resolve</button>` : r.status === "failed" ? `<button class="small" data-retry>Retry</button>` : r.status === "pushed" ? `<button class="small" data-rollback>Roll back</button>` : r.status === "rolled_back" ? `<button class="small" data-reset>Re-queue</button>` : ""}</td></tr>`).join("")}</tbody></table>`
    : '<div class="card"><div class="empty">No records match.</div></div>';
  $("#records").querySelectorAll("tr.clickable").forEach(tr => {
    tr.onclick = e => { if (e.target.tagName === "BUTTON") return; openRecord(tr.dataset.key); };
    const k = tr.dataset.key;
    const on = (sel, fn) => { const b = $(sel, tr); if (b) b.onclick = fn; };
    on("[data-go-queue]", () => go("queue"));
    on("[data-retry]", () => pushCall({keys: [k], mode: "retry"}));
    on("[data-rollback]", () => rollbackCall({keys: [k]}));
    on("[data-reset]", async () => { await api(`/api/records/${encodeURIComponent(k)}/reset`, {method: "POST"}); toast("Record re-queued as ready"); });
  });
}
$("#recSearch").oninput = renderRecords; $("#recStatus").onchange = renderRecords;
async function openRecord(key) {
  const r = await api(`/api/records/${encodeURIComponent(key)}`);
  $("#drawerBody").innerHTML = `<h2 style="margin:0 0 4px">${esc(r.fields.first_name || "")} ${esc(r.fields.last_name || "")} <span class="tag ${r.status}">${r.status}</span></h2>
    <div class="muted small">${esc(r.key)} · from ${r.sources.map(s => `${esc(s.file)} row ${s.row}`).join(", ")}</div>
    <h3 class="section">Final record</h3><div class="kv">${Object.entries(r.fields).map(([k, v]) => `<b>${k}</b><span class="mono">${fmt(v)}</span>`).join("")}</div>
    ${r.issues.length ? `<h3 class="section">Open issues</h3><ul>${r.issues.map(i => `<li>${esc(i)}</li>`).join("")}</ul>` : ""}
    <h3 class="section">What changed and why (${r.changes.length})</h3>
    <table class="changes"><thead><tr><th>Field</th><th>Before</th><th>After</th><th>Why</th><th>By</th></tr></thead><tbody>
    ${r.changes.map(c => `<tr><td>${esc(c.field)}</td><td class="mono">${fmt(c.before)}</td><td class="mono">${fmt(c.after)}</td><td class="muted">${esc(c.note)}${c.confidence < 1 ? ` <span class="tag">${Math.round(c.confidence * 100)}%</span>` : ""}</td><td class="${c.actor === "human" ? "h" : ""}">${esc(c.actor)}</td></tr>`).join("")}</tbody></table>
    ${r.push_log.length ? `<h3 class="section">Target API calls</h3><table class="changes"><thead><tr><th>Time</th><th>Action</th><th>HTTP</th><th>Outcome</th></tr></thead><tbody>${r.push_log.map(p => `<tr><td>${p.ts.slice(11, 19)}</td><td>${p.action}</td><td>${p.http ?? "—"}</td><td>${esc(p.outcome)}${p.error ? ` <span class="muted">${esc(p.error)}</span>` : ""}</td></tr>`).join("")}</tbody></table>` : ""}`;
  $("#drawer").classList.remove("hidden");
}
function closeDrawer() { $("#drawer").classList.add("hidden"); }
$("#drawer").onclick = e => { if (e.target.id === "drawer") closeDrawer(); };

/* ---------------- audit ---------------- */
async function renderAudit() {
  const a = await api("/api/audit");
  const dec = a.decisions || [], pl = a.push_log || [];
  $("#audit").innerHTML = `
    <h3 class="section">Human decisions (${dec.length})</h3>
    ${dec.length ? `<table><thead><tr><th>When</th><th>Escalation</th><th>Decision</th><th>Note</th></tr></thead><tbody>${dec.map(d => `<tr><td class="small mono">${d.ts.slice(11, 19)}</td><td>${esc(d.escalation.title)}</td><td class="mono">${esc(JSON.stringify(d.decision.values || d.decision.value))}</td><td class="muted">${esc(d.decision.note)}</td></tr>`).join("")}</tbody></table>` : '<div class="muted small">none yet</div>'}
    <h3 class="section">Target API calls (${pl.length})</h3>
    ${pl.length ? `<table><thead><tr><th>When</th><th>Record</th><th>Action</th><th>HTTP</th><th>Outcome</th></tr></thead><tbody>${pl.map(p => `<tr><td class="small mono">${p.ts.slice(11, 19)}</td><td>${esc(p.employee_id)} <span class="muted small">${esc(p.key)}</span></td><td>${p.action}</td><td>${p.http ?? "—"}</td><td>${p.outcome === "success" ? '<span class="tag pushed">success</span>' : `<span class="tag failed">failed</span> <span class="muted small">${esc(p.error || "")}</span>`}</td></tr>`).join("")}</tbody></table>` : '<div class="muted small">nothing pushed yet</div>'}
    <h3 class="section">Agent event log (${(a.events || []).length})</h3>
    <table><tbody>${(a.events || []).map(e => `<tr><td class="small mono">${e.ts.slice(11, 23)}</td><td class="small muted">${esc(e.kind)}</td><td>${esc(e.message)}</td></tr>`).join("")}</tbody></table>`;
}

/* ---------------- actions ---------------- */
$("#btnRun").onclick = async () => {
  try { lastSeq = 0; $("#feed").innerHTML = ""; await api("/api/run", {method: "POST", body: JSON.stringify({files: null, reset: true})}); go("live"); }
  catch (e) { toast("Failed: " + e.message); }
};
async function pushCall(body) { try { toast("Pushing to target…"); const r = await api("/api/push", {method: "POST", body: JSON.stringify(body)}); toast(`Push done: ${r.ok} ok, ${r.failed} failed${r.deferred ? `, ${r.deferred} deferred until their manager is migrated` : ""}`); go("records"); } catch (e) { toast("Failed: " + e.message); } }
async function rollbackCall(body) { if (!confirm("Remove these records from the target system?")) return; try { const r = await api("/api/rollback", {method: "POST", body: JSON.stringify(body)}); toast(`Rolled back ${r.rolled_back} record(s)`); } catch (e) { toast("Failed: " + e.message); } }
$("#btnPush").onclick = () => pushCall({mode: "push"});
$("#btnRetry").onclick = () => pushCall({mode: "retry"});
$("#btnRetryNoMgr").onclick = () => pushCall({mode: "retry_without_manager"});
$("#btnRollback").onclick = () => rollbackCall({});
$("#fileInput").onchange = async e => {
  for (const f of e.target.files) { const fd = new FormData(); fd.append("file", f); await fetch("/api/upload", {method: "POST", body: fd}); }
  toast(`${e.target.files.length} file(s) uploaded; they will be included in the next run`); e.target.value = "";
};

function render() {
  if (activeTab === "queue") renderQueue();
  else if (activeTab === "mappings") renderMappings();
  else if (activeTab === "records") renderRecords();
  else if (activeTab === "audit") renderAudit();
  // keep the badge fresh regardless of tab
  const n = escalations.open.length; const b = $("#queueBadge"); b.textContent = n; b.classList.toggle("zero", n === 0);
}
poll();
