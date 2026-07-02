"use strict";
/* Ad-hoc live data via an iOS Shortcut.
 *
 * A browser can't call the Tesla API (no CORS), but the iOS Shortcuts app can.
 * A Shortcut fetches vehicle_data and hands the JSON to this app (pasted, or via
 * a #live=<base64> URL). We store each snapshot and reconstruct drive/charge
 * sessions from consecutive snapshots (tap when you park / arrive), merging them
 * into the imported dataset so the normal dashboard analysis updates.
 *
 * Exposes: TA.parseVehicleData, TA.ingestSnapshot, TA.liveStatus.
 */
(function () {
  const SNAP_KEY = "ta_live_snaps";
  const STORE_KEY = "ta_dataset";
  const CAP_KWH = 60;            // usable pack estimate for energy from SoC delta
  const MI_KM = 1.60934;

  const km = (mi) => (+mi || 0) * MI_KM;

  function parseVehicleData(input) {
    const vd = typeof input === "string" ? JSON.parse(input) : input;
    const r = vd.response || vd; // accept the raw body or the inner response
    const ds = r.drive_state || {}, cs = r.charge_state || {},
          cl = r.climate_state || {}, vs = r.vehicle_state || {},
          cfg = r.vehicle_config || {};
    const tsRaw = ds.timestamp || cs.timestamp || vs.timestamp || Date.now();
    return {
      ts: typeof tsRaw === "number" ? tsRaw : Date.parse(tsRaw) || Date.now(),
      name: r.display_name || vs.vehicle_name || "My Tesla",
      model: (cfg.car_type || "").replace("model", "Model ").trim(),
      odo_km: km(vs.odometer),
      soc: +cs.battery_level || 0,
      range_km: km(cs.battery_range),
      shift: ds.shift_state || "P",
      speed_kmh: ds.speed != null ? km(ds.speed) : 0,
      charging: cs.charging_state === "Charging",
      charger_kw: +cs.charger_power || 0,
      fast: !!cs.fast_charger_present,
      out_temp: cl.outside_temp != null ? +cl.outside_temp : 20,
    };
  }

  function loadSnaps() {
    try { return JSON.parse(localStorage.getItem(SNAP_KEY)) || []; } catch (_) { return []; }
  }
  function saveSnaps(s) { localStorage.setItem(SNAP_KEY, JSON.stringify(s.slice(-200))); }

  function getDataset() {
    try { return JSON.parse(localStorage.getItem(STORE_KEY)); } catch (_) { return null; }
  }
  function saveDataset(ds) { localStorage.setItem(STORE_KEY, JSON.stringify(ds)); }

  function iso(ms) { return new Date(ms).toISOString(); }

  // Compare the newest snapshot with the previous one and, if a drive or charge
  // completed between them, return the new session(s).
  function reconstruct(prev, cur) {
    const drives = [], charges = [];
    if (!prev) return { drives, charges };
    const dtMin = Math.max((cur.ts - prev.ts) / 60000, 0);

    const odoDelta = cur.odo_km - prev.odo_km;
    if (odoDelta > 0.5) {
      const socUsed = Math.max(prev.soc - cur.soc, 0);
      const energy = (socUsed / 100) * CAP_KWH;
      drives.push({
        start_time: iso(prev.ts), end_time: iso(cur.ts),
        distance_km: +odoDelta.toFixed(1), duration_min: +dtMin.toFixed(1),
        start_soc: prev.soc, end_soc: cur.soc, energy_used_kwh: +energy.toFixed(2),
        avg_speed_kmh: dtMin ? +(odoDelta / (dtMin / 60)).toFixed(1) : 0,
        max_speed_kmh: 0, outside_temp_c: cur.out_temp,
        start_location: "", end_location: "",
      });
    }

    const socGain = cur.soc - prev.soc;
    if (socGain > 0.5) {
      const energy = (socGain / 100) * CAP_KWH;
      const dc = prev.fast || cur.fast;
      charges.push({
        start_time: iso(prev.ts), end_time: iso(cur.ts), duration_min: +dtMin.toFixed(1),
        start_soc: prev.soc, end_soc: cur.soc, energy_added_kwh: +energy.toFixed(2),
        charge_type: dc ? "DC" : "AC", max_power_kw: Math.max(prev.charger_kw, cur.charger_kw),
        location: "", cost: +(energy * 0.90).toFixed(2), outside_temp_c: cur.out_temp,
      });
    }
    return { drives, charges };
  }

  // Ingest one vehicle_data snapshot; returns {status, added:{drives,charges}}.
  function ingestSnapshot(input) {
    const snap = parseVehicleData(input);
    const snaps = loadSnaps();
    const prev = snaps.length ? snaps[snaps.length - 1] : null;
    snaps.push(snap);
    saveSnaps(snaps);

    const { drives, charges } = reconstruct(prev, snap);
    if (drives.length || charges.length) {
      const ds = getDataset() || {
        vehicle: { name: snap.name, model: snap.model || "Tesla", trim: "" },
        drives: [], charges: [], source: "imported",
      };
      ds.drives = (ds.drives || []).concat(drives);
      ds.charges = (ds.charges || []).concat(charges);
      ds.source = "imported";
      saveDataset(ds);
    }
    return { status: snap, added: { drives: drives.length, charges: charges.length } };
  }

  function liveStatus() {
    const s = loadSnaps();
    return s.length ? s[s.length - 1] : null;
  }

  window.TA = window.TA || {};
  window.TA.parseVehicleData = parseVehicleData;
  window.TA.ingestSnapshot = ingestSnapshot;
  window.TA.liveStatus = liveStatus;
})();
