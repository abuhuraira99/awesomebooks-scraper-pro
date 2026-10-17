"use strict";

/* ============================================================
   AwesomeBooks Scraper Pro — dashboard
   Vanilla JS, no build step. One JobView class powers both
   modules; the server is the single source of truth.
   ============================================================ */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const icon = (name, cls = "") => `<svg class="i ${cls}"><use href="#i-${name}"/></svg>`;
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const CSRF = "awesomebooks-pro";

const local = {
  get(key, fallback = null) { try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ } },
  del(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } },
};

const fmt = {
  int: (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString("en-US")),
  money: (n) => (n === null || n === undefined ? "—" : "$" + Number(n).toFixed(2)),
  dur(sec) {
    if (sec === null || sec === undefined || !isFinite(sec)) return "—";
    sec = Math.max(0, Math.round(sec));
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
    if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
    return `${s}s`;
  },
  clock: (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—"),
  date: (ts) => (ts ? new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—"),
  ago(ts) {
    if (!ts) return "";
    const d = Date.now() / 1000 - ts;
    if (d < 45) return "just now";
    if (d < 3600) return `${Math.round(d / 60)}m ago`;
    if (d < 86400) return `${Math.round(d / 3600)}h ago`;
    return fmt.date(ts);
  },
};

/* ---------------- API ---------------- */
async function api(method, path, body) {
  const opts = { method, headers: { "X-Requested-With": CSRF } };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(path, opts); } catch { throw new Error("Can't reach the scraper. Is its window still open?"); }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `Request failed (HTTP ${res.status})`);
  return data;
}

/* ---------------- UI primitives ---------------- */
function toast(message, kind = "info") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `${icon(kind === "ok" ? "check-circle" : kind === "bad" ? "x-circle" : "info")}<div>${esc(message)}</div>`;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; setTimeout(() => el.remove(), 320); }, kind === "bad" ? 6500 : 3800);
}

function confirmDialog(title, text, yes = "Delete") {
  const dlg = $("#confirmModal");
  $("#confirmTitle").textContent = title;
  $("#confirmText").textContent = text;
  $("#confirmYes").textContent = yes;
  dlg.returnValue = "no";
  dlg.showModal();
  return new Promise((resolve) => {
    const onClick = (e) => {
      const answer = e.target.closest("[data-answer]");
      if (!answer) return;
      dlg.returnValue = answer.dataset.answer;
      dlg.close();
    };
    const onClose = () => {
      dlg.removeEventListener("click", onClick);
      dlg.removeEventListener("close", onClose);
      resolve(dlg.returnValue === "yes");
    };
    dlg.addEventListener("click", onClick);
    dlg.addEventListener("close", onClose); // also fires on Esc
  });
}

const STATUS_LABEL = {
  queued: "Starting", discovering: "Scanning pages", running: "Running", paused: "Paused",
  done: "Complete", failed: "Stopped (error)", ready: "Ready to start", interrupted: "Interrupted",
  stopping: "Stopping…",
};

/* ---------------- shared panel markup ---------------- */
function runMarkup(kind) {
  const stats = kind === "catalog"
    ? [["inStock", "In stock", "ok"], ["withUsed", "With used", "gold"], ["outStock", "Out of stock", ""], ["rate", "Per minute", ""], ["eta", "Time left", ""], ["errors", "Failed", "bad"]]
    : [["inStock", "In stock", "ok"], ["outStock", "Out of stock", ""], ["notListed", "Not listed", "warn"], ["rate", "Per minute", ""], ["eta", "Time left", ""], ["errors", "Failed", "bad"]];
  const phases = kind === "catalog" ? ["Discover", "Extract", "Export"] : ["Upload", "Check", "Export"];
  return `
    <div class="card-head">
      <div class="card-title">${icon("gauge")}Live run</div>
      <span class="run-status" data-el="status"><span class="status-dot"></span><span>Idle</span></span>
    </div>
    <div class="empty-run" data-el="empty">
      ${icon(kind === "catalog" ? "layers" : "package")}
      <b>Nothing running yet</b>
      <span>${kind === "catalog" ? "Paste a section link and press Start." : "Upload an ISBN list to begin."} A new run replaces the previous one; finished runs are saved to your Downloads folder automatically.</span>
    </div>
    <div class="hidden" data-el="body">
      <div class="run-top">
        <div class="ring">
          <svg viewBox="0 0 120 120"><circle class="track" cx="60" cy="60" r="52" fill="none" stroke-width="7"/><circle class="bar" data-el="ring" cx="60" cy="60" r="52" fill="none" stroke="url(#ringGrad)" stroke-width="7" stroke-linecap="round" stroke-dasharray="326.73" stroke-dashoffset="326.73"/></svg>
          <div class="ring-center"><div class="ring-pct" data-el="pct">0<small>%</small></div><div class="ring-sub" data-el="ringSub">0 / 0</div></div>
        </div>
        <div class="run-meta">
          <div class="run-name" data-el="name">—</div>
          <div class="run-count" data-el="count">—</div>
          <div class="phases" data-el="phases">
            ${phases.map((p, i) => `${i ? '<span class="phase-line"></span>' : ""}<span class="phase" data-phase="${i}"><span class="pip"></span>${p}</span>`).join("")}
          </div>
        </div>
      </div>
      <div class="stats">
        ${stats.map(([k, label, tone]) => `<div class="stat"><div class="stat-label">${label}</div><div class="stat-value ${tone}" data-stat="${k}">—</div></div>`).join("")}
      </div>
      <div class="run-actions">
        <button class="btn danger" data-act="stop">${icon("stop", "sm")}Stop</button>
        <button class="btn" data-act="resume">${icon("play", "sm")}Resume</button>
        <button class="btn" data-act="retry">${icon("retry", "sm")}Retry failed</button>
        <button class="btn success" data-act="export">${icon("download", "sm")}<span data-el="exportLabel">Export so far</span></button>
      </div>
      <div class="saved hidden" data-el="saved">
        ${icon("check-circle")}
        <div class="saved-text"><b>Excel saved to Downloads</b><span data-el="savedPath"></span></div>
        <button class="btn sm" data-act="reveal">${icon("sheet", "sm")}Show in folder</button>
      </div>
    </div>`;
}

