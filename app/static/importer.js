"use strict";
/* Client-side importer — a JavaScript port of app/importer.py. Parses a Tesla
 * data export (CSV / JSON / ZIP) in the browser into normalised drive/charge
 * records. Uses JSZip (vendored) for ZIP. Exposes window.TA.parseUpload(file).
 */
(function () {
  const MILES_TO_KM = 1.60934;

  const DRIVE_ALIASES = {
    start_time: ["starttime", "startdate", "begin", "starteddate", "date", "departuretime"],
    end_time: ["endtime", "enddate", "finish", "endeddate", "arrivaltime"],
    distance_km: ["distancekm", "distance", "km", "kilometers"],
    distance_miles: ["distancemiles", "distancemi", "miles", "mi"],
    duration_min: ["durationmin", "duration", "durationminutes", "minutes"],
    duration_sec: ["durations", "durationsec", "durationseconds", "drivedurations"],
    start_soc: ["startsoc", "startbatterylevel", "socstart", "beginsoc"],
    end_soc: ["endsoc", "endbatterylevel", "socend"],
    energy_used_kwh: ["energyusedkwh", "energyused", "energy", "kwhused", "consumedkwh"],
    avg_speed_kmh: ["avgspeedkmh", "avgspeed", "averagespeed", "speed"],
    max_speed_kmh: ["maxspeedkmh", "maxspeed", "topspeed"],
    outside_temp_c: ["outsidetempc", "outsidetemp", "temperature", "temp"],
    start_location: ["startlocation", "origin", "from", "startaddress"],
    end_location: ["endlocation", "destination", "to", "endaddress"],
  };
  const CHARGE_ALIASES = {
    start_time: ["starttime", "startdate", "begin", "date", "chargestarttime"],
    end_time: ["endtime", "enddate", "finish", "chargeendtime"],
    duration_min: ["durationmin", "duration", "minutes"],
    duration_sec: ["durations", "durationsec", "durationseconds", "chargedurations"],
    start_soc: ["startsoc", "startbatterylevel", "socstart"],
    end_soc: ["endsoc", "endbatterylevel", "socend"],
    energy_added_kwh: ["energyaddedkwh", "energyadded", "kwhadded", "energy", "addedkwh"],
    charge_type: ["chargetype", "chargertype", "type", "current", "chargercurrenttype"],
    max_power_kw: ["maxpowerkw", "maxpower", "power", "chargerpower"],
    location: ["location", "site", "address", "sitename"],
    cost: ["cost", "price", "amount", "totalcost"],
    outside_temp_c: ["outsidetempc", "outsidetemp", "temperature", "temp"],
  };

  const norm = (s) => s.toLowerCase().replace(/[^a-z0-9]/g, "");
  function buildIndex(headers, aliases) {
    const normed = {};
    headers.forEach((h) => {
      const key = norm(h);
      if (normed[key] === undefined) normed[key] = h;
      // Strip a trailing "utc" so "Charge Start Time (UTC)" matches "chargestarttime".
      if (key.endsWith("utc") && normed[key.slice(0, -3)] === undefined) normed[key.slice(0, -3)] = h;
    });
    const idx = {};
    for (const field in aliases) {
      for (const cand of [field, ...aliases[field]]) {
        if (normed[norm(cand)] !== undefined) { idx[field] = normed[norm(cand)]; break; }
      }
    }
    return idx;
  }
  function num(v, def = 0.0) {
    if (v === null || v === undefined || v === "") return def;
    const n = parseFloat(String(v).replace(/,/g, "").trim());
    return isNaN(n) ? def : n;
  }
  function dt(v) {
    if (!v) return null;
    const d = new Date(v);
    return isNaN(d.getTime()) ? null : d;
  }
  function looksLikeCharges(headers) {
    const idx = buildIndex(headers, CHARGE_ALIASES);
    return "energy_added_kwh" in idx || "charge_type" in idx || "max_power_kw" in idx;
  }

  function normaliseDrive(row, idx) {
    const g = (f) => (idx[f] !== undefined ? row[idx[f]] : undefined);
    const start = dt(g("start_time"));
    if (!start) return null;
    let duration = num(g("duration_min")) || num(g("duration_sec")) / 60;
    let end = dt(g("end_time"));
    if (!end && duration) end = new Date(start.getTime() + duration * 60000);
    end = end || start;
    let distance = num(g("distance_km"));
    if (!distance && idx.distance_miles !== undefined) distance = num(g("distance_miles")) * MILES_TO_KM;
    if (!duration && end && start) duration = Math.max((end - start) / 60000, 0);
    return {
      start_time: start.toISOString(), end_time: end.toISOString(),
      distance_km: +distance.toFixed(2), duration_min: +duration.toFixed(1),
      start_soc: num(g("start_soc")), end_soc: num(g("end_soc")),
      energy_used_kwh: +num(g("energy_used_kwh")).toFixed(3),
      avg_speed_kmh: num(g("avg_speed_kmh")) || (duration ? +(distance / (duration / 60)).toFixed(1) : 0),
      max_speed_kmh: num(g("max_speed_kmh")),
      outside_temp_c: num(g("outside_temp_c"), 20.0),
      start_location: String(g("start_location") || ""), end_location: String(g("end_location") || ""),
    };
  }
  function normaliseCharge(row, idx) {
    const g = (f) => (idx[f] !== undefined ? row[idx[f]] : undefined);
    const start = dt(g("start_time"));
    if (!start) return null;
    let duration = num(g("duration_min")) || num(g("duration_sec")) / 60;
    let end = dt(g("end_time"));
    if (!end && duration) end = new Date(start.getTime() + duration * 60000);
    end = end || start;
    if (!duration && end && start) duration = Math.max((end - start) / 60000, 0);
    const raw = String(g("charge_type") || "").toUpperCase();
    const power = num(g("max_power_kw"));
    let ctype;
    if (raw.includes("DC") || raw.includes("SUPERCHARG") || raw.includes("FAST")) ctype = "DC";
    else if (raw.includes("AC") || (power && power <= 22)) ctype = "AC";
    else ctype = power > 22 ? "DC" : "AC";
    let cost = num(g("cost"));
    const energy = +num(g("energy_added_kwh")).toFixed(3);
    if (!cost && energy) cost = +(energy * (ctype === "DC" ? 0.45 : 0.30)).toFixed(2);
    return {
      start_time: start.toISOString(), end_time: end.toISOString(),
      duration_min: +duration.toFixed(1), start_soc: num(g("start_soc")), end_soc: num(g("end_soc")),
      energy_added_kwh: energy, charge_type: ctype, max_power_kw: power,
      location: String(g("location") || ""), cost, outside_temp_c: num(g("outside_temp_c"), 20.0),
    };
  }

  // Minimal CSV parser (handles quoted fields and commas).
  function parseCSVText(text) {
    const rows = [];
    let row = [], field = "", inQ = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (inQ) {
        if (c === '"') { if (text[i + 1] === '"') { field += '"'; i++; } else inQ = false; }
        else field += c;
      } else if (c === '"') inQ = true;
      else if (c === ",") { row.push(field); field = ""; }
      else if (c === "\n" || c === "\r") {
        if (c === "\r" && text[i + 1] === "\n") i++;
        row.push(field); field = "";
        if (row.length > 1 || row[0] !== "") rows.push(row);
        row = [];
      } else field += c;
    }
    if (field !== "" || row.length) { row.push(field); rows.push(row); }
    return rows;
  }
  function parseCSV(text) {
    const rows = parseCSVText(text);
    if (!rows.length) return { drives: [], charges: [] };
    const headers = rows[0];
    const records = rows.slice(1).map((r) => { const o = {}; headers.forEach((h, i) => o[h] = r[i]); return o; });
    if (looksLikeCharges(headers)) {
      const idx = buildIndex(headers, CHARGE_ALIASES);
      return { drives: [], charges: records.map((r) => normaliseCharge(r, idx)).filter(Boolean) };
    }
    const idx = buildIndex(headers, DRIVE_ALIASES);
    return { drives: records.map((r) => normaliseDrive(r, idx)).filter(Boolean), charges: [] };
  }
  function parseJSONText(text) {
    const data = JSON.parse(text);
    let dr = [], ch = [];
    if (Array.isArray(data)) {
      data.forEach((rec) => { if (rec && typeof rec === "object") (looksLikeCharges(Object.keys(rec)) ? ch : dr).push(rec); });
    } else if (data && typeof data === "object") {
      dr = data.drives || []; ch = data.charges || [];
    }
    const drives = dr.map((r) => normaliseDrive(r, buildIndex(Object.keys(r), DRIVE_ALIASES))).filter(Boolean);
    const charges = ch.map((r) => normaliseCharge(r, buildIndex(Object.keys(r), CHARGE_ALIASES))).filter(Boolean);
    return { drives, charges };
  }

  function isJunk(path) {
    const parts = path.replace(/\\/g, "/").split("/");
    const base = parts[parts.length - 1];
    return path.startsWith("__MACOSX") || parts.includes("__MACOSX") ||
      base.startsWith("._") || base.startsWith(".") || base === "Thumbs.db" || base === "desktop.ini";
  }

  async function parseZip(arrayBuffer) {
    if (typeof JSZip === "undefined") throw new Error("ZIP support failed to load.");
    const zip = await JSZip.loadAsync(arrayBuffer);
    let drives = [], charges = [];
    const entries = Object.values(zip.files);
    for (const entry of entries) {
      if (entry.dir || isJunk(entry.name)) continue;
      const lower = entry.name.toLowerCase();
      try {
        if (lower.endsWith(".zip")) {
          const buf = await entry.async("arraybuffer");
          const inner = await parseZip(buf);
          drives = drives.concat(inner.drives); charges = charges.concat(inner.charges);
        } else if (lower.endsWith(".csv") || lower.endsWith(".tsv") || lower.endsWith(".txt")) {
          const r = parseCSV(await entry.async("string")); drives = drives.concat(r.drives); charges = charges.concat(r.charges);
        } else if (lower.endsWith(".json")) {
          const r = parseJSONText(await entry.async("string")); drives = drives.concat(r.drives); charges = charges.concat(r.charges);
        }
      } catch (e) { /* skip an unreadable inner file */ }
    }
    return { drives, charges };
  }

  async function parseUpload(file) {
    const name = (file.name || "").toLowerCase();
    const head = new Uint8Array(await file.slice(0, 2).arrayBuffer());
    const isZip = name.endsWith(".zip") || (head[0] === 0x50 && head[1] === 0x4b); // "PK"
    let result;
    if (isZip) {
      result = await parseZip(await file.arrayBuffer());
    } else {
      const text = await file.text();
      const t = text.trimStart();
      result = name.endsWith(".json") || t[0] === "{" || t[0] === "[" ? parseJSONText(text) : parseCSV(text);
    }
    if (!result.drives.length && !result.charges.length) {
      throw new Error(isZip
        ? "No drive or charge records found inside the ZIP. Make sure it contains CSV/JSON files with columns like start_time, distance, energy, soc (drives) or energy_added, charge_type, power (charges)."
        : "No records found. Expected CSV/JSON/ZIP with columns like start_time, distance, energy, soc (drives) or energy_added, charge_type, power (charges).");
    }
    return result;
  }

  window.TA = window.TA || {};
  window.TA.parseUpload = parseUpload;
})();
