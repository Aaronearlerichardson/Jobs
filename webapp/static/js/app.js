"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

const state = { jobs: [], pipeline: [], companies: [], stats: {},
                tracks: [], track: null, config: null,
                expanded: null, logTotal: 0, wasRunning: false };

const currentTrack = () => state.tracks.find(t => t.id === state.track);

/* ---------------- theme ---------------- */
const themeBtn = $("#themebtn");
function applyTheme(t) {
  if (t) document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
}
applyTheme(localStorage.getItem("theme") || "");
themeBtn.onclick = () => {
  const cur = localStorage.getItem("theme") || "";
  const next = cur === "" ? "dark" : cur === "dark" ? "light" : "";
  if (next) localStorage.setItem("theme", next); else localStorage.removeItem("theme");
  applyTheme(next);
};

/* ---------------- plumbing ---------------- */
let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.status);
  return data;
}
async function post(path, body) {
  return api(path, { method: "POST", headers: {"Content-Type": "application/json"},
                     body: JSON.stringify(body || {}) });
}
async function put(path, body) {
  return api(path, { method: "PUT", headers: {"Content-Type": "application/json"},
                     body: JSON.stringify(body || {}) });
}
/* Append the active track to an API path (all data routes are track-scoped). */
function withTrack(path) {
  if (!state.track) return path;
  return path + (path.includes("?") ? "&" : "?") +
         "track=" + encodeURIComponent(state.track);
}

/* ---------------- tabs ---------------- */
document.querySelectorAll("nav button").forEach(b => b.onclick = () => {
  document.querySelectorAll("nav button").forEach(x => x.classList.toggle("on", x === b));
  for (const s of ["jobs", "pipeline", "companies", "ops", "settings"])
    $("#tab-" + s).hidden = b.dataset.tab !== s;
  if (b.dataset.tab === "settings" && !state.config) loadSettings();
});

