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

function kpiCard(label, value, sub, tone) {
  return `<div class="kpi${tone ? " tone-" + tone : ""}"><div class="label">${label}</div>
    <div class="value">${value}</div><div class="sub">${sub || ""}</div></div>`;
}

function renderKpis(d) {
  const drv = d.driving, chg = d.charging, eff = d.efficiency, cur = d.currency;
  const cards = [];
  const lt = d.live_trip;
  if (lt) {
    // A drive is in progress — show its live numbers first.
    cards.push(kpiCard("Current Drive", fmt(lt.distance_km, 1) + " km",
      `started ${tripWhen(lt.start_time)} · in progress`, "blue"));
    cards.push(kpiCard("Drive Time", fmt(lt.duration_min) + " min",
      `avg ${fmt(lt.avg_speed_kmh)} km/h · max ${fmt(lt.max_speed_kmh)}`, "amber"));
    cards.push(kpiCard("Battery Used", fmt(lt.soc_used, 1) + "%",
      `${fmt(lt.start_soc)}% → ${fmt(lt.soc)}%`, "green"));
    if (lt.km_per_soc) {
      cards.push(kpiCard("km / 1% Battery", fmt(lt.km_per_soc, 1) + " km",
        "this drive", "teal"));
    }
  }
  if (drv.available) {
    cards.push(kpiCard("Distance", fmt(drv.total_distance_km) + " km",
      `${fmt(drv.total_drives)} drives · ${fmt(drv.total_duration_h)} h`, "blue"));
    cards.push(kpiCard("Avg Efficiency", fmt(eff.avg_efficiency_wh_per_km) + " Wh/km",
      `${eff.vs_rated_pct >= 0 ? "+" : ""}${fmt(eff.vs_rated_pct, 1)}% vs rated`, "green"));
    cards.push(kpiCard("Avg Speed", fmt(drv.avg_speed_kmh) + " km/h",
      `peak ${fmt(drv.p95_speed_kmh)} km/h (p95)`, "amber"));
    if (drv.km_per_soc_pct) {
      cards.push(kpiCard("km / 1% Battery", fmt(drv.km_per_soc_pct, 1) + " km",
        `${fmt(drv.soc_used_pct)}% battery used`, "teal"));
    }
  }
  if (chg.available) {
    cards.push(kpiCard("Energy Charged", fmt(chg.total_energy_kwh) + " kWh",
      `${fmt(chg.total_sessions)} sessions`, "violet"));
    cards.push(kpiCard("Charging Cost", cur + " " + fmt(chg.total_cost),
      `${cur} ${fmt(chg.avg_cost_per_kwh, 2)}/kWh avg`, "teal"));
    cards.push(kpiCard("DC Fast Charging", fmt(chg.dc_energy_share_pct, 0) + "%",
      `of energy · ${fmt(chg.full_charge_share_pct, 0)}% to 100%`, "red"));
  }
  if (!cards.length) {
    // The window is genuinely empty (e.g. "Since charge" right after charging)
    // — say so instead of leaving a hole where the KPIs were.
    const label = d.window_label === "all data" ? "yet"
      : (d.window_label || `in the last ${d.window_days} day${d.window_days > 1 ? "s" : ""}`);
    document.getElementById("kpis").innerHTML =
      `<div class="kpi kpi-empty"><div class="label">No activity ${label}</div>` +
      `<div class="sub">Your stats appear here after the next synced drive or charge — ` +
      `or pick a longer window (e.g. 7 days) above.</div></div>`;
    return;
  }
  document.getElementById("kpis").innerHTML = cards.join("");
}

function barConfig(labels, data, label, color) {
  return {
    type: "bar",
    data: { labels, datasets: [{ label, data, backgroundColor: color,
      hoverBackgroundColor: color + "cc", borderRadius: 6, maxBarThickness: 44 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, border: { display: false } },
        y: { beginAtZero: true, border: { display: false }, grid: { color: GRID } },
      },
    },
  };
}

// Show/hide a chart's card; destroy the chart when hidden so stale data from a
// previous dataset (e.g. demo) never lingers after importing charge-only data.
function showCard(canvasId, show) {
  const canvas = document.getElementById(canvasId);
  const card = canvas && canvas.closest(".card");
  if (card) card.style.display = show ? "" : "none";
  if (!show) destroy(canvasId);
}

