"use strict";

const charts = {};
let lastData = null;   // most recent /summary payload, for re-rendering lists
const GRID = "#262b34";
const TICK = "#9aa4b2";
Chart.defaults.color = TICK;
Chart.defaults.borderColor = GRID;
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";

function fmt(n, digits = 0, fixed = false) {
  if (n === null || n === undefined) return "–";
  // fixed: always show exactly `digits` decimals (e.g. "928.0", not "928")
  // — for a handful of KPI headline numbers where consistent formatting
  // matters more than trimming a redundant ".0". Off by default so every
  // other existing fmt() call (charts, trip lists, ...) is untouched.
  return Number(n).toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: fixed ? digits : 0,
  });
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

// Colour band for a 0-100 driving score.
function scoreTone(s) {
  return s >= 85 ? "green" : s >= 70 ? "blue" : s >= 55 ? "amber" : "red";
}

// Semantic rail colour for an efficiency reading vs the rated figure.
// vs_rated_pct is the overshoot: <=0 means at/under rated (good, green),
// a little over is amber, well over is red. Null (unknown) stays neutral.
function effTone(vsRatedPct) {
  if (vsRatedPct == null) return "green";
  return vsRatedPct <= 2 ? "green" : vsRatedPct <= 15 ? "amber" : "red";
}

// Driving Behaviour breakdown (habit costs vs. this driver's own best
// quartile) as supporting detail under the one driving score — not a
// second competing "/100" number, which used to read as a mismatch
// against the main score above it (they grade different things: this
// window vs. rated efficiency, vs. this window's drives vs. each other).
function behaviourHtml(b) {
  if (!b || !b.available) return "";
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
  return `<div class="score-behaviour">
    <div class="bat-line">Typical driving vs your own best quartile
      (<strong>${Math.round(b.best_quartile_wh_per_km)} Wh/km</strong>)</div>
    ${rows || '<div class="bat-line">No costly habits detected in this window 🎉</div>'}
    ${b.potential_saving_kwh >= 1
      ? `<div class="bat-line">Potential if all drives matched your best: <strong>${b.potential_saving_kwh} kWh</strong></div>`
      : ""}
  </div>`;
}

// Prominent window driving score at the top, with an info popover explaining
// how it's computed, plus the behaviour breakdown underneath — one box, one
// score.
function renderScore(d) {
  const el = document.getElementById("score-banner");
  if (!el) return;
  const drv = d.driving || {};
  const sep = document.getElementById("sep-score");
  // Hide the score (and its separator) when efficiency is unknown (no energy
  // data) — a 0 would wrongly read as "grade E".
  if (!drv.available || drv.eco_score == null) {
    el.style.display = "none";
    if (sep) sep.style.display = "none";
    return;
  }
  el.style.display = "";
  if (sep) sep.style.display = "";
  const s = drv.eco_score, grade = drv.eco_grade;
  const rated = (d.efficiency && d.efficiency.rated_wh_per_km) || 150;
  const info = `How the driving score works:<br>` +
    `It grades this window's efficiency (<strong>${drv.avg_efficiency_wh_per_km} Wh/km</strong>) ` +
    `against your car's rated <strong>${rated} Wh/km</strong>.<br>` +
    `• ~15% under rated → 100 &nbsp; • exactly rated → 85<br>` +
    `• about −1 point per 1% over rated<br>` +
    `Grades: A ≥85 · B ≥70 · C ≥55 · D ≥40 · E below. ` +
    `Lower Wh/km (gentler speed, smoother acceleration, less climate use) lifts it.`;
  el.className = `score-banner tone-${scoreTone(s)}`;
  el.innerHTML =
    `<div class="score-ring">${s}<span>/100</span></div>` +
    `<div class="score-text">` +
    `<div class="score-grade">Driving score: grade ${grade}` +
    `<button class="info-btn" data-info="score-info">!</button></div>` +
    `<div class="score-sub">${drv.avg_efficiency_wh_per_km} Wh/km this ${d.window_label || "window"} · ` +
    `${drv.total_distance_km} km over ${drv.total_drives} drives</div>` +
    `<div id="score-info" class="info-pop hidden">${info}</div>` +
    behaviourHtml(drv.behaviour) +
    `</div>`;
  wireInfoButtons(el);
}

// --- Home (garage) page: pick a car, then open its dashboard --------------
function showHome() {
  document.body.classList.add("view-home");
  renderHome();
}
function showCar() {
  document.body.classList.remove("view-home");
  window.scrollTo(0, 0);
}

// Enter a car's dashboard. On the backend, set it as the active car first so the
// whole dashboard follows it; in the static demo there's only one car to show.
async function openCar(vin) {
  if (!STATIC_MODE && vin) {
    try {
      await fetch("/api/active-vehicle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ vin }),
      });
    } catch (_) { /* ignore; load() reflects the active car anyway */ }
  }
  showCar();
  await load();
}

function carCard(car) {
  const btn = document.createElement("button");
  btn.className = "car-card";
  const ico = document.createElement("span");
  ico.className = "car-card-ico";
  ico.textContent = "🚗";
  const main = document.createElement("span");
  main.className = "car-card-main";
  const name = document.createElement("span");
  name.className = "car-card-name";
  name.textContent = car.name || "My Tesla";
  const sub = document.createElement("span");
  sub.className = "car-card-sub";
  const last4 = (car.vin || "").slice(-4);
  const realVin = car.vin && !/^(DEMO|IMPORT|LINKED)/.test(car.vin);
  sub.textContent = [car.model, realVin && last4 ? "VIN …" + last4 : ""]
    .filter(Boolean).join(" · ") || "Tap to view analytics";
  main.append(name, sub);
  const go = document.createElement("span");
  go.className = "car-card-go";
  go.textContent = "›";
  btn.append(ico, main, go);
  btn.addEventListener("click", () => openCar(car.vin));
  return btn;
}

// Populate the landing page: app version/clock (handled by the clock/build
// tickers) plus a card per car, and reveal Unlink only when a car is linked.
async function renderHome() {
  const cars = document.getElementById("home-cars");
  let list = [], mode = "demo";
  if (STATIC_MODE) {
    const ds = importedDataset() || (await demoDataset());
    mode = ds.source === "imported" ? "imported" : "demo";
    list = [{ vin: "", name: mode === "imported" ? "Imported data" : "Demo car", model: "" }];
  } else {
    try {
      const health = await (await fetch("/api/health")).json();
      mode = health.mode;
      setBuildInfo(health.build);
      const vs = await (await fetch("/api/vehicles")).json();
      const real = (vs || []).filter((v) => !/^(DEMO|IMPORT)/.test(v.vin));
      list = (mode === "live" && real.length) ? real : (vs || []);
    } catch (_) { list = []; }
  }
  const badge = document.getElementById("home-badge");
  if (badge) { badge.textContent = mode; badge.className = "badge " + mode; }
  document.getElementById("btn-unlink")?.classList.toggle("hidden", mode !== "live");
  // Compare is only meaningful with more than one real car on the account.
  document.getElementById("btn-compare")?.classList.toggle(
    "hidden", STATIC_MODE || mode !== "live" || list.length < 2);

  cars.innerHTML = "";
  if (!list.length) {
    const p = document.createElement("p");
    p.className = "home-empty";
    p.textContent = "No car yet — link your Tesla account or load a data export below to begin.";
    cars.appendChild(p);
    return;
  }
  for (const c of list) cars.appendChild(carCard(c));
}

document.getElementById("btn-home")?.addEventListener("click", showHome);

// Unlink: disconnect the current Tesla so a different account can be linked
// (the logged history is kept). Returns to the home page afterwards.
document.getElementById("btn-unlink")?.addEventListener("click", async () => {
  if (!confirm(
    "Disconnect the current Tesla account?\n\nYour logged drives and charges are " +
    "kept. You can then sign in with a different Tesla account.")) return;
  try {
    const r = await fetch("/api/unlink", { method: "POST" });
    if (!r.ok) throw new Error(await r.text());
  } catch (e) { alert("Unlink failed: " + e.message); return; }
  renderHome();
});

// Compare Cars: every real (linked) car's driving/cost/battery figures for
// the same window, side by side — button only shows with >1 real car (see
// renderHome). Rows = metrics, columns = cars, so scanning across a row
// compares the same thing rather than reading one car's whole column first.
const COMPARE_ROWS = [
  { label: "Distance", key: "distance_km", fmt: (v) => v != null ? `${fmt(v)} km` : "—" },
  { label: "Drives", key: "drives", fmt: (v) => v != null ? fmt(v) : "—" },
  { label: "Avg Efficiency", key: "avg_wh_per_km", fmt: (v) => v != null ? `${fmt(v)} Wh/km` : "—" },
  { label: "Driving Cost", key: "driving_cost",
    fmt: (v, cur) => v != null ? `${cur} ${fmt(v, 2)}` : "—" },
  { label: "Cost / km", key: "cost_per_km",
    fmt: (v, cur) => v != null ? `${cur} ${fmt(v, 3)}` : "—" },
  { label: "Energy Charged", key: "energy_charged_kwh", fmt: (v) => v != null ? `${fmt(v)} kWh` : "—" },
  { label: "Charging Cost", key: "charging_cost",
    fmt: (v, cur) => v != null ? `${cur} ${fmt(v, 2)}` : "—" },
  { label: "Battery Health", key: "health_pct", fmt: (v) => v != null ? `${fmt(v, 1)}%` : "—" },
  { label: "vs Fleet Degradation", key: "vs_fleet_pct",
    fmt: (v) => v != null ? `${v > 0 ? "+" : ""}${fmt(v, 1)}pp` : "—" },
];

async function renderCompareTable() {
  const table = document.getElementById("compare-table");
  const days = document.getElementById("compare-range").value;
  table.innerHTML = "<tr><td>Loading…</td></tr>";
  let data;
  try {
    data = await (await fetch(`/api/compare?days=${days}`)).json();
  } catch (e) {
    table.innerHTML = "<tr><td>Couldn't load — try again.</td></tr>";
    return;
  }
  const cars = data.vehicles || [];
  if (!cars.length) {
    table.innerHTML = "<tr><td>No linked cars to compare.</td></tr>";
    return;
  }
  const head = `<tr><th></th>${cars.map((c) => `<th>${c.name || c.model || "Car"}</th>`).join("")}</tr>`;
  const body = COMPARE_ROWS.map((row) =>
    `<tr><th>${row.label}</th>${cars.map((c) => `<td>${row.fmt(c[row.key], data.currency)}</td>`).join("")}</tr>`
  ).join("");
  table.innerHTML = head + body;
}

document.getElementById("btn-compare")?.addEventListener("click", () => {
  openModal("compare-modal");
  renderCompareTable();
});
document.getElementById("compare-range")?.addEventListener("change", renderCompareTable);

// Full car-info panel (opened by the "!" after the VIN in the header).
function fillCarInfo(v) {
  const el = document.getElementById("car-info");
  if (!el || !v) return;
  const realVin = v.vin && !/^(DEMO|IMPORT|LINKED)/.test(v.vin) ? v.vin : null;
  const badge = ((v.trim || "").match(/\b(P?\d+D?)\b/) || [])[1];
  const wheel = (v.trim || "").split(/\s+/).find((t) =>
    /^(nova|photon|pinwheel|gemini|induction|crossflow|uberturbine|apollo|turbine|helix|arachnid|cyberstream|stiletto)/i.test(t));
  // Colour = trim tokens that aren't the badge or the wheel.
  const colour = (v.trim || "").split(/\s+/)
    .filter((t) => t && !/^P?\d+D?$/i.test(t) && !/(1[89]|2[012])/.test(t))
    .join(" ");
  const rows = [
    realVin ? `VIN: <strong>${realVin}</strong>` : null,
    [v.year, v.model, badge].filter(Boolean).length
      ? `Model: <strong>${[v.year, v.model, badge].filter(Boolean).join(" ")}</strong>` : null,
    colour ? `Colour: <strong>${colour}</strong>` : null,
    wheel ? `Wheels: <strong>${prettyWheel(wheel)}</strong>` : null,
    v.plant ? `Built at: <strong>Giga ${v.plant}</strong>` : null,
    // The pack size used to turn range/SoC deltas into kWh — every drive's
    // kWh scales with it, so show it (and its source) to make a wrong value
    // obvious. "measured" = learned from your charges, "variant spec" = from
    // the car's badge, "override" = set in config.
    v.usable_capacity_kwh
      ? `Usable capacity: <strong>${fmt(v.usable_capacity_kwh, 1)} kWh</strong>` +
        (v.capacity_source ? ` <span class="muted">(${v.capacity_source})</span>` : "") : null,
    v.tou_enabled ? `Tariff: <strong>time-of-use</strong> <span class="muted">(peak/off-peak)</span>` : null,
  ].filter(Boolean);
  el.innerHTML = rows.map((r) => `<div>${r}</div>`).join("")
    || "<div>No linked car yet.</div>";
}

// Close the car-info panel when tapping elsewhere.
document.addEventListener("click", (e) => {
  const el = document.getElementById("car-info");
  if (el && !el.classList.contains("hidden") &&
      !el.contains(e.target) && !e.target.classList.contains("info-btn")) {
    el.classList.add("hidden");
  }
});

