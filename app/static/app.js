"use strict";

const charts = {};
const GRID = "#262b34";
const TICK = "#9aa4b2";
Chart.defaults.color = TICK;
Chart.defaults.borderColor = GRID;
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";

function fmt(n, digits = 0) {
  if (n === null || n === undefined) return "–";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });
}

function destroy(id) {
  if (charts[id]) { charts[id].destroy(); delete charts[id]; }
}

function makeChart(id, config) {
  destroy(id);
  const ctx = document.getElementById(id);
  charts[id] = new Chart(ctx, config);
}

function kpiCard(label, value, sub) {
  return `<div class="kpi"><div class="label">${label}</div>
    <div class="value">${value}</div><div class="sub">${sub || ""}</div></div>`;
}

function renderKpis(d) {
  const drv = d.driving, chg = d.charging, eff = d.efficiency, cur = d.currency;
  const cards = [];
  if (drv.available) {
    cards.push(kpiCard("Distance", fmt(drv.total_distance_km) + " km",
      `${fmt(drv.total_drives)} drives · ${fmt(drv.total_duration_h)} h`));
    cards.push(kpiCard("Avg Efficiency", fmt(eff.avg_efficiency_wh_per_km) + " Wh/km",
      `${eff.vs_rated_pct >= 0 ? "+" : ""}${fmt(eff.vs_rated_pct, 1)}% vs rated`));
    cards.push(kpiCard("Avg Speed", fmt(drv.avg_speed_kmh) + " km/h",
      `peak ${fmt(drv.p95_speed_kmh)} km/h (p95)`));
  }
  if (chg.available) {
    cards.push(kpiCard("Energy Charged", fmt(chg.total_energy_kwh) + " kWh",
      `${fmt(chg.total_sessions)} sessions`));
    cards.push(kpiCard("Charging Cost", cur + " " + fmt(chg.total_cost),
      `${cur} ${fmt(chg.avg_cost_per_kwh, 2)}/kWh avg`));
    cards.push(kpiCard("DC Fast Charging", fmt(chg.dc_energy_share_pct, 0) + "%",
      `of energy · ${fmt(chg.full_charge_share_pct, 0)}% to 100%`));
  }
  document.getElementById("kpis").innerHTML = cards.join("");
}

function barConfig(labels, data, label, color) {
  return {
    type: "bar",
    data: { labels, datasets: [{ label, data, backgroundColor: color, borderRadius: 4 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { grid: { display: false } }, y: { beginAtZero: true } },
    },
  };
}

function renderCharts(d) {
  const eff = d.efficiency, drv = d.driving, chg = d.charging;

  if (eff.available) {
    const t = eff.efficiency_by_temp;
    makeChart("effTempChart", barConfig(Object.keys(t).map(k => k + "°C"),
      Object.values(t), "Wh/km", "#3b82f6"));

    const w = eff.weekly_efficiency;
    makeChart("effTrendChart", {
      type: "line",
      data: { labels: Object.keys(w), datasets: [{
        label: "Wh/km", data: Object.values(w), borderColor: "#e82127",
        backgroundColor: "rgba(232,33,39,.1)", fill: true, tension: .3, pointRadius: 2 }] },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 8 } } } },
    });
  }

  if (drv.available) {
    const sb = drv.distance_by_speed_band;
    makeChart("speedBandChart", barConfig(Object.keys(sb), Object.values(sb),
      "km", "#22c55e"));

    const th = drv.trips_by_hour;
    makeChart("tripsHourChart", barConfig(Object.keys(th).map(h => h + "h"),
      Object.values(th), "trips", "#f59e0b"));
  }

  if (chg.available) {
    makeChart("acdcChart", {
      type: "doughnut",
      data: { labels: ["AC (home/dest)", "DC (fast)"],
        datasets: [{ data: [chg.ac_energy_kwh, chg.dc_energy_kwh],
          backgroundColor: ["#22c55e", "#e82127"] }] },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: "bottom" } } },
    });

    const st = chg.end_soc_targets;
    makeChart("socTargetChart", barConfig(Object.keys(st).map(s => s + "%"),
      Object.values(st), "sessions", "#3b82f6"));
  }
}

function renderLists(d) {
  const routes = (d.driving.top_routes || [])
    .map(([r, c]) => `<li><span>${r}</span><span class="count">${c}×</span></li>`).join("");
  document.getElementById("topRoutes").innerHTML = routes || "<li>No data</li>";

  const locs = (d.charging.top_locations || [])
    .map(([l, c]) => `<li><span>${l}</span><span class="count">${c}×</span></li>`).join("");
  document.getElementById("topLocations").innerHTML = locs || "<li>No data</li>";
}

function renderRecommendations(recs) {
  const html = recs.map(r => `
    <div class="rec ${r.priority}">
      <span class="pri">${r.priority}</span>
      <div class="body">
        <span class="cat">${r.category}</span>
        <h3>${r.title}</h3>
        <p>${r.detail}</p>
        ${r.estimated_saving ? `<div class="saving">⤳ ${r.estimated_saving}</div>` : ""}
      </div>
    </div>`).join("");
  document.getElementById("recommendations").innerHTML = html;
}

// In static mode (e.g. GitHub Pages) the dashboard reads pre-built JSON
// snapshots instead of calling the live API. window.SUMMARY_URL is set by the
// static index.html; otherwise we fall back to the live API endpoint.
const STATIC_MODE = typeof window.SUMMARY_URL === "function";
const summaryUrl = STATIC_MODE
  ? window.SUMMARY_URL
  : (days) => `/api/summary?days=${days}`;