function renderCharts(d) {
  const eff = d.efficiency, drv = d.driving, chg = d.charging;

  showCard("effTempChart", eff.available);
  showCard("effTrendChart", eff.available);
  showCard("speedBandChart", drv.available);
  showCard("tripsHourChart", drv.available);
  showCard("acdcChart", chg.available);

  if (eff.available) {
    const t = eff.efficiency_by_temp;
    makeChart("effTempChart", barConfig(Object.keys(t).map(k => k + "°C"),
      Object.values(t), "Wh/km", "#3b82f6"));

    const w = eff.weekly_efficiency;
    makeChart("effTrendChart", {
      type: "line",
      data: { labels: Object.keys(w), datasets: [{
        label: "Wh/km", data: Object.values(w), borderColor: "#e82127", borderWidth: 2,
        backgroundColor: "rgba(232,33,39,.06)", fill: true, tension: .35,
        pointRadius: 0, pointHitRadius: 12, pointHoverRadius: 4,
        pointBackgroundColor: "#e82127" }] },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { display: false }, border: { display: false }, ticks: { maxTicksLimit: 8 } },
          y: { border: { display: false }, grid: { color: GRID } },
        } },
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
          backgroundColor: ["#22c55e", "#e82127"],
          borderColor: "#171b22", borderWidth: 3, hoverOffset: 6 }] },
      options: { responsive: true, maintainAspectRatio: false, cutout: "62%",
        plugins: { legend: { position: "bottom",
          labels: { usePointStyle: true, boxWidth: 8, boxHeight: 8, padding: 16 } } } },
    });

    const st = chg.end_soc_targets;
    const hasSoc = Object.keys(st).some((k) => +k > 0); // exports without SoC -> all "0"
    showCard("socTargetChart", hasSoc);
    if (hasSoc) {
      makeChart("socTargetChart", barConfig(Object.keys(st).map(s => s + "%"),
        Object.values(st), "sessions", "#3b82f6"));
    }
  } else {
    showCard("socTargetChart", false);
  }
}

// "2026-07-03T21:15" (wall time) or "...Z" (UTC) -> "03 Jul 21:15" in MYT.
const TRIP_MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const tripTimeFmt = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kuala_Lumpur", day: "2-digit", month: "short",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
function tripWhen(s) {
  if (/Z$|[+-]\d\d:\d\d$/.test(s)) return tripTimeFmt.format(new Date(s)).replace(",", "");
  const m = String(s).match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (!m) return s;
  return `${m[3]} ${TRIP_MONTHS[+m[2] - 1]} ${m[4]}:${m[5]}`;
}
// End timestamp of a trip: just "HH:MM" when it ends the same day it started.
function tripEnd(start, end) {
  const full = tripWhen(end);
  if (String(start).slice(0, 10) === String(end).slice(0, 10)) {
    const m = full.match(/(\d{2}:\d{2})$/);
    if (m) return m[1];
  }
  return full;
}

function renderLists(d) {
  const trips = (d.driving.recent_trips || [])
    .map((t) => {
      const when = t.end_time
        ? `${tripWhen(t.start_time)} → ${tripEnd(t.start_time, t.end_time)}`
        : tripWhen(t.start_time);
      const speed = t.avg_speed_kmh ? ` · avg ${t.avg_speed_kmh} km/h` : "";
      return `<li class="trip"><span class="trip-route">${when}${t.route ? "<br>" + t.route : ""}</span>` +
        `<span class="trip-meta">${t.distance_km} km · ${t.duration_min} min${speed} · ${t.wh_per_km} Wh/km</span></li>`;
    })
    .join("");
  document.getElementById("recentTrips").innerHTML =
    trips || '<li class="empty">No trips in this window</li>';

  const routes = (d.driving.top_routes || [])
    .map(([r, c]) => `<li><span>${r}</span><span class="count">${c}×</span></li>`).join("");
  document.getElementById("topRoutes").innerHTML =
    routes || '<li class="empty">No repeated routes yet</li>';

  const locs = (d.charging.top_locations || [])
    .map(([l, c]) => `<li><span>${l}</span><span class="count">${c}×</span></li>`).join("");
  document.getElementById("topLocations").innerHTML =
    locs || '<li class="empty">No charging sessions in this window</li>';
}