function guardMarkup() {
  return `
    <div class="card-head"><div class="card-title">${icon("shield")}Traffic Guard</div><span class="card-hint">Anti-block</span></div>
    <div class="guard-state idle" data-el="guardState">${icon("shield", "lg")}<div><b>Standing by</b><span>Protection activates when a job starts.</span></div></div>
    <div class="meter">
      <div class="meter-row"><span>Effective pace</span><b data-el="paceVal">—</b></div>
      <div class="meter-bar"><span data-el="paceBar" style="width:0%"></span></div>
    </div>
    <div class="guard-grid">
      <div class="guard-cell"><div class="k">${icon("activity", "sm")}Requests</div><div class="v" data-g="requests">0</div></div>
      <div class="guard-cell"><div class="k">${icon("fingerprint", "sm")}Shoppers</div><div class="v" data-g="identities">0</div></div>
      <div class="guard-cell"><div class="k">${icon("shield-alert", "sm")}Slow-downs</div><div class="v" data-g="warnings">0</div></div>
      <div class="guard-cell"><div class="k">${icon("wifi-off", "sm")}Net retries</div><div class="v" data-g="network_errors">0</div></div>
      <div class="guard-cell wide"><div class="k">${icon("globe", "sm")}Switched to Ship&nbsp;to&nbsp;US</div><div class="v" data-g="locale_switches">0</div></div>
    </div>`;
}

function feedMarkup() {
  return `
    <div class="card-head"><div class="card-title">${icon("activity")}Activity</div><span class="card-hint" data-el="feedHint"></span></div>
    <ul class="feed" data-el="feed"><li class="feed-empty">Events from the running job appear here.</li></ul>`;
}

function tableMarkup(kind) {
  const tabs = kind === "catalog"
    ? [["", "All"], ["in_stock", "In stock"], ["used", "With used"], ["out_of_stock", "Out of stock"], ["pending", "Queued"], ["error", "Failed"]]
    : [["", "All"], ["in_stock", "In stock"], ["used", "With used"], ["out_of_stock", "Out of stock"], ["not_found", "Not listed"], ["invalid", "Invalid"], ["error", "Failed"]];
  const heads = kind === "catalog"
    ? `<th>Book</th><th>ISBN-13</th><th>Format</th><th>New · Used (US$ × qty)</th><th class="num">Qty</th><th>Status</th><th>Publisher</th><th></th>`
    : `<th>Row</th><th>SKU</th><th>ISBN-13</th><th>Title</th><th>Status</th><th class="num">Qty</th><th>New · Used (US$ × qty)</th><th>Checked</th><th></th>`;
  return `
    <div class="card-head"><div class="card-title">${icon("sheet")}Results</div><span class="card-hint" data-el="tableHint">Prices for US shipping, in USD</span></div>
    <div class="toolbar">
      <div class="tabs" data-el="tabs">${tabs.map(([k, l], i) => `<button class="tab ${i ? "" : "on"}" data-tab="${k}">${l}<span class="n" data-n="${k || "all"}">0</span></button>`).join("")}</div>
      <div class="input-wrap search">${icon("search", "sm")}<input class="input sm" name="q" aria-label="Search results" data-el="q" placeholder="Search title, ISBN, author…" /></div>
    </div>
    <div class="table-wrap"><table><thead><tr>${heads}</tr></thead><tbody data-el="rows"><tr><td colspan="9" class="table-empty">No data yet.</td></tr></tbody></table></div>
    <div class="pager">
      <span data-el="range">—</span>
      <div class="btns">
        <select class="select" name="limit" aria-label="Rows per page" data-el="limit"><option>25</option><option>50</option><option>100</option></select>
        <button class="btn icon sm" data-page="-1" aria-label="Previous">${icon("left", "sm")}</button>
        <button class="btn icon sm" data-page="1" aria-label="Next">${icon("right", "sm")}</button>
      </div>
    </div>`;
}