// Wire every ".info-btn[data-info]" inside a container to toggle its popover.
function wireInfoButtons(root) {
  root.querySelectorAll(".info-btn[data-info]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const pop = document.getElementById(btn.dataset.info);
      if (pop) pop.classList.toggle("hidden");
    });
  });
}

// Tap a trip's tag chip to cycle untagged -> work -> personal -> untagged,
// persisting each step immediately.
const TAG_CYCLE = ["", "work", "personal"];
function wireTagChips(root) {
  root.querySelectorAll(".trip-tag[data-trip-id]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = +btn.dataset.tripId;
      const next = TAG_CYCLE[(TAG_CYCLE.indexOf(btn.dataset.tag) + 1) % TAG_CYCLE.length];
      btn.disabled = true;
      try {
        const resp = await fetch("/api/data/tag-drive", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id, tag: next }),
        });
        if (resp.ok && lastData) {
          const trip = (lastData.driving.recent_trips || []).find((t) => t.id === id);
          if (trip) trip.tag = next;
          renderLists(lastData);   // full re-render: chip label + by-tag summary
          return;
        }
      } catch (err) { /* leave the chip as-is on failure */ }
      btn.disabled = false;
    });
  });
}

function renderKpis(d) {
  const drv = d.driving, chg = d.charging, eff = d.efficiency, cur = d.currency;
  const cards = [];
  const lt = d.live_trip;
  const bal = d.battery_balance;
  if (lt) {
    // A drive is in progress — show its live numbers first.
    cards.push(kpiCard("Current Drive", fmt(lt.distance_km, 1) + " km",
      `started ${tripWhen(lt.start_time)} · in progress`, "blue"));
    cards.push(kpiCard("Drive Time", fmt(lt.duration_min) + " min",
      `avg ${fmt(lt.avg_speed_kmh)}${lt.max_speed_kmh > lt.avg_speed_kmh ? " · max " + fmt(lt.max_speed_kmh) : ""} km/h`, "amber"));
    if (lt.wh_per_km) {
      const hasDrive = lt.driving_wh_per_km != null && lt.driving_wh_per_km < lt.wh_per_km - 3;
      const dKwh = (hasDrive && lt.driving_energy_kwh != null) ? `${fmt(lt.driving_energy_kwh, 1)} kWh / ` : "";
      const dsub = hasDrive ? ` · drive ≈${dKwh}${fmt(lt.driving_wh_per_km)} Wh/km` : "";
      // Colour by how this drive's efficiency compares to rated (drive-only
      // figure when idle was stripped out, else the raw one).
      const rated = (eff && eff.rated_wh_per_km) || null;
      const liveEff = lt.driving_wh_per_km != null ? lt.driving_wh_per_km : lt.wh_per_km;
      const liveVs = rated ? (liveEff - rated) / rated * 100 : null;
      cards.push(kpiCard("Efficiency", fmt(lt.wh_per_km) + " Wh/km",
        `${fmt(lt.energy_kwh, 1)} kWh this drive${dsub}`, effTone(liveVs)));
    }
    // Battery use and km/1% in one box: % used as the headline, start→now
    // and the km/1% range figure on the sub-line.
    cards.push(kpiCard("Battery", fmt(lt.soc_used, 1) + "% used",
      `${fmt(lt.start_soc)}% → ${fmt(lt.soc)}%` +
      (lt.km_per_soc ? ` · ${fmt(lt.km_per_soc, 1)} km/1%` : ""), "teal"));
    // Straight-line ETA to the nearest named place not already reached, with
    // the SoC it projects to on arrival at this drive's own pace/efficiency
    // (see /api/places — needs at least one named place to show at all).
    if (lt.eta) {
      const soc = lt.eta.projected_soc;
      cards.push(kpiCard(`ETA · ${lt.eta.place}`, fmt(lt.eta.eta_min) + " min",
        `${fmt(lt.eta.distance_km, 1)} km` + (soc != null ? ` · ~${fmt(soc, 1)}% on arrival` : ""),
        soc != null && soc < 15 ? "amber" : "blue"));
    }
  }
  if (drv.available) {
    cards.push(kpiCard("Distance", fmt(drv.total_distance_km, 1, true) + " km",
      `${fmt(drv.total_drives)} drives · ${fmt(drv.total_duration_h)} h`, "blue"));
    // Efficiency is unknown when the drive logged no energy (range gap).
    if (eff.available && eff.avg_efficiency_wh_per_km) {
      // eff.avg_efficiency_wh_per_km comes from efficiency_analysis.analyze()
      // — its OWN drive filter (energy-bearing AND not a <40 Wh/km
      // contamination-excluded reading), computed independently of
      // driving_analysis.analyze()'s eff_drives/_trip_kwh(). bal.trip_kwh
      // (tried previously) sums a DIFFERENT set of drives with a different
      // per-trip rule, so it never quite multiplied out against this Wh/km
      // either (reported live: 13.2 kWh over 84.2 km implies 156.8 Wh/km,
      // not the 153 shown). eff.total_energy_kwh is the exact number
      // efficiency_analysis divided to get this ratio in the first place —
      // guaranteed to multiply back out exactly, by construction.
      const usedKwh = eff.total_energy_kwh;
      cards.push(kpiCard("Avg Efficiency", fmt(eff.avg_efficiency_wh_per_km, 1, true) + " Wh/km",
        `${fmt(usedKwh, 1)} kWh used · ${eff.vs_rated_pct >= 0 ? "+" : ""}${fmt(eff.vs_rated_pct, 1)}% vs rated`,
        effTone(eff.vs_rated_pct)));
    } else {
      cards.push(kpiCard("Avg Efficiency", "—",
        "waiting on range data from a synced drive", "green"));
    }
    cards.push(kpiCard("Avg Speed", fmt(drv.avg_speed_kmh) + " km/h",
      `max ${fmt(drv.max_speed_kmh)} · peak ${fmt(drv.p95_speed_kmh)} km/h`, "amber"));
    // Always present so the box never "disappears"; "—" until energy data lands.
    // Wide windows drain the pack many times over — phrase that as full
    // charges rather than a ">100%" that reads as a glitch.
    // This is deliberately the GROSS % (trip + idle, same total as Battery
    // Used above) — "real-world range" is meant to include the cost of
    // standby drain, not just driving (see driving_analysis.analyze()'s own
    // docstring on km_per_soc_pct). For a since-charge window bal.used_pct
    // is that same total anchored to the real SoC delta (see routes.py's
    // summary()), not analyze()'s own bottom-up estimate — use it so this
    // card and Battery Used are always talking about the same %, the same
    // fix already applied to Avg Efficiency (driving-only there instead).
    const socPct = bal && bal.used_pct != null ? bal.used_pct : drv.soc_used_pct;
    const kmPerSoc = socPct != null && socPct >= 0.2 && drv.total_distance_km
      ? drv.total_distance_km / socPct : null;
    const socSub = socPct == null ? "real-world range"
      : socPct <= 100 ? `${fmt(socPct, 1)}% battery used`
      : `${fmt(socPct / 100, 1)} full charges used`;
    cards.push(kmPerSoc
      ? kpiCard("km / 1% Battery", fmt(kmPerSoc, 1) + " km", socSub, "teal")
      : kpiCard("km / 1% Battery", "—", "waiting on range data from a synced drive", "teal"));
    // Battery Used: % of the full (degradation-adjusted) pack — same basis
    // as km/1% Battery's soc_used_pct and every trip's own soc_used_pct, so
    // all the %-of-battery figures on this screen are directly comparable
    // and summable with Vampire Drain's own % below. Still only shown for
    // the "since charge" window: any other window can span several
    // charge/discharge cycles with cumulative use exceeding one pack, so
    // it's just the raw kWh there. Split trip-vs-parked so the headline
    // doesn't read as "all driving" — bal.trip_kwh + bal.vampire_kwh always
    // sums to bal.used_kwh exactly (see driving_analysis.analyze()). Shown
    // even when idle is exactly 0 — that's real information too (every
    // charge-free gap this window genuinely measured no SoC movement), not
    // just "nothing to report", so hiding it would look like the two
    // numbers don't add up when they actually do.
    const split = bal
      ? ` (${fmt(bal.trip_kwh, 1)} trip + ${fmt(bal.vampire_kwh, 1)} idle)` : "";
    // "!" info popover explaining how full_charge_kwh (shared by Battery
    // Used and Vampire Drain below) accounts for degradation — see
    // _usable_capacity() in routes.py for the actual priority order.
    const battpackBtn = bal
      ? `<button class="info-btn" data-info="battpack-info" title="How is the battery pack capacity calculated?">!</button>` : "";
    // Vampire Drain gets its own info button: same pack-capacity note, plus
    // what counts as a "parked gap" in the first place — see
    // driving_analysis.VAMPIRE_MIN_GAP_HOURS / vampire_drain().
    const vampireInfoBtn = bal
      ? `<button class="info-btn" data-info="vampire-info" title="What counts as a parked gap?">!</button>` : "";
    if (bal && bal.used_kwh != null) {
      if (bal.used_pct != null) {
        cards.push(kpiCard(`Battery Used${battpackBtn}`, fmt(bal.used_pct, 1, true) + "%",
          `${fmt(bal.used_kwh, 1)} kWh${split} of ${fmt(bal.full_charge_kwh, 1)} kWh full pack`,
          "amber"));
      } else {
        cards.push(kpiCard(`Battery Used${battpackBtn}`, `${fmt(bal.used_kwh, 1)} kWh`,
          `${split ? split.trim() + " · " : ""}% not shown — window may span more than one charge`, "amber"));
      }
    }
    // Vampire Drain: standby loss in parked gaps between drives (sentry
    // mode, preconditioning, plain self-discharge) — see
    // driving_analysis.vampire_drain(). % uses the SAME denominator as
    // Battery Used (full degradation-adjusted pack, bal.full_charge_kwh) —
    // not a share of this window's total used — so the two %s are on the
    // same footing and add up (trip % + vampire % = Battery Used %). Only
    // shown when Battery Used's own % is (since-charge window); other
    // windows fall back to the raw kWh, matching Battery Used's own
    // fallback. No %/day rate — a typical gap is only a few hours, and
    // linearly projecting that to 24h overstates a full day's real drain
    // (most of a day is near-zero deep-sleep, punctuated by short bursts
    // like sentry triggers or cabin cooling — a short gap is disproportionately
    // likely to catch one of those bursts, not represent the day as a whole).
    // Card itself is always shown (like every other KPI here) rather than
    // disappearing when nothing qualified this window — a nightly-charging
    // driver legitimately sees "0" most windows (the overnight gap always
    // has a charge in it, so it's excluded), which reads as "measured,
    // found none" rather than "this feature isn't working."
    if (bal) {
      const vampirePct = bal.used_pct != null && bal.full_charge_kwh > 0
        ? bal.vampire_kwh / bal.full_charge_kwh * 100 : null;
      // vampire_gaps/_hours only count gaps >=1h (the "parked gap" narrative);
      // vampire_kwh itself sums EVERY charge-free gap, any duration — so a
      // window full of only short (<1h) stops can still show real kWh here
      // even though vampire_gaps is 0. Don't let a 0 gap count hide nonzero
      // kWh (see driving_analysis.vampire_drain()'s kWh/narrative split).
      const gapInfo = bal.vampire_gaps > 0
        ? `${bal.vampire_gaps} parked gap${bal.vampire_gaps === 1 ? "" : "s"} · ${fmt(bal.vampire_hours, 0)} h parked`
        : (bal.vampire_kwh > 0 ? "no single gap 1h+ — several shorter stops" : "no qualifying parked gap (charge-free) in this window");
      cards.push(vampirePct != null
        ? kpiCard(`Vampire Drain${vampireInfoBtn}`, fmt(vampirePct, 1, true) + "%",
            `${fmt(bal.vampire_kwh, 1)} kWh of ${fmt(bal.full_charge_kwh, 1)} kWh full pack · ${gapInfo}`,
            "amber")
        : kpiCard(`Vampire Drain${vampireInfoBtn}`, `${fmt(bal.vampire_kwh, 1)} kWh`, gapInfo, "amber"));
    }
    if (bal) {
      const v = d.vehicle || {};
      const packNote = `Full pack (<strong>${fmt(bal.full_charge_kwh, 1)} kWh</strong>) is the pack's usable ` +
        `capacity adjusted for real-world battery degradation` +
        `${v.capacity_source ? ` <span class="muted">(${v.capacity_source})</span>` : ""}: ` +
        `a direct measurement from your own charging sessions when there's enough history, ` +
        `otherwise your car's spec capacity reduced by its estimated degradation % — see the ` +
        `Battery Health card below for that %.`;
      cards.push(`<div id="battpack-info" class="info-pop hidden kpi-info">${packNote}<br><br>` +
        `"trip" here won't always exactly match the sum of the kWh shown per trip in Recent Trips ` +
        `below. Each trip's own listed kWh is its <strong>measured</strong> figure (from its range ` +
        `reading); "trip" in this breakdown is <strong>the true total minus idle</strong> — if any ` +
        `trip's actual whole-percent battery drop came in higher than its range-based reading ` +
        `suggested (range estimates can under-read real usage), that gap shows up here without ` +
        `changing the number shown on that trip itself. trip + idle still always sums to the ` +
        `total exactly.</div>`);
      cards.push(`<div id="vampire-info" class="info-pop hidden kpi-info">${packNote}<br><br>` +
        `Any gap between two drives with <strong>no charge</strong> in it (a charge moves SoC ` +
        `upward, so it can't be isolated as pure drain) counts toward the kWh/% shown here, no ` +
        `matter how short — even a quick errand stop drains a little. The "parked gaps · hours ` +
        `parked" note only counts gaps of <strong>at least 1 hour</strong> though, so that count ` +
        `reads as genuine idle stretches rather than every red-light stop. A gap still adds its ` +
        `kWh even if SoC happened to read unchanged — SoC is only whole-percent precision, so a ` +
        `real small loss over a short stop doesn't always cross a full point; that just means ` +
        `0 kWh is attributed to it, not that nothing happened.</div>`);
    }
    // Estimated Range: total km this charge cycle is good for — this
    // window's distance already driven, plus how far the CURRENT battery
    // balance (not this window's — the latest known SoC) still goes before
    // hitting 0%. Wh/km is this window's own measured efficiency when
    // there's enough driving to measure one, else the car's rated spec.
    if (bal && bal.current_soc_pct != null && bal.full_charge_kwh > 0) {
      const measured = eff && eff.available && eff.avg_efficiency_wh_per_km;
      const whPerKm = measured || (eff && eff.rated_wh_per_km) || 150;
      const soc = bal.current_soc_pct;
      const kmAt = (pct) => Math.max(soc - pct, 0) / 100 * bal.full_charge_kwh / whPerKm * 1000;
      const totalKm = kmAt(0) + (drv.available ? drv.total_distance_km : 0);
      const thresholds = [];
      if (soc > 50) thresholds.push(`${fmt(kmAt(50), 1, true)} km to 50%`);
      if (soc > 20) thresholds.push(`${fmt(kmAt(20), 1, true)} km to 20%`);
      thresholds.push(`${fmt(kmAt(0), 1, true)} km to 0%`);
      thresholds.push(`at ${fmt(whPerKm, 1, true)} Wh/km${measured ? "" : " rated"}`);
      cards.push(kpiCard("Estimated Range", fmt(totalKm, 1, true) + " km",
        thresholds.join(" · "), "violet"));
    }
    // Longest Idle: the single biggest qualifying parked gap this window,
    // separate from Vampire Drain's aggregate — "that's when I was away for
    // the weekend" vs. day-to-day standby. Always shown alongside Vampire
    // Drain rather than only appearing once there's a gap to report.
    // vampire_longest_inducer (server-mode only — see _idle_inducer() in
    // routes.py) is a POSITIVE detection ("Sentry Mode was on") from
    // BatteryReading rows logged right before the car fell asleep, the only
    // part of the gap there's any visibility into — absent whenever there's
    // nothing to confirm, not a claim that nothing was running.
    if (bal) {
      const longestH = bal.vampire_longest_hours;
      const inducer = bal.vampire_longest_inducer ? ` · ${bal.vampire_longest_inducer}` : "";
      cards.push(longestH != null
        ? kpiCard("Longest Idle", fmt(longestH, 1, true) + " h",
            `${fmt(longestH / 24, 1, true)} days · ended ${tripWhen(bal.vampire_longest_end)}${inducer}`, "violet")
        : kpiCard("Longest Idle", "—", "no qualifying parked gap yet", "violet"));
    }
    // TCO: what this window's distance would have cost in an equivalent
    // petrol car, vs. what it actually cost to charge. Hidden entirely
    // unless both petrol inputs are configured (see PETROL_PRICE_PER_LITER /
    // PETROL_L_PER_100KM) — no assumed "average car" figure is guessed.
    const pc = d.petrol_comparison;
    if (pc && pc.savings != null) {
      cards.push(kpiCard("vs Petrol", `${cur} ${fmt(Math.abs(pc.savings), 2)}`,
        `${pc.savings >= 0 ? "saved" : "cost more"} vs ${cur} ${fmt(pc.petrol_cost, 2)} ` +
        `petrol equivalent (${fmt(pc.petrol_l_per_100km, 1)} L/100km)`,
        pc.savings >= 0 ? "green" : "red"));
    }
  }
  if (chg.available) {
    cards.push(kpiCard("Energy Charged", fmt(chg.total_energy_kwh, 1, true) + " kWh",
      `${fmt(chg.total_sessions)} sessions`, "violet"));
    // AC vs DC split — compact single-line value; sub shows the actual kWh.
    const dcShare = chg.dc_energy_share_pct;
    const acShare = Math.max(0, 100 - dcShare);
    cards.push(kpiCard("AC / DC Energy", `${fmt(acShare, 1, true)} / ${fmt(dcShare, 1, true)}%`,
      `${fmt(chg.ac_energy_kwh, 1, true)} / ${fmt(chg.dc_energy_kwh, 1, true)} kWh`, "red"));
    // Driving Cost sits immediately left of Charging Cost so the two money
    // figures for the same window are directly comparable side by side.
    if (drv.available && drv.total_cost != null) {
      cards.push(kpiCard("Driving Cost", `${cur} ${fmt(drv.total_cost, 1, true)}`,
        drv.cost_per_km != null ? `${cur} ${fmt(drv.cost_per_km, 3)} / km` : "", "violet"));
    }
    // What the window's charging cost, with the per-100km "fuel cost" figure
    // a petrol driver can compare directly.
    if (chg.total_cost != null && chg.total_cost > 0) {
      const per100 = chg.cost_per_100km != null ? ` · ${cur} ${fmt(chg.cost_per_100km, 2)}/100km` : "";
      cards.push(kpiCard("Charging Cost", `${cur} ${fmt(chg.total_cost, 1, true)}`,
        `AC ${cur} ${fmt(chg.ac_cost, 2)} · DC ${cur} ${fmt(chg.dc_cost, 2)}${per100}`, "blue"));
    }
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
  wireInfoButtons(document.getElementById("kpis"));
}

function barConfig(labels, data, label, color, unit) {
  unit = unit || label;
  return {
    type: "bar",
    data: { labels, datasets: [{ label, data, backgroundColor: color,
      hoverBackgroundColor: color + "cc", borderRadius: 6, maxBarThickness: 44 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: {
          label: (c) => ` ${fmt(c.parsed.y, 1)} ${unit}`,
        } },
      },
      scales: {
        x: { grid: { display: false }, border: { display: false } },
        y: { beginAtZero: true, border: { display: false }, grid: { color: GRID },
          ticks: { maxTicksLimit: 6 } },
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

// "This week vs last week" pulse: rolling 7-day windows, shown only when
// both weeks have drives (the API sends null otherwise).
function renderWeekCompare(d) {
  const el = document.getElementById("week-compare");
  if (!el) return;
  const wc = d.week_compare;
  if (!wc) { el.style.display = "none"; return; }
  const delta = (a, b, goodDown = false) => {
    if (a == null || b == null || !b) return "";
    const pct = Math.round((a - b) / b * 100);
    if (!pct) return `<span class="wk-delta flat">＝</span>`;
    const good = goodDown ? pct < 0 : pct > 0;
    return `<span class="wk-delta ${good ? "good" : "bad"}">${pct > 0 ? "+" : ""}${pct}%</span>`;
  };
  const t = wc.this, l = wc.last;
  el.innerHTML =
    `<span class="wk-title">vs last week</span>` +
    `<span>${fmt(t.distance_km)} km ${delta(t.distance_km, l.distance_km)}</span>` +
    `<span>${fmt(t.drives)} drives ${delta(t.drives, l.drives)}</span>` +
    `<span>${fmt(t.energy_kwh, 1)} kWh ${delta(t.energy_kwh, l.energy_kwh, true)}</span>` +
    (t.wh_per_km && l.wh_per_km
      ? `<span>${fmt(t.wh_per_km)} Wh/km ${delta(t.wh_per_km, l.wh_per_km, true)}</span>` : "");
  el.style.display = "";
}

// Data-driven observations mined from the window's own drives.
function renderInsights(d) {
  const card = document.getElementById("insights-card");
  const body = document.getElementById("insights-body");
  if (!card || !body) return;
  const ins = (d.driving && d.driving.insights) || [];
  card.style.display = ins.length ? "" : "none";
  body.innerHTML = ins.map((s) => `<li>💡 ${s}</li>`).join("");
}

// A short data-driven narrative for the window (see /api/summary's
// "narrative" — only present for a plain days-based window of >= 14 days,
// where a natural "period before" exists to compare against).
function renderNarrative(d) {
  const card = document.getElementById("narrative-card");
  const body = document.getElementById("narrative-body");
  if (!card || !body) return;
  const lines = d.narrative || [];
  card.style.display = lines.length ? "" : "none";
  body.textContent = lines.join(" ");
}

function renderCharts(d) {
  const eff = d.efficiency, drv = d.driving, chg = d.charging;

  showCard("effTempChart", eff.available);
  showCard("effTrendChart", eff.available);
  showCard("effDailyTrendChart", eff.available);
  showCard("speedBandChart", drv.available);
  showCard("tripsHourChart", drv.available);
  showCard("acdcChart", chg.available);

  if (eff.available) {
    // A bucket's Wh/km can be dragged around by traffic/route composition
    // (a few slow, stop-go trips, or one highland climb) just as easily as
    // by temperature — especially with few trips in it, common for the
    // colder buckets in a tropical climate. Bars backed by fewer than 3
    // trips render faded with a hatch-like lower opacity, and every bar's
    // tooltip surfaces the trip count and average speed so a low/slow bar
    // reads as "thin evidence," not a confirmed temperature effect.
    const t = eff.efficiency_by_temp;
    const tempLabels = Object.keys(t);
    const THIN_N = 3;
    makeChart("effTempChart", {
      type: "bar",
      data: { labels: tempLabels.map(k => k + "°C"), datasets: [{
        label: "Wh/km", data: tempLabels.map(k => t[k].wh_per_km),
        backgroundColor: tempLabels.map(k => t[k].n < THIN_N ? "#3b82f680" : "#3b82f6"),
        hoverBackgroundColor: tempLabels.map(k => t[k].n < THIN_N ? "#3b82f6a0" : "#3b82f6cc"),
        borderRadius: 6, maxBarThickness: 44 }] },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: {
            label: (c) => {
              const b = t[tempLabels[c.dataIndex]];
              return ` ${fmt(b.wh_per_km, 1)} Wh/km · ${b.n} trip${b.n === 1 ? "" : "s"} · avg ${fmt(b.avg_speed_kmh)} km/h`;
            },
            afterLabel: (c) => {
              const b = t[tempLabels[c.dataIndex]];
              return b.n < THIN_N
                ? "Only a few trips here — could reflect traffic/route, not temperature"
                : "";
            },
          } },
        },
        scales: {
          x: { grid: { display: false }, border: { display: false } },
          y: { beginAtZero: true, border: { display: false }, grid: { color: GRID },
            ticks: { maxTicksLimit: 6 } },
        },
      },
    });

    const w = eff.weekly_efficiency;
    makeChart("effTrendChart", {
      type: "line",
      data: { labels: Object.keys(w), datasets: [{
        label: "Wh/km", data: Object.values(w), borderColor: "#e82127", borderWidth: 2,
        backgroundColor: "rgba(232,33,39,.06)", fill: true, tension: .35,
        pointRadius: 0, pointHitRadius: 12, pointHoverRadius: 4,
        pointBackgroundColor: "#e82127" }] },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: (c) => ` ${fmt(c.parsed.y, 0)} Wh/km` } } },
        scales: {
          x: { grid: { display: false }, border: { display: false }, ticks: { maxTicksLimit: 8 } },
          y: { border: { display: false }, grid: { color: GRID }, ticks: { maxTicksLimit: 6 } },
        } },
    });

    // Same shape as the weekly trend, grouped by calendar day instead —
    // finer-grained, so a single bad day (heat, traffic) doesn't get
    // smoothed away by a whole week's average.
    const dd = eff.daily_efficiency;
    makeChart("effDailyTrendChart", {
      type: "line",
      data: { labels: Object.keys(dd), datasets: [{
        label: "Wh/km", data: Object.values(dd), borderColor: "#e82127", borderWidth: 2,
        backgroundColor: "rgba(232,33,39,.06)", fill: true, tension: .35,
        pointRadius: 0, pointHitRadius: 12, pointHoverRadius: 4,
        pointBackgroundColor: "#e82127" }] },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: (c) => ` ${fmt(c.parsed.y, 0)} Wh/km` } } },
        scales: {
          x: { grid: { display: false }, border: { display: false }, ticks: { maxTicksLimit: 8 } },
          y: { border: { display: false }, grid: { color: GRID }, ticks: { maxTicksLimit: 6 } },
        } },
    });
  }

  if (drv.available) {
    const sb = drv.distance_by_speed_band;
    makeChart("speedBandChart", barConfig(Object.keys(sb), Object.values(sb),
      "km", "#22c55e", "km"));

    // Trip-count bars with an average-efficiency line overlaid on its own
    // scale — shows commute rhythm and whether particular hours (hot
    // afternoons, cold mornings) run less efficiently, in one chart.
    const th = drv.trips_by_hour;
    const eh = drv.efficiency_by_hour || {};
    makeChart("tripsHourChart", {
      data: {
        labels: Object.keys(th).map(h => h + "h"),
        datasets: [
          { type: "bar", label: "Trips", data: Object.values(th), yAxisID: "y",
            backgroundColor: "#f59e0b", hoverBackgroundColor: "#f59e0bcc",
            borderRadius: 6, maxBarThickness: 44 },
          { type: "line", label: "Wh/km", data: Object.keys(th).map(h => eh[h] ?? null),
            yAxisID: "y1", borderColor: "#e82127", borderWidth: 2, tension: .35,
            spanGaps: true, pointRadius: 0, pointHitRadius: 12, pointHoverRadius: 4,
            pointBackgroundColor: "#e82127" },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: true, position: "bottom",
            labels: { usePointStyle: true, boxWidth: 8, boxHeight: 8, padding: 16 } },
          tooltip: { callbacks: { label: (c) =>
            c.dataset.yAxisID === "y1"
              ? (c.parsed.y == null ? " No energy data" : ` ${fmt(c.parsed.y, 0)} Wh/km`)
              : ` ${fmt(c.parsed.y, 0)} trips` } },
        },
        scales: {
          x: { grid: { display: false }, border: { display: false } },
          y: { beginAtZero: true, border: { display: false }, grid: { color: GRID },
            ticks: { maxTicksLimit: 6 } },
          y1: { beginAtZero: true, position: "right", border: { display: false },
            grid: { display: false }, ticks: { maxTicksLimit: 6 } },
        },
      },
    });
  }

  if (chg.available) {
    const acdcTotal = (chg.ac_energy_kwh || 0) + (chg.dc_energy_kwh || 0);
    makeChart("acdcChart", {
      type: "doughnut",
      data: { labels: ["AC (home/dest)", "DC (fast)"],
        datasets: [{ data: [chg.ac_energy_kwh, chg.dc_energy_kwh],
          backgroundColor: ["#22c55e", "#e82127"],
          borderColor: "#171b22", borderWidth: 3, hoverOffset: 6 }] },
      options: { responsive: true, maintainAspectRatio: false, cutout: "62%",
        plugins: { legend: { position: "bottom",
          labels: { usePointStyle: true, boxWidth: 8, boxHeight: 8, padding: 16 } },
          tooltip: { callbacks: { label: (c) =>
            ` ${fmt(c.parsed, 0)} kWh (${acdcTotal ? Math.round(100 * c.parsed / acdcTotal) : 0}%)` } } } },
    });

    const st = chg.end_soc_targets;
    const hasSoc = Object.keys(st).some((k) => +k > 0); // exports without SoC -> all "0"
    showCard("socTargetChart", hasSoc);
    if (hasSoc) {
      makeChart("socTargetChart", barConfig(Object.keys(st).map(s => s + "%"),
        Object.values(st), "sessions", "#3b82f6", "sessions"));
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

// Plain-language reason for a trip's condition tags, from its own numbers.
function tripConditionWhy(t) {
  const avg = t.avg_speed_kmh || 0;
  const bits = [
    `Inferred from this trip's own data — average speed ${avg} km/h` +
    (t.duration_min ? `, ${t.duration_min} min over ${t.distance_km} km` : "") + ".",
    "Highway cruise = sustained high speed; highway + congestion = high top " +
    "speed but low average; stop-go = low average with spiky peaks; " +
    "city = low steady speed. Peak hour (7–9am, 5–7pm) and hot (33°C+) are " +
    "added as context.",
  ];
  return bits.join("<br>");
}

function renderLists(d) {
  const rated = (d.efficiency && d.efficiency.rated_wh_per_km) || 150;
  const recent = d.driving.recent_trips || [];
  const trips = recent
    .map((t, i) => {
      const when = t.end_time
        ? `${tripWhen(t.start_time)} → ${tripEnd(t.start_time, t.end_time)}`
        : tripWhen(t.start_time);
      // Only show "max" when a live mid-drive reading actually beat the average
      // (otherwise max is just floored to the average and reads as duplicate).
      const speed = t.avg_speed_kmh
        ? ` · avg ${t.avg_speed_kmh}${t.max_speed_kmh > t.avg_speed_kmh ? " · max " + t.max_speed_kmh : ""} km/h`
        : "";
      const score = t.eco_score != null
        ? `<span class="trip-score tone-${scoreTone(t.eco_score)}">${t.eco_score}</span>` : "";
      // Propulsion-only figures (idle/AC stripped) shown next to the gross
      // total when meaningfully lower — ≈ Tesla's "Driving" energy-breakdown
      // line. (The gross total is what matches Tesla's "Current Drive", which
      // includes climate/idle.)
      const hasDrive = t.driving_wh_per_km != null && t.wh_per_km != null
        && t.driving_wh_per_km < t.wh_per_km - 3;
      const driveKwh = (hasDrive && t.driving_energy_kwh != null)
        ? `${t.driving_energy_kwh} kWh / ` : "";
      const drv = hasDrive ? ` · drive ≈${driveKwh}${t.driving_wh_per_km} Wh/km` : "";
      const kwh = t.energy_kwh != null ? ` · ${t.energy_kwh} kWh` : "";
      const whkm = t.wh_per_km != null ? ` · ${t.wh_per_km} Wh/km${drv}` : "";
      const soc = t.soc_used_pct != null ? ` · ${fmt(t.soc_used_pct, 1)}% battery` : "";
      const cost = t.cost != null ? ` · ${d.currency} ${fmt(t.cost, 2)}` : "";
      // Live directions link when the trip's raw endpoints were stored.
      const mapLink = t.map_url
        ? ` <a class="trip-map" href="${t.map_url}" target="_blank" rel="noopener" title="Open route in Google Maps">🗺</a>`
        : "";
      // "Name this place" pins: turn a trip's own start/end coords into a
      // reusable geofence (self-hosted only — needs real coords + a place
      // to persist against). A route like "12 Main St, George Town → Home"
      // reads as start → end, so the two pins sit either side of the arrow.
      const routeParts = t.route ? t.route.split(" → ") : [];
      const pin = (coords) => coords && !STATIC_MODE
        ? `<button class="place-pin" data-coords="${coords}" title="Name this place">📍</button>` : "";
      const routeHtml = t.route
        ? `${routeParts[0] || ""}${pin(t.start_coords)} → ${routeParts.slice(1).join(" → ")}${pin(t.end_coords)}`
        : "";
      // Data-quality badge: silent when the trip's figures are a real
      // measurement (the expected default); a small marker only when they're
      // a fallback estimate or genuinely unavailable, so a glance at the list
      // shows which trips to trust at face value.
      const dq = t.data_quality === "estimated"
        ? `<span class="dq-badge dq-estimated" title="Idle wasn't live-tracked for this trip — Wh/km uses a speed-based estimate, not a measured stop">≈</span>`
        : t.data_quality === "incomplete"
        ? `<span class="dq-badge dq-incomplete" title="No valid energy reading for this trip (a range-reading gap) — efficiency figures are unavailable">✕</span>`
        : "";
      const distFlag = t.distance_flag
        ? `<span class="dq-badge dq-warn" title="Logged distance is shorter than the straight-line distance between this trip's own start/end points — likely an odometer or GPS glitch">⚠</span>`
        : "";
      // In select mode, a checkbox precedes each trip (self-hosted only).
      const check = tripSelectMode && t.id != null
        ? `<input type="checkbox" class="trip-check" value="${t.id}" aria-label="Select trip" />` : "";
      const condId = `cond-why-${i}`;
      const cond = t.conditions
        ? `<span class="trip-cond">🚦 ${t.conditions}` +
          `<button class="info-btn" data-info="${condId}">!</button></span>` +
          `<span id="${condId}" class="info-pop hidden">${tripConditionWhy(t)}</span>`
        : "";
      // Work/personal category: a tap cycles untagged -> work -> personal ->
      // untagged. Self-hosted only (needs a real trip id to persist against).
      const tagChip = t.id != null && !tripSelectMode
        ? `<button class="trip-tag trip-tag-${t.tag || "none"}" data-trip-id="${t.id}" data-tag="${t.tag || ""}" title="Tap to set work/personal">` +
          `${t.tag === "work" ? "💼 Work" : t.tag === "personal" ? "🏠 Personal" : "+ tag"}</button>`
        : "";
      // Parked gap right before this trip, if it was long enough and
      // charge-free to count as vampire/standby drain (see
      // driving_analysis.vampire_drain()) — its own slim row rather than
      // crowding the trip's own meta line, since it happened *before* the
      // trip, not during it.
      const vb = t.vampire_before;
      const vampireNote = vb
        ? `<li class="vampire-note">🔋 Parked ${fmt(vb.hours, 0)} h · lost ${fmt(vb.pct, 1)}% (${fmt(vb.kwh, 1)} kWh) standby</li>`
        : "";
      return `${vampireNote}<li class="trip${tripSelectMode ? " selectable" : ""}">` +
        `<span class="trip-head">${check}${score}${dq}${distFlag}<span class="trip-route">${when}${t.route ? "<br>" + routeHtml : ""}${mapLink}</span></span>` +
        `<span class="trip-meta">${t.distance_km} km · ${t.duration_min} min${speed}${kwh}${whkm}${soc}${cost}${tagChip}</span>${cond}</li>`;
    })
    .join("");
  const list = document.getElementById("recentTrips");
  list.innerHTML = trips || '<li class="empty">No trips in this window</li>';
  wireInfoButtons(list);
  wireTagChips(list);
  wirePlacePins(list);
  // "Show more": every window (including since-charge) caps recent_trips
  // at 5 by default now — "current drive" is the only exception, always
  // just the one trip (see driving_analysis.analyze()'s recent_trips_limit),
  // so total_drives can never exceed what's shown there.
  const showMoreBtn = document.getElementById("show-more-trips");
  if (showMoreBtn) {
    const capped = document.getElementById("range").value !== "drive";
    const hasMore = capped && d.driving.total_drives > recent.length;
    showMoreBtn.classList.toggle("hidden", !hasMore);
    if (hasMore) {
      const remaining = d.driving.total_drives - recent.length;
      showMoreBtn.textContent = `Show ${Math.min(5, remaining)} more (${remaining} left)`;
    }
  }
  // Only offer the trip tools when there's a real (self-hosted) DB behind them.
  const tools = document.getElementById("trip-tools");
  if (tools) tools.classList.toggle("hidden", STATIC_MODE || !recent.some((t) => t.id != null));
  updateDeleteSelectedLabel();

  // Per-tag totals ("work" vs "personal" vs untagged), shown only once
  // something's actually been tagged.
  const byTag = d.driving.by_tag;
  const tagSummary = document.getElementById("tag-summary");
  if (tagSummary) {
    if (byTag) {
      const label = (k) => (k === "work" ? "💼 Work" : k === "personal" ? "🏠 Personal" : "Untagged");
      tagSummary.innerHTML = Object.entries(byTag).map(([k, v]) =>
        `<span>${label(k)}: ${v.distance_km} km` +
        `${v.cost != null ? ` · ${d.currency} ${fmt(v.cost, 2)}` : ""}</span>`).join("");
      tagSummary.style.display = "";
    } else {
      tagSummary.style.display = "none";
    }
  }

  const routes = (d.driving.top_routes || [])
    .map(([r, c]) => `<li><span>${r}</span><span class="count">${c}×</span></li>`).join("");
  document.getElementById("topRoutes").innerHTML =
    routes || '<li class="empty">No repeated routes yet</li>';

  const chargesEl = document.getElementById("recentCharges");
  if (chargesEl) {
    const recentCharges = d.charging.recent_charges || [];
    // The window's own boundary charge (e.g. "since charge") is otherwise
    // invisible below — it ended right at the window's start, so it's
    // excluded from recent_charges by definition. Pin it atop the list
    // instead of a separate card, skipping it when the list already has it
    // anywhere (recent_charges sorts by start time, last_charge by end
    // time, so "already included" isn't guaranteed to mean "first row").
    const lc = d.last_charge;
    const pinned = lc && !recentCharges.some((c) => c.id === lc.id) ? [lc] : [];
    const allCharges = [...pinned, ...recentCharges];
    const rows = allCharges.map((c) => chargeRowHtml(c, d.currency)).join("");
    chargesEl.innerHTML = rows || '<li class="empty">No charging sessions in this window</li>';
    wireEditRateButtons(chargesEl);
    // Only offer the charge tools when there's a real (self-hosted) DB behind them.
    const chargeTools = document.getElementById("charge-tools");
    if (chargeTools) {
      chargeTools.classList.toggle("hidden", STATIC_MODE || !allCharges.some((c) => c.id != null));
    }
    updateDeleteSelectedChargesLabel();
  }
}

// TNB residential Time-of-Use, all-in per-kWh (energy + network + capacity
// charges; excludes the flat monthly retail charge and ICPT surcharge,
// which aren't assignable to one session) — weekday 2pm-10pm is peak,
// everything else (incl. all of Sat/Sun/public holidays) is off-peak, same
// shape as tariff.price_at() server-side. A quick-fill suggestion, not an
// authoritative bill calculation — Home still lets you adjust before saving.
// Cached /api/pricing-prefs response (Public/Home/Office x AC/DC rates +
// default source) — fetched once by loadPricingPrefs(), read by both the
// Recent Charges row buttons and the Rates modal. Falls back to the same
// defaults the backend seeds a fresh install with, so the buttons still
// show a sane number before the first fetch resolves.
let pricingPrefs = null;
const PRICING_PREFS_FALLBACK = {
  rates: { public_ac: 1.10, public_dc: 1.40, home_ac: 0.90, home_dc: 1.13, office_ac: 0.57, office_dc: 0.57 },
  default_source: "public",
  updated_at: null,
};

const DEFAULT_SOURCE_ICONS = { public: "🌐", home: "🏠", office: "🏢" };
const DEFAULT_SOURCE_ORDER = ["public", "home", "office"];

// Reflects the current default source on the quick-switch button beside the
// Recent Charges title — called after every fetch/save of pricingPrefs so
// it never drifts from what the Rates modal (or this same button) last set.
function renderDefaultSourceBtn() {
  const btn = document.getElementById("btn-default-source");
  if (!btn) return;
  const source = (pricingPrefs || PRICING_PREFS_FALLBACK).default_source || "public";
  btn.textContent = DEFAULT_SOURCE_ICONS[source];
  const label = source.charAt(0).toUpperCase() + source.slice(1);
  btn.title = `Default source for new charges: ${label} — tap to change`;
}

async function loadPricingPrefs() {
  if (STATIC_MODE) return;
  try {
    pricingPrefs = await (await fetch("/api/pricing-prefs")).json();
  } catch (e) { /* keep whatever's cached; callers fall back below */ }
  renderDefaultSourceBtn();
}

// Quick-switch button beside "Recent Charges": tap to cycle Public → Home →
// Office as the default source, without opening the Rates modal — same
// effect as its ★ toggles, just one tap away from where you'd notice a
// mispriced session.
function setupDefaultSourceButton() {
  const btn = document.getElementById("btn-default-source");
  if (!btn) return;
  btn.classList.remove("hidden");
  renderDefaultSourceBtn();
  btn.addEventListener("click", async () => {
    if (!pricingPrefs) await loadPricingPrefs();
    const prefs = pricingPrefs || PRICING_PREFS_FALLBACK;
    const current = prefs.default_source || "public";
    const next = DEFAULT_SOURCE_ORDER[(DEFAULT_SOURCE_ORDER.indexOf(current) + 1) % DEFAULT_SOURCE_ORDER.length];
    try {
      const resp = await fetch("/api/pricing-prefs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rates: prefs.rates, default_source: next }),
      });
      if (!resp.ok) throw new Error((await resp.json()).detail || "Failed");
      pricingPrefs = await resp.json();
      renderDefaultSourceBtn();
    } catch (e) { /* leave the button showing the last-known source */ }
  });
}

function quickRate(source, chargeType) {
  const rates = (pricingPrefs || PRICING_PREFS_FALLBACK).rates;
  return rates[`${source}_${chargeType === "DC" ? "dc" : "ac"}`];
}

// Icon + label for each of the four things a charge can be priced as. The
// three presets (Public/Home/Office) come with a configured rate; "Others"
// covers everything else — a promo, a one-off price, or just marking the
// session Free — and replaces what used to be a separate ✎ button, folded
// into the same row of icons instead of sitting apart from them.
const SOURCE_META = {
  public: ["🌐", "Public"],
  home: ["🏠", "Home"],
  office: ["🏢", "Office"],
  other: ["🏷️", "Others"],
};

// Which of Public/Home/Office/Others this session is currently priced
// against — a read-only indicator so the row's four source buttons double
// as a "this is a Home/Office/Public/Others session" selector, not just a
// set of suggestions. Prefers the backend's persisted c.source (set when
// the charge was first priced, and updated whenever one of the four
// buttons is used to fix one) so picking a different source and saving
// moves the highlight there right away. Falls back to guessing from
// location text only for a charge that predates that column — location
// doesn't drift the way a saved rate does, so that's still better than
// comparing against today's configured numbers.
function matchedSource(c) {
  if (c.is_free) return null;
  if (c.source) return c.source;
  const loc = (c.location || "").toLowerCase();
  if (loc.includes("office")) return "office";
  if (loc.includes("home")) return "home";
  return "public";
}

// One Recent Charges row — shared by the pinned "last charge" entry and
// every session in charging.recent_charges, so both look and behave
// identically (same buttons) instead of two different formats.
function chargeRowHtml(c, currency) {
  const when = `${tripWhen(c.start_time)} → ${tripEnd(c.start_time, c.end_time)}`;
  const loc = c.location ? `${c.location} · ${c.charge_type}` : c.charge_type;
  const kwh = `${fmt(c.energy_added_kwh, 1)} kWh`;
  const soc = c.start_soc != null && c.end_soc != null
    ? ` · ${fmt(c.start_soc)}% → ${fmt(c.end_soc)}%` : "";
  const cost = c.is_free ? "Free" : `${currency} ${fmt(c.cost, 2)}`;
  const rate = !c.is_free && c.rate_per_kwh != null ? ` (${fmt(c.rate_per_kwh, 2)}/kWh)` : "";
  // In select mode, a checkbox precedes each charge (self-hosted only) and
  // the pricing buttons step aside — same trade as the trip list's own
  // select mode, so a stray tap can't fire a price edit while deleting.
  const check = chargeSelectMode && c.id != null
    ? `<input type="checkbox" class="charge-check" value="${c.id}" aria-label="Select charge" />` : "";
  let buttons = "";
  if (!STATIC_MODE && c.id != null && !chargeSelectMode) {
    const escLoc = loc.replace(/"/g, "&quot;");
    // A home/office DC charger is unusual but real (an EVSE, a workplace fast
    // charger), so these apply to both types, not just AC. Whichever one
    // matches the session's current price is highlighted in bold color;
    // click any of the four to switch it (Others opens with the session's
    // own current rate and Free state, for a fully custom fix).
    const active = matchedSource(c);
    for (const source of ["public", "home", "office", "other"]) {
      const [icon, label] = SOURCE_META[source];
      const isOther = source === "other";
      const r = isOther ? (c.rate_per_kwh ?? "") : quickRate(source, c.charge_type);
      const title = isOther
        ? "Custom rate, or mark this session Free"
        : `${label} rate (${fmt(r, 2)}/kWh) — edit in Rates`;
      const sel = active === source ? " selected" : "";
      buttons += ` <button class="quick-rate-btn${sel}" data-charge-id="${c.id}" ` +
        `data-loc="${escLoc}" data-kwh="${c.energy_added_kwh}" data-quick-rate="${r}" ` +
        `data-source="${source}" data-is-free="${c.is_free}" data-start-time="${c.start_time}" ` +
        `title="${title}">${icon}</button>`;
    }
  }
  return `<li class="charge${chargeSelectMode && c.id != null ? " selectable" : ""}">` +
    `<span class="charge-main">${check}<span class="charge-loc">${loc}</span>` +
    `<span class="charge-when">${when}</span></span>` +
    `<span class="charge-figs">${kwh}${soc} · ${cost}${rate}${buttons}</span></li>`;
}

// TNB residential Time-of-Use, all-in per-kWh (energy + network + capacity
// charges; excludes the flat monthly retail charge and ICPT surcharge,
// which aren't assignable to one session) — weekday 2pm-10pm is peak,
// everything else (incl. all of Sat/Sun/public holidays) is off-peak.
// Parses the year/month/day/hour directly out of the naive "local time"
// string instead of `new Date(startTimeIso)`, since the timestamp has no
// timezone offset — letting the browser interpret it in *its own* zone
// would shift which hour (and sometimes which day) counts as peak whenever
// the phone checking this isn't in Malaysia's timezone.
const TNB_TOU_PEAK_RATE = 0.46;
const TNB_TOU_OFFPEAK_RATE = 0.42;
function tnbTouRate(startTimeIso) {
  const m = String(startTimeIso).match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (!m) return null;
  const [, y, mo, d, h] = m;
  const day = new Date(+y, +mo - 1, +d).getDay();   // 0=Sun..6=Sat
  const isWeekday = day >= 1 && day <= 5;
  const isPeak = isWeekday && +h >= 14 && +h < 22;
  return isPeak ? TNB_TOU_PEAK_RATE : TNB_TOU_OFFPEAK_RATE;
}

function openEditChargeModal(chargeId, loc, kwh, rate, isFree, source, startTime) {
  const form = document.getElementById("edit-charge-form");
  form.dataset.chargeId = chargeId;
  // Which source (public/home/office/other) this edit is attributed to, so
  // the row's selected-icon indicator follows the pick as soon as this is
  // saved, instead of only reacting to location text.
  form.dataset.source = source;
  form.dataset.startTime = startTime || "";
  const [icon, label] = SOURCE_META[source] || SOURCE_META.other;
  document.getElementById("edit-charge-title").textContent = `${icon} Fix charging cost — ${label}`;
  const freeCb = document.getElementById("edit-charge-free");
  const rateInput = document.getElementById("edit-charge-rate");
  const touRow = document.getElementById("edit-charge-tou-row");
  const touCb = document.getElementById("edit-charge-tou");
  freeCb.checked = isFree;
  rateInput.value = isFree ? "" : (rate ?? "");
  rateInput.disabled = isFree;
  touCb.checked = false;
  touRow.classList.toggle("hidden", source !== "home");
  document.getElementById("edit-charge-summary").textContent = `${loc} — ${kwh} kWh`;
  setStatus(document.getElementById("edit-charge-status"), "", "");
  openModal("edit-charge-modal");
}

// One toggle button per source (🌐/🏠/🏢/🏷️) on a Recent Charges row opens
// the same "Fix charging cost" modal, covering both ways a session's cost
// can be wrong: mark it free (FOC, e.g. a Tesla Destination Charger) or
// supply its actual RM/kWh rate. Public/Home/Office pre-fill their
// configured rate; Others (🏷️) pre-fills the session's own current rate
// and Free state, for a fully custom fix. Ticking Free disables the rate
// field, same interaction as the Add Historical Charge form's own toggle.
function wireEditRateButtons(root) {
  root.querySelectorAll(".quick-rate-btn[data-charge-id]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOther = btn.dataset.source === "other";
      openEditChargeModal(btn.dataset.chargeId, btn.dataset.loc, btn.dataset.kwh,
        btn.dataset.quickRate, isOther && btn.dataset.isFree === "true", btn.dataset.source,
        btn.dataset.startTime);
    });
  });
}