function renderBehaviour(d) {
  const card = document.getElementById("behaviour-card");
  const body = document.getElementById("behaviour-body");
  if (!card || !body) return;
  const b = (d.driving || {}).behaviour;
  if (!b || !b.available) { card.style.display = "none"; return; }
  card.style.display = "";
  const rows = [
    ["Highway >110 km/h", b.speeding_share_pct, b.speeding_penalty_wh],
    ["Stop-and-go", b.stopgo_share_pct, b.stopgo_penalty_wh],
    ["Short trips <3 km", b.short_trip_share_pct, b.short_trip_penalty_wh],
    ["Peak hours", b.peak_hour_share_pct, b.peak_hour_penalty_wh],
    ["Hot weather 33°C+", b.hot_weather_share_pct, b.hot_weather_penalty_wh],
  ].filter(([, share, pen]) => share >= 5 && pen > 0)
   .map(([label, share, pen]) =>
     `<div class="bat-line">${label}: <strong>${share}%</strong> of km · +${pen} Wh/km</div>`)
   .join("");
  const scoreCls = b.score >= 80 ? "" : b.score >= 60 ? " warn" : " bad";
  body.innerHTML = `
    <div class="bat-health${scoreCls}">${b.score}<span style="font-size:20px">/100</span></div>
    <div class="bat-line">Typical driving vs your own best quartile
      (<strong>${Math.round(b.best_quartile_wh_per_km)} Wh/km</strong>)</div>
    ${rows || '<div class="bat-line">No costly habits detected in this window 🎉</div>'}
    ${b.potential_saving_kwh >= 1
      ? `<div class="bat-line">Potential if all drives matched your best: <strong>${b.potential_saving_kwh} kWh</strong></div>`
      : ""}`;
}

function renderBattery(d) {
  const card = document.getElementById("battery-card");
  const body = document.getElementById("battery-body");
  if (!card || !body) return;
  const b = d.battery;
  const chg = d.charging || {};
  if (!b || (!b.available && !(b.n_readings > 0))) { card.style.display = "none"; return; }
  card.style.display = "";
  if (!b.available) {
    body.innerHTML = `<p class="bat-note">${b.note}</p>`;
    return;
  }
  const habits = chg.available
    ? `<div class="bat-line">Charging habits: avg target ${chg.avg_end_soc}% · ` +
      `${chg.full_charge_share_pct}% to 100% · DC ${chg.dc_energy_share_pct}% of energy</div>`
    : "";
  const healthCls = b.health_pct >= 90 ? "" : b.health_pct >= 80 ? " warn" : " bad";
  const ref = b.reference === "factory spec"
    ? `when-new spec ${b.reference_km} km`
    : `best seen ${b.baseline_full_range_km} km`;
  body.innerHTML = `
    <div class="bat-health${healthCls}">${b.health_pct}%
      <button id="batt-info-btn" class="info-btn"
        title="How is the 100% reference chosen?">!</button></div>
    <div id="batt-info" class="bat-info hidden">${battInfoHtml(d)}</div>
    <div class="bat-line">Estimated full range <strong>${b.est_full_range_km} km</strong>
      vs ${ref} (${b.degradation_pct}% degradation)</div>
    <div class="bat-line">Based on ${b.n_readings} readings · avg SoC ${b.avg_soc}% · lowest seen ${b.min_soc_seen}%</div>
    ${habits}`;
  const btn = document.getElementById("batt-info-btn");
  if (btn) btn.addEventListener("click", () =>
    document.getElementById("batt-info").classList.toggle("hidden"));
}