/* ---------------- row renderers ---------------- */
function stateBadge(row) {
  if (row.state === "pending") return `<span class="badge">Queued</span>`;
  if (row.state === "processing") return `<span class="badge info processing">Reading</span>`;
  if (row.state === "error") return `<span class="badge bad" title="${esc(row.error)}">Failed</span>`;
  if (row.state === "invalid") return `<span class="badge warn" title="${esc(row.error)}">Invalid ISBN</span>`;
  return null;
}

function offerChip(r, prefix, label) {
  const avail = r[`${prefix}_available`];
  if (avail === null || avail === undefined) return "";
  if (!avail) return `<span class="offer ${prefix} off"><span class="k">${label}</span><span class="p">none</span></span>`;
  const qty = r[`${prefix}_qty`];
  return `<span class="offer ${prefix}"><span class="k">${label}</span><span class="p">${fmt.money(r[`${prefix}_price`])}</span>${qty !== null && qty !== undefined ? `<span class="q">× ${fmt.int(qty)}</span>` : ""}</span>`;
}

function offers(r) {
  if (r.state !== "done") return `<span class="faint">—</span>`;
  const chips = offerChip(r, "new", "NEW") + offerChip(r, "used", "USED");
  return chips ? `<div class="offers">${chips}</div>` : `<span class="faint">—</span>`;
}

function totalQty(r) {
  if (r.state !== "done") return `<span class="qty zero">—</span>`;
  const parts = [["new", r.new_available], ["used", r.used_available]].filter(([, a]) => a).map(([p]) => r[`${p}_qty`]);
  if (!parts.length) return r.new_available === null && r.used_available === null ? `<span class="qty zero">—</span>` : `<span class="qty zero">0</span>`;
  if (parts.some((q) => q === null || q === undefined)) return `<span class="qty zero" title="Quantity is read from book pages">—</span>`;
  const sum = parts.reduce((a, b) => a + b, 0);
  return `<span class="qty${sum ? "" : " zero"}">${fmt.int(sum)}</span>`;
}

const extLink = (url) => (url ? `<a class="btn icon sm ghost" href="${esc(url)}" target="_blank" rel="noopener" title="Open on AwesomeBooks">${icon("external", "sm")}</a>` : "");

const ROWS = {
  catalog(r) {
    let status = stateBadge(r);
    if (!status) {
      const known = r.new_available !== null || r.used_available !== null;
      status = !known ? `<span class="badge">Listed</span>`
        : (r.new_available || r.used_available) ? `<span class="badge ok">In stock</span>`
        : `<span class="badge" title="${esc(r.note || "")}">Out of stock</span>`;
    }
    return `<tr>
      <td><div class="book">${r.image_url ? `<img class="cover" loading="lazy" src="${esc(r.image_url)}" alt="">` : `<div class="cover"></div>`}
        <div style="min-width:0"><div class="book-t" title="${esc(r.title)}">${esc(r.title)}</div><div class="book-a">${esc(r.author || "")}</div></div></div></td>
      <td class="mono">${esc(r.isbn || "—")}</td>
      <td>${esc(r.format || "—")}</td>
      <td>${offers(r)}</td>
      <td class="num">${totalQty(r)}</td>
      <td>${status}</td>
      <td class="clip muted">${esc(r.publisher || "—")}</td>
      <td>${extLink(r.product_url)}</td></tr>`;
  },
  inventory(r) {
    let status = stateBadge(r);
    if (!status) status = r.stock_status === "in_stock" ? `<span class="badge ok">In stock</span>`
      : r.found ? `<span class="badge" title="${esc(r.note || "")}">Out of stock</span>` : `<span class="badge warn" title="Not listed on AwesomeBooks">Out · not listed</span>`;
    const qty = r.state === "done" ? `<span class="qty${r.quantity ? "" : " zero"}">${r.quantity === null ? "—" : fmt.int(r.quantity)}</span>` : `<span class="qty zero">—</span>`;
    return `<tr>
      <td class="mono faint">${r.row_no ?? ""}</td>
      <td class="mono">${esc(r.raw_sku)}</td>
      <td class="mono">${esc(r.isbn || "—")}</td>
      <td class="clip" title="${esc(r.title || r.note || r.error || "")}">${esc(r.title || (r.state === "done" ? r.note : "") || "—")}</td>
      <td>${status}</td>
      <td class="num">${qty}</td>
      <td>${offers(r)}</td>
      <td class="mono muted">${r.checked_at ? fmt.clock(r.checked_at) : "—"}</td>
      <td>${extLink(r.product_url)}</td></tr>`;
  },
};

/* ============================================================
   JobView — one per module
   ============================================================ */
class JobView {
  constructor(kind) {
    this.kind = kind;
    this.root = $(`[data-view="${kind}"]`);
    this.jobId = local.get(`abp-${kind}-job`);
    this.progress = null;
    this.table = { offset: 0, limit: 25, q: "", status: "" };
    this.pollTimer = null;
    this.tableTick = 0;
    this.mount();
  }

  el(name) { return $(`[data-el="${name}"]`, this.root); }

