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

  // --- driving (mirror app/analysis/driving.py) ---
  function analyzeDriving(drives) {
    if (!drives.length) return { available: false };
    const dist = drives.map((d) => d.distance_km);
    const dur = drives.map((d) => d.duration_min);
    const spd = drives.map((d) => d.avg_speed_kmh);
    const withDist = drives.filter((d) => d.distance_km > 0);
    const effs = withDist.map(whPerKm);

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
    const [slope] = linregress(withDist.map((d) => d.avg_speed_kmh), effs);

    const distBand = {}; [...bySpeed.keys()].sort().forEach((k) => distBand[k] = round(bySpeed.get(k), 1));
    const tbh = {}; for (let h = 0; h < 24; h++) tbh[String(h)] = byHour.get(h) || 0;
    const tbw = {}; for (let i = 0; i < 7; i++) tbw[WEEKDAYS[i]] = byWd.get(i) || 0;

    return {
      available: true,
      total_drives: drives.length,
      total_distance_km: round(dist.reduce((a, b) => a + b, 0), 1),
      total_duration_h: round(dur.reduce((a, b) => a + b, 0) / 60.0, 1),
      total_energy_kwh: round(drives.reduce((a, d) => a + d.energy_used_kwh, 0), 1),
      avg_trip_distance_km: round(mean(dist), 1),
      avg_trip_duration_min: round(mean(dur), 1),
      avg_speed_kmh: round(mean(spd), 1),
      p95_speed_kmh: round(percentile(drives.map((d) => d.max_speed_kmh), 0.95), 1),
      longest_trip_km: round(Math.max(...dist), 1),
      distance_by_speed_band: distBand,
      trips_by_hour: tbh,
      trips_by_weekday: tbw,
      top_routes: counterTop(routes, 5),
      speed_efficiency_slope_wh_per_kmh: round(slope, 3),
      avg_efficiency_wh_per_km: round(mean(effs), 1),
      recent_trips: [...drives]
        .sort((a, b) => new Date(b.start_time) - new Date(a.start_time))
        .slice(0, 5)
        .map((d) => ({
          start_time: d.start_time,
          distance_km: round(d.distance_km, 1),
          duration_min: Math.round(d.duration_min),
          wh_per_km: Math.round(whPerKm(d)),
          route: d.start_location && d.end_location
            ? `${d.start_location} → ${d.end_location}` : "",
        })),
    };
  }

  // --- charging (mirror app/analysis/charging.py) ---
  function analyzeCharging(charges) {
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
    charges.forEach((c) => {
      const h = new Date(c.start_time).getHours();
      byHour.set(h, (byHour.get(h) || 0) + 1);
      if (c.location) byLoc.set(c.location, (byLoc.get(c.location) || 0) + 1);
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
      top_locations: counterTop(byLoc, 5),
    };
  }

  // --- efficiency (mirror app/analysis/efficiency.py) ---
  function analyzeEfficiency(drives, rated) {
    const dr = drives.filter((d) => d.distance_km > 0);
    if (!dr.length) return { available: false };
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
      avg_efficiency_wh_per_km: round(mean(effs), 1),
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
  function analyzeBattery(readings) {
    const proj = (readings || [])
      .filter((r) => (r.soc || 0) >= 20 && (r.range_km || 0) > 0)
      .map((r) => ({ soc: r.soc, p: r.range_km / (r.soc / 100) }));
    if (proj.length < 5) {
      return { available: false, n_readings: proj.length,
        note: `Collecting data — ${proj.length}/5 usable battery readings so far.` };
    }
    const values = proj.map((x) => x.p);
    const baseline = percentile(values, 0.95);
    const current = mean(values.slice(-10));
    const degradation = baseline ? Math.max(0, 100 * (baseline - current) / baseline) : 0;
    const socs = proj.map((x) => x.soc);
    return {
      available: true, n_readings: proj.length,
      health_pct: round(100 - degradation, 1),
      degradation_pct: round(degradation, 1),
      est_full_range_km: round(current, 0),
      baseline_full_range_km: round(baseline, 0),
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
    if (days === "charge") {
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

    const driving = analyzeDriving(drives);
    const charging = analyzeCharging(charges);
    const efficiency = analyzeEfficiency(drives, rated);
    const battery = analyzeBattery(dataset.battery_readings || []);
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