// The "!" popover: which car config the 100% reference was derived from.
function battInfoHtml(d) {
  const b = d.battery, v = d.vehicle || {};
  const realVin = v.vin && !/^(DEMO|IMPORT|LINKED)/.test(v.vin) ? v.vin : null;
  const badge = ((v.trim || "").match(/\b(P?\d+D?)\b/) || [])[1];
  const wheel = (v.trim || "").split(/\s+/).find((t) =>
    /^(nova|photon|gemini|induction|crossflow|uberturbine|apollo|turbine|helix|arachnid|cyberstream)/i.test(t));
  const carLine = [v.year, v.model, badge && `(${badge})`].filter(Boolean).join(" ");
  const rows = [
    realVin ? `VIN: <strong>${realVin}</strong>` : null,
    carLine ? `Car: <strong>${carLine}</strong>` : null,
    `Wheels: <strong>${wheel || "not reported yet — will appear after a sync"}</strong>`,
    b.new_range_km
      ? `When-new 100% range for this config: <strong>${b.new_range_km} km</strong> (EPA)`
      : "When-new range unknown for this variant — using your best readings instead",
    b.reference === "factory spec"
      ? `Health compares recent readings against <strong>${b.reference_km} km</strong>.`
      : `Reference in use: your best readings (<strong>${b.baseline_full_range_km} km</strong>).`,
  ];
  return rows.filter(Boolean).map((r) => `<div>${r}</div>`).join("");
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

// Static/PWA mode computes everything in-browser with no backend (TA.buildSummary);
// the self-hosted app uses the REST API. window.TA_STATIC is set by the static
// index.html (window.SUMMARY_URL kept for backward compatibility).
const STATIC_MODE = window.TA_STATIC === true || typeof window.SUMMARY_URL === "function";
const DEMO_URL = window.DEMO_URL || "data/demo.json";
const STORE_KEY = "ta_dataset";
let demoCache = null;

function importedDataset() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY)); } catch (_) { return null; }
}
async function demoDataset() {
  if (!demoCache) demoCache = await (await fetch(DEMO_URL)).json();
  return demoCache;
}

async function load() {
  const rawRange = document.getElementById("range").value;
  const sinceCharge = rawRange === "charge";
  const currentDrive = rawRange === "drive";
  const days = sinceCharge || currentDrive ? 90 : +rawRange;
  document.getElementById("kpis").innerHTML = '<div class="loading">Loading…</div>';
  try {
    let d, mode;
    if (STATIC_MODE) {
      const ds = importedDataset() || (await demoDataset());
      d = TA.buildSummary(ds, currentDrive ? "drive" : (sinceCharge ? "charge" : days));
      mode = ds.source === "imported" ? "imported" : "demo";
    } else {
      const extra = currentDrive ? "&current_drive=1" : (sinceCharge ? "&since_charge=1" : "");
      const res = await fetch(`/api/summary?days=${days}${extra}`);
      if (!res.ok) throw new Error(await res.text());
      d = await res.json();
      const health = await (await fetch("/api/health")).json();
      mode = health.mode;
      setBuildInfo(health.build);
    }

    const badge = document.getElementById("mode-badge");
    badge.textContent = mode;
    badge.className = "badge " + mode;
    if (STATIC_MODE) updateResetButton();
    // The clear-history action only makes sense against a real server DB.
    const clearBtn = document.getElementById("clear-trips");
    if (clearBtn) clearBtn.classList.toggle("hidden", STATIC_MODE);

    // Live mode: reveal the Sync button and snapshot the car once per visit.
    const syncBtn = document.getElementById("btn-sync");
    if (syncBtn) {
      syncBtn.classList.toggle("hidden", STATIC_MODE || mode !== "live");
      if (!STATIC_MODE && mode === "live" && !window._syncedOnce) {
        window._syncedOnce = true;
        syncNow();
      }
    }

    const v = d.vehicle;
    const realVin = v.vin && !/^(DEMO|IMPORT|LINKED)/.test(v.vin) ? `VIN ${v.vin}` : null;
    // Model year (decoded from the VIN by the server) leads the description.
    document.getElementById("subtitle").textContent =
      [v.name, [v.year, v.model, v.trim].filter(Boolean).join(" "), realVin]
        .filter(Boolean).join(" · ");

    renderKpis(d);
    renderCharts(d);
    renderBehaviour(d);
    renderBattery(d);
    renderLists(d);
    renderRecommendations(d.recommendations);

    const now = new Date();
    const windowText = d.window_label || `${d.window_days}-day window`;
    document.getElementById("footer-meta").textContent =
      `Generated ${footerDateFmt.format(now)} ${hhmm(now)} MYT · ${windowText} · Tesla Analyzer v0.1`;
  } catch (e) {
    document.getElementById("kpis").innerHTML =
      `<div class="loading">Could not load data: ${e.message}</div>`;
  }
}