  mount() {
    $('[data-slot="run"]', this.root).innerHTML = runMarkup(this.kind);
    $('[data-slot="guard"]', this.root).innerHTML = guardMarkup();
    $('[data-slot="feed"]', this.root).innerHTML = feedMarkup();
    $('[data-slot="table"]', this.root).innerHTML = tableMarkup(this.kind);

    $$("[data-act]", this.root).forEach((b) => b.addEventListener("click", () => this.act(b.dataset.act, b)));
    $$("[data-tab]", this.root).forEach((b) => b.addEventListener("click", () => {
      $$("[data-tab]", this.root).forEach((t) => t.classList.toggle("on", t === b));
      this.table.status = b.dataset.tab; this.table.offset = 0; this.loadRows();
    }));
    let debounce;
    this.el("q").addEventListener("input", (e) => {
      clearTimeout(debounce);
      debounce = setTimeout(() => { this.table.q = e.target.value.trim(); this.table.offset = 0; this.loadRows(); }, 250);
    });
    this.el("limit").addEventListener("change", (e) => { this.table.limit = Number(e.target.value); this.table.offset = 0; this.loadRows(); });
    $$("[data-page]", this.root).forEach((b) => b.addEventListener("click", () => {
      const next = this.table.offset + Number(b.dataset.page) * this.table.limit;
      if (next < 0 || (this.lastTotal !== undefined && next >= this.lastTotal)) return;
      this.table.offset = next; this.loadRows();
    }));
  }

  controls() {
    const pre = this.kind === "catalog" ? "cat" : "inv";
    return { sessions: Number($(`#${pre}Sessions`).textContent), pace: $(`[data-pace="${pre}"] .on`)?.dataset.key || "balanced" };
  }

  setJob(id) {
    this.jobId = id;
    this.table.offset = 0;
    if (id) local.set(`abp-${this.kind}-job`, id); else local.del(`abp-${this.kind}-job`);
    this.refresh(true);
  }

  async refresh(withRows = false) {
    clearTimeout(this.pollTimer);
    if (!this.jobId && !this.looked) {
      this.looked = true; // ask once: storage may have been cleared, or this is another browser
      try {
        const { jobs } = await api("GET", `/api/${this.kind}/jobs`);
        if (jobs.length) { this.setJob(jobs[0].id); return; }
      } catch { /* server unreachable: shown by the engine pill */ }
    }
    if (!this.jobId) { this.render(null); return; }
    try {
      const p = await api("GET", `/api/${this.kind}/jobs/${this.jobId}`);
      this.render(p);
      this.tableTick++;
      if (withRows || !p.running || this.tableTick % 2 === 0) this.loadRows();
      if (p.running) this.pollTimer = setTimeout(() => this.refresh(), 1500);
    } catch (err) {
      if (/not found/i.test(err.message)) { this.looked = false; this.setJob(null); return; }
      this.pollTimer = setTimeout(() => this.refresh(), 4000);
    }
  }