function setupEditChargeModal() {
  const form = document.getElementById("edit-charge-form");
  if (!form) return;
  const freeCb = document.getElementById("edit-charge-free");
  const rateInput = document.getElementById("edit-charge-rate");
  const touCb = document.getElementById("edit-charge-tou");
  freeCb.addEventListener("change", (e) => {
    rateInput.disabled = e.target.checked;
    if (e.target.checked) {
      rateInput.value = "";
      touCb.checked = false;   // free and a computed rate are mutually exclusive
    }
  });
  // Fills the rate box from TNB's Time-of-Use tariff at this charge's own
  // start time — a suggestion like the 🌐/🏠/🏢 quick-rate buttons, not a
  // lock: the field stays editable, and unticking just leaves the number
  // as-is rather than reverting it.
  touCb.addEventListener("change", (e) => {
    if (!e.target.checked) return;
    const touRate = tnbTouRate(form.dataset.startTime);
    if (touRate == null) return;
    freeCb.checked = false;
    rateInput.disabled = false;
    rateInput.value = touRate.toFixed(2);
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const statusEl = document.getElementById("edit-charge-status");
    const isFree = freeCb.checked;
    let rate = 0;
    if (!isFree) {
      rate = parseFloat(rateInput.value);
      if (!Number.isFinite(rate) || rate < 0) {
        setStatus(statusEl, "Enter a rate of 0 or more, or tick Free.", "err");
        return;
      }
    }
    try {
      const resp = await fetch("/api/charges/edit-rate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: +form.dataset.chargeId, price_per_kwh: rate, source: form.dataset.source || "",
        }),
      });
      if (!resp.ok) throw new Error((await resp.json()).detail || "Failed");
      closeModal("edit-charge-modal");
      load();   // refresh KPIs/lists so the corrected cost shows immediately
    } catch (err) {
      setStatus(statusEl, err.message, "err");
    }
  });
}

