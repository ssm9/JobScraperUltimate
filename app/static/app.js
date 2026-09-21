"use strict";

const state = {
  view: "jobs",
  page: 1,
  perPage: 50,
  q: "",
  status: "",
  site: "",
  sort: "first_seen",
  order: "desc",
  selected: new Set(),
  selectAllMatching: false,  // "all N matching", not just the loaded page
  currentJobs: [],           // this page, in display order, for shift-ranges
  lastIndex: null,           // anchor for shift-click
  total: 0,
  sites: [],
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer;
function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

function timeAgo(iso) {
  if (!iso) return "";
  const then = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  const seconds = (Date.now() - then.getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
  return then.toLocaleDateString();
}

/* ============================ navigation ============================ */

function showView(view) {
  state.view = view;
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  if (view === "jobs") loadJobs();
  if (view === "settings") loadSettings();
  if (view === "activity") loadRuns();
}

/* ============================== jobs =============================== */

async function loadStats() {
  try {
    const stats = await api("/api/stats");
    const by = stats.by_status || {};
    $("#stats").innerHTML = [
      ["Total jobs", stats.total],
      ["Last 24h", stats.last_24h],
      ["New", by.new || 0],
      ["Saved", by.saved || 0],
      ["Applied", by.applied || 0],
    ].map(([k, n]) => `<div class="stat"><div class="n">${n}</div><div class="k">${k}</div></div>`).join("");

    const pill = $("#status-pill");
    if (stats.scraping) {
      pill.textContent = "Scraping…";
      pill.className = "pill busy";
    } else if (stats.schedule_enabled) {
      const mins = Math.round((stats.interval_seconds || 0) / 60);
      pill.textContent = `Auto · every ${mins}m`;
      pill.className = "pill live";
    } else {
      pill.textContent = "Manual";
      pill.className = "pill";
    }
    $("#scrape-now").disabled = stats.scraping;

    // Keep the source filter in sync with whatever is actually in the DB.
    const sites = Object.keys(stats.by_site || {}).filter(Boolean).sort();
    if (sites.join() !== state.sites.join()) {
      state.sites = sites;
      const select = $("#filter-site");
      select.innerHTML = `<option value="">All sources</option>` +
        sites.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
      select.value = state.site;
    }
  } catch (err) {
    $("#status-pill").textContent = "Offline";
  }
}

function jobCard(job, index) {
  const isSelected = state.selectAllMatching || state.selected.has(job.id);
  const checked = isSelected ? "checked" : "";
  const meta = [job.location, job.salary, job.job_type].filter(Boolean);
  return `
  <div class="job ${job.status === "applied" ? "is-applied" : ""} ${isSelected ? "selected" : ""}"
       data-id="${job.id}" data-index="${index}">
    <input type="checkbox" class="select" ${checked} aria-label="Select job">
    <div class="job-main">
      <div class="job-title"><a href="${esc(job.job_url)}" target="_blank" rel="noopener">${esc(job.title) || "(untitled)"}</a></div>
      <div class="job-meta">
        <span class="company">${esc(job.company) || "Unknown company"}</span>
        ${meta.map((m) => `<span>· ${esc(m)}</span>`).join("")}
      </div>
      <div class="badges">
        <span class="badge status-${esc(job.status)}">${esc(job.status)}</span>
        ${job.site ? `<span class="badge">${esc(job.site)}</span>` : ""}
        ${job.is_remote ? `<span class="badge">remote</span>` : ""}
        <span class="badge">found ${esc(timeAgo(job.first_seen))}</span>
      </div>
    </div>
    <div class="job-actions">
      ${job.status !== "saved" ? `<button class="btn small" data-act="saved">Save</button>` : ""}
      ${job.status !== "applied" ? `<button class="btn small" data-act="applied">Applied</button>` : ""}
      ${job.status !== "hidden"
        ? `<button class="btn small ghost danger" data-act="hidden">Hide</button>`
        : `<button class="btn small ghost" data-act="new">Unhide</button>`}
      ${job.company ? `<button class="btn small ghost danger" data-block="${esc(job.company)}"
        title="Hide every job from ${esc(job.company)} and stop scraping it">Block</button>` : ""}
    </div>
  </div>`;
}

async function loadJobs() {
  const params = new URLSearchParams({
    q: state.q, status: state.status, site: state.site,
    sort: state.sort, order: state.order,
    page: state.page, per_page: state.perPage,
  });
  try {
    const data = await api(`/api/jobs?${params}`);
    state.currentJobs = data.jobs;
    state.total = data.total;
    $("#jobs-list").innerHTML = data.jobs.length
      ? data.jobs.map((job, i) => jobCard(job, i)).join("")
      : `<div class="empty"><p>No jobs match these filters.</p>
         <p class="muted">Try “Scrape now”, or widen your role keywords in Settings.</p></div>`;
    $("#page-info").textContent = `Page ${data.page} of ${data.pages} · ${data.total} jobs`;
    $("#prev").disabled = data.page <= 1;
    $("#next").disabled = data.page >= data.pages;
    $("#export").href = `/api/export.csv?${new URLSearchParams({ q: state.q, status: state.status })}`;
    refreshBulkBar();
  } catch (err) {
    toast(`Could not load jobs: ${err.message}`, true);
  }
  loadStats();
}

function selectedCount() {
  return state.selectAllMatching ? state.total : state.selected.size;
}

function refreshBulkBar() {
  const count = selectedCount();
  $("#bulkbar").hidden = count === 0;
  $("#bulk-count").textContent = state.selectAllMatching
    ? `All ${count} matching jobs selected`
    : `${count} selected`;

  // "Select page" reflects whether every job on this page is selected.
  const pageIds = state.currentJobs.map((j) => j.id);
  const allOnPage = pageIds.length > 0 && pageIds.every((id) => state.selected.has(id));
  const box = $("#select-page");
  box.checked = state.selectAllMatching || allOnPage;
  box.indeterminate = !box.checked && state.selected.size > 0;

  // Offer to extend the selection past this page when there is more to select.
  const hint = $("#select-all-hint");
  if (state.selectAllMatching) {
    hint.innerHTML = `<button type="button" id="clear-all-matching">Select only this page instead</button>`;
  } else if (allOnPage && state.total > pageIds.length) {
    hint.innerHTML = `All ${pageIds.length} on this page selected.
      <button type="button" id="select-all-matching">Select all ${state.total} matching</button>`;
  } else {
    hint.innerHTML = "";
  }
}

function setSelected(id, on, cardIndex) {
  on ? state.selected.add(id) : state.selected.delete(id);
  const card = cardIndex === undefined
    ? document.querySelector(`.job[data-id="${id}"]`)
    : document.querySelector(`.job[data-index="${cardIndex}"]`);
  if (card) {
    card.classList.toggle("selected", on);
    const box = card.querySelector(".select");
    if (box) box.checked = on;
  }
}

function clearSelection() {
  state.selected.clear();
  state.selectAllMatching = false;
  state.lastIndex = null;
}

async function applyBulk(status) {
  if (selectedCount() === 0) return;

  try {
    if (state.selectAllMatching) {
      if (!confirm(`Apply "${status}" to all ${state.total} jobs matching the current filters?`)) return;
      const res = await api("/api/jobs/bulk-filter", {
        method: "POST",
        body: JSON.stringify({
          q: state.q, status: state.status, site: state.site,
          company: state.company || "", new_status: status,
        }),
      });
      toast(`Updated ${res.updated} job(s).`);
    } else {
      await api("/api/jobs/bulk", {
        method: "POST",
        body: JSON.stringify({ ids: [...state.selected], status }),
      });
    }
    clearSelection();
    loadJobs();
  } catch (err) {
    toast(err.message, true);
  }
}

/* ============================ settings ============================= */

const LIST_FIELDS = ["roles_of_interest", "exclude_keywords", "exclude_companies"];

async function loadSettings() {
  try {
    const { settings, available_sites } = await api("/api/settings");
    const form = $("#settings-form");

    $("#sites").innerHTML = available_sites.map((site) => `
      <label class="chip"><input type="checkbox" name="site" value="${esc(site)}"
        ${settings.scrape_from.includes(site) ? "checked" : ""}> ${esc(site)}</label>`).join("");

    for (const [key, value] of Object.entries(settings)) {
      const field = form.elements[key];
      if (!field || key === "scrape_from") continue;
      if (field.type === "checkbox") field.checked = Boolean(value);
      else if (LIST_FIELDS.includes(key)) field.value = (value || []).join("\n");
      else field.value = value ?? "";
    }
    loadCompanies();
  } catch (err) {
    toast(`Could not load settings: ${err.message}`, true);
  }
}

async function loadCompanies() {
  try {
    const { companies } = await api("/api/companies?limit=12");
    const noisy = companies.filter((c) => c.total > 1);
    if (!noisy.length) { $("#company-counts").innerHTML = ""; return; }
    $("#company-counts").innerHTML = `
      <h3>Most frequent companies</h3>
      <p class="muted">Postings vs. distinct roles — a big gap means the same job reposted.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>Company</th><th>Postings</th><th>Distinct roles</th><th></th></tr></thead>
        <tbody>${noisy.map((c) => `<tr>
          <td>${esc(c.company)}</td><td>${c.total}</td><td>${c.distinct_roles}</td>
          <td>${c.blocked
            ? `<span class="badge">blocked</span>`
            : `<button class="btn small ghost danger" data-block-row="${esc(c.company)}">Block</button>`}</td>
        </tr>`).join("")}</tbody></table></div>`;
  } catch (err) {
    $("#company-counts").innerHTML = "";
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const form = event.target;
  const payload = {};

  for (const field of form.elements) {
    if (!field.name || field.name === "site") continue;
    if (field.type === "checkbox") payload[field.name] = field.checked;
    else if (LIST_FIELDS.includes(field.name))
      payload[field.name] = field.value.split("\n").map((s) => s.trim()).filter(Boolean);
    else if (field.type === "number") payload[field.name] = field.value === "" ? null : Number(field.value);
    else payload[field.name] = field.value;
  }
  payload.scrape_from = $$('#sites input:checked').map((el) => el.value);
  Object.keys(payload).forEach((k) => payload[k] === null && delete payload[k]);

  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify(payload) });
    $("#save-msg").textContent = "Saved ✓";
    setTimeout(() => { $("#save-msg").textContent = ""; }, 2500);
    loadStats();
  } catch (err) {
    toast(`Save failed: ${err.message}`, true);
  }
}