  render(p) {
    this.progress = p;
    const empty = !p;
    this.el("empty").classList.toggle("hidden", !empty);
    this.el("body").classList.toggle("hidden", empty);
    const statusEl = this.el("status");
    if (empty) {
      statusEl.className = "run-status";
      statusEl.innerHTML = `<span class="status-dot"></span><span>Idle</span>`;
      this.renderGuard(null);
      this.el("feed").innerHTML = `<li class="feed-empty">Events from the running job appear here.</li>`;
      this.el("rows").innerHTML = `<tr><td colspan="9" class="table-empty">No data yet.</td></tr>`;
      this.el("range").textContent = "—";
      this.el("feedHint").textContent = "";
      $$("[data-n]", this.root).forEach((n) => (n.textContent = "0"));
      if (this.onRender) this.onRender(null);
      return;
    }
    const job = p.job;
    const status = p.running && ["queued", "paused", "ready"].includes(p.status) ? "running" : p.status;
    statusEl.className = `run-status ${status}`;
    statusEl.innerHTML = `<span class="status-dot ${p.running ? "live" : ""}"></span><span>${STATUS_LABEL[status] || status}</span>`;

    const scanning = this.kind === "catalog" && !job.discovery_done;
    const pct = scanning ? 0 : p.percent;
    this.el("ring").style.strokeDashoffset = String(326.73 * (1 - Math.min(100, pct) / 100));
    this.el("pct").innerHTML = `${pct >= 100 ? 100 : pct.toFixed(pct >= 10 ? 0 : 1)}<small>%</small>`;
    this.el("ringSub").textContent = `${fmt.int(p.processed)} / ${fmt.int(p.total)}`;

    if (this.kind === "catalog") {
      this.el("name").textContent = job.section;
      this.el("name").title = job.url;
      const listed = job.listed_total ? ` of ${fmt.int(job.listed_total)} listed` : "";
      this.el("count").textContent = scanning
        ? `Scanning page ${fmt.int(job.pages_scanned + 1)} · ${fmt.int(p.total)} books found${listed} · ${fmt.dur(p.elapsed)}`
        : `${fmt.int(p.total)} books · ${fmt.int(p.stock.units)} units in stock · ${fmt.dur(p.elapsed)} elapsed`;
      this.setPhase(scanning ? 0 : status === "done" ? 3 : 1);
      this.stat("inStock", fmt.int(p.stock.in_stock));
      this.stat("withUsed", fmt.int(p.stock.used));
      this.stat("outStock", fmt.int(p.stock.out_of_stock));
    } else {
      this.el("name").textContent = job.filename;
      this.el("count").textContent = `${fmt.int(job.valid_rows)} ISBNs · ${fmt.int(p.stock.units)} units in stock · ${fmt.dur(p.elapsed)} elapsed`;
      this.setPhase(status === "ready" ? 1 : status === "done" ? 3 : 1, status === "ready");
      this.stat("inStock", fmt.int(p.stock.in_stock));
      this.stat("outStock", fmt.int(p.stock.out_of_stock));
      this.stat("notListed", fmt.int(p.stock.not_found));
    }
    this.stat("rate", p.running ? fmt.int(Math.round(p.rate_per_min)) : "—");
    this.stat("eta", p.running ? (p.eta ? fmt.dur(p.eta) : "…") : status === "done" ? "Done" : "—");
    this.stat("errors", fmt.int(p.counts.error || 0));

    const workLeft = (p.counts.pending || 0) + (p.counts.processing || 0) > 0 || scanning;
    const resumable = !p.running && (["paused", "failed", "queued", "interrupted"].includes(status) || (status === "done" && workLeft));
    this.btn("stop").classList.toggle("hidden", !p.running || status === "done");
    this.btn("resume").classList.toggle("hidden", !resumable);
    this.btn("retry").classList.toggle("hidden", p.running || !(p.counts.error > 0));
    this.btn("export").disabled = p.total === 0 || (this.kind === "inventory" && p.processed === 0);
    const saved = status === "done" && job.export_path;
    this.el("saved").classList.toggle("hidden", !saved);
    this.el("exportLabel").textContent = saved ? "Download again" : status === "done" ? "Download Excel" : "Export so far";
    if (saved) this.el("savedPath").textContent = job.export_path;
    this.btn("resume").disabled = !!App.active && !p.running;
    this.btn("retry").disabled = !!App.active && !p.running;
    this.btn("stop").disabled = status === "stopping";

    this.renderGuard(p);
    this.renderFeed(p.events || []);
    this.renderTabs(p);
    if (this.onRender) this.onRender(p);
  }

  btn(name) { return $(`[data-act="${name}"]`, this.root); }
  stat(key, value) { const el = $(`[data-stat="${key}"]`, this.root); if (el) el.textContent = value; }

  setPhase(active, waiting = false) {
    $$("[data-phase]", this.root).forEach((el) => {
      const i = Number(el.dataset.phase);
      el.className = "phase" + (i < active ? " done" : i === active && !waiting && active < 3 ? " now" : "");
      if (waiting && i === 0) el.className = "phase done";
    });
  }

  renderGuard(p) {
    const state = this.el("guardState");
    const live = p && p.live;
    const keys = ["requests", "identities", "warnings", "network_errors", "locale_switches"];
    if (!live) {
      const t = p && p.traffic;
      state.className = "guard-state idle";
      state.innerHTML = t && t.requests
        ? `${icon("shield", "lg")}<div><b>Last run report</b><span>${fmt.int(t.requests)} requests · ${t.warnings ? `${fmt.int(t.warnings)} slow-down signal${t.warnings > 1 ? "s" : ""} handled` : "never throttled or blocked"}</span></div>`
        : `${icon("shield", "lg")}<div><b>Standing by</b><span>Protection activates when a job starts.</span></div>`;
      this.el("paceVal").textContent = "—";
      this.el("paceBar").style.width = "0%";
      keys.forEach((k) => ($(`[data-g="${k}"]`, this.root).textContent = fmt.int((t && t[k]) || 0)));
      return;
    }
    const L = live.limiter, T = live.traffic;
    if (T.offline_for > 0) {
      state.className = "guard-state cool";
      state.innerHTML = `${icon("wifi-off", "lg")}<div><b>Waiting for internet · ${fmt.dur(T.offline_for)}</b><span>The connection dropped. Nothing is marked failed; the run continues by itself once it's back.</span></div>`;
    } else if (L.cooldown_remaining > 0) {
      state.className = "guard-state cool";
      state.innerHTML = `${icon("shield-alert", "lg")}<div><b>Cooling down · ${fmt.dur(L.cooldown_remaining)}</b><span>The site asked us to slow down. Every session pauses, then resumes at a gentler pace.</span></div>`;
    } else {
      state.className = "guard-state";
      state.innerHTML = `${icon("shield", "lg")}<div><b>All clear</b><span>${live.sessions} shopper${live.sessions > 1 ? "s" : ""} · real browser fingerprints · adaptive pace</span></div>`;
    }
    const perMin = 60 / L.current_interval;
    this.el("paceVal").textContent = `${perMin.toFixed(0)} req/min${L.current_interval > L.base_interval ? " (eased)" : ""}`;
    this.el("paceBar").style.width = `${Math.max(6, Math.min(100, (L.base_interval / L.current_interval) * 100))}%`;
    keys.forEach((k) => ($(`[data-g="${k}"]`, this.root).textContent = fmt.int(T[k] || 0)));
  }