// "Rates" page: set Public/Home/Office AC & DC rates and the default source
// new charges fall back to when their location doesn't auto-match Home/Office.
function setupRatesModal() {
  const btn = document.getElementById("btn-rates");
  const form = document.getElementById("rates-form");
  if (!btn || !form) return;
  btn.classList.remove("hidden");

  const fields = {
    public_ac: document.getElementById("rate-public-ac"),
    public_dc: document.getElementById("rate-public-dc"),
    home_ac: document.getElementById("rate-home-ac"),
    home_dc: document.getElementById("rate-home-dc"),
    office_ac: document.getElementById("rate-office-ac"),
    office_dc: document.getElementById("rate-office-dc"),
  };
  const sourceInput = document.getElementById("rate-default-source");
  const starBtns = form.querySelectorAll(".default-star-btn");
  const statusEl = document.getElementById("rates-status");

  function setDefaultSource(source) {
    sourceInput.value = source;
    starBtns.forEach((s) => {
      const active = s.dataset.source === source;
      s.classList.toggle("selected", active);
      s.textContent = active ? "★" : "☆";
    });
  }
  starBtns.forEach((s) => s.addEventListener("click", () => setDefaultSource(s.dataset.source)));

  // No live TNB/public-charger rate feed exists to auto-refresh from (see
  // the link below the rates form), so this is a manual-review reminder
  // instead: when the numbers were last saved, standing in for a freshness
  // check the app can't actually perform on its own.
  function renderUpdatedNote(prefs) {
    const note = document.getElementById("rates-updated-note");
    if (!note) return;
    note.textContent = prefs.updated_at
      ? `Rates last updated: ${prefs.updated_at}`
      : "Rates have never been saved — showing built-in defaults.";
  }

  function populate() {
    const prefs = pricingPrefs || PRICING_PREFS_FALLBACK;
    for (const key in fields) fields[key].value = prefs.rates[key] ?? "";
    setDefaultSource(prefs.default_source || "public");
    renderUpdatedNote(prefs);
  }

  btn.addEventListener("click", async () => {
    setStatus(statusEl, "", "");
    if (!pricingPrefs) await loadPricingPrefs();
    populate();
    openModal("rates-modal");
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const rates = {};
    for (const key in fields) {
      const v = parseFloat(fields[key].value);
      if (!Number.isFinite(v) || v < 0) {
        setStatus(statusEl, "Enter a rate of 0 or more for every field.", "err");
        return;
      }
      rates[key] = v;
    }
    try {
      const resp = await fetch("/api/pricing-prefs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rates, default_source: sourceInput.value }),
      });
      if (!resp.ok) throw new Error((await resp.json()).detail || "Failed");
      pricingPrefs = await resp.json();
      renderDefaultSourceBtn();
      renderUpdatedNote(pricingPrefs);
      setStatus(statusEl, "Saved.", "ok");
      load();   // refresh Recent Charges so 🌐/🏠/🏢 suggestions reflect the new rates
    } catch (err) {
      setStatus(statusEl, err.message, "err");
    }
  });
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
  // Fleet benchmark: how this car's degradation compares to a typical pack
  // at the same mileage (rough aggregate-data yardstick, not per-VIN
  // precision — see fleet_degradation_pct's docstring). Hidden until an
  // odometer reading exists to anchor the comparison to.
  const fleet = b.vs_fleet_pct != null
    ? `<div class="bat-line">vs. typical pack at ${fmt(b.current_odo_km)} km ` +
      `(~${fmt(b.fleet_degradation_pct, 1)}% degradation): ` +
      `<strong class="${b.vs_fleet_pct <= 0 ? "tone-good" : "tone-bad"}">` +
      `${b.vs_fleet_pct > 0 ? "+" : ""}${fmt(b.vs_fleet_pct, 1)}pp` +
      `${b.vs_fleet_pct <= 0 ? " (better than typical)" : " (faster than typical)"}</strong></div>`
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
    ${fleet}
    ${habits}`;
  const btn = document.getElementById("batt-info-btn");
  if (btn) btn.addEventListener("click", () =>
    document.getElementById("batt-info").classList.toggle("hidden"));

  // Health as a curve: monthly median projected full range. Needs at least
  // two plottable months to be a trend; hidden until then.
  const trendEl = document.getElementById("battTrendChart");
  const trend = b.trend || [];
  if (trendEl) {
    trendEl.style.display = trend.length >= 2 ? "" : "none";
    if (trend.length >= 2) {
      makeChart("battTrendChart", {
        type: "line",
        data: { labels: trend.map((p) => p.month), datasets: [{
          label: "km", data: trend.map((p) => p.full_range_km),
          borderColor: "#2dd4bf", borderWidth: 2, tension: .3,
          backgroundColor: "rgba(45,212,191,.08)", fill: true,
          pointRadius: 2, pointBackgroundColor: "#2dd4bf" }] },
        options: { responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false },
            tooltip: { callbacks: { label: (c) => ` ${fmt(c.parsed.y)} km projected full range` } } },
          scales: {
            x: { grid: { display: false }, border: { display: false } },
            y: { border: { display: false }, grid: { color: GRID }, ticks: { maxTicksLimit: 5 } },
          } },
      });
    } else {
      destroy("battTrendChart");
    }
  }
}

// Tesla's API reports wheels by internal engineering names; show the
// marketing name people actually know (Helix19 -> "Nova 19″").
const WHEEL_MARKETING = {
  HELIX: "Nova", NOVA: "Nova", PHOTON: "Photon", PINWHEEL: "Photon",
  GEMINI: "Gemini", INDUCTION: "Induction", CROSSFLOW: "Crossflow",
  APOLLO: "Apollo", UBERTURBINE: "Überturbine", STILETTO: "Stiletto",
};
function prettyWheel(tok) {
  const m = String(tok || "").toUpperCase().match(/^([A-Z]+)(1[89]|2[012])/);
  if (!m) return tok;
  const name = WHEEL_MARKETING[m[1]]
    || m[1].charAt(0) + m[1].slice(1).toLowerCase();
  return `${name} ${m[2]}″`;
}

// The "!" popover: which car config the 100% reference was derived from.
function battInfoHtml(d) {
  const b = d.battery, v = d.vehicle || {};
  const realVin = v.vin && !/^(DEMO|IMPORT|LINKED)/.test(v.vin) ? v.vin : null;
  const badge = ((v.trim || "").match(/\b(P?\d+D?)\b/) || [])[1];
  const wheel = (v.trim || "").split(/\s+/).find((t) =>
    /^(nova|photon|pinwheel|gemini|induction|crossflow|uberturbine|apollo|turbine|helix|arachnid|cyberstream)/i.test(t));
  const carLine = [v.year, v.model, badge && `(${badge})`].filter(Boolean).join(" ");
  const wheelTxt = wheel
    ? `${prettyWheel(wheel)} <span style="opacity:.7">(reported as ${wheel})</span>`
    : "not reported yet — tap Sync while the car is awake";
  const rows = [
    realVin ? `VIN: <strong>${realVin}</strong>` : null,
    carLine ? `Car: <strong>${carLine}</strong>` : null,
    `Wheels: <strong>${wheelTxt}</strong>`,
    b.new_range_km
      ? `When-new 100% range for this config: <strong>${b.new_range_km} km</strong> (EPA)`
      : "When-new range unknown for this variant — using your best readings instead",
  ];
  // Step-by-step computation of the estimate and health.
  const band = b.est_soc_band ? ` (SoC ${b.est_soc_band}${b.reliable_band ? "" : ", low-SoC fallback"})` : "";
  const comp = [
    `<strong>How it's computed</strong>`,
    `1. Each sync projects full range = <strong>rated range ÷ (SoC ÷ 100)</strong>.`,
    `2. Estimated full range = <strong>median of the last ${b.est_from_n || 0} projections</strong>${band} = <strong>${b.est_full_range_km} km</strong>.`,
    `3. 100% reference = ${b.reference === "factory spec"
      ? `factory when-new <strong>${b.reference_km} km</strong>`
      : `your best-seen <strong>${b.baseline_full_range_km} km</strong>`}.`,
    `4. Health = ${b.est_full_range_km} ÷ ${b.reference_km} = <strong>${b.health_pct}%</strong> (${b.degradation_pct}% degradation).`,
  ];
  return [...rows.filter(Boolean), `<div class="bat-comp">${comp.join("<br>")}</div>`]
    .map((r) => (r.startsWith("<div") ? r : `<div>${r}</div>`)).join("");
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
// How many trips Recent Trips shows, for any window — the "Show more"
// button bumps this and reloads; the window-change handler resets it back
// to 5 so switching windows always starts collapsed again. Meaningless for
// "current drive" (always just the one trip — see driving_analysis.
// analyze()'s recent_trips_limit), so left at its default there; unused.
let recentTripsLimit = 5;

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
      d = TA.buildSummary(ds, currentDrive ? "drive" : (sinceCharge ? "charge" : days),
        { tripsLimit: recentTripsLimit });
      mode = ds.source === "imported" ? "imported" : "demo";
    } else {
      const extra = currentDrive ? "&current_drive=1" : (sinceCharge ? "&since_charge=1" : "");
      // current_drive is always just the one trip — no cap to raise there.
      const tripsExtra = !currentDrive ? `&trips_limit=${recentTripsLimit}` : "";
      const res = await fetch(`/api/summary?days=${days}${extra}${tripsExtra}`);
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
    // (The trip tools' visibility is handled in renderLists, once trips load.)

    // Show the background cron's last-known status immediately from what's
    // already in the database — before any fresh Tesla call of our own even
    // starts. With an external cron pinging every minute or so, this is
    // near-live without the page itself having to reach Tesla first.
    if (!STATIC_MODE && mode === "live") {
      if (d.last_status) {
        renderLastStatus(d.last_status);
      } else {
        // Live mode but never once synced — the cron isn't configured/
        // reaching this app at all, not just stale. Distinct from the
        // "was working, now stopped" case above.
        setSyncStatus("", "⚠️ No sync data yet — set up your cron job to ping "
          + "/api/sync, or tap Sync below.", "err");
      }
    }

    // Live mode: reveal the Sync button. The dashboard itself never pings
    // Tesla on its own any more — it's a pure read-only view of Neon (see
    // renderLastStatus above); the external cron is the only thing that
    // talks to the car, plus this button for an explicit manual check.
    const syncBtn = document.getElementById("btn-sync");
    if (syncBtn) syncBtn.classList.toggle("hidden", STATIC_MODE || mode !== "live");

    const v = d.vehicle;
    const realVin = v.vin && !/^(DEMO|IMPORT|LINKED)/.test(v.vin) ? `VIN ${v.vin}` : null;
    // Compact one-line description: name · year model badge · VIN. Colour and
    // wheel live in the battery "!" panel, so they're dropped here to fit.
    // Keep the badge and wheel (wheel drives the battery reference); drop the
    // paint colour so the VIN fits in two lines. Wheel shown by marketing name
    // (Helix19 -> Nova 19″). Full colour is in the battery "!" panel.
    const trimTxt = (v.trim || "").split(/\s+/)
      .filter((t) => /^P?\d+D?$/i.test(t) || /(1[89]|2[012])/.test(t))
      .map(prettyWheel).join(" ");
    const sub = document.getElementById("subtitle");
    sub.textContent = [v.name, [v.year, v.model, trimTxt].filter(Boolean).join(" ")]
      .filter(Boolean).join(" · ");
    if (realVin) {
      // Keep "VIN <number> !" together so it wraps onto the last line as a unit;
      // the "!" opens the full car-info panel.
      sub.appendChild(document.createTextNode(" · "));
      const span = document.createElement("span");
      span.className = "nowrap";
      span.textContent = realVin + " ";
      const info = document.createElement("button");
      info.className = "info-btn";
      info.textContent = "!";
      info.title = "Full car info";
      info.addEventListener("click", (e) => {
        e.stopPropagation();
        document.getElementById("car-info").classList.toggle("hidden");
      });
      span.appendChild(info);
      sub.appendChild(span);
    }
    fillCarInfo(d.vehicle);

    lastData = d;
    renderScore(d);
    renderKpis(d);
    renderWeekCompare(d);
    renderCharts(d);
    renderBattery(d);
    renderLists(d);
    renderInsights(d);
    renderNarrative(d);
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
  recentTripsLimit = 5;   // switching windows always starts Recent Trips collapsed again
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

// "Show more" trips: bump the cap by 5 and reload (see renderLists()'s
// visibility logic — hidden only for "current drive", which is always
// just the one trip).
document.getElementById("show-more-trips")?.addEventListener("click", () => {
  recentTripsLimit += 5;
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

// Setup guide: step-by-step for a brand-new user (Neon → Render → Tesla dev
// account → link → auto-sync → install). Pure static content, works offline.
document.getElementById("btn-guide")?.addEventListener("click", () =>
  openModal("setup-modal"));

// Trip tools (self-hosted only): select individual trips to delete, or clear
// all. "Select" reveals a checkbox on each trip; "Delete selected" removes the
// ticked ones. Charging history and battery-health readings are always kept.
let tripSelectMode = false;

function setSelectMode(on) {
  tripSelectMode = on;
  const show = (id, vis) => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle("hidden", !vis);
  };
  show("select-trips", !on);
  show("clear-trips", !on);
  show("delete-selected", on);
  show("cancel-select", on);
  renderLists(lastData || {});
}

function updateDeleteSelectedLabel() {
  const btn = document.getElementById("delete-selected");
  if (!btn) return;
  const n = document.querySelectorAll(".trip-check:checked").length;
  btn.textContent = n ? `Delete selected (${n})` : "Delete selected";
  btn.disabled = !n;
}

document.getElementById("recentTrips")?.addEventListener("change", (e) => {
  if (e.target.classList.contains("trip-check")) updateDeleteSelectedLabel();
});

document.getElementById("select-trips")?.addEventListener("click", () => setSelectMode(true));
document.getElementById("cancel-select")?.addEventListener("click", () => setSelectMode(false));

document.getElementById("delete-selected")?.addEventListener("click", async () => {
  const ids = [...document.querySelectorAll(".trip-check:checked")].map((c) => +c.value);
  if (!ids.length) return;
  if (!confirm(`Delete ${ids.length} selected trip(s)?\n\nThis cannot be undone.`)) return;
  const btn = document.getElementById("delete-selected");
  btn.disabled = true; btn.textContent = "Deleting…";
  try {
    const res = await fetch("/api/data/delete-drives", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "Could not delete trips");
    tripSelectMode = false;
    setSelectMode(false);
    await load();
  } catch (e) {
    alert(e.message);
    btn.disabled = false;
  }
});

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
      clearTripsBtn.textContent = "🗑 Clear all";
    }
  });
}