// Live date/time in the header, fixed to Malaysia time (Asia/Kuala_Lumpur),
// regardless of the device's own timezone. Time is shown as a 4-digit 24-hour
// value (HHMM, e.g. 1530); the year is 4 digits.
const dateFmt = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kuala_Lumpur",
  weekday: "short", day: "2-digit", month: "short", year: "numeric",
});
const footerDateFmt = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kuala_Lumpur", day: "2-digit", month: "short", year: "numeric",
});
const hmFmt = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kuala_Lumpur", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
const hhmm = (d) => hmFmt.format(d).replace(":", ""); // "15:30" -> "1530"
function tickClock() {
  const el = document.getElementById("clock");
  if (el) { const n = new Date(); el.textContent = `${dateFmt.format(n)} ${hhmm(n)} MYT`; }
}

// Build stamp in the header: run #/SHA + build time (MYT), so it's obvious
// which deployed version the phone is showing.
function setBuildInfo(info) {
  const el = document.getElementById("build-info");
  if (!el || !info) return;
  const parts = [];
  if (info.run) parts.push(`build #${info.run}`);
  if (info.sha) parts.push(info.sha);
  if (info.time) parts.push(`${info.time} MYT`);
  el.textContent = parts.length ? `⚙ ${parts.join(" · ")}` : "";
}
if (window.BUILD_INFO) setBuildInfo(window.BUILD_INFO);
tickClock();
setInterval(tickClock, 1000);

// Window selector: fixed choices plus "Custom…" which asks for any number of
// days (1–730, the API's window limit) and pins it as a selectable option.
const rangeSel = document.getElementById("range");
let lastRange = rangeSel.value;
rangeSel.addEventListener("change", () => {
  if (rangeSel.value !== "custom") {
    lastRange = rangeSel.value;
    load();
    return;
  }
  const raw = prompt("Show how many days? (1–730)", lastRange);
  const days = Math.round(+String(raw).trim());
  if (!raw || !isFinite(days) || days < 1 || days > 730) {
    rangeSel.value = lastRange; // cancelled or invalid — keep the old window
    return;
  }
  let opt = document.getElementById("custom-days");
  if (!opt) {
    opt = document.createElement("option");
    opt.id = "custom-days";
    rangeSel.insertBefore(opt, rangeSel.querySelector('option[value="custom"]'));
  }
  opt.value = String(days);
  opt.textContent = `${days} days`;
  rangeSel.value = String(days);
  lastRange = String(days);
  load();
});

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
let pendingFiles = [];

document.getElementById("btn-import").addEventListener("click", () => {
  openModal("import-modal");
  updateResetButton();
});

// Danger zone: wipe the recorded trip history for a clean start.
// Lives at the bottom of the Recent Trips card (self-hosted mode only).
const clearTripsBtn = document.getElementById("clear-trips");
if (clearTripsBtn) {
  clearTripsBtn.addEventListener("click", async () => {
    if (!confirm("Delete ALL recorded trips?\n\nCharging history and battery-health " +
                 "readings are kept. This cannot be undone.")) return;
    clearTripsBtn.disabled = true;
    clearTripsBtn.textContent = "Clearing…";
    try {
      const res = await fetch("/api/data/clear-drives", { method: "POST" });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || "Could not clear trips");
      await load();
    } catch (e) {
      alert(e.message);
    } finally {
      clearTripsBtn.disabled = false;
      clearTripsBtn.textContent = "🗑 Clear trip history";
    }
  });
}

// "Use demo data" reset (static/PWA mode): clears the imported dataset.
function updateResetButton() {
  const btn = document.getElementById("import-reset");
  if (!btn) return;
  btn.classList.toggle("hidden", !(STATIC_MODE && importedDataset()));
}
const resetBtn = document.getElementById("import-reset");
if (resetBtn) {
  resetBtn.addEventListener("click", () => {
    localStorage.removeItem(STORE_KEY);
    setStatus(importStatus, "Reverted to demo data.", "ok");
    updateResetButton();
    setTimeout(() => { closeModal("import-modal"); load(); }, 600);
  });
}