  renderFeed(events) {
    const feed = this.el("feed");
    if (!events.length) { feed.innerHTML = `<li class="feed-empty">No events yet.</li>`; return; }
    const icons = { info: "info", success: "check", warn: "alert", error: "x" };
    feed.innerHTML = events.map((e) => `<li class="${e.level}"><span class="ic">${icon(icons[e.level] || "info")}</span><span class="msg">${esc(e.message)}</span><time title="${fmt.date(e.ts)}">${fmt.clock(e.ts)}</time></li>`).join("");
    this.el("feedHint").textContent = `${events.length} latest`;
  }

  renderTabs(p) {
    const n = (k, v) => { const el = $(`[data-n="${k}"]`, this.root); if (el) el.textContent = fmt.int(v); };
    n("all", p.total + ((p.stock && p.stock.invalid) || 0));
    n("in_stock", p.stock.in_stock);
    n("out_of_stock", p.stock.out_of_stock);
    n("used", p.stock.used);
    n("error", p.counts.error || 0);
    if (this.kind === "catalog") n("pending", (p.counts.pending || 0) + (p.counts.processing || 0));
    else { n("not_found", p.stock.not_found); n("invalid", p.stock.invalid); }
  }

  async loadRows() {
    if (!this.jobId) return;
    const t = this.table;
    const qs = new URLSearchParams({ offset: t.offset, limit: t.limit, q: t.q, status: t.status });
    try {
      const data = await api("GET", `/api/${this.kind}/jobs/${this.jobId}/rows?${qs}`);
      this.lastTotal = data.total;
      this.el("rows").innerHTML = data.rows.length
        ? data.rows.map(ROWS[this.kind]).join("")
        : `<tr><td colspan="9" class="table-empty">${t.q || t.status ? "Nothing matches this filter." : "Rows appear here as soon as they're found."}</td></tr>`;
      const from = data.total ? t.offset + 1 : 0, to = Math.min(t.offset + t.limit, data.total);
      this.el("range").textContent = `${fmt.int(from)}–${fmt.int(to)} of ${fmt.int(data.total)}`;
    } catch { /* transient; next poll retries */ }
  }

  async act(name, button) {
    const id = this.jobId;
    if (!id) return;
    try {
      if (name === "stop") {
        button.disabled = true;
        await api("POST", "/api/stop");
        toast("Stopping… in-flight requests finish first, nothing is lost.", "info");
        setTimeout(() => { button.disabled = false; }, 1500);
      } else if (name === "resume") {
        await api("POST", `/api/${this.kind}/jobs/${id}/start`, this.controls());
        toast("Resumed where it stopped.", "ok");
      } else if (name === "retry") {
        await api("POST", `/api/${this.kind}/jobs/${id}/start`, { ...this.controls(), retry_failed: true });
        toast("Retrying failed items.", "ok");
      } else if (name === "export") {
        window.location.href = `/api/${this.kind}/jobs/${id}/export`;
        toast("Preparing your Excel file…", "ok");
        return;
      } else if (name === "reveal") {
        await api("POST", `/api/${this.kind}/jobs/${id}/reveal`);
        return;
      }
      App.pollStatus();
      this.refresh(true);
    } catch (err) { toast(err.message, "bad"); }
  }
}

/* ============================================================
   App wiring
   ============================================================ */
const App = { config: null, active: null, views: {}, statusTimer: null };

function setupPace(prefix) {
  const seg = $(`[data-pace="${prefix}"]`);
  const hint = $(`[data-pace-hint="${prefix}"]`);
  const saved = local.get(`abp-${prefix}-pace`, App.config.default_pace);
  seg.innerHTML = App.config.paces.map((p) => `<button type="button" data-key="${p.key}" class="${p.key === saved ? "on" : ""}">${p.label}</button>`).join("");
  if (!$(".on", seg)) $(`[data-key="${App.config.default_pace}"]`, seg)?.classList.add("on");
  const describe = () => { const p = App.config.paces.find((x) => x.key === $(".on", seg)?.dataset.key); hint.textContent = p ? p.description : ""; };
  seg.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    $$("button", seg).forEach((x) => x.classList.toggle("on", x === b));
    local.set(`abp-${prefix}-pace`, b.dataset.key); describe();
  });
  describe();
}

function setupStepper(prefix) {
  const out = $(`#${prefix}Sessions`);
  out.textContent = Math.min(local.get(`abp-${prefix}-sessions`, 3), App.config.max_sessions);
  $(`[data-stepper="${prefix}"]`).addEventListener("click", (e) => {
    const b = e.target.closest("[data-step]"); if (!b) return;
    const v = Math.max(1, Math.min(App.config.max_sessions, Number(out.textContent) + Number(b.dataset.step)));
    out.textContent = v; local.set(`abp-${prefix}-sessions`, v);
  });
}