// Charge tools (self-hosted only): same select/delete/clear-all pattern as
// the trip tools above, just wired against Recent Charges instead. Trip and
// battery-health history are always kept.
let chargeSelectMode = false;

function setChargeSelectMode(on) {
  chargeSelectMode = on;
  const show = (id, vis) => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle("hidden", !vis);
  };
  show("select-charges", !on);
  show("clear-charges", !on);
  show("delete-selected-charges", on);
  show("cancel-select-charges", on);
  renderLists(lastData || {});
}

function updateDeleteSelectedChargesLabel() {
  const btn = document.getElementById("delete-selected-charges");
  if (!btn) return;
  const n = document.querySelectorAll(".charge-check:checked").length;
  btn.textContent = n ? `Delete selected (${n})` : "Delete selected";
  btn.disabled = !n;
}

document.getElementById("recentCharges")?.addEventListener("change", (e) => {
  if (e.target.classList.contains("charge-check")) updateDeleteSelectedChargesLabel();
});

document.getElementById("select-charges")?.addEventListener("click", () => setChargeSelectMode(true));
document.getElementById("cancel-select-charges")?.addEventListener("click", () => setChargeSelectMode(false));

document.getElementById("delete-selected-charges")?.addEventListener("click", async () => {
  const ids = [...document.querySelectorAll(".charge-check:checked")].map((c) => +c.value);
  if (!ids.length) return;
  if (!confirm(`Delete ${ids.length} selected charging session(s)?\n\nThis cannot be undone.`)) return;
  const btn = document.getElementById("delete-selected-charges");
  btn.disabled = true; btn.textContent = "Deleting…";
  try {
    const res = await fetch("/api/data/delete-charges", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "Could not delete charges");
    chargeSelectMode = false;
    setChargeSelectMode(false);
    await load();
  } catch (e) {
    alert(e.message);
    btn.disabled = false;
  }
});

