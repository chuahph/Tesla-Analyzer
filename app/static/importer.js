"use strict";
/* Client-side importer — a JavaScript port of app/importer.py. Parses a Tesla
 * data export (CSV / JSON / ZIP) in the browser into normalised drive/charge
 * records. Uses JSZip (vendored) for ZIP. Exposes window.TA.parseUpload(file).
 */
(function () {
  const MILES_TO_KM = 1.60934;

  // Tesla's "Download Your Data" ZIP is password-protected; browsers/JSZip can't
  // read it. Ask the user to unzip and upload the CSVs instead.
  class EncryptedZipError extends Error {
    constructor() {
      super("This ZIP is password-protected — Tesla encrypts the export. Unzip it " +
        "(with Tesla's password) and upload the CSV files inside, e.g. “Charging " +
        "Data.csv” and “Vehicle Details.csv”.");
      this.name = "EncryptedZipError";
    }
  }

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

  // --- Vehicle Details (VIN/YEAR/MODEL/COLOR) so the dashboard shows the car ---
  const VEHICLE_ALIASES = {
    vin: ["vin"],
    year: ["year", "modelyear"],
    model: ["model", "carmodel"],
    color: ["color", "colour", "paint", "exteriorcolor", "paintcolor"],
    plate: ["licenseplate", "plate", "registration", "regno"],
  };
  function looksLikeVehicle(headers) {
    const idx = buildIndex(headers, VEHICLE_ALIASES);
    return "vin" in idx && ("model" in idx || "year" in idx) && !looksLikeCharges(headers);
  }
  function titleCase(s) {
    return String(s || "").trim().split(/\s+/)
      .map((w) => (w ? w[0].toUpperCase() + w.slice(1).toLowerCase() : w)).join(" ");
  }
  function parseVehicle(records, headers) {
    if (!records.length) return null;
    const idx = buildIndex(headers, VEHICLE_ALIASES);
    const r = records[0];
    const g = (f) => (idx[f] !== undefined ? r[idx[f]] : "");
    const year = String(g("year") || "").trim();
    const model = titleCase(g("model"));   // "MODEL 3" -> "Model 3"
    const color = titleCase(g("color"));
    const name = [year, model].filter(Boolean).join(" ") || "My Tesla";
    return { name, model: color, trim: "", vin: String(g("vin") || "").trim() };
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
    const startSoc = num(g("start_soc")), endSoc = num(g("end_soc"));
    let energy = num(g("energy_used_kwh"));
    // Manual logs record battery % but not kWh — estimate from the SoC drop
    // against a typical ~60 kWh usable pack.
    if (!energy && startSoc > endSoc && endSoc > 0) energy = (startSoc - endSoc) / 100 * 60;
    return {
      start_time: start.toISOString(), end_time: end.toISOString(),
      distance_km: +distance.toFixed(2), duration_min: +duration.toFixed(1),
      start_soc: startSoc, end_soc: endSoc,
      energy_used_kwh: +energy.toFixed(3),
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
    if (!cost && energy) cost = +(energy * 0.90).toFixed(2); // RM 0.90/kWh
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
    if (looksLikeVehicle(headers)) {
      return { drives: [], charges: [], vehicle: parseVehicle(records, headers) };
    }
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
    let zip;
    try {
      zip = await JSZip.loadAsync(arrayBuffer);
    } catch (e) {
      if (/encrypt|password/i.test(String(e && e.message))) throw new EncryptedZipError();
      throw e;
    }
    let drives = [], charges = [], vehicle = null, encrypted = false;
    const entries = Object.values(zip.files);
    for (const entry of entries) {
      if (entry.dir || isJunk(entry.name)) continue;
      const lower = entry.name.toLowerCase();
      try {
        if (lower.endsWith(".zip")) {
          const inner = await parseZip(await entry.async("arraybuffer"));
          drives = drives.concat(inner.drives); charges = charges.concat(inner.charges);
          vehicle = vehicle || inner.vehicle;
        } else if (lower.endsWith(".csv") || lower.endsWith(".tsv") || lower.endsWith(".txt")) {
          const r = parseCSV(await entry.async("string"));
          drives = drives.concat(r.drives); charges = charges.concat(r.charges);
          vehicle = vehicle || r.vehicle;
        } else if (lower.endsWith(".json")) {
          const r = parseJSONText(await entry.async("string"));
          drives = drives.concat(r.drives); charges = charges.concat(r.charges);
        }
      } catch (e) {
        if (/encrypt|password/i.test(String(e && e.message))) encrypted = true;
        // otherwise skip an unreadable inner file
      }
    }
    if (encrypted && !drives.length && !charges.length && !vehicle) throw new EncryptedZipError();
    return { drives, charges, vehicle };
  }

  async function parseOne(file) {
    const name = (file.name || "").toLowerCase();
    const head = new Uint8Array(await file.slice(0, 2).arrayBuffer());
    const isZip = name.endsWith(".zip") || (head[0] === 0x50 && head[1] === 0x4b); // "PK"
    if (isZip) return await parseZip(await file.arrayBuffer());
    const text = await file.text();
    const t = text.trimStart();
    return name.endsWith(".json") || t[0] === "{" || t[0] === "[" ? parseJSONText(text) : parseCSV(text);
  }

  // Parse and MERGE one or more files (e.g. Vehicle Details + Charging Data).
  async function parseFiles(files) {
    let drives = [], charges = [], vehicle = null;
    for (const f of Array.from(files)) {
      const r = await parseOne(f);
      drives = drives.concat(r.drives || []);
      charges = charges.concat(r.charges || []);
      vehicle = vehicle || r.vehicle || null;
    }
    if (!drives.length && !charges.length) {
      throw new Error(vehicle
        ? "Loaded the vehicle details, but no drive or charge records were found. Add your Charging Data / trips file too."
        : "No records found. Upload your Tesla Charging Data (and optionally Vehicle Details) as CSV/JSON, or an unencrypted ZIP of them.");
    }
    return { drives, charges, vehicle };
  }

  async function parseUpload(file) {
    const result = await parseFiles([file]);
    return result;
  }

  window.TA = window.TA || {};
  window.TA.parseUpload = parseUpload;
  window.TA.parseFiles = parseFiles;
})();