/* ---------- Catalog form ---------- */
function setupCatalog(view) {
  const url = $("#catUrl");
  url.value = local.get("abp-cat-url", "");
  $("#catChips").innerHTML = App.config.quick_sections.map((c) => `<button class="chip" data-url="${esc(c.url)}">${esc(c.label)}</button>`).join("");
  $("#catChips").addEventListener("click", (e) => { const c = e.target.closest("[data-url]"); if (c) { url.value = c.dataset.url; url.focus(); hideErr(); } });
  $("#catDetails").checked = local.get("abp-cat-details", true);
  $("#catDetails").addEventListener("change", (e) => local.set("abp-cat-details", e.target.checked));
  $("#catLimit").value = local.get("abp-cat-limit", "") || "";
  $("#catLimit").addEventListener("change", (e) => local.set("abp-cat-limit", e.target.value));
  $("#catPaste").addEventListener("click", async () => {
    try { url.value = (await navigator.clipboard.readText()).trim(); hideErr(); } catch { toast("Clipboard access was blocked. Press Ctrl+V in the field instead.", "info"); url.focus(); }
  });
  const err = $("#catUrlError");
  const hideErr = () => err.classList.add("hidden");
  url.addEventListener("input", hideErr);
  url.addEventListener("keydown", (e) => { if (e.key === "Enter") $("#catStart").click(); });

  $("#catStart").addEventListener("click", async () => {
    const value = url.value.trim();
    if (!value) { err.innerHTML = `${icon("alert", "sm")}Paste an AwesomeBooks section link first.`; err.classList.remove("hidden"); url.focus(); return; }
    if (!(await confirmReplace(view))) return;
    const btn = $("#catStart"); btn.disabled = true;
    try {
      const res = await api("POST", "/api/catalog/jobs", {
        url: value, ...view.controls(), with_details: $("#catDetails").checked, max_products: $("#catLimit").value.trim() || null,
      });
      local.set("abp-cat-url", value);
      view.setJob(res.job.id);
      toast("Extraction started. You can close this tab; it keeps running.", "ok");
      App.pollStatus();
    } catch (e) {
      err.innerHTML = `${icon("alert", "sm")}${esc(e.message)}`; err.classList.remove("hidden");
    } finally { btn.disabled = false; applyBusy(); }
  });
  $("#catTransport").textContent = App.config.transport === "curl_cffi" ? "Browser-grade TLS · adaptive pacing" : "Adaptive pacing";
}

/* ---------- Inventory form ---------- */
function setupInventory(view) {
  const drop = $("#invDrop"), input = $("#invFile");
  const pick = () => input.click();
  drop.addEventListener("click", pick);
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
  $("#invReplace").addEventListener("click", pick);
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) upload(f); });
  input.addEventListener("change", () => { if (input.files[0]) upload(input.files[0]); input.value = ""; });

  async function upload(file) {
    if (!(await confirmReplace(view))) return;
    const fd = new FormData(); fd.append("file", file);
    drop.querySelector("b").textContent = `Reading ${file.name}…`;
    try {
      const res = await api("POST", "/api/inventory/uploads", fd);
      view.setJob(res.job.id);
      toast(`${fmt.int(res.upload.valid)} ISBNs ready from column "${res.upload.column}".`, "ok");
    } catch (e) { toast(e.message, "bad"); }
    finally { drop.querySelector("b").innerHTML = `Drop your file here or <span class="link">browse</span>`; }
  }

  view.onRender = (p) => {
    const job = p && p.job;
    $("#invDrop").classList.toggle("hidden", !!job);
    $("#invLoaded").classList.toggle("hidden", !job);
    const start = $("#invStart");
    if (!job) { start.disabled = true; start.innerHTML = `${icon("play", "sm")}Start stock check`; return; }
    $("#invFileName").textContent = job.filename;
    $("#invFileSub").textContent = `Uploaded ${fmt.ago(job.created_at)} · ${fmt.int(job.total)} rows`;
    $("#invSummary").innerHTML = [
      ["ISBNs", job.valid_rows], ["Unique", job.valid_rows - job.duplicate_rows], ["Duplicates", job.duplicate_rows], ["Invalid", job.invalid_rows],
    ].map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${fmt.int(v)}</div></div>`).join("");
    const ready = job.status === "ready";
    start.disabled = !ready || !!App.active;
    start.innerHTML = ready ? `${icon("play", "sm")}Start stock check` : p.running ? `${icon("zap", "sm")}Checking…` : `${icon("check", "sm")}${job.status === "done" ? "Completed" : "Started"}. Upload a new file to run again`;
  };

  $("#invStart").addEventListener("click", async () => {
    if (!view.jobId) return;
    const btn = $("#invStart"); btn.disabled = true;
    try {
      await api("POST", `/api/inventory/jobs/${view.jobId}/start`, view.controls());
      toast("Stock check started. You can close this tab; it keeps running.", "ok");
      App.pollStatus();
      view.refresh(true);
    } catch (e) { toast(e.message, "bad"); btn.disabled = false; }
  });
}

/* ---------- replacing a run ---------- */
async function confirmReplace(view) {
  const p = view.progress;
  if (!p || !p.job) return true;
  const unfinished = !["done", "ready"].includes(p.status) || (p.status === "done" && !p.job.export_path);
  if (!unfinished) return true;
  const left = fmt.int((p.counts.pending || 0) + (p.counts.processing || 0));
  return confirmDialog(
    "Replace the unfinished run?",
    `The current run isn't finished (${left} item(s) left) and its data will be cleared. Press Resume instead to finish it.`,
    "Start new run",
  );
}