/* ============================ activity ============================= */

async function loadRuns() {
  try {
    const { runs } = await api("/api/runs?limit=40");
    $("#runs").innerHTML = runs.length ? `<div class="table-wrap"><table>
      <thead><tr><th>Started</th><th>Trigger</th><th>Status</th><th>Scraped</th>
      <th>Matched</th><th>New</th><th>Message</th></tr></thead><tbody>
      ${runs.map((r) => `<tr>
        <td>${esc(timeAgo(r.started_at))}</td>
        <td>${esc(r.trigger)}</td>
        <td class="status-${esc(r.status)}">${esc(r.status)}</td>
        <td>${r.scraped}</td><td>${r.matched}</td><td>${r.new_jobs}</td>
        <td class="muted">${esc(r.message || "")}</td>
      </tr>`).join("")}</tbody></table></div>`
      : `<p class="muted">No scrape runs yet.</p>`;
  } catch (err) {
    toast(`Could not load activity: ${err.message}`, true);
  }
}

/* ============================== events ============================= */

let searchTimer;

document.addEventListener("DOMContentLoaded", () => {
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => showView(tab.dataset.view)));

  $("#search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.q = e.target.value.trim(); state.page = 1; clearSelection(); loadJobs(); }, 300);
  });
  $("#filter-status").addEventListener("change", (e) => { state.status = e.target.value; state.page = 1; clearSelection(); loadJobs(); });
  $("#filter-site").addEventListener("change", (e) => { state.site = e.target.value; state.page = 1; clearSelection(); loadJobs(); });
  $("#sort").addEventListener("change", (e) => {
    [state.sort, state.order] = e.target.value.split(":");
    state.page = 1;
    clearSelection();
    loadJobs();
  });
  $("#prev").addEventListener("click", () => { if (state.page > 1) { state.page--; state.lastIndex = null; loadJobs(); } });
  $("#next").addEventListener("click", () => { state.page++; state.lastIndex = null; loadJobs(); });

  // Shift-clicking a checkbox otherwise highlights all the text in between.
  $("#jobs-list").addEventListener("mousedown", (e) => {
    if (e.shiftKey && e.target.classList.contains("select")) e.preventDefault();
  });

  // Job card actions (delegated).
  $("#jobs-list").addEventListener("click", async (e) => {
    const card = e.target.closest(".job");
    if (!card) return;
    const id = Number(card.dataset.id);
    if (e.target.classList.contains("select")) {
      const index = Number(card.dataset.index);
      const on = e.target.checked;

      if (e.shiftKey && state.lastIndex !== null && state.lastIndex !== index) {
        // Apply this checkbox's new state across the whole range.
        const [from, to] = [Math.min(state.lastIndex, index), Math.max(state.lastIndex, index)];
        for (let i = from; i <= to; i++) {
          const job = state.currentJobs[i];
          if (job) setSelected(job.id, on, i);
        }
      } else {
        setSelected(id, on, index);
      }

      state.lastIndex = index;
      state.selectAllMatching = false;  // a manual change ends "all matching"
      refreshBulkBar();
      return;
    }
    const company = e.target.dataset.block;
    if (company) {
      if (!confirm(`Block "${company}"?\n\nIts existing jobs will be hidden and future scrapes will skip it. You can undo this in Settings.`)) return;
      try {
        const res = await api("/api/companies/block", {
          method: "POST",
          body: JSON.stringify({ company, hide_existing: true }),
        });
        toast(`Blocked ${company} — ${res.hidden} job(s) hidden.`);
        loadJobs();
      } catch (err) {
        toast(err.message, true);
      }
      return;
    }

    const action = e.target.dataset.act;
    if (!action) return;
    try {
      await api(`/api/jobs/${id}`, { method: "PATCH", body: JSON.stringify({ status: action }) });
      loadJobs();
    } catch (err) {
      toast(err.message, true);
    }
  });

  $$("[data-bulk]").forEach((btn) =>
    btn.addEventListener("click", () => applyBulk(btn.dataset.bulk)));
  $("#bulk-clear").addEventListener("click", () => { clearSelection(); loadJobs(); });

  // Select / deselect everything on the current page.
  $("#select-page").addEventListener("change", (e) => {
    const on = e.target.checked;
    state.selectAllMatching = false;
    state.currentJobs.forEach((job, i) => setSelected(job.id, on, i));
    state.lastIndex = null;
    refreshBulkBar();
  });

  // Extend to / retreat from every job matching the current filters.
  $("#select-all-hint").addEventListener("click", (e) => {
    if (e.target.id === "select-all-matching") {
      state.selectAllMatching = true;
      $$(".job").forEach((card) => {
        card.classList.add("selected");
        card.querySelector(".select").checked = true;
      });
      refreshBulkBar();
    } else if (e.target.id === "clear-all-matching") {
      state.selectAllMatching = false;
      state.currentJobs.forEach((job, i) => setSelected(job.id, true, i));
      refreshBulkBar();
    }
  });

  $("#scrape-now").addEventListener("click", async () => {
    try {
      await api("/api/scrape/run", { method: "POST" });
      toast("Scrape started — results appear as they land.");
      loadStats();
      setTimeout(loadJobs, 5000);
    } catch (err) {
      toast(err.message, true);
    }
  });

  $("#company-counts").addEventListener("click", async (e) => {
    const company = e.target.dataset.blockRow;
    if (!company) return;
    try {
      const res = await api("/api/companies/block", {
        method: "POST",
        body: JSON.stringify({ company, hide_existing: true }),
      });
      toast(`Blocked ${company} — ${res.hidden} job(s) hidden.`);
      loadSettings();
    } catch (err) {
      toast(err.message, true);
    }
  });

  $("#collapse-dupes").addEventListener("click", async () => {
    if (!confirm("Hide repeat postings of roles already in the list?\n\nThe earliest posting of each role is kept. Saved and applied jobs are never touched.")) return;
    try {
      const res = await api("/api/jobs/collapse-duplicates", { method: "POST" });
      toast(`Hid ${res.hidden} duplicate posting(s).`);
    } catch (err) {
      toast(err.message, true);
    }
  });

  $("#settings-form").addEventListener("submit", saveSettings);
  $("#test-email").addEventListener("click", async () => {
    try {
      const res = await api("/api/settings/test-email", { method: "POST" });
      toast(res.message);
    } catch (err) {
      toast(err.message, true);
    }
  });

  $$("[data-cleanup]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const hidden = btn.dataset.cleanup === "hidden";
      const what = hidden ? "all hidden jobs" : "jobs older than 30 days";
      if (!confirm(`Delete ${what}? This cannot be undone.`)) return;
      const params = hidden ? "status=hidden" : "older_than_days=30";
      try {
        const res = await api(`/api/jobs?${params}`, { method: "DELETE" });
        toast(`Deleted ${res.deleted} job(s).`);
        loadRuns();
      } catch (err) {
        toast(err.message, true);
      }
    }));

  showView("jobs");
  setInterval(() => {
    if (state.view === "jobs") loadStats();
    if (state.view === "activity") loadRuns();
  }, 15000);
});