async function load() {
  const days = document.getElementById("range").value;
  document.getElementById("kpis").innerHTML = '<div class="loading">Loading…</div>';
  try {
    const res = await fetch(summaryUrl(days));
    if (!res.ok) throw new Error(await res.text());
    const d = await res.json();

    const badge = document.getElementById("mode-badge");
    if (STATIC_MODE) {
      badge.textContent = "demo";
      badge.className = "badge demo";
    } else {
      const health = await (await fetch("/api/health")).json();
      badge.textContent = health.mode;
      badge.className = "badge " + health.mode;
    }

    document.getElementById("subtitle").textContent =
      `${d.vehicle.name} · ${d.vehicle.model} ${d.vehicle.trim}`;

    renderKpis(d);
    renderCharts(d);
    renderLists(d);
    renderRecommendations(d.recommendations);

    document.getElementById("footer-meta").textContent =
      `Generated ${d.generated_at} · ${d.window_days}-day window · Tesla Analyzer v0.1`;
  } catch (e) {
    document.getElementById("kpis").innerHTML =
      `<div class="loading">Could not load data: ${e.message}</div>`;
  }
}

document.getElementById("range").addEventListener("change", load);

/* ------------------------------------------------------------------ */
/* Data-source buttons: import file + link account                     */
/* ------------------------------------------------------------------ */

function openModal(id) { document.getElementById(id).classList.remove("hidden"); }
function closeModal(id) { document.getElementById(id).classList.add("hidden"); }

// Generic close handlers (× button and backdrop click).
document.querySelectorAll(".modal").forEach((m) => {
  m.addEventListener("click", (e) => {
    if (e.target === m || e.target.hasAttribute("data-close")) m.classList.add("hidden");
  });
});

const staticNote =
  "This is the static demo dashboard. Loading your own data or linking a Tesla " +
  "account needs the self-hosted app — see the README to run it locally.";

function setStatus(el, msg, kind) {
  el.textContent = msg;
  el.className = "status" + (kind ? " " + kind : "");
}

// --- Button 1: import ---
const fileInput = document.getElementById("file-input");
const dropzone = document.getElementById("dropzone");
const importSubmit = document.getElementById("import-submit");
const importStatus = document.getElementById("import-status");
let pendingFile = null;

document.getElementById("btn-import").addEventListener("click", () => {
  openModal("import-modal");
  if (STATIC_MODE) setStatus(importStatus, staticNote, "warn");
});

dropzone.addEventListener("click", () => fileInput.click());
["dragover", "dragenter"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); }));
["dragleave", "drop"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); }));
dropzone.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) selectFile(e.dataTransfer.files[0]); });
fileInput.addEventListener("change", () => { if (fileInput.files[0]) selectFile(fileInput.files[0]); });

function selectFile(file) {
  pendingFile = file;
  document.getElementById("file-name").textContent = file.name;
  importSubmit.disabled = STATIC_MODE;
  if (!STATIC_MODE) setStatus(importStatus, "", "");
}

importSubmit.addEventListener("click", async () => {
  if (!pendingFile || STATIC_MODE) return;
  setStatus(importStatus, "Importing…", "");
  importSubmit.disabled = true;
  try {
    const fd = new FormData();
    fd.append("file", pendingFile);
    const res = await fetch("/api/import", { method: "POST", body: fd });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "Import failed");
    setStatus(importStatus,
      `Imported ${body.imported_drives} drives & ${body.imported_charges} charges.`, "ok");
    setTimeout(() => { closeModal("import-modal"); load(); }, 800);
  } catch (e) {
    setStatus(importStatus, e.message, "err");
    importSubmit.disabled = false;
  }
});

// --- Button 2: link account ---
document.getElementById("btn-link").addEventListener("click", async () => {
  openModal("link-modal");
  if (STATIC_MODE) {
    setStatus(document.getElementById("link-status"), staticNote, "warn");
    document.getElementById("oauth-unavailable").classList.remove("hidden");
    const oauthBtn = document.getElementById("oauth-btn");
    oauthBtn.classList.add("disabled");
    oauthBtn.removeAttribute("href");
    return;
  }
  // Reflect server OAuth availability.
  try {
    const h = await (await fetch("/api/health")).json();
    const unavailable = document.getElementById("oauth-unavailable");
    const oauthBtn = document.getElementById("oauth-btn");
    if (!h.oauth_available) {
      unavailable.classList.remove("hidden");
      oauthBtn.classList.add("disabled");
      oauthBtn.removeAttribute("href");
    }
  } catch (_) { /* ignore */ }
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    document.querySelectorAll(".tab-panel").forEach((p) =>
      p.classList.toggle("hidden", p.dataset.panel !== tab.dataset.tab));
  });
});

document.getElementById("link-submit").addEventListener("click", async () => {
  if (STATIC_MODE) return;
  const status = document.getElementById("link-status");
  const token = document.getElementById("token-input").value.trim();
  if (!token) { setStatus(status, "Please paste an access token.", "err"); return; }
  setStatus(status, "Linking…", "");
  try {
    const res = await fetch("/api/link/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        access_token: token,
        refresh_token: document.getElementById("refresh-input").value.trim(),
        base_url: document.getElementById("baseurl-input").value,
      }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "Linking failed");
    const names = (body.vehicles || []).map((v) => v.name || v.vin).join(", ");
    setStatus(status, `Linked: ${names || "account"}.`, "ok");
    setTimeout(() => { closeModal("link-modal"); load(); }, 900);
  } catch (e) {
    setStatus(status, e.message, "err");
  }
});

load();