dropzone.addEventListener("click", () => fileInput.click());
["dragover", "dragenter"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); }));
["dragleave", "drop"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); }));
dropzone.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) selectFiles(e.dataTransfer.files); });
fileInput.addEventListener("change", () => { if (fileInput.files.length) selectFiles(fileInput.files); });

function selectFiles(files) {
  pendingFiles = Array.from(files);
  document.getElementById("file-name").textContent =
    pendingFiles.map((f) => f.name).join(", ");
  importSubmit.disabled = false;
  setStatus(importStatus, "", "");
}

importSubmit.addEventListener("click", async () => {
  if (!pendingFiles.length) return;
  setStatus(importStatus, "Importing…", "");
  importSubmit.disabled = true;
  try {
    let drivesN, chargesN;
    if (STATIC_MODE) {
      // Parse, merge and analyse entirely in the browser — no backend needed.
      const { drives, charges, vehicle } = await TA.parseFiles(pendingFiles);
      const dataset = {
        vehicle: vehicle || { name: "Imported Tesla", model: "Imported", trim: "" },
        drives, charges, source: "imported",
      };
      localStorage.setItem(STORE_KEY, JSON.stringify(dataset));
      drivesN = drives.length; chargesN = charges.length;
    } else {
      // Self-hosted: send each selected file to the API (last one wins for now).
      let body;
      for (const f of pendingFiles) {
        const fd = new FormData();
        fd.append("file", f);
        const res = await fetch("/api/import", { method: "POST", body: fd });
        body = await res.json();
        if (!res.ok) throw new Error(body.detail || "Import failed");
      }
      drivesN = body.imported_drives; chargesN = body.imported_charges;
    }
    setStatus(importStatus, `Imported ${drivesN} drives & ${chargesN} charges.`, "ok");
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

// --- Sync now (live mode): snapshot the car, log drives/charges since last time ---
// Battery reading on the first line, car condition/remark on the second.
function setSyncStatus(batt, cond, kind) {
  const wrap = document.getElementById("sync-status");
  const b = document.getElementById("sync-batt");
  const c = document.getElementById("sync-cond");
  if (!wrap || !b || !c) return;
  b.textContent = batt || "";
  c.textContent = cond || "";
  wrap.className = "status" + (kind ? " " + kind : "");
  wrap.style.display = batt || cond ? "" : "none";
}

let syncBusy = false;
// wake=true (the Sync button) nudges a sleeping car online before reading it;
// the automatic syncs never wake the car.
async function syncNow(wake) {
  if (syncBusy) return;
  syncBusy = true;
  setSyncStatus("", wake ? "Waking car & syncing… (can take ~30 s)" : "Syncing…", "");
  try {
    const res = await fetch(`/api/sync${wake ? "?wake=1" : ""}`, { method: "POST" });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "Sync failed");
    if (body.status === "asleep") {
      const batt = body.last && body.last.soc
        ? `🔋 ${Math.round(body.last.soc)}% (last known)` : "";
      setSyncStatus(batt, body.tried_wake
        ? "😴 Couldn't wake the car — it may be offline. Try again in a minute."
        : "😴 Car asleep — tap Sync to wake it, or sync after a drive.", "warn");
    } else if (body.status === "sleep-window") {
      const batt = body.last && body.last.soc
        ? `🔋 ${Math.round(body.last.soc)}% (last known)` : "";
      setSyncStatus(batt, `💤 ${body.note} Tap Sync for fresh data now.`, "");
    } else {
      const l = body.logged || {};
      const extra = (l.drives || l.charges)
        ? ` · logged ${l.drives} drive(s), ${l.charges} charge(s)`
        : "";
      const statusTxt = {
        charging: "⚡ Charging",
        driving: `🚗 Driving${body.speed_kmh ? " · " + body.speed_kmh + " km/h" : ""}`,
        stopped: "🚗 Trip in progress — stopped briefly",
        parked: "🅿️ Parked",
      }[body.status] || body.status;
      const noLoc = body.location_access === false
        ? " · 📍 no location access — sign in with Tesla again" : "";
      setSyncStatus(`🔋 ${Math.round(body.soc)}%`, `${statusTxt}${extra}${noLoc}`, "ok");
      // Refresh the dashboard when something was logged, or live while a trip
      // is running so the "Current drive" window tracks the car.
      if (l.drives || l.charges || body.trip_in_progress) load();
    }
  } catch (e) {
    setSyncStatus("", e.message, "err");
  } finally {
    syncBusy = false;
  }
}
const syncBtnEl = document.getElementById("btn-sync");
if (syncBtnEl) syncBtnEl.addEventListener("click", () => syncNow(true));
// Re-sync every 5 minutes while the dashboard stays open and visible.
setInterval(() => {
  if (!document.hidden && !STATIC_MODE && window._syncedOnce) syncNow();
}, 5 * 60 * 1000);

// --- Export all data as a ZIP of CSVs (drives.csv + charges.csv) ---
function csvOf(rows, headers) {
  const esc = (v) => {
    const s = String(v ?? "");
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  return headers.join(",") + "\n" +
    rows.map((r) => headers.map((h) => esc(r[h])).join(",")).join("\n") + "\n";
}
async function exportCsv() {
  // Ask which scope: OK = everything, Cancel = only the current window.
  const rawRange = document.getElementById("range").value;
  const sinceCharge = rawRange === "charge";
  const currentDrive = rawRange === "drive";
  const windowTxt = currentDrive ? "current drive"
    : sinceCharge ? "since last charge" : `last ${rawRange} day(s)`;
  const all = confirm(
    `Export ALL data?\n\nOK — everything\nCancel — current window only (${windowTxt})`
  );

  if (!STATIC_MODE) {
    const q = all ? "" : currentDrive ? "?current_drive=1"
      : (sinceCharge ? "?since_charge=1" : `?days=${rawRange}`);
    window.location.href = "/api/export/csv" + q;
    return;
  }

  // Static/PWA: build the same ZIP in the browser from the local dataset.
  const ds = importedDataset() || (await demoDataset());
  let drives = ds.drives || [];
  let charges = ds.charges || [];
  if (!all) {
    let since = 0;
    if (currentDrive) {
      const starts = drives.map((d) => new Date(d.start_time).getTime()).filter(isFinite);
      since = starts.length ? Math.max(...starts) : 0;
    } else if (sinceCharge) {
      const ends = charges.map((c) => new Date(c.end_time || c.start_time).getTime())
        .filter(isFinite);
      since = ends.length ? Math.max(...ends) : 0;
    } else {
      since = Date.now() - (+rawRange) * 86400000;
    }
    drives = drives.filter((d) => new Date(d.start_time).getTime() >= since);
    charges = charges.filter((c) => new Date(c.start_time).getTime() >= since);
  }
  const zip = new JSZip();
  zip.file("drives.csv", csvOf(drives, [
    "start_time", "end_time", "distance_km", "duration_min", "start_soc",
    "end_soc", "energy_used_kwh", "avg_speed_kmh", "max_speed_kmh",
    "outside_temp_c", "start_location", "end_location"]));
  zip.file("charges.csv", csvOf(charges, [
    "start_time", "end_time", "duration_min", "start_soc", "end_soc",
    "energy_added_kwh", "charge_type", "max_power_kw", "location",
    "cost", "outside_temp_c"]));
  const blob = await zip.generateAsync({ type: "blob" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = all ? "tesla-analyzer-all.zip"
    : `tesla-analyzer-${currentDrive ? "current-drive" : sinceCharge ? "since-charge" : rawRange + "d"}.zip`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
const exportBtn = document.getElementById("btn-export");
if (exportBtn) exportBtn.addEventListener("click", exportCsv);

// Register the service worker so the app installs and works offline on iOS.
// Self-hosted serves it at /sw.js (root scope); the static Pages build serves
// it next to index.html (relative scope under the project subpath).
if ("serviceWorker" in navigator) {
  const swUrl = STATIC_MODE ? "./sw.js" : "/sw.js";
  window.addEventListener("load", () =>
    navigator.serviceWorker.register(swUrl).catch(() => {})
  );
}

load();