/* ---------- global status ---------- */
function applyBusy() {
  const a = App.active;
  $$("[data-nav]").forEach((n) => n.classList.toggle("busy", !!a && a.kind === n.dataset.nav));
  Object.values(App.views).forEach((v) => {
    const note = $("[data-busy-note]", v.root);
    const blocked = a && a.kind !== v.kind;
    note.classList.toggle("hidden", !blocked);
    if (blocked) $("span", note).textContent = a.kind === "catalog" ? "A catalog extraction is running. One job at a time." : "A stock check is running. One job at a time.";
  });
  $("#catStart").disabled = !!a;
  const inv = App.views.inventory;
  if (inv.progress) inv.onRender(inv.progress);
}

App.pollStatus = async function () {
  clearTimeout(App.statusTimer);
  try {
    const { active } = await api("GET", "/api/status");
    const wasActive = App.active;
    App.active = active;
    const dot = $("#engineDot"), title = $("#engineTitle"), state = $("#engineState"), bar = $("#engineBar");
    if (active) {
      dot.className = "status-dot live";
      title.textContent = active.kind === "catalog" ? "Extracting catalog" : "Checking stock";
      state.textContent = `${fmt.int(active.processed)} of ${fmt.int(active.total)} · ${active.eta ? fmt.dur(active.eta) + " left" : "estimating…"}`;
      bar.style.width = `${active.percent}%`;
      const view = App.views[active.kind];
      if (view.jobId !== active.job.id) view.setJob(active.job.id);
      else if (!view.pollTimer || !view.progress?.running) view.refresh();
    } else {
      dot.className = "status-dot";
      title.textContent = "Engine idle";
      state.textContent = "Ready · one job at a time";
      bar.style.width = "0%";
      if (wasActive) App.views[wasActive.kind].refresh(true);
    }
    applyBusy();
  } catch { $("#engineDot").className = "status-dot warn"; $("#engineTitle").textContent = "Server unreachable"; $("#engineState").textContent = "Is the scraper window open?"; }
  App.statusTimer = setTimeout(App.pollStatus, App.active ? 2000 : 4000);
};

/* ---------- routing & theme ---------- */
const currentRoute = () => (location.hash.includes("inventory") ? "inventory" : "catalog");

function moveGlider() {
  const active = $(".switcher a.active"), glider = $(".switcher-glider");
  if (!active || !glider) return;
  glider.style.width = `${active.offsetWidth}px`;
  glider.style.transform = `translateX(${active.offsetLeft - 4}px)`;
}

function route() {
  const r = currentRoute();
  $$("[data-view]").forEach((v) => v.classList.toggle("hidden", v.dataset.view !== r));
  $$("[data-nav]").forEach((n) => n.classList.toggle("active", n.dataset.nav === r));
  document.title = `${r === "catalog" ? "Catalog Extractor" : "Stock Monitor"} · AwesomeBooks Scraper Pro`;
  moveGlider();
  App.views[r].refresh(true);
}

function setupTheme() {
  const btn = $("#themeBtn");
  const paint = () => {
    const light = document.documentElement.dataset.theme === "light";
    btn.innerHTML = icon(light ? "moon" : "sun", "sm");
  };
  btn.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("abp-theme", next); } catch { /* ignore */ }
    paint();
  });
  paint();
}

async function boot() {
  setupTheme();
  try { App.config = await api("GET", "/api/config"); }
  catch (e) { toast(e.message, "bad"); App.config = { paces: [{ key: "balanced", label: "Balanced", description: "" }], default_pace: "balanced", max_sessions: 6, quick_sections: [], transport: "", ship_to: "US", currency: "USD" }; }
  $("#version").textContent = `v${App.config.version || "1.0.0"}`;
  $("#localeText").textContent = `Ship to ${App.config.ship_to || "US"} · ${App.config.currency || "USD"}`;
  ["cat", "inv"].forEach((p) => { setupPace(p); setupStepper(p); });
  App.views.catalog = new JobView("catalog");
  App.views.inventory = new JobView("inventory");
  setupCatalog(App.views.catalog);
  setupInventory(App.views.inventory);
  window.addEventListener("hashchange", route);
  window.addEventListener("resize", moveGlider);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(moveGlider);
  if (!location.hash) history.replaceState(null, "", "#/catalog");
  route();
  App.views[currentRoute() === "catalog" ? "inventory" : "catalog"].refresh(false);
  App.pollStatus();
}

document.addEventListener("DOMContentLoaded", boot);