const clearChargesBtn = document.getElementById("clear-charges");
if (clearChargesBtn) {
  clearChargesBtn.addEventListener("click", async () => {
    if (!confirm("Delete ALL recorded charging sessions?\n\nTrip and battery-health " +
                 "history are kept. This cannot be undone.")) return;
    clearChargesBtn.disabled = true;
    clearChargesBtn.textContent = "Clearing…";
    try {
      const res = await fetch("/api/data/clear-charges", { method: "POST" });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || "Could not clear charges");
      await load();
    } catch (e) {
      alert(e.message);
    } finally {
      clearChargesBtn.disabled = false;
      clearChargesBtn.textContent = "🗑 Clear all";
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
    setTimeout(() => { closeModal("import-modal"); showHome(); }, 600);
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
    setTimeout(() => { closeModal("import-modal"); showHome(); }, 800);
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
    setTimeout(() => { closeModal("link-modal"); showHome(); }, 900);
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

// "42s ago" / "5m ago" / "2h ago" from a unix-seconds timestamp.
function relTime(ts) {
  if (!ts) return "";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 90) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m}m ago`;
  return `${Math.round(m / 60)}h ago`;
}

// Paint the background cron's last-known status (persisted every /api/sync
// tick, including "asleep") straight from /api/summary — no Tesla call of
// our own; this is the dashboard's only view of car status now that the
// page never pings Tesla itself. A manual Sync (below) overwrites this with
// a fresher line once its own check completes — or, if that fails, falls
// back to re-showing this cached copy rather than losing it to a bare error.
let lastStatusCache = null;
function renderLastStatus(ls, note) {
  if (!ls || !ls.status) return;
  lastStatusCache = ls;
  // last_status.ts is refreshed by /api/sync every cron tick regardless of
  // whether the car itself was reachable — so a large gap here means the
  // cron has stopped firing, or something is failing before it can even
  // record a status (e.g. a database write failure), not that the car has
  // been busy. Surface this distinctly rather than quietly showing an old
  // status as if it were current.
  if (ls.stale) {
    const batt = ls.soc != null ? `🔋 ${Math.round(ls.soc)}% (last known)` : "";
    setSyncStatus(batt, `⚠️ No sync update in over ${relTime(ls.ts).replace(" ago", "")} — `
      + `check your cron job / Render deploy.${note ? " · " + note : ""}`, "err");
    return;
  }
  const label = {
    charging: "⚡ Charging",
    driving: `🚗 Driving${ls.speed_kmh ? " · " + Math.round(ls.speed_kmh) + " km/h" : ""}`,
    stopped: "🚗 Trip in progress — stopped briefly",
    parked: "🅿️ Parked",
    asleep: "😴 Asleep",
  }[ls.status] || ls.status;
  const batt = ls.soc != null ? `🔋 ${Math.round(ls.soc)}%` : "";
  const kind = ls.status === "asleep" ? "warn" : "ok";
  setSyncStatus(batt, `${label} · as of ${relTime(ls.ts)}${note ? " · " + note : ""}`, kind);
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
    // Parse defensively: a proxy/5xx can return non-JSON, which must not surface
    // as a scary "Unexpected token" error.
    let body = {};
    try { body = await res.json(); } catch (_) { body = {}; }
    if (!res.ok) throw new Error(body.detail || `Sync unavailable (${res.status}) — try again shortly`);
    // Keep the fallback cache fresh with this live result, so a *later*
    // failed check still has something better than a bare error to fall
    // back on than the page-load snapshot from Neon.
    lastStatusCache = {
      status: body.status, ts: Date.now() / 1000,
      soc: body.soc ?? (body.last && body.last.soc),
      odo_km: body.odo_km, speed_kmh: body.speed_kmh,
    };
    if (body.status === "asleep") {
      const batt = body.last && body.last.soc
        ? `🔋 ${Math.round(body.last.soc)}% (last known)` : "";
      setSyncStatus(batt, body.tried_wake
        ? "😴 Couldn't wake the car — it may be offline. Try again in a minute."
        : "😴 Car asleep — tap Sync to wake it, or sync after a drive.", "warn");
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
    // A failed live check shouldn't erase perfectly good info we already
    // have from Neon (this tab's last load, or an earlier successful sync
    // this visit) — re-show it with a note instead of a bare error.
    if (lastStatusCache) {
      renderLastStatus(lastStatusCache, `⚠️ live check failed: ${e.message}`);
    } else {
      setSyncStatus("", e.message, "err");
    }
  } finally {
    syncBusy = false;
  }
}
const syncBtnEl = document.getElementById("btn-sync");
if (syncBtnEl) syncBtnEl.addEventListener("click", () => syncNow(true));

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

// Printable summary report: build a clean report DOM from the data already
// on screen, then hand it to the browser's print flow (Save as PDF on
// phones). Print CSS shows only the report while printing.
function buildReport(d) {
  const drv = d.driving || {}, chg = d.charging || {}, eff = d.efficiency || {};
  const b = d.battery || {}, v = d.vehicle || {}, cur = d.currency || "";
  const bal = d.battery_balance;
  const windowText = d.window_label || `last ${d.window_days} days`;
  const row = (k, val) => val != null && val !== ""
    ? `<tr><td>${k}</td><td>${val}</td></tr>` : "";
  const trips = (drv.recent_trips || []).slice(0, 5).map((t) =>
    `<tr><td>${tripWhen(t.start_time)}</td><td>${t.distance_km} km</td>` +
    `<td>${t.wh_per_km != null ? t.wh_per_km + " Wh/km" : "—"}</td>` +
    `<td>${t.cost != null ? cur + " " + fmt(t.cost, 2) : "—"}</td></tr>`).join("");
  // Same kWh figure as the Avg Efficiency KPI card (see renderKpis) —
  // eff.total_energy_kwh, the exact number efficiency_analysis.analyze()
  // divided to get the Avg efficiency Wh/km row right below this one, so
  // the two always multiply out exactly against each other.
  const repUsedKwh = drv.available ? (eff.total_energy_kwh ?? null) : null;
  const repUsedPct = drv.available && bal && bal.full_charge_kwh > 0 && repUsedKwh != null
    ? repUsedKwh / bal.full_charge_kwh * 100 : null;
  // km per 1% battery: deliberately the GROSS % (trip + idle), same
  // ground-truth total as Battery Used when available — see renderKpis'
  // km / 1% Battery card for the matching fix/reasoning.
  const repSocPct = drv.available ? (bal && bal.used_pct != null ? bal.used_pct : drv.soc_used_pct) : null;
  const repKmPerSoc = repSocPct != null && repSocPct >= 0.2 && drv.total_distance_km
    ? drv.total_distance_km / repSocPct : null;
  return `
    <h1>Tesla Analyzer — ${windowText}</h1>
    <p class="rep-sub">${[v.year, v.model, v.name].filter(Boolean).join(" · ")}
      · generated ${footerDateFmt.format(new Date())}</p>
    <h2>Driving</h2>
    <table>${
      row("Distance", drv.available ? fmt(drv.total_distance_km) + " km · " + fmt(drv.total_drives) + " drives" : null)
    }${row("Energy used", repUsedKwh != null
      ? fmt(repUsedKwh, 1) + " kWh" + (repUsedPct != null ? " (" + (repUsedPct <= 100
          ? fmt(repUsedPct, 1) + "% battery" : "≈ " + fmt(repUsedPct / 100, 1) + " full charges") + ")" : "")
      : null)
    }${row("Avg efficiency", eff.avg_efficiency_wh_per_km ? fmt(eff.avg_efficiency_wh_per_km) + " Wh/km" : null)
    }${row("Driving cost", drv.total_cost != null ? cur + " " + fmt(drv.total_cost, 2) + (drv.cost_per_km != null ? " (" + cur + " " + fmt(drv.cost_per_km, 3) + "/km)" : "") : null)
    }${row("km per 1% battery", repKmPerSoc ? fmt(repKmPerSoc, 1) + " km" : null)}</table>
    <h2>Charging</h2>
    <table>${
      row("Energy added", chg.available ? fmt(chg.total_energy_kwh, 1) + " kWh · " + fmt(chg.total_sessions) + " sessions" : null)
    }${row("Cost", chg.total_cost ? cur + " " + fmt(chg.total_cost, 2) + (chg.cost_per_100km != null ? " (" + cur + " " + fmt(chg.cost_per_100km, 2) + "/100km)" : "") : null)
    }${row("AC / DC split", chg.available ? fmt(chg.ac_energy_kwh, 0) + " / " + fmt(chg.dc_energy_kwh, 0) + " kWh" : null)}</table>
    <h2>Battery Health</h2>
    <table>${
      row("Health", b.available ? fmt(b.health_pct, 1) + "% (" + fmt(b.degradation_pct, 1) + "% degradation)" : null)
    }${row("Est. full range", b.available ? fmt(b.est_full_range_km) + " km vs " + fmt(b.reference_km) + " km reference" : null)}</table>
    ${trips ? `<h2>Recent Trips</h2>
    <table><tr><th>When</th><th>Distance</th><th>Efficiency</th><th>Cost</th></tr>${trips}</table>` : ""}`;
}

const reportBtn = document.getElementById("btn-report");
if (reportBtn) reportBtn.addEventListener("click", () => {
  if (!lastData) return;
  const holder = document.getElementById("print-report");
  holder.innerHTML = buildReport(lastData);
  window.print();
});

// Pull-to-refresh: drag down from the top of the page and release to reload
// the app (fresh data AND the newest deployed version, thanks to the
// network-first service worker). iOS standalone PWAs have no native one.
(function setupPullToRefresh() {
  if (!("ontouchstart" in window)) return;
  const el = document.createElement("div");
  el.id = "ptr";
  el.textContent = "↓ Pull down to refresh";
  document.body.appendChild(el);
  const THRESHOLD = 80;
  let startY = null, armed = false;
  window.addEventListener("touchstart", (e) => {
    const modalOpen = document.querySelector(".modal:not(.hidden)");
    startY = (!modalOpen && window.scrollY <= 0) ? e.touches[0].clientY : null;
    armed = false;
  }, { passive: true });
  window.addEventListener("touchmove", (e) => {
    if (startY === null) return;
    const dy = e.touches[0].clientY - startY;
    if (dy > 20) {
      el.classList.add("show");
      armed = dy >= THRESHOLD;
      el.textContent = armed ? "↻ Release to refresh" : "↓ Pull down to refresh";
    } else {
      el.classList.remove("show");
      armed = false;
    }
  }, { passive: true });
  window.addEventListener("touchend", () => {
    if (startY !== null && armed) {
      el.textContent = "Refreshing…";
      window.location.reload();
    } else {
      el.classList.remove("show");
    }
    startY = null;
    armed = false;
  });
})();

// Register the service worker so the app installs and works offline on iOS.
// Self-hosted serves it at /sw.js (root scope); the static Pages build serves
// it next to index.html (relative scope under the project subpath).
if ("serviceWorker" in navigator) {
  const swUrl = STATIC_MODE ? "./sw.js" : "/sw.js";
  // Auto-reload once when a NEW service worker takes over, so a fresh deploy's
  // updated JS/CSS is actually applied instead of the bundle already running
  // in this tab. Without this, a new version installs and activates in the
  // background but the *visible* page keeps its old code until the user
  // happens to reload again by hand — which is exactly why an updated KPI can
  // look "not deployed" long after it shipped. Only armed when a controller
  // already exists (i.e. this is an update, not the first-ever install, where
  // claiming control is normal and no reload is wanted), and guarded so it can
  // fire at most one reload, never a loop.
  if (navigator.serviceWorker.controller) {
    let reloading = false;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (reloading) return;
      reloading = true;
      window.location.reload();
    });
  }
  window.addEventListener("load", () =>
    // update() forces an immediate check for a new sw.js rather than waiting
    // for the browser's own periodic poll, so updates land on the next load.
    navigator.serviceWorker.register(swUrl)
      .then((reg) => reg.update().catch(() => {}))
      .catch(() => {})
  );
}

// Web push: needs the self-hosted backend (subscriptions are stored there
// and it's what sends the pushes), so the button stays hidden in the
// on-device/GitHub Pages build, same as the Tesla-account link button.
function urlBase64ToUint8Array(base64) {
  const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  const raw = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function setupPushButton() {
  const btn = document.getElementById("btn-notify");
  if (!btn || STATIC_MODE || !("serviceWorker" in navigator) || !("PushManager" in window)) return;

  let vapidKey;
  try {
    const resp = await fetch("/api/push/vapid-public-key");
    if (!resp.ok) return;   // not configured server-side — stay hidden
    vapidKey = (await resp.json()).key;
  } catch (e) {
    return;
  }

  btn.classList.remove("hidden");
  const reg = await navigator.serviceWorker.ready;

  async function refresh() {
    const sub = await reg.pushManager.getSubscription();
    const on = !!sub && Notification.permission === "granted";
    btn.textContent = on ? "🔔 Notifications on" : "🔕 Enable notifications";
    btn.title = on ? "Tap to turn off charge/battery alerts on this device"
                   : "Get alerts here for charge complete and low battery";
    return sub;
  }

  let current = await refresh();

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      if (current) {
        await current.unsubscribe();
        await fetch("/api/push/unsubscribe", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint: current.endpoint }),
        });
      } else {
        const permission = await Notification.requestPermission();
        if (permission !== "granted") { btn.disabled = false; return; }
        const sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(vapidKey),
        });
        await fetch("/api/push/subscribe", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(sub.toJSON()),
        });
      }
    } catch (e) {
      // Permission denied, or the browser/OS blocked it — leave the button
      // as-is rather than claim a state that didn't actually take.
    }
    current = await refresh();
    btn.disabled = false;
  });
}
if (!STATIC_MODE) {
  window.addEventListener("load", () => setupPushButton().catch(() => {}));
}

// Named places (Home/Office geofences): needs the self-hosted backend to
// persist against, same as push notifications and the Tesla-account link
// button, so the whole feature stays hidden in the static/demo build.
async function savePlace(coords, name) {
  const [lat, lon] = coords.split(",").map((s) => parseFloat(s.trim()));
  if (!isFinite(lat) || !isFinite(lon) || !name || !name.trim()) return false;
  try {
    const resp = await fetch("/api/places", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), lat, lon, radius_km: 0.15 }),
    });
    return resp.ok;
  } catch (e) {
    return false;
  }
}

// A pin's coords come straight from a logged trip, so no prompt is needed
// for the *location* — just the name — matching the tag chip's one-tap feel.
function wirePlacePins(root) {
  root.querySelectorAll(".place-pin[data-coords]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const name = window.prompt("Name this place (e.g. Home, Office):", "");
      if (!name || !name.trim()) return;
      // Naming a place relabels every matching trip, not just this one —
      // a full reload (not a cached re-render) picks all of them up.
      if (await savePlace(btn.dataset.coords, name)) load();
    });
  });
}

async function renderPlacesList() {
  const listEl = document.getElementById("places-list");
  listEl.innerHTML = '<li class="places-empty">Loading…</li>';
  let places = [];
  try {
    places = await (await fetch("/api/places")).json();
  } catch (e) {
    // leave the loading message — the modal is still usable to add a place
  }
  if (!places.length) {
    listEl.innerHTML = '<li class="places-empty">No named places yet — tap 📍 next to '
      + 'a trip below, or "Use my location" here.</li>';
    return;
  }
  listEl.innerHTML = places.map((p) =>
    `<li><span>${p.name}<span class="place-meta"> · ${p.radius_km} km radius</span></span>` +
    `<button class="place-del" data-id="${p.id}" title="Remove this place">✕</button></li>`
  ).join("");
  listEl.querySelectorAll(".place-del").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await fetch(`/api/places/${btn.dataset.id}`, { method: "DELETE" });
      renderPlacesList();
      load();   // trips this place used to relabel go back to their geocoded name
    });
  });
}

function setupPlacesButton() {
  const btn = document.getElementById("btn-places");
  if (!btn) return;
  btn.classList.remove("hidden");
  btn.addEventListener("click", () => {
    openModal("places-modal");
    renderPlacesList();
  });
  const useLocationBtn = document.getElementById("places-use-location");
  const statusEl = document.getElementById("places-status");
  useLocationBtn.addEventListener("click", () => {
    if (!("geolocation" in navigator)) {
      setStatus(statusEl, "Geolocation isn't available in this browser.", "err");
      return;
    }
    setStatus(statusEl, "Locating…", "");
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        setStatus(statusEl, "", "");
        const name = window.prompt("Name this place (e.g. Home, Office):", "");
        if (!name || !name.trim()) return;
        const ok = await savePlace(`${pos.coords.latitude}, ${pos.coords.longitude}`, name);
        setStatus(statusEl, ok ? `Saved "${name.trim()}".` : "Couldn't save — try again.", ok ? "ok" : "err");
        renderPlacesList();
        if (ok) load();
      },
      () => setStatus(statusEl, "Location permission denied.", "err"),
      { enableHighAccuracy: true, timeout: 8000 },
    );
  });
}
if (!STATIC_MODE) setupPlacesButton();

// Service & tyre tracker: same self-hosted-only gating as Places/push.
const SERVICE_STATUS_LABEL = {
  ok: "OK", due_soon: "Due soon", overdue: "Overdue", unknown: "Not logged",
};

function fmtServiceDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

async function renderServicePanel() {
  const dueEl = document.getElementById("service-due");
  const listEl = document.getElementById("service-list");
  const typeSelect = document.getElementById("service-type");
  dueEl.innerHTML = '<li class="places-empty">Loading…</li>';
  let data;
  try {
    data = await (await fetch("/api/service")).json();
  } catch (e) {
    dueEl.innerHTML = '<li class="places-empty">Couldn’t load — try again.</li>';
    return;
  }

  // Populate the type dropdown once per open (keeps any free-text option a
  // user typed before a refresh from being clobbered mid-edit).
  if (typeSelect.options.length <= 1) {
    data.types.forEach((t) => {
      const opt = document.createElement("option");
      opt.value = t; opt.textContent = t;
      typeSelect.appendChild(opt);
    });
  }
  if (data.current_odo_km != null) {
    const odoInput = document.getElementById("service-odo");
    if (!odoInput.value) odoInput.value = Math.round(data.current_odo_km);
  }

  dueEl.innerHTML = data.due.map((r) => {
    const meta = r.status === "unknown"
      ? "never logged"
      : `last ${fmtServiceDate(r.last_date)}` +
        (r.due_date ? ` · due ${fmtServiceDate(r.due_date)}` : "") +
        (r.due_odo_km != null ? ` · due ${fmt(r.due_odo_km)} km` : "");
    return `<li><span>${r.type}<span class="svc-meta"><br>${meta}</span></span>` +
      `<span class="svc-status ${r.status}">${SERVICE_STATUS_LABEL[r.status]}</span></li>`;
  }).join("");

  if (!data.records.length) {
    listEl.innerHTML = '<li class="places-empty">No service history logged yet.</li>';
  } else {
    listEl.innerHTML = data.records.map((r) =>
      `<li><span>${r.type} · ${fmtServiceDate(r.date)}` +
      `<span class="place-meta">${r.odo_km ? ` · ${fmt(r.odo_km)} km` : ""}` +
      `${r.cost ? ` · ${fmt(r.cost, 2)}` : ""}${r.notes ? ` · ${r.notes}` : ""}</span></span>` +
      `<button class="place-del" data-id="${r.id}" title="Remove this record">✕</button></li>`
    ).join("");
    listEl.querySelectorAll(".place-del").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await fetch(`/api/service/${btn.dataset.id}`, { method: "DELETE" });
        renderServicePanel();
      });
    });
  }
}

function setupServiceButton() {
  const btn = document.getElementById("btn-service");
  if (!btn) return;
  btn.classList.remove("hidden");
  btn.addEventListener("click", () => {
    openModal("service-modal");
    renderServicePanel();
  });

  const form = document.getElementById("service-form");
  const statusEl = document.getElementById("service-status");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const type = document.getElementById("service-type").value;
    const date = document.getElementById("service-date").value;
    if (!type || !date) return;
    const payload = {
      type, date,
      odo_km: +document.getElementById("service-odo").value || 0,
      cost: +document.getElementById("service-cost").value || 0,
      notes: document.getElementById("service-notes").value || "",
    };
    try {
      const resp = await fetch("/api/service", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) throw new Error(await resp.text());
      setStatus(statusEl, "Logged.", "ok");
      form.reset();
      renderServicePanel();
    } catch (err) {
      setStatus(statusEl, "Couldn't save — try again.", "err");
    }
  });
}
if (!STATIC_MODE) setupServiceButton();

// Manually log a charge the sync loop never caught (before the car was
// linked, or dropped by a since-fixed bug) — additive only, same
// self-hosted-only gating as the other data-entry buttons.
function setupAddChargeButton() {
  const btn = document.getElementById("btn-add-charge");
  if (!btn) return;
  btn.classList.remove("hidden");
  btn.addEventListener("click", () => openModal("add-charge-modal"));

  const form = document.getElementById("add-charge-form");
  const statusEl = document.getElementById("add-charge-status");
  const costInput = document.getElementById("charge-cost");
  document.getElementById("charge-free").addEventListener("change", (e) => {
    costInput.disabled = e.target.checked;
    if (e.target.checked) costInput.value = "";
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const start = document.getElementById("charge-start").value;
    const end = document.getElementById("charge-end").value;
    const energy = document.getElementById("charge-energy").value;
    if (!start || !end || !energy) return;
    const isFree = document.getElementById("charge-free").checked;
    const payload = {
      start_time: start, end_time: end, energy_added_kwh: +energy,
      charge_type: document.getElementById("charge-type").value,
      start_soc: +document.getElementById("charge-start-soc").value || 0,
      end_soc: +document.getElementById("charge-end-soc").value || 0,
      location: document.getElementById("charge-location").value || "",
      is_free: isFree,
    };
    const costVal = document.getElementById("charge-cost").value;
    if (costVal && !isFree) payload.cost = +costVal;
    try {
      const resp = await fetch("/api/charges/manual", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) throw new Error((await resp.json()).detail || "Failed");
      setStatus(statusEl, "Added.", "ok");
      form.reset();
      costInput.disabled = false;   // form.reset() unchecks "Free" but doesn't re-enable this
      load();   // refresh KPIs/lists so the new session shows immediately
    } catch (err) {
      setStatus(statusEl, err.message, "err");
    }
  });
}
if (!STATIC_MODE) setupAddChargeButton();
if (!STATIC_MODE) setupEditChargeModal();
if (!STATIC_MODE) setupRatesModal();
if (!STATIC_MODE) setupDefaultSourceButton();
if (!STATIC_MODE) loadPricingPrefs();

// Wire the static chart "!" explainers once (dynamic panels wire themselves).
wireInfoButtons(document);

// First load lands on the home (garage) page for car selection.
showHome();
