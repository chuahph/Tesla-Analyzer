"use strict";
/* Client-side analytics engine — a JavaScript port of the Python app/analysis
 * package, so the installed PWA can compute everything on-device with no backend.
 * Exposes window.TA.buildSummary(dataset, days, opts).
 */
(function () {
  const RATED_WH_PER_KM = 150.0;
  const ENERGY_PRICE = 0.90;   // RM per kWh
  const CURRENCY = "RM";

  // --- stats helpers (mirror app/analysis/__init__.py) ---
  function mean(xs) {
    const v = xs.filter((x) => x !== null && x !== undefined && !isNaN(x));
    return v.length ? v.reduce((a, b) => a + b, 0) / v.length : 0.0;
  }
  function safeDiv(a, b) { return b ? a / b : 0.0; }
  function round(x, n = 0) { const f = Math.pow(10, n); return Math.round(x * f) / f; }
  function linregress(xs, ys) {
    const n = xs.length;
    if (n < 2) return [0.0, ys.length ? ys[0] : 0.0];
    const mx = mean(xs), my = mean(ys);
    let denom = 0, num = 0;
    for (let i = 0; i < n; i++) { denom += (xs[i] - mx) ** 2; num += (xs[i] - mx) * (ys[i] - my); }
    if (denom === 0) return [0.0, my];
    const slope = num / denom;
    return [slope, my - slope * mx];
  }
  function percentile(xs, p) {
    if (!xs.length) return 0.0;
    const s = [...xs].sort((a, b) => a - b);
    const k = (s.length - 1) * p, lo = Math.floor(k), hi = Math.min(lo + 1, s.length - 1);
    return s[lo] + (s[hi] - s[lo]) * (k - lo);
  }
  function counterTop(map, n) {
    return [...map.entries()].sort((a, b) => b[1] - a[1]).slice(0, n);
  }
  function whPerKm(d) { return d.distance_km > 0 ? (d.energy_used_kwh * 1000.0) / d.distance_km : 0.0; }
  // A real drive can't average below 40 Wh/km — a lower figure means the range
  // reading was refilled mid-trip; exclude it from efficiency. Mirror of
  // app/analysis has_valid_energy.
  const MIN_PLAUSIBLE_WH_PER_KM = 40.0;
  function hasValidEnergy(d) {
    return d.energy_used_kwh > 0 && whPerKm(d) >= MIN_PLAUSIBLE_WH_PER_KM;
  }

  // ISO week + year (matches Python isocalendar()).
  function isoWeek(date) {
    const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
    const day = d.getUTCDay() || 7;
    d.setUTCDate(d.getUTCDate() + 4 - day);
    const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
    const week = Math.ceil(((d - yearStart) / 86400000 + 1) / 7);
    return { year: d.getUTCFullYear(), week };
  }

  const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  function speedBucket(s) {
    if (s < 30) return "City (<30)";
    if (s < 60) return "Urban (30-60)";
    if (s < 90) return "Rural (60-90)";
    return "Highway (90+)";
  }
  function tempBucket(t) {
    if (t < 0) return "<0";
    if (t < 10) return "0-10";
    if (t < 20) return "10-20";
    if (t < 30) return "20-30";
    return "30+";
  }
  function sortedObj(map) {
    const out = {};
    [...map.keys()].sort().forEach((k) => { out[k] = map.get(k); });
    return out;
  }

  // --- driving behaviour (mirror driving.py _behaviour) ---
  function analyzeBehaviour(drives, totalDistance, totalEnergy, effs) {
    const w = drives.filter((d) => d.distance_km > 0);
    if (w.length < 5 || !totalDistance) return { available: false, n_drives: w.length };
    const eff = (sub) => mean(sub.map(whPerKm));
    const factor = (sub) => {
      const rest = w.filter((d) => !sub.includes(d));
      if (!sub.length || !rest.length) return [0, 0, 0];
      const pen = eff(sub) - eff(rest);
      const km = sub.reduce((a, d) => a + d.distance_km, 0);
      return [round(100 * km / totalDistance, 1), round(pen, 1),
              round(km * Math.max(pen, 0) / 1000, 2)];
    };
    const hour = (d) => new Date(d.start_time).getHours();
    const speeding = factor(w.filter((d) => d.max_speed_kmh > 110));
    const sg = factor(w.filter((d) => d.avg_speed_kmh < 50 && d.max_speed_kmh > 2.2 * d.avg_speed_kmh));
    const st = factor(w.filter((d) => d.distance_km < 3));
    const pk = factor(w.filter((d) => [7, 8, 17, 18, 19].includes(hour(d))));
    const ht = factor(w.filter((d) => d.outside_temp_c >= 33));
    const bestQ = percentile(effs, 0.25);
    const overall = mean(effs);
    const potential = Math.max(0, totalEnergy - bestQ * totalDistance / 1000);
    return {
      available: true, n_drives: w.length,
      score: overall ? Math.round(Math.min(100, 100 * bestQ / overall)) : 0,
      best_quartile_wh_per_km: round(bestQ, 1),
      potential_saving_kwh: round(potential, 1),
      speeding_share_pct: speeding[0], speeding_penalty_wh: speeding[1], speeding_saving_kwh: speeding[2],
      stopgo_share_pct: sg[0], stopgo_penalty_wh: sg[1], stopgo_saving_kwh: sg[2],
      short_trip_share_pct: st[0], short_trip_penalty_wh: st[1], short_trip_saving_kwh: st[2],
      peak_hour_share_pct: pk[0], peak_hour_penalty_wh: pk[1], peak_hour_saving_kwh: pk[2],
      hot_weather_share_pct: ht[0], hot_weather_penalty_wh: ht[1], hot_weather_saving_kwh: ht[2],
    };
  }

  // Route/traffic character from the trip's own signals (mirror driving.py).
  function tripConditions(d) {
    const avg = d.avg_speed_kmh || 0, mx = d.max_speed_kmh || 0;
    let base;
    if (mx >= 90) base = avg < 50 ? "highway + congestion" : "highway cruise";
    else if (avg < 50 && mx > 2.2 * avg && avg > 0) base = "stop-go traffic";
    else if (avg < 40) base = "city driving";
    else base = "steady flow";
    const parts = [base];
    const hour = new Date(d.start_time).getHours();
    if ([7, 8, 17, 18, 19].includes(hour)) parts.push("peak hour");
    if ((d.outside_temp_c || 0) >= 33) parts.push(`hot ${Math.round(d.outside_temp_c)}°C`);
    return parts.join(" · ");
  }

  // Absolute efficiency grade (mirror driving.py eco_score / score_grade).
  function ecoScore(whPerKm, rated) {
    if (!rated || whPerKm <= 0) return 0;
    return Math.max(0, Math.min(100, Math.round(100 - (whPerKm / rated - 0.85) * 100)));
  }
  function scoreGrade(s) {
    return s >= 85 ? "A" : s >= 70 ? "B" : s >= 55 ? "C" : s >= 40 ? "D" : "E";
  }

  // --- driving (mirror app/analysis/driving.py) ---
  function analyzeDriving(drives, rated, capacity) {
    rated = rated || RATED_WH_PER_KM;
    capacity = capacity || 75.0;
    if (!drives.length) return { available: false };
    const dist = drives.map((d) => d.distance_km);
    const dur = drives.map((d) => d.duration_min);
    const spd = drives.map((d) => d.avg_speed_kmh);
    // Efficiency-bearing drives only (energy > 0) — a missing range reading
    // logs 0 kWh; including its distance would understate Wh/km. Mirror of
    // app/analysis/driving.py. Distance/duration/counts still use every drive.
    const effDrives = drives.filter((d) => d.distance_km > 0 && hasValidEnergy(d));
    const effs = effDrives.map(whPerKm);
    const effKm = effDrives.reduce((a, d) => a + d.distance_km, 0);
    const effKwh = effDrives.reduce((a, d) => a + d.energy_used_kwh, 0);

    const bySpeed = new Map();
    drives.forEach((d) => { const b = speedBucket(d.avg_speed_kmh); bySpeed.set(b, (bySpeed.get(b) || 0) + d.distance_km); });
    const byHour = new Map(), byWd = new Map(), routes = new Map();
    drives.forEach((d) => {
      const dt = new Date(d.start_time);
      byHour.set(dt.getHours(), (byHour.get(dt.getHours()) || 0) + 1);
      byWd.set((dt.getDay() + 6) % 7, (byWd.get((dt.getDay() + 6) % 7) || 0) + 1);
      if (d.start_location && d.end_location) {
        const r = `${d.start_location} → ${d.end_location}`;
        routes.set(r, (routes.get(r) || 0) + 1);
      }
    });
    const [slope] = linregress(effDrives.map((d) => d.avg_speed_kmh), effs);
    const totKm = dist.reduce((a, b) => a + b, 0);
    const totKwh = drives.reduce((a, d) => a + d.energy_used_kwh, 0);
    // Real-world range yardstick: km per 1% of battery. Take the largest of
    // three sources so short trips still yield a value (see driving.py):
    // net first→last SoC drop, energy-derived %, and summed per-trip deltas.
    const ordered = [...drives].sort((a, b) => new Date(a.start_time) - new Date(b.start_time));
    const socNet = ordered.length ? Math.max((ordered[0].start_soc || 0) - (ordered[ordered.length - 1].end_soc || 0), 0) : 0;
    const socFromInt = drives.reduce((a, d) => a + Math.max((d.start_soc || 0) - (d.end_soc || 0), 0), 0);
    const socFromEnergy = capacity ? (totKwh / capacity * 100.0) : 0;
    const socUsed = Math.max(socNet, socFromInt, socFromEnergy);
    const kmPerSoc = (socUsed >= 0.2 && totKm) ? round(totKm / socUsed, 1) : null;

    const distBand = {}; [...bySpeed.keys()].sort().forEach((k) => distBand[k] = round(bySpeed.get(k), 1));
    const tbh = {}; for (let h = 0; h < 24; h++) tbh[String(h)] = byHour.get(h) || 0;
    const tbw = {}; for (let i = 0; i < 7; i++) tbw[WEEKDAYS[i]] = byWd.get(i) || 0;

    // Zero energy = missing range reading (data gap), not real 0 Wh/km.
    // Energy-bearing drives only, so phantom 0-energy distance can't dilute it.
    const windowEff = (effKm && effKwh > 0) ? round(effKwh * 1000.0 / effKm, 1) : null;
    const windowScore = windowEff ? ecoScore(windowEff, rated) : null;

    return {
      available: true,
      total_drives: drives.length,
      total_distance_km: round(dist.reduce((a, b) => a + b, 0), 1),
      total_duration_h: round(dur.reduce((a, b) => a + b, 0) / 60.0, 1),
      total_energy_kwh: round(drives.reduce((a, d) => a + d.energy_used_kwh, 0), 1),
      avg_trip_distance_km: round(mean(dist), 1),
      avg_trip_duration_min: round(mean(dur), 1),
      avg_speed_kmh: round(mean(spd), 1),
      km_per_soc_pct: kmPerSoc,
      soc_used_pct: round(socFromInt, 1),
      p95_speed_kmh: round(percentile(drives.map((d) => d.max_speed_kmh), 0.95), 1),
      longest_trip_km: round(Math.max(...dist), 1),
      distance_by_speed_band: distBand,
      trips_by_hour: tbh,
      trips_by_weekday: tbw,
      top_routes: counterTop(routes, 5),
      speed_efficiency_slope_wh_per_kmh: round(slope, 3),
      // Distance-weighted (total energy / total km) — see driving.py.
      avg_efficiency_wh_per_km: windowEff,
      eco_score: windowScore,
      eco_grade: windowScore != null ? scoreGrade(windowScore) : null,
      behaviour: analyzeBehaviour(effDrives, effKm, effKwh, effs),
      recent_trips: [...drives]
        .sort((a, b) => new Date(b.start_time) - new Date(a.start_time))
        .slice(0, 5)
        .map((d) => ({
          id: d.id,
          start_time: d.start_time,
          end_time: d.end_time,
          distance_km: round(d.distance_km, 1),
          duration_min: Math.round(d.duration_min),
          avg_speed_kmh: Math.round(d.avg_speed_kmh || 0),
          wh_per_km: hasValidEnergy(d) ? Math.round(whPerKm(d)) : null,
          eco_score: hasValidEnergy(d) ? ecoScore(whPerKm(d), rated) : null,
          conditions: tripConditions(d),
          route: d.start_location && d.end_location
            ? `${d.start_location} → ${d.end_location}` : "",
        })),
    };
  }

  // Infer a charge's place from the trip that ended nearest its start (within
  // 2 h), since a car charges where its last drive ended. Mirror of charging.py.
  function inferChargeLocation(charge, drives) {
    let best = "", bestGap = null;
    const cs = new Date(charge.start_time).getTime();
    (drives || []).forEach((d) => {
      if (!d.end_location) return;
      const gap = Math.abs(cs - new Date(d.end_time).getTime()) / 1000;
      if (bestGap === null || gap < bestGap) { best = d.end_location; bestGap = gap; }
    });
    return (bestGap !== null && bestGap <= 7200) ? best : "";
  }

  // --- charging (mirror app/analysis/charging.py) ---
  function analyzeCharging(charges, drives) {
    if (!charges.length) return { available: false };
    const ac = charges.filter((c) => c.charge_type === "AC");
    const dc = charges.filter((c) => c.charge_type === "DC");
    const totalEnergy = charges.reduce((a, c) => a + c.energy_added_kwh, 0);
    const totalCost = charges.reduce((a, c) => a + c.cost, 0);
    const acEnergy = ac.reduce((a, c) => a + c.energy_added_kwh, 0);
    const dcEnergy = dc.reduce((a, c) => a + c.energy_added_kwh, 0);

    const targets = new Map();
    charges.forEach((c) => { const k = Math.round(c.end_soc / 5) * 5; targets.set(k, (targets.get(k) || 0) + 1); });
    const full = charges.filter((c) => c.end_soc >= 99).length;
    const byHour = new Map(), byLoc = new Map();
    // Named GPS place > inferred-from-trip > raw coords; then tag with AC/DC.
    const placeOf = (c) => {
      if (c.location && /[a-z]/i.test(c.location)) return c.location; // named place
      const inferred = inferChargeLocation(c, drives);
      if (inferred) return inferred;
      return c.location || "";  // raw coords or nothing
    };
    const locOf = (c) => {
      const place = placeOf(c);
      return place ? `${place} · ${c.charge_type}`
        : (c.charge_type === "DC" ? "DC fast charger" : "AC / home charger");
    };
    const locEnergy = new Map();
    charges.forEach((c) => {
      const h = new Date(c.start_time).getHours();
      byHour.set(h, (byHour.get(h) || 0) + 1);
      const l = locOf(c);
      byLoc.set(l, (byLoc.get(l) || 0) + 1);
      locEnergy.set(l, (locEnergy.get(l) || 0) + c.energy_added_kwh);
    });

    const soc = {}; [...targets.keys()].sort((a, b) => a - b).forEach((k) => soc[k] = targets.get(k));
    const cbh = {}; for (let h = 0; h < 24; h++) cbh[String(h)] = byHour.get(h) || 0;

    return {
      available: true,
      total_sessions: charges.length,
      total_energy_kwh: round(totalEnergy, 1),
      total_cost: round(totalCost, 2),
      avg_cost_per_kwh: round(safeDiv(totalCost, totalEnergy), 3),
      ac_sessions: ac.length,
      dc_sessions: dc.length,
      ac_energy_kwh: round(acEnergy, 1),
      dc_energy_kwh: round(dcEnergy, 1),
      dc_energy_share_pct: round(100 * safeDiv(dcEnergy, totalEnergy), 1),
      avg_energy_per_session_kwh: round(mean(charges.map((c) => c.energy_added_kwh)), 1),
      avg_dc_power_kw: dc.length ? round(mean(dc.map((c) => c.max_power_kw)), 1) : 0.0,
      full_charges: full,
      full_charge_share_pct: round(100 * safeDiv(full, charges.length), 1),
      avg_end_soc: round(mean(charges.filter((c) => c.end_soc > 0).map((c) => c.end_soc)), 0),
      end_soc_targets: soc,
      charges_by_hour: cbh,
      // [name, count, kWh] — energy delivered per spot.
      top_locations: counterTop(byLoc, 5)
        .map(([name, count]) => [name, count, round(locEnergy.get(name) || 0, 1)]),
    };
  }

  // --- efficiency (mirror app/analysis/efficiency.py) ---
  function analyzeEfficiency(drives, rated) {
    // Only trips with real energy data — a missing range reading logs 0 kWh
    // and would drag the average to a meaningless 0 Wh/km.
    const dr = drives.filter((d) => d.distance_km > 0 && hasValidEnergy(d));
    if (!dr.length) return { available: false, rated_wh_per_km: rated,
      note: "No energy data yet — efficiency needs a synced drive with battery range readings." };
    const effs = dr.map(whPerKm);

    const byTemp = new Map();
    dr.forEach((d) => { const b = tempBucket(d.outside_temp_c); (byTemp.get(b) || byTemp.set(b, []).get(b)).push(whPerKm(d)); });
    const effByTemp = new Map();
    byTemp.forEach((v, k) => effByTemp.set(k, round(mean(v), 1)));
    const [tslope] = linregress(dr.map((d) => d.outside_temp_c), effs);

    const weekly = new Map();
    dr.forEach((d) => { const iso = isoWeek(new Date(d.start_time)); const key = `${iso.year}-W${String(iso.week).padStart(2, "0")}`; (weekly.get(key) || weekly.set(key, []).get(key)).push(whPerKm(d)); });
    const weeklyEff = {}; [...weekly.keys()].sort().forEach((k) => weeklyEff[k] = round(mean(weekly.get(k)), 1));

    const totalDist = dr.reduce((a, d) => a + d.distance_km, 0);
    const actualEnergy = dr.reduce((a, d) => a + d.energy_used_kwh, 0);
    const ratedEnergy = rated * totalDist / 1000.0;
    const overshoot = ratedEnergy ? 100 * (actualEnergy - ratedEnergy) / ratedEnergy : 0.0;
    const best = [...effs].sort((a, b) => a - b).slice(0, Math.max(1, Math.floor(effs.length / 10)));

    return {
      available: true,
      // Distance-weighted: total energy over total km, not a mean of ratios.
      avg_efficiency_wh_per_km: totalDist ? round(actualEnergy * 1000.0 / totalDist, 1) : 0.0,
      rated_wh_per_km: rated,
      vs_rated_pct: round(overshoot, 1),
      best_efficiency_wh_per_km: round(Math.min(...effs), 1),
      worst_efficiency_wh_per_km: round(Math.max(...effs), 1),
      efficiency_by_temp: sortedObj(effByTemp),
      temp_efficiency_slope_wh_per_c: round(tslope, 2),
      weekly_efficiency: weeklyEff,
      best_decile_efficiency_wh_per_km: round(mean(best), 1),
      total_distance_km: round(totalDist, 1),
      total_energy_kwh: round(actualEnergy, 1),
    };
  }

  // --- battery health (mirror app/analysis/battery.py) ---
  // Tesla VIN: char 4 = model line, char 10 = model-year letter.
  const VIN_YEARS = { A:2010,B:2011,C:2012,D:2013,E:2014,F:2015,G:2016,H:2017,
    J:2018,K:2019,L:2020,M:2021,N:2022,P:2023,R:2024,S:2025,T:2026,V:2027,
    W:2028,X:2029,Y:2030,1:2031,2:2032,3:2033,4:2034,5:2035,6:2036,7:2037,
    8:2038,9:2039 };
  function vinYear(vin) {
    vin = String(vin || "").trim().toUpperCase();
    if (vin.length !== 17 || /^(DEMO|IMPORT|LINKED)/.test(vin)) return null;
    return VIN_YEARS[vin[9]] || null;
  }

  // Factory rated range at 100% when new (km, EPA scale). Each entry needs
  // the model substring, ALL listed tokens (badge, optionally wheel type),
  // and — when given — a model-year window (from the VIN). Most specific
  // entries first — first match wins. Mirror of app/analysis/battery.py.
  // "@19" = any wheel-name token with that diameter (Nova19, Stiletto19, ...).
  const NEW_RANGE_KM = [
    ["MODEL 3", ["P74D"], [2024, 2100], 476],
    ["MODEL 3", ["74D", "@19"], [2024, 2100], 491],  // LR AWD, 19" (305 mi)
    ["MODEL 3", ["74D"], [2024, 2100], 549],         // LR AWD, 18" (341 mi)
    ["MODEL 3", ["P74D"], [2017, 2023], 507],
    ["MODEL 3", ["74D"], [2017, 2023], 536],
    ["MODEL 3", ["P74D"], null, 476],
    ["MODEL 3", ["74D", "@19"], null, 491],
    ["MODEL 3", ["74D"], null, 549],
    ["MODEL 3", ["74"], null, 549], ["MODEL 3", ["50"], null, 438],
    ["MODEL Y", ["P74D"], null, 459], ["MODEL Y", ["74D"], null, 531],
    ["MODEL Y", ["50"], null, 418],
  ];
  function newRangeFor(model, trim, year) {
    const text = `${model || ""} ${trim || ""}`.toUpperCase();
    const tokens = [...new Set(text.split(/[^A-Z0-9]+/))].filter(Boolean);
    const diam = () => {
      for (const t of tokens) {
        const m = t.match(/^[A-Z]+(1[89]|2[012])/);
        if (m) return +m[1];
      }
      return null;
    };
    const has = (req) => req.startsWith("@")
      ? diam() === +req.slice(1)
      : tokens.some((t) => t === req || t.startsWith(req));
    for (const [m, required, years, km] of NEW_RANGE_KM) {
      if (years && (!year || year < years[0] || year > years[1])) continue;
      if (text.includes(m) && required.every(has)) return km;
    }
    return null;
  }

  function analyzeBattery(readings, newRangeKm) {
    const proj = (readings || [])
      .filter((r) => (r.soc || 0) >= 20 && (r.range_km || 0) > 0)
      .map((r) => ({ soc: r.soc, p: r.range_km / (r.soc / 100) }));
    if (proj.length < 5) {
      return { available: false, n_readings: proj.length,
        note: `Collecting data — ${proj.length}/5 usable battery readings so far.` };
    }
    const values = proj.map((x) => x.p);
    const baseline = percentile(values, 0.95);
    const current = percentile(values.slice(-10), 0.5); // median resists outliers
    // Prefer the factory when-new figure as the 100% mark when it matches the
    // scale of this car's readings (see app/analysis/battery.py).
    let referenceKm = baseline, reference = "best seen";
    if (newRangeKm && baseline <= newRangeKm * 1.03) {
      referenceKm = newRangeKm; reference = "factory spec";
    }
    const degradation = referenceKm ? Math.max(0, 100 * (referenceKm - current) / referenceKm) : 0;
    const socs = proj.map((x) => x.soc);
    return {
      available: true, n_readings: proj.length,
      health_pct: round(Math.min(100, 100 - degradation), 1),
      degradation_pct: round(degradation, 1),
      est_full_range_km: round(current, 0),
      baseline_full_range_km: round(baseline, 0),
      reference_km: round(referenceKm, 0),
      reference,
      new_range_km: newRangeKm ? round(newRangeKm, 0) : null,
      min_soc_seen: round(Math.min(...socs), 0),
      avg_soc: round(mean(socs), 0),
    };
  }

  // --- recommendations (mirror app/analysis/recommendations.py) ---
  function rec(category, priority, title, detail, saving) {
    return { category, priority, title, detail, estimated_saving: saving || null };
  }
  function buildRecommendations(driving, charging, efficiency, price, currency, battery) {
    const recs = [];
    const beh = (driving || {}).behaviour || {};
    if (beh.available) {
      const cost = (kwh) => `~${kwh.toFixed(1)} kWh / ${currency} ${(kwh * price).toFixed(2)} in this window`;
      const factors = [
        ["speeding", "medium", "Fast highway driving is costing you range",
         (s, p) => `In ${s}% of your kilometres you exceeded 110 km/h, and those drives averaged +${p} Wh/km versus your calmer ones. Easing the cruise speed by ~10 km/h recovers most of it.`],
        ["stopgo", "medium", "Stop-and-go driving pattern detected",
         (s, p) => `${s}% of your kilometres show a stop-go signature (low average but high peak speed), costing +${p} Wh/km. Smoother acceleration and letting regen do the braking (one-pedal style) narrows this.`],
        ["short_trip", "low", "Short cold-start trips are inefficient",
         (s, p) => `Trips under 3 km make up ${s}% of your kilometres at +${p} Wh/km — the battery and cabin never reach efficient temperature. Chaining errands into one round trip helps.`],
        ["peak_hour", "low", "Peak-hour congestion is measurable in your data",
         (s, p) => `Driving at 7–8 or 17–19 h costs you +${p} Wh/km over ${s}% of your kilometres. Shifting departures even 30 minutes can help.`],
        ["hot_weather", "low", "Hot-weather driving penalty",
         (s, p) => `Drives at 33°C+ cost +${p} Wh/km (${s}% of km) — mostly A/C load. Pre-cool the cabin while still plugged in and park in shade where possible.`],
      ];
      for (const [key, pri, title, detail] of factors) {
        const s = beh[`${key}_share_pct`] || 0, p = beh[`${key}_penalty_wh`] || 0,
              k = beh[`${key}_saving_kwh`] || 0;
        if (s >= 10 && p >= 8 && k >= 0.5) {
          recs.push(rec("Driving behaviour", pri, title, detail(s, p), cost(k)));
        }
      }
      if ((beh.potential_saving_kwh || 0) >= 1 && (beh.score ?? 100) < 90) {
        recs.push(rec("Driving behaviour", "low",
          `Driving like your own best quartile would save ${beh.potential_saving_kwh.toFixed(1)} kWh`,
          `Your most efficient quartile of drives averages ${Math.round(beh.best_quartile_wh_per_km)} Wh/km — a benchmark you already achieve regularly. Matching it across all driving is the single biggest efficiency lever in your data.`,
          `${currency} ${(beh.potential_saving_kwh * price).toFixed(2)} in this window`));
      }
    }
    if (battery && battery.available) {
      const deg = battery.degradation_pct;
      if (deg >= 8) {
        recs.push(rec("Battery health", "high",
          `Estimated battery degradation is ${deg.toFixed(0)}%`,
          "The pack's projected full range has dropped noticeably from its best observed value. Some loss is normal with age and mileage, but you can slow it down: avoid sitting at very high or very low charge for long periods, prefer AC charging, and minimise DC fast-charging in hot conditions.", null));
      } else if (deg >= 4) {
        recs.push(rec("Battery health", "low",
          `Mild battery degradation (~${deg.toFixed(0)}%)`,
          "Projected full range is slightly below the best this pack has shown — well within normal ageing. Current charging habits are worth keeping an eye on but no action is needed.", null));
      }
    }
    if (efficiency.available) {
      const vs = efficiency.vs_rated_pct;
      if (vs > 12) {
        const extra = efficiency.total_energy_kwh * (vs / (100 + vs));
        recs.push(rec("Efficiency", "high", `Driving ${vs.toFixed(0)}% above rated consumption`,
          "Your average Wh/km is well above the EPA/rated figure. The gap is usually a mix of high cruising speed, hard acceleration, climate use and cold weather. Smoother acceleration and using scheduled pre-conditioning while plugged in recovers most of this.",
          `~${extra.toFixed(0)} kWh / ${currency} ${(extra * price).toFixed(0)} over the analysed period`));
      }
      const slope = driving.speed_efficiency_slope_wh_per_kmh || 0;
      if (slope > 0.6) {
        recs.push(rec("Driving", "medium", "High speed is costing significant range",
          `Each extra 1 km/h of average speed adds ~${slope.toFixed(2)} Wh/km. Reducing motorway cruising speed by 10 km/h would noticeably cut consumption on long trips, where aerodynamic drag dominates.`,
          `~${(slope * 10).toFixed(0)} Wh/km on highway legs`));
      }
      const tslope = efficiency.temp_efficiency_slope_wh_per_c || 0;
      if (tslope < -1.0) {
        recs.push(rec("Efficiency", "medium", "Cold weather is hurting efficiency",
          "Consumption climbs sharply as temperature drops. Pre-condition the cabin and battery while still plugged in (so the energy comes from the wall, not the pack), and use seat heaters instead of cabin heat where possible.",
          `~${Math.abs(tslope).toFixed(1)} Wh/km per °C colder`));
      }
    }
    if (charging.available) {
      const fullShare = charging.full_charge_share_pct;
      if (fullShare > 15) {
        recs.push(rec("Battery health", "high", `${fullShare.toFixed(0)}% of charges go to 100%`,
          "Frequent charging to 100% accelerates calendar/cycle degradation on the NCA/NMC pack. Unless you need the full range for a trip, set the daily charge limit to 80–90% and only top up to 100% just before departure.",
          "Slower long-term battery degradation"));
      }
      const dcShare = charging.dc_energy_share_pct;
      if (dcShare > 25) {
        recs.push(rec("Battery health", "medium", `${dcShare.toFixed(0)}% of energy comes from DC fast charging`,
          "Heavy reliance on Superchargers/DC adds heat and stress to the pack and is more expensive per kWh than home AC. Shifting routine charging to overnight AC at home extends battery life and lowers cost.",
          `Up to ${currency} ${((charging.avg_cost_per_kwh - price) * charging.dc_energy_kwh).toFixed(0)} saved by moving DC energy to home AC`));
      }
      const byHour = charging.charges_by_hour;
      let peak = 0; Object.keys(byHour).forEach((h) => { if (+h >= 7 && +h <= 21) peak += byHour[h]; });
      if (peak > charging.total_sessions * 0.4) {
        recs.push(rec("Cost", "medium", "A lot of charging happens during peak hours",
          "Many sessions start between 07:00 and 21:00. If your utility has a time-of-use tariff, scheduling charging to start after midnight (the car supports a scheduled departure/charge time) can cut the per-kWh price substantially.",
          "10–40% off the electricity portion of your charging bill"));
      }
    }
    if (driving.available && driving.avg_trip_distance_km < 6) {
      recs.push(rec("Usage", "low", "Many very short trips",
        "Short hops never let the battery and cabin reach efficient operating temperature, so the Wh/km on these is high. Combining errands into a single round-trip improves overall efficiency.", null));
    }
    if (!recs.length) {
      recs.push(rec("Overall", "low", "Driving and charging look efficient",
        "No major inefficiencies detected in the analysed period. Keep charging mostly to 80–90% on AC and maintain your current driving style.", null));
    }
    const order = { high: 0, medium: 1, low: 2 };
    recs.sort((a, b) => order[a.priority] - order[b.priority]);
    return recs;
  }

  // --- assemble the same payload the dashboard consumes ---
  function buildSummary(dataset, days, opts) {
    opts = opts || {};
    const rated = opts.rated || RATED_WH_PER_KM;
    const price = opts.price || ENERGY_PRICE;
    const currency = opts.currency || CURRENCY;
    let since, windowLabel = null;
    if (days === "drive") {
      // The most recent trip on record — static builds have no live car.
      const starts = (dataset.drives || [])
        .map((d) => new Date(d.start_time).getTime())
        .filter((t) => isFinite(t));
      since = starts.length ? Math.max(...starts) : 0;
      windowLabel = starts.length ? "last drive" : "all data";
      days = 0;
    } else if (days === "charge") {
      // Window starts when the most recent charge ended ("since last charge").
      const ends = (dataset.charges || [])
        .map((c) => new Date(c.end_time || c.start_time).getTime())
        .filter((t) => isFinite(t));
      since = ends.length ? Math.max(...ends) : 0;
      windowLabel = ends.length ? "since last charge" : "all data";
      days = 0;
    } else {
      since = Date.now() - days * 86400000;
    }
    const drives = (dataset.drives || []).filter((d) => new Date(d.start_time).getTime() >= since);
    const charges = (dataset.charges || []).filter((c) => new Date(c.start_time).getTime() >= since);

    const capacity = (dataset.vehicle && dataset.vehicle.battery_capacity_kwh) || 75.0;
    const driving = analyzeDriving(drives, rated, capacity);
    const charging = analyzeCharging(charges, drives);
    const efficiency = analyzeEfficiency(drives, rated);
    const v = dataset.vehicle || {};
    const battery = analyzeBattery(
      dataset.battery_readings || [],
      newRangeFor(v.model, v.trim, vinYear(v.vin)));
    const recommendations = buildRecommendations(driving, charging, efficiency, price, currency, battery);

    return {
      vehicle: dataset.vehicle || { name: "Tesla", model: "", trim: "" },
      window_days: days,
      window_label: windowLabel,
      generated_at: new Date().toISOString().slice(0, 19),
      currency,
      driving, charging, efficiency, battery, recommendations,
    };
  }

  window.TA = window.TA || {};
  window.TA.buildSummary = buildSummary;
})();