/* ---------------- stats tiles ---------------- */
function renderTiles() {
  const s = state.stats;
  const dated = s.open ? Math.round(100 * s.dated / s.open) : 0;
  $("#tiles").innerHTML = [
    [s.open, "open jobs"], [s.new_today, "new today"],
    [s.pipeline, "in pipeline"], [s.saved, "saved"],
    [s.closed, "closed"], [dated + "%", "have post date"],
    [s.companies_active, "active companies"], [s.watched, "watched"],
  ].map(([v, l]) => `<div class="tile"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("");
  $("#keywarn").hidden = !!s.api_key;
  $("#dbpath").textContent =
    `${s.screen_model} screen · ${s.verify_model} verify · ${s.db}`;
}

/* ---------------- jobs ---------------- */
function ageChip(age) {
  if (age === "NEW") return `<span class="chip age-new">NEW</span>`;
  if (age && age.endsWith("!")) return `<span class="chip age-stale">⏳ ${esc(age)}</span>`;
  if (age === "?") return `<span class="chip">?</span>`;
  return `<span class="chip">${esc(age)}</span>`;
}
function cleanReason(r) { return (r || "").replace(/^\[[^\]]*\]\s*/, ""); }
function geoChip(bucket) {
  if (bucket === "local")  return `<span class="chip geo-local">local</span>`;
  if (bucket === "remote") return `<span class="chip geo-remote">remote</span>`;
  if (bucket === "relocation")
    return `<span class="chip geo-reloc" title="onsite outside your configured locality — you would have to move">relocation</span>`;
  return `<span class="chip">?</span>`;
}
const watchedNames = () =>
  new Set(state.companies.filter(c => c.watched).map(c => c.name));

function jobFilters(j) {
  const q = $("#f-search").value.trim().toLowerCase();
  if (q && !(`${j.title} ${j.company_name}`.toLowerCase().includes(q))) return false;
  const mf = parseFloat($("#f-fit").value || "0");
  if (mf > 0 && !(j.resume_fit_score >= mf)) return false;
  const geo = $("#f-geo").value;
  if (geo && j.geo_bucket !== geo) return false;
  // Relocation gate: hidden unless the user is willing to move (or is
  // explicitly browsing the relocation bucket via the dropdown).
  if (!geo && !$("#f-move").checked && j.geo_bucket === "relocation") return false;
  if (j.geo_bucket === "remote") {
    // Non-US remotes aren't takeable for a US applicant; always hide.
    if (!j.us_ok) return false;
    // Precision rule (per track config): remote rows only at watched
    // companies — unless the user explicitly selected the remote bucket.
    const t = currentTrack();
    if (t && t.remote_requires_watch && !j.watched && geo !== "remote") return false;
  }
  const age = $("#f-age").value;
  if (age === "new" && j.age !== "NEW") return false;
  if (age === "fresh") {
    const d = parseInt(j.age); if (!(j.age === "NEW" || (d >= 0 && d <= 7))) return false;
  }
  if (age === "stale" && !(j.age || "").endsWith("!")) return false;
  if ($("#f-verified").checked && !j.verified) return false;
  if ($("#f-watched").checked && !watchedNames().has(j.company_name)) return false;
  return true;
}

function renderJobs() {
  const rows = state.jobs.filter(jobFilters);
  $("#jobcount").textContent = `${rows.length} of ${state.jobs.length} shown`;
  if (!rows.length) { $("#jobs").innerHTML = `<div class="empty">No jobs match.</div>`; return; }
  const body = rows.map(j => {
    const fit = j.resume_fit_score == null ? "–" : j.resume_fit_score.toFixed(2);
    const comb = j.combined_score == null ? "–" : j.combined_score.toFixed(2);
    const gates = (j.fit_gates || "").split(",").filter(Boolean)
      .map(g => `<span class="chip gate">${esc(g)}</span>`).join(" ");
    const chips = [
      j.status === "closed" ? `<span class="chip closed">closed</span>` : "",
      j.disposition ? `<span class="chip disp">${esc(j.disposition)}</span>` : "",
      j.verified ? `<span class="chip ver">✓ verified</span>` : "",
      gates,
    ].filter(Boolean).join(" ");
    const expanded = state.expanded === j.job_id ? renderDetail(j) : "";
    return `
      <tr class="jobrow" data-id="${esc(j.job_id)}">
        <td class="num">${j.rank ?? ""}</td>
        <td class="num fit">${fit}</td>
        <td class="num comb">${comb}</td>
        <td>${ageChip(j.age)}</td>
        <td class="title-cell">
          <a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a>
          <div class="co">${esc(j.company_name)}
            <span class="chip tier">${esc(j.mission_tier || "?")}</span> ${chips}</div>
        </td>
        <td>${geoChip(j.geo_bucket)}<div class="loc">${esc(j.location || "")}</div></td>
        <td><div class="acts">
          <button data-act="saved" title="shortlist — stays ranked">☆ save</button>
          <button data-act="applied" title="move to pipeline">applied</button>
          <button data-act="interviewing" title="move to pipeline">interview</button>
          <button data-act="dismissed" title="hide + teach the scorer why">dismiss</button>
          <button data-act="rejected" title="they said no">rejected</button>
        </div></td>
      </tr>${expanded}`;
  }).join("");
  $("#jobs").innerHTML = `<table>
    <thead><tr><th class="num">#</th><th class="num">Fit</th><th class="num">Comb</th>
    <th>Age</th><th>Job</th><th>Geo</th><th>Decide</th></tr></thead>
    <tbody>${body}</tbody></table>`;

  document.querySelectorAll(".jobrow").forEach(tr => {
    tr.onclick = e => {
      if (e.target.closest("a, button")) return;
      const id = tr.dataset.id;
      state.expanded = state.expanded === id ? null : id;
      renderJobs();
      if (state.expanded === id) loadDetail(id);
    };
    tr.querySelectorAll("button[data-act]").forEach(btn => {
      btn.onclick = e => { e.stopPropagation(); mark(tr.dataset.id, btn.dataset.act); };
    });
  });
}

function meterRow(label, v) {
  const pct = v == null ? 0 : Math.round(v * 100);
  const val = v == null ? "–" : v.toFixed(2);
  return `<div class="meter"><span class="lab">${label}</span>
    <span class="track"><span class="fill" style="width:${pct}%"></span></span>
    <span class="val">${val}</span></div>`;
}
function renderDetail(j) {
  return `<tr class="detail"><td colspan="7">
    <div class="meters">
      ${meterRow("domain", j.fit_domain)}${meterRow("function", j.fit_function)}
      ${meterRow("stack", j.fit_stack)}${meterRow("seniority", j.fit_seniority)}
    </div>
    <div class="reason">${esc(cleanReason(j.fit_reason)) || "<i>unscored</i>"}</div>
    <div class="meta">posted ${esc(j.posted_at || "?")} · first seen ${esc((j.first_seen || "").slice(0,10))}
      · last seen ${esc((j.last_seen || "").slice(0,10))} · status ${esc(j.status || "open")}
      ${j.disposition ? `· <b>${esc(j.disposition)}</b> ${esc(j.disposition_note || "")}
        <button class="clearbtn" data-id="${esc(j.job_id)}">clear</button>` : ""}
      · <span style="user-select:all">${esc(j.job_id)}</span></div>
    <div class="descr" id="descr-${esc(j.job_id)}">loading description…</div>
  </td></tr>`;
}
async function loadDetail(id) {
  try {
    const d = await api(withTrack(`/api/job/${encodeURIComponent(id)}`));
    const el = document.getElementById(`descr-${id}`);
    if (el) el.textContent = (d.description || "(no description stored)").slice(0, 4000);
  } catch (e) { toast("detail failed: " + e.message); }
  document.querySelectorAll(".clearbtn").forEach(b =>
    b.onclick = () => mark(b.dataset.id, "clear"));
}

async function mark(id, disp) {
  let note = null;
  if (disp === "dismissed" || disp === "rejected") {
    note = prompt(`Why ${disp}? (optional — dismissal reasons teach the scorer)`);
    if (note === null) return;              // cancelled
  }
  try {
    await post(withTrack(`/api/job/${encodeURIComponent(id)}/disposition`),
               { disposition: disp, note });
    toast(disp === "clear" ? "cleared" : `marked ${disp}`);
    await refreshData();
  } catch (e) { toast("failed: " + e.message); }
}

/* ---------------- pipeline ---------------- */
function renderPipeline() {
  const groups = ["applied", "interviewing", "saved", "rejected", "dismissed"];
  const byDisp = Object.fromEntries(groups.map(g => [g, []]));
  state.pipeline.forEach(j => (byDisp[j.disposition] || []).push(j));
  const total = state.pipeline.length;
  if (!total) {
    $("#pipeline").innerHTML =
      `<div class="empty">Nothing decided yet — use the buttons on the Jobs tab.
       Applied/interviewing land here; dismissals teach the scorer.</div>`;
    return;
  }
  $("#pipeline").innerHTML = groups.filter(g => byDisp[g].length).map(g => `
    <div class="pgroup"><h3>${g} (${byDisp[g].length})</h3>
    <table><tbody>${byDisp[g].map(j => `
      <tr>
        <td style="width:110px">${esc((j.disposition_at || "").slice(0,10))}</td>
        <td class="title-cell">
          <a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a>
          <div class="co">${esc(j.company_name)}</div></td>
        <td>${j.status === "closed"
              ? `<span class="chip closed">${g === "applied" || g === "interviewing"
                  ? "closed after apply" : "closed"}</span>` : ageChip(j.age)}</td>
        <td class="note">${esc(j.disposition_note || "")}</td>
        <td><div class="acts">
          ${g !== "applied" ? `<button data-d="applied">applied</button>` : ""}
          ${g !== "interviewing" ? `<button data-d="interviewing">interview</button>` : ""}
          ${g !== "rejected" ? `<button data-d="rejected">rejected</button>` : ""}
          <button data-d="clear">clear</button>
        </div></td>
      </tr>`).join("")}</tbody></table></div>`).join("");
  document.querySelectorAll("#pipeline button[data-d]").forEach(b => {
    const tr = b.closest("tr");
    const url = tr.querySelector("a").href;
    const job = state.pipeline.find(j => j.url === url);
    b.onclick = () => job && mark(job.job_id, b.dataset.d);
  });
}

/* ---------------- companies ---------------- */
function renderCompanies() {
  const q = $("#c-search").value.trim().toLowerCase();
  const activeOnly = $("#c-activeonly").checked;
  let rows = state.companies
    .filter(c => (!activeOnly || c.active) && (!q || c.name.toLowerCase().includes(q)));
  rows.sort((a, b) => (b.watched - a.watched) || (b.open_jobs - a.open_jobs)
                      || ((b.mission_score || 0) - (a.mission_score || 0)));
  $("#companies").innerHTML = `<table>
    <thead><tr><th></th><th>Company</th><th>ATS</th><th>Mission</th>
    <th class="num">Score</th><th class="num">Open jobs</th><th>Tags</th><th>Active</th></tr></thead>
    <tbody>${rows.map(c => `
      <tr>
        <td><button class="star ${c.watched ? "on" : ""}" data-id="${c.id}"
             title="watch: whole board fetched, new technical postings flagged">${c.watched ? "★" : "☆"}</button></td>
        <td>${esc(c.name)}</td>
        <td class="loc">${esc(c.ats || "—")}</td>
        <td><span class="chip tier">${esc(c.mission_tier || "?")}</span></td>
        <td class="num">${c.mission_score == null ? "–" : c.mission_score.toFixed(2)}</td>
        <td class="num">${c.open_jobs}</td>
        <td class="loc">${esc((c.tags || []).filter(t => t !== "watch").join(", "))}</td>
        <td><input type="checkbox" data-active="${c.id}" ${c.active ? "checked" : ""}></td>
      </tr>`).join("")}</tbody></table>`;
  document.querySelectorAll(".star").forEach(b => b.onclick = async () => {
    try {
      await post(withTrack(`/api/company/${b.dataset.id}/watch`), { on: !b.classList.contains("on") });
      toast(b.classList.contains("on") ? "unwatched" : "watching");
      await refreshData();
    } catch (e) { toast("failed: " + e.message); }
  });
  document.querySelectorAll("input[data-active]").forEach(cb => cb.onchange = async () => {
    try { await post(withTrack(`/api/company/${cb.dataset.active}/active`), { on: cb.checked }); }
    catch (e) { toast("failed: " + e.message); }
    await refreshData();
  });
}

/* ---------------- operations ---------------- */
const OPS = [
  ["crawl", "Crawl", "Run the active track's crawl — its engine, sources, keyword focus and DB all come from the track's config. Scores new jobs; verify/websearch flags apply where the engine supports them.",
   [["top", "top N", 15], ["workers", "workers", 6], ["no_verify", "skip verify", false, "check"],
    ["no_websearch", "skip web search", false, "check"], ["no_fit", "skip fit scoring", false, "check"]]],
  ["sync", "Sync statuses", "Re-fetch boards; reconcile open/closed + backfill posting dates. No scoring, no API.",
   [["top", "top N", 15]]],
  ["verify", "Deep-verify top N", "Full-JD requirements read of the ranking's finalists (Opus verify model).",
   [["top", "top N", 15]]],
  ["check-closed", "Probe stale URLs", "Probe jobs no board has vouched for lately; close the provably dead.",
   [["stale_days", "stale days", 2], ["limit", "limit", ""]]],
  ["rescore", "Rescore all", "Re-screen every described, undecided open job with the current rubric.",
   [["described_only", "described only", true, "check"]]],
  ["backfill-descriptions", "Backfill descriptions", "Fetch full JD text for rows missing a body (any ATS, board title-match).",
   [["limit", "limit", ""]]],
  ["backfill-workday", "Backfill Workday JDs", "Fetch full JD text for stored Workday rows via the CXS endpoint.",
   [["limit", "limit", ""]]],
  ["nlx", "NLx ingest", "Pull NC postings for bot-gated employers (Meta, Google, Qualcomm…) from the federal NLx feed.",
   [["companies", "companies (comma-sep)", "", "wide"]]],
  ["discover-local", "Discover local companies", "Full sourcing pass: directories + web search + LLM brainstorm + ATS-dork sweep → new roster rows (long).",
   [["no_dork", "skip dork sweep", false, "check"]]],
  ["dork", "ATS dork sweep", "Search-indexed board URLs only (site:greenhouse/lever/… + locality) — finds employers by their postings, no name guessing.", []],
  ["score-missions", "Score missions", "Mission-tier any active company that has a board but no score yet.",
   [["rescore", "re-score all", false, "check"]]],
  ["prune", "Prune dead boards", "Deactivate companies whose ATS board 404s.",
   [["offmission", "also off-mission", false, "check"]]],
  ["dedup", "Dedup companies", "Merge duplicate company rows pointing at the same board.", []],
];

function opParam([k, lab, dv, type]) {
  if (type === "check")
    return `<label class="loc"><input type="checkbox" data-p="${k}" ${dv ? "checked" : ""}> ${lab}</label>`;
  if (type === "wide")
    return `<input class="wide" data-p="${k}" placeholder="${lab}" value="${dv}">`;
  return `<label class="loc">${lab} <input data-p="${k}" value="${dv}"></label>`;
}

function renderOps() {
  const allowed = new Set(currentTrack()?.ops || OPS.map(o => o[0]));
  $("#opcards").innerHTML = OPS.map(([name, label, desc, params]) => `
    <div class="op ${allowed.has(name) ? "" : "dim"}"><h3>${label}</h3><p>${desc}</p>
      ${params.filter(p => p[3] === "wide").map(opParam).join("")}
      <div class="row">
        ${params.filter(p => p[3] !== "wide").map(opParam).join("")}
        <button class="run" data-op="${name}" ${allowed.has(name) ? "" : "disabled title='not available on this track'"}>Run</button>
      </div></div>`).join("") + `
    <div class="op ${allowed.has("add-job") ? "" : "dim"}"><h3>Add manual job</h3>
      <p>Curated add: registers the company, pulls its board's other local jobs.</p>
      <input class="wide" data-a="url" placeholder="posting URL">
      <input class="wide" data-a="title" placeholder="title">
      <input class="wide" data-a="company" placeholder="company">
      <div class="row">
        <input data-a="location" placeholder="location" style="width:150px">
        <button class="run" data-op="add-job" ${allowed.has("add-job") ? "" : "disabled"}>Add</button>
      </div></div>
    <div class="op ${allowed.has("add-board") ? "" : "dim"}"><h3>Add company board</h3>
      <p>Register a company by its careers/board URL: resolves the ATS, counts local jobs, mission-scores it.</p>
      <input class="wide" data-a="name" placeholder="company name">
      <input class="wide" data-a="url" placeholder="careers / board URL">
      <div class="row"><button class="run" data-op="add-board" ${allowed.has("add-board") ? "" : "disabled"}>Add</button></div>
    </div>`;
  document.querySelectorAll("button[data-op]").forEach(b => b.onclick = async () => {
    const card = b.closest(".op");
    const params = {};
    card.querySelectorAll("input[data-p]").forEach(i =>
      params[i.dataset.p] = i.type === "checkbox" ? i.checked : i.value);
    card.querySelectorAll("input[data-a]").forEach(i => params[i.dataset.a] = i.value);
    try {
      await post(withTrack(`/api/run/${b.dataset.op}`), params);
      toast(`started: ${b.dataset.op}`);
      state.logTotal = 0; $("#oplog").textContent = "";
    } catch (e) { toast(e.message); }
  });
}

async function pollOps() {
  try {
    const s = await api(`/api/run/status?since=${state.logTotal}`);
    const pill = $("#oppill");
    if (s.running) {
      pill.textContent = `running: ${s.name}`; pill.className = "pill running";
    } else if (s.error) {
      pill.textContent = `failed: ${s.name}`; pill.className = "pill err";
    } else {
      pill.textContent = s.name ? `idle (last: ${s.name})` : "idle";
      pill.className = "pill";
    }
    $("#opinfo").textContent = s.error || "";
    const allowed = new Set(currentTrack()?.ops || []);
    document.querySelectorAll("button.run[data-op]").forEach(b =>
      b.disabled = s.running || !allowed.has(b.dataset.op));
    document.querySelectorAll("#setcards button.run").forEach(b => b.disabled = s.running);
    if (s.lines.length) {
      const log = $("#oplog");
      const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
      log.textContent += s.lines.join("\n") + "\n";
      state.logTotal = s.total;
      if (atBottom) log.scrollTop = log.scrollHeight;
    }
    if (state.wasRunning && !s.running) await refreshData();  // op just finished
    state.wasRunning = s.running;
  } catch (e) { /* server briefly busy — next poll catches up */ }
}

/* ---------------- settings (profile.toml editor) ---------------- */
const getPath = (o, p) => p.split(".").reduce((x, k) => (x == null ? x : x[k]), o);
const chipVals = {};          // dotted path -> live array of strings
let rawDirty = false;

function chipField(path, label) {
  chipVals[path] = (getPath(state.config, path) || []).slice();
  return `<div class="chipfield" data-path="${esc(path)}">
    <div class="chiplab">${esc(label)}</div><div class="chips"></div>
    <input class="chipadd" placeholder="type + Enter to add"></div>`;
}
function renderChips(cf) {
  const path = cf.dataset.path;
  cf.querySelector(".chips").innerHTML = (chipVals[path] || []).map((v, i) =>
    `<span class="chip">${esc(v)}<button data-i="${i}" title="remove">×</button></span>`).join("")
    || `<span class="loc">empty</span>`;
  cf.querySelectorAll(".chips button").forEach(b => b.onclick = () => {
    chipVals[path].splice(+b.dataset.i, 1); renderChips(cf);
  });
}
function wireChipFields(root) {
  root.querySelectorAll(".chipfield").forEach(cf => {
    renderChips(cf);
    cf.querySelector(".chipadd").addEventListener("keydown", e => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const vals = e.target.value.split(",").map(s => s.trim()).filter(Boolean);
      if (vals.length) { chipVals[cf.dataset.path].push(...vals); e.target.value = ""; renderChips(cf); }
    });
  });
}
const numField = (path, label, val) =>
  `<label>${esc(label)}<input type="number" min="0" max="1" step="0.01"
     data-num="${esc(path)}" value="${val ?? ""}"></label>`;
const chkField = (path, label, val) =>
  `<label class="loc"><input type="checkbox" data-chk="${esc(path)}" ${val ? "checked" : ""}> ${esc(label)}</label>`;
const txtField = (path, label, val) =>
  `<input class="txt" data-txt="${esc(path)}" placeholder="${esc(label)}" value="${esc(val ?? "")}" title="${esc(label)}">`;

function settingsCard(title, desc, body) {
  return `<div class="setcard"><h3>${esc(title)}</h3>${desc ? `<p>${esc(desc)}</p>` : ""}
    ${body}<div class="row"><button class="run savecard">Save &amp; restart</button></div></div>`;
}

function renderSettings() {
  const c = state.config || {};
  const cards = [];
  const isTable = v => v && typeof v === "object" && !Array.isArray(v);

  // Keywords: global tiers + one card per track sub-table — built from
  // whatever the profile actually contains, nothing hardcoded.
  const kw = c.keywords || {};
  cards.push(settingsCard("Keywords", "Tier model: any CORE hit is relevant; DOMAIN needs a SKILL pair.",
    ["core", "domain", "skill"].map(k => chipField(`keywords.${k}`, k)).join("")));
  for (const [sub, tbl] of Object.entries(kw)) {
    if (!isTable(tbl)) continue;
    cards.push(settingsCard(`Keywords — ${sub}`, `Track-specific additions ([keywords.${sub}]).`,
      Object.keys(tbl).map(k => chipField(`keywords.${sub}.${k}`, k)).join("")));
  }

  // Excludes: global lists + track sub-tables.
  const exc = c.exclude || {};
  const excLists = Object.entries(exc).filter(([, v]) => Array.isArray(v));
  cards.push(settingsCard("Excludes", "Phrases that drop a posting (title_phrases match the title only).",
    excLists.map(([k]) => chipField(`exclude.${k}`, k)).join("")));
  for (const [sub, tbl] of Object.entries(exc)) {
    if (!isTable(tbl)) continue;
    cards.push(settingsCard(`Excludes — ${sub}`, `Track-specific exclusions ([exclude.${sub}]).`,
      Object.keys(tbl).filter(k => Array.isArray(tbl[k]))
        .map(k => chipField(`exclude.${sub}.${k}`, k)).join("")));
  }

  // Locations + locality.
  const loc = c.locations || {};
  cards.push(settingsCard("Locations", "Location-field filters for the shared fetch path.",
    ["onsite", "remote", "exclude"].map(k => chipField(`locations.${k}`, k)).join("") +
    `<div class="row">${chkField("locations.accept_remote", "accept remote listings", loc.accept_remote)}</div>`));
  const lcl = c.locality || {};
  cards.push(settingsCard("Locality", "What counts as “local” — drives the geo bucket and the local track's gate.",
    txtField("locality.name", "locality name", lcl.name) +
    ["word_tokens", "substrings", "state_suffix"].map(k => chipField(`locality.${k}`, k)).join("")));

  // Fit scoring: weights + gate penalties, keys from the profile itself.
  const fit = c.fit || {};
  const numGrid = (group) => `<div class="numgrid">${
    Object.entries(fit[group] || {}).map(([k, v]) => numField(`fit.${group}.${k}`, k, v)).join("")}</div>`;
  cards.push(settingsCard("Fit scoring", "Axis weights and gate penalties (0..1). Ladder/stack live in the raw editor.",
    `<div class="chiplab">weights</div>${numGrid("weights")}
     <div class="chiplab">gate penalties</div>${numGrid("gate_penalty")}`));

  // Tracks: label + UI defaults per configured track.
  for (const [tid, t] of Object.entries(c.tracks || {})) {
    if (!isTable(t)) continue;
    cards.push(settingsCard(`Track — ${tid}`, `Engine: ${t.engine || "local"} · db: ${t.db || "?"} (change via raw editor)`,
      txtField(`tracks.${tid}.label`, "label", t.label) +
      `<div class="numgrid">${numField(`tracks.${tid}.min_fit_default`, "min fit default", t.min_fit_default)}</div>
       <div class="row">${chkField(`tracks.${tid}.willing_to_move_default`, "willing to move by default", t.willing_to_move_default)}
       ${chkField(`tracks.${tid}.remote_requires_watch`, "remote rows need watch tag", t.remote_requires_watch)}</div>`));
  }

  // Raw TOML editor — the escape hatch for everything else.
  cards.push(`<div class="setcard rawcard"><h3>Raw profile.toml</h3>
    <p>Full profile (source: ${esc(state.configSource || "?")}) — candidate, mission,
       discovery, fit ladder, per-track db/engine, and anything not covered above.</p>
    <textarea id="rawtoml" spellcheck="false">${esc(state.configRaw || "")}</textarea>
    <div class="valerrs" id="valerrs"></div>
    <div class="row">
      <button class="run" id="rawvalidate">Validate</button>
      <button class="run" id="rawsave">Save &amp; restart</button>
    </div></div>`);

  const root = $("#setcards");
  root.innerHTML = cards.join("");
  wireChipFields(root);

  root.querySelectorAll(".savecard").forEach(b => b.onclick = async () => {
    const card = b.closest(".setcard");
    const updates = {};
    card.querySelectorAll(".chipfield").forEach(cf =>
      updates[cf.dataset.path] = chipVals[cf.dataset.path]);
    card.querySelectorAll("input[data-num]").forEach(i => {
      if (i.value !== "") updates[i.dataset.num] = parseFloat(i.value); });
    card.querySelectorAll("input[data-chk]").forEach(i =>
      updates[i.dataset.chk] = i.checked);
    card.querySelectorAll("input[data-txt]").forEach(i =>
      updates[i.dataset.txt] = i.value);
    try {
      const r = await put("/api/config", { updates });
      if (r.restarting) showRestart();
    } catch (e) { toast("save failed: " + e.message); }
  });

  $("#rawtoml").addEventListener("input", () => { rawDirty = true; });
  $("#rawvalidate").onclick = async () => {
    try {
      const r = await post("/api/config/validate", { toml: $("#rawtoml").value });
      $("#valerrs").textContent = r.errors.length ? r.errors.join("\n") : "";
      toast(r.errors.length ? `${r.errors.length} problem(s)` : "valid ✓");
    } catch (e) { toast("validate failed: " + e.message); }
  };
  $("#rawsave").onclick = async () => {
    try {
      const r = await put("/api/config/raw", { toml: $("#rawtoml").value });
      if (r.restarting) { rawDirty = false; showRestart(); }
    } catch (e) {
      toast("save refused: " + e.message);
      try {
        const v = await post("/api/config/validate", { toml: $("#rawtoml").value });
        $("#valerrs").textContent = (v.errors || []).join("\n");
      } catch {}
    }
  };
}

async function loadSettings() {
  try {
    const c = await api("/api/config");
    state.config = c.parsed; state.configRaw = c.raw; state.configSource = c.source;
    renderSettings();
  } catch (e) { toast("config load failed: " + e.message); }
}

window.addEventListener("beforeunload", e => {
  if (rawDirty) { e.preventDefault(); e.returnValue = ""; }
});

function showRestart() {
  rawDirty = false;
  const prevBoot = state.stats.boot_id;
  $("#overlaymsg").textContent = "Config saved — restarting…";
  $("#overlay").classList.add("show");
  const t0 = Date.now();
  const iv = setInterval(async () => {
    if (Date.now() - t0 > 30000) {
      clearInterval(iv);
      $("#overlaymsg").textContent =
        "Restart timed out — start the server manually (python webapp.py), then reload this page.";
      return;
    }
    try {
      const s = await api(withTrack("/api/stats"));
      if (s.boot_id && s.boot_id !== prevBoot) { clearInterval(iv); location.reload(); }
    } catch (e) { /* successor not up yet */ }
  }, 500);
}

/* ---------------- data loading ---------------- */
async function loadJobs() {
  const closed = $("#f-closed").checked ? "1" : "0";
  const disp = $("#f-disp").checked ? "1" : "0";
  state.jobs = await api(withTrack(`/api/jobs?closed=${closed}&dispositioned=${disp}`));
}
async function refreshData() {
  const [stats, , pipeline, companies] =
    await Promise.all([api(withTrack("/api/stats")), loadJobs(),
                       api(withTrack("/api/pipeline")),
                       api(withTrack("/api/companies"))]);
  state.stats = stats; state.pipeline = pipeline; state.companies = companies;
  renderTiles(); renderJobs(); renderPipeline(); renderCompanies();
}

/* ---------------- tracks ---------------- */
function applyTrackDefaults(t) {
  $("#f-fit").value = t.min_fit_default ?? 0;
  $("#f-move").checked = !!t.willing_to_move_default;
}
function renderTrackPick() {
  $("#trackpick").innerHTML = state.tracks.map(t =>
    `<option value="${esc(t.id)}" ${t.id === state.track ? "selected" : ""}>${esc(t.label)}</option>`
  ).join("");
  // Export link is a plain <a>: keep its track param current.
  const exp = document.querySelector('a[href^="/api/export/companies"]');
  if (exp) exp.href = withTrack("/api/export/companies");
  renderOps();  // op availability differs per track
}
async function loadTracks() {
  state.tracks = await api("/api/tracks");
  const saved = localStorage.getItem("track");
  const t = state.tracks.find(x => x.id === saved)
         || state.tracks.find(x => x.default) || state.tracks[0];
  state.track = t ? t.id : null;
  if (t) applyTrackDefaults(t);
  renderTrackPick();
}
$("#trackpick").addEventListener("change", async e => {
  state.track = e.target.value;
  localStorage.setItem("track", state.track);
  const t = currentTrack();
  if (t) applyTrackDefaults(t);
  state.expanded = null;
  renderTrackPick();
  await refreshData();
});

["#f-search", "#f-fit", "#f-geo", "#f-move", "#f-age", "#f-verified", "#f-watched"]
  .forEach(s => $(s).addEventListener("input", renderJobs));
["#f-closed", "#f-disp"].forEach(s => $(s).addEventListener("change",
  async () => { await loadJobs(); renderJobs(); }));
["#c-search", "#c-activeonly"].forEach(s => $(s).addEventListener("input", renderCompanies));
$("#c-import").addEventListener("change", async e => {
  const f = e.target.files[0];
  if (!f) return;
  const fd = new FormData(); fd.append("file", f);
  try {
    const r = await fetch(withTrack("/api/import/companies"), { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
    toast(`imported/refreshed ${d.imported} companies`);
    await refreshData();
  } catch (err) { toast("import failed: " + err.message); }
  e.target.value = "";
});

(async () => {
  try { await loadTracks(); }               // sets state.track + op availability
  catch (e) { toast("tracks failed: " + e.message); renderOps(); }
  refreshData().catch(e => toast("load failed: " + e.message));
})();
setInterval(pollOps, 1500);
pollOps();
