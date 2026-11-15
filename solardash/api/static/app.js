/* solardash dashboard */
(() => {
  const $ = (s) => document.querySelector(s);
  const fmtW = (w) => (w == null ? "–" : Math.abs(w) >= 10000 ? (w / 1000).toFixed(1) + " kW" : Math.round(w) + " W");
  const fmt = (v, d = 1, u = "") => (v == null ? "–" : (typeof v === "number" ? v.toFixed(d) : v) + (u ? " " + u : ""));
  const ago = (s) => (s == null ? "" : s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${(s / 3600).toFixed(1)}h ago`);
  let state = null, range = "24h", settingsLoaded = false, ws;
  const charts = {};
  const palette = { pv: "#e0a300", load: "#2a7de1", batt: "#2eaa5b", gen: "#8a5cd6", cv: "#e5553c", soc: "#2eaa5b" };

  // tabs
  document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
    if (b.dataset.tab === "settings" && !settingsLoaded) loadSettings();
    if (b.dataset.tab === "system") loadSystem();
    if (b.dataset.tab === "batteries") loadBattChart();
    if (b.dataset.tab === "chargeverter") loadCvChart();
    if (b.dataset.tab === "forecast") loadForecast();
    try { localStorage.setItem("tab", b.dataset.tab); } catch (e) {}
  }));
  try { const t = localStorage.getItem("tab"); if (t) document.querySelector(`nav button[data-tab="${t}"]`)?.click(); } catch (e) {}
  document.querySelectorAll("#ranges button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("#ranges button").forEach((x) => x.classList.toggle("active", x === b));
    range = b.dataset.range; loadPowerChart(); loadSocChart();
  }));

  function toast(msg, ms = 3500) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), ms); }
  function kv(el, rows) {
    el.innerHTML = rows.filter((r) => r && r[1] !== undefined && r[1] !== null && r[1] !== "").map(([k, v]) => `<div><span>${k}</span><span>${v}</span></div>`).join("");
  }

  // ---- live state ---------------------------------------------------------
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { $("#conn").textContent = "live"; $("#conn").className = "conn ok"; };
    ws.onclose = () => { $("#conn").textContent = "disconnected – retrying"; $("#conn").className = "conn bad"; setTimeout(connect, 3000); };
    ws.onmessage = (ev) => { state = JSON.parse(ev.data); render(); };
  }

  function render() {
    if (!state) return;
    const inv = state.inverter || {}, tot = state.total || {}, cv = state.chargeverter || {};
    const invOk = state.inverter_age_s != null && state.inverter_age_s < 60;
    $("#t-pv").textContent = invOk ? fmtW(inv.pv_power) : "offline";
    $("#t-pv-sub").textContent = invOk ? `${fmtW(inv.pv1_power)} + ${fmtW(inv.pv2_power)} · ${fmt(inv.pv_energy_today, 1, "kWh")} today` : (state.devices && Object.values(state.devices).find(d => d.kind === "inverter")?.last_error) || "";
    $("#t-load").textContent = invOk ? fmtW(tot.load_power) : "–";
    $("#t-load-sub").textContent = invOk ? `L1 ${fmtW(inv.eps_l1_power)} · L2 ${fmtW(inv.eps_l2_power)} · ${fmt(inv.eps_voltage, 0, "V")} ${fmt(inv.eps_frequency, 2, "Hz")}` : "";
    const soc = tot.battery_state_of_charge;
    $("#t-soc").textContent = soc == null ? "–" : `${Math.round(soc)} %`;
    $("#t-soc-bar").style.width = (soc || 0) + "%";
    const bp = tot.battery_power;
    $("#t-batt-sub").textContent = bp == null ? "" : `${bp >= 0 ? "charging" : "discharging"} ${fmtW(Math.abs(bp))} · ${fmt(tot.battery_voltage, 1, "V")} ${fmt(tot.battery_current, 1, "A")} · ${fmt(tot.battery_temperature, 1, "°C")} · ${tot.battery_source === "bms" ? tot.battery_count + " packs" : "inverter data"}`;
    const gp = tot.generator_power;
    const runningGens = (tot.generators || []).filter((g) => g.running);
    const warming = runningGens.find((g) => g.warming_up);
    $("#t-gen").textContent = warming && !(gp > 0) ? "warming up" : runningGens.length ? (gp > 0 ? fmtW(gp) : "running") : gp == null ? "no data" : "off";
    const parts = [];
    runningGens.forEach((g) => parts.push(`${g.name} (${g.source === "signal" ? "sense wire" : "inferred"}${g.contactor === true ? ", contactor closed" : g.contactor === false ? ", contactor open" : ""})`));
    if (!runningGens.length) parts.push(`${tot.generator_active || ""} selected · ${(tot.generator_runtime_today_h ?? 0).toFixed(1)} h today`);
    const fp = state.forecast_summary?.plan;
    if (fp) parts.push(fp.stop_recommended ? "plan: stop now" : fp.recommended_now ? "plan: START NOW" : fp.needed ? `plan: start ${fmtDT(fp.start_at)} for ${fp.run_h.toFixed(1)} h` : `plan: no run needed (min ${fp.min_soc_without}%)`);
    (tot.generator_warnings || []).forEach((w) => parts.push("⚠ " + w));
    $("#t-gen-sub").textContent = parts.join(" · ");
    renderGenSelect(tot);
    $("#t-cv").textContent = cv.mode ? (cv.charger_online ? "polling" : "silent") : "not wired";
    $("#t-cv-sub").textContent = cv.mode ? `${cv.mode}${cv.presented_soc != null ? ` · telling it ${cv.presented_soc}%` : ""}${cv.last_reply?.soc != null ? ` · bus SOC ${cv.last_reply.soc}%` : ""}` : (tot.chargeverter_power_raw != null ? `raw ${fmtW(tot.chargeverter_power_raw)} (bms − inverter)` : "add an RS485 link to see its SOC bus");
    kv($("#energy-today"), [
      ["Solar", fmt(inv.pv_energy_today, 1, "kWh")], ["Load", fmt(inv.load_energy_today ?? inv.eps_energy_today, 1, "kWh")],
      ["Battery charged", fmt(inv.battery_charge_energy_today, 1, "kWh")], ["Battery discharged", fmt(inv.battery_discharge_energy_today, 1, "kWh")],
      ["Solar lifetime", fmt(inv.pv_energy_total, 0, "kWh")], ["Inverter AC input", tot.ac_input_live ? "LIVE – unexpected" : "nothing connected"],
    ]);
    kv($("#gen-hours"), (tot.generators || []).map((g) => [g.name + (g.active ? " (selected)" : ""),
      `${g.runtime_today_h.toFixed(2)} h today${genTotals[g.name] != null ? ` · ${genTotals[g.name].toFixed(1)} h total` : ""} · ${g.running ? (g.warming_up ? "warming up" : "running") : "off"} (${g.source === "signal" ? "sense wire" : "inferred"})`]));
    renderBatteries(); renderInverter(); renderCv(); renderWeather();
    document.querySelector(".tile.cv").style.display = cv.mode ? "" : "none";
    if (document.querySelector('nav button[data-tab="system"]').classList.contains("active")) renderDevices();
  }

  let genTotals = {}, genSelectBuilt = false;
  let occBuilt = false;
  async function initOccupancy() {
    try {
      const d = await (await fetch("/api/occupancy")).json();
      const sel = $("#occ-select");
      if (!d.enabled) { $("#occ-wrap").style.display = "none"; return; }
      sel.innerHTML = d.modes.map((m) => `<option value="${m}">${m}</option>`).join(""); sel.value = d.mode; occBuilt = true;
      sel.addEventListener("change", async () => {
        const r = await fetch("/api/occupancy", { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ mode: sel.value }) });
        toast(r.ok ? `Cabin set to ${sel.value}; planner will use that load profile` : "Could not set occupancy");
      });
    } catch (e) {}
  }
  function renderGenSelect(tot) {
    if (occBuilt && tot.occupancy && document.activeElement !== $("#occ-select")) $("#occ-select").value = tot.occupancy;
    const sel = $("#gen-select"); const gens = tot.generators || [];
    if (!genSelectBuilt && gens.length) {
      sel.innerHTML = gens.map((g) => `<option value="${g.name}">${g.name}</option>`).join("");
      sel.addEventListener("change", async () => {
        const r = await fetch("/api/generators/active", { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ name: sel.value }) });
        toast(r.ok ? `Run hours now go to ${sel.value}` : "Could not change generator"); loadGenTotals();
      });
      genSelectBuilt = true;
    }
    if (document.activeElement !== sel && tot.generator_active) sel.value = tot.generator_active;
  }
  async function loadGenTotals() {
    try { const d = await (await fetch("/api/generators")).json(); genTotals = Object.fromEntries(d.generators.map((g) => [g.name, g.runtime_total_h])); } catch (e) {}
  }
  function renderBatteries() {
    const tot = state.total || {};
    kv($("#bank"), [
      ["Packs online", `${tot.battery_count ?? 0} / ${tot.battery_count_configured ?? 0}`], ["State of charge", fmt(tot.battery_state_of_charge, 1, "%")],
      ["SOC spread", tot.battery_soc_min != null ? `${tot.battery_soc_min}–${tot.battery_soc_max} %` : null], ["Voltage", fmt(tot.battery_voltage, 2, "V")],
      ["Current", fmt(tot.battery_current, 1, "A")], ["Power", fmtW(tot.battery_power)], ["Remaining", tot.battery_remaining_ah != null ? `${tot.battery_remaining_ah} / ${tot.battery_capacity_ah} Ah` : null],
      ["Cell min / max", tot.battery_cell_voltage_min != null ? `${tot.battery_cell_voltage_min.toFixed(3)} / ${tot.battery_cell_voltage_max.toFixed(3)} V` : null],
      ["Worst imbalance", fmt(tot.battery_cell_imbalance_max, 3, "V")], ["Temperature", tot.battery_temperature_min != null ? `${tot.battery_temperature_min}–${tot.battery_temperature_max} °C` : null],
      ["Protection", (tot.battery_protection_active || []).join(", ") || "none"],
    ]);
    const el = $("#packs");
    el.innerHTML = (state.batteries || []).map((b) => {
      const cells = b.cells || [], mn = b.cell_min, mx = b.cell_max;
      const bars = cells.map((c, i) => {
        const h = Math.max(6, Math.min(100, ((c - 3.0) / 0.6) * 100));
        const cls = c === mn ? "lo" : c === mx ? "hi" : "";
        const bal = (b.balancing || []).includes(i + 1) ? " bal" : "";
        return `<div class="${cls}${bal}" style="height:${h}%" title="cell ${i + 1}: ${c.toFixed(3)} V"></div>`;
      }).join("");
      const stale = b.age_s > 60;
      return `<div class="pack"><h3><span>${b.name}</span><span class="pill ${stale ? "bad" : "ok"}">${stale ? "stale " + ago(b.age_s) : "live"}</span></h3>
        <div class="big">${b.soc ?? "–"} % <span class="muted">${fmt(b.voltage, 2, "V")} · ${fmt(b.current, 1, "A")} · ${fmtW(b.power)}</span></div>
        <div class="cells">${bars}</div>
        <div class="muted">cells ${fmt(mn, 3)}–${fmt(mx, 3)} V, Δ ${fmt(b.cell_imbalance, 3)} V${(b.balancing || []).length ? " · balancing " + b.balancing.join(",") : ""}</div>
        <div class="kv" style="margin-top:6px">
          <div><span>Temperatures</span><span>${(b.temperatures || []).map((t) => t.toFixed(1)).join(" / ")} °C</span></div>
          <div><span>Remaining</span><span>${fmt(b.remaining_ah, 1)} / ${fmt(b.nominal_ah, 0)} Ah</span></div>
          <div><span>Cycles</span><span>${b.cycles ?? "–"}</span></div>
          <div><span>FETs</span><span>chg ${b.charge_fet_on ? "on" : "OFF"} · dis ${b.discharge_fet_on ? "on" : "OFF"}</span></div>
          <div><span>Protection</span><span>${(b.protection || []).join(", ") || "none"}</span></div>
          <div><span>Firmware</span><span>${b.software_version ?? ""} ${b.hardware_version ?? ""}</span></div>
        </div></div>`;
    }).join("") || '<div class="card muted">No batteries configured.</div>';
  }

  function renderInverter() {
    const inv = state.inverter || {};
    kv($("#inv-live"), [
      ["Status", inv.status], ["PV1", `${fmt(inv.pv1_voltage, 1, "V")} · ${fmtW(inv.pv1_power)}`], ["PV2", `${fmt(inv.pv2_voltage, 1, "V")} · ${fmtW(inv.pv2_power)}`],
      ["Battery (inverter)", `${fmt(inv.battery_voltage, 1, "V")} · ${inv.battery_soc ?? "–"} % · ${fmtW(inv.battery_power)}`], ["Battery current (BMS via inverter)", fmt(inv.bms_battery_current, 1, "A")],
      ["AC output", `${fmt(inv.eps_voltage, 1, "V")} ${fmt(inv.eps_frequency, 2, "Hz")} · ${fmtW(inv.eps_power)} / ${fmt(inv.eps_apparent_power, 0, "VA")}`],
      ["AC output L1 / L2", `${fmt(inv.eps_l1_voltage, 1, "V")} ${fmtW(inv.eps_l1_power)} · ${fmt(inv.eps_l2_voltage, 1, "V")} ${fmtW(inv.eps_l2_power)}`],
      ["Load", fmtW(inv.load_power)], ["AC input (unused)", `${inv.ac_input_live ? "LIVE" : "dead"} · ${fmt(inv.ac_input_voltage, 1, "V")} ${fmt(inv.ac_input_frequency, 2, "Hz")} · ${fmtW(inv.ac_input_total_power)}`],
      ["AC input mode", inv.ac_input_is_generator ? "generator" : "utility"], ["Inverter power", fmtW(inv.inverter_power)],
      ["Temperatures", `inner ${fmt(inv.temperature_inner, 0, "°C")} · rad1 ${fmt(inv.temperature_radiator_1, 0, "°C")} · rad2 ${fmt(inv.temperature_radiator_2, 0, "°C")} · bat ${fmt(inv.temperature_battery, 0, "°C")}`],
      ["Faults", (inv.faults || []).join(", ") || "none"], ["Warnings", (inv.warnings || []).join(", ") || "none"],
    ]);
    kv($("#inv-energy"), [
      ["PV today", fmt(inv.pv_energy_today, 1, "kWh")], ["PV lifetime", fmt(inv.pv_energy_total, 1, "kWh")], ["Load today", fmt(inv.load_energy_today, 1, "kWh")],
      ["EPS today / lifetime", `${fmt(inv.eps_energy_today, 1)} / ${fmt(inv.eps_energy_total, 0)} kWh`], ["Charged today / lifetime", `${fmt(inv.battery_charge_energy_today, 1)} / ${fmt(inv.battery_charge_energy_total, 0)} kWh`],
      ["Discharged today / lifetime", `${fmt(inv.battery_discharge_energy_today, 1)} / ${fmt(inv.battery_discharge_energy_total, 0)} kWh`], ["AC input today / lifetime", `${fmt(inv.ac_input_total_energy_today, 1)} / ${fmt(inv.ac_input_total_energy_total, 0)} kWh`],
    ]);
    kv($("#inv-bms"), [
      ["Comms type code", inv.battery_comms_type], ["Packs in parallel", inv.battery_parallel_count], ["Capacity", fmt(inv.battery_capacity_ah, 0, "Ah")],
      ["Max charge / discharge", `${fmt(inv.bms_max_charge_current, 0)} / ${fmt(inv.bms_max_discharge_current, 0)} A`], ["Charge voltage ref", fmt(inv.bms_charge_voltage_ref, 1, "V")],
      ["Discharge cut-off", fmt(inv.bms_discharge_cutoff_voltage, 1, "V")], ["Cell min / max", `${fmt(inv.bms_cell_voltage_min, 3)} / ${fmt(inv.bms_cell_voltage_max, 3)} V`],
      ["Cell temp min / max", `${fmt(inv.bms_cell_temp_min, 1)} / ${fmt(inv.bms_cell_temp_max, 1)} °C`], ["Cycles", inv.bms_cycle_count], ["BMS fault / warning", `${inv.bms_fault_code ?? "–"} / ${inv.bms_warning_code ?? "–"}`],
    ]);
    const s = state.settings || {};
    kv($("#inv-diag"), [
      ["Serial", s.serial_number], ["Inverter clock", s.inverter_time], ["Modbus address", s.com_addr], ["Device type", s.device_type],
      ["Status code", inv.status_code != null ? "0x" + inv.status_code.toString(16) : null], ["Fault / warning code", `${inv.fault_code ?? "–"} / ${inv.warning_code ?? "–"}`],
      ["Internal fault", inv.internal_fault], ["Bus voltages", `${fmt(inv.bus_voltage_1, 1)} / ${fmt(inv.bus_voltage_2, 1)} / half ${fmt(inv.half_bus_voltage, 1)} V`],
      ["Runtime", inv.runtime_seconds != null ? (inv.runtime_seconds / 86400).toFixed(1) + " days" : null], ["Data age", ago(state.inverter_age_s)],
    ]);
  }

  function renderCv() {
    const cv = state.chargeverter || {}, tot = state.total || {};
    kv($("#cv"), [
      ["Mode", cv.mode || "not configured"], ["Port", cv.port], ["Charger polling", cv.mode ? (cv.charger_online ? "yes" : "no") : null],
      ["Last poll", cv.seconds_since_poll != null ? ago(cv.seconds_since_poll) : null], ["Polls / replies seen", cv.mode ? `${cv.poll_count} / ${cv.reply_count}` : null],
      ["Answered by solardash", cv.mode === "emulate" ? `${cv.answered} (refused stale: ${cv.refused_stale})` : null], ["SOC presented", cv.presented_soc != null ? cv.presented_soc + " %" : null],
      ["SOC on bus", cv.last_reply?.soc != null ? cv.last_reply.soc + " %" : null], ["Voltage on bus", fmt(cv.last_reply?.voltage, 2, "V")],
      ["Derived output", fmtW(tot.chargeverter_power)], ["Raw difference", fmtW(tot.chargeverter_power_raw)],
    ]);
  }

  // ---- charts ---------------------------------------------------------------
  const chartOpts = (unit) => ({
    animation: false, responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false }, plugins: { legend: { labels: { boxWidth: 12 } } },
    scales: { x: { type: "time", ticks: { maxTicksLimit: 8 }, grid: { display: false } }, y: { ticks: { callback: (v) => v + " " + unit } } },
    elements: { point: { radius: 0 }, line: { borderWidth: 1.6, tension: 0.2 } },
  });
  const fmtTs = (ts) => { const d = new Date(ts * 1000); return range === "1h" || range === "6h" || range === "24h" ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };

  async function fetchSeries(metrics) {
    const r = await fetch(`/api/history?metrics=${encodeURIComponent(metrics.join(","))}&range=${range}`);
    return (await r.json()).series;
  }
  function upsert(id, labels, datasets, unit) {
    if (typeof Chart === "undefined") return;
    const ctx = document.getElementById(id);
    if (charts[id]) { charts[id].data.labels = labels; charts[id].data.datasets = datasets; charts[id].update(); return; }
    const o = chartOpts(unit); o.scales.x = { ticks: { maxTicksLimit: 8, callback: function (v) { return fmtTs(this.getLabelForValue(v)); } }, grid: { display: false } };
    charts[id] = new Chart(ctx, { type: "line", data: { labels, datasets }, options: o });
  }
  function align(series, keys) {
    const ts = new Set(); keys.forEach((k) => (series[k] || []).forEach(([t]) => ts.add(t)));
    const labels = [...ts].sort((a, b) => a - b);
    const maps = keys.map((k) => new Map(series[k] || []));
    return { labels, cols: maps.map((m) => labels.map((t) => (m.has(t) ? m.get(t) : null))) };
  }
  async function loadPowerChart() {
    const keys = ["total/pv_power", "total/load_power", "total/battery_power", "total/generator_power"];
    const s = await fetchSeries(keys); const { labels, cols } = align(s, keys);
    upsert("chart-power", labels, [
      { label: "Solar", data: cols[0], borderColor: palette.pv, backgroundColor: palette.pv + "33", fill: true },
      { label: "Load", data: cols[1], borderColor: palette.load },
      { label: "Battery (+chg)", data: cols[2], borderColor: palette.batt },
      { label: "Generator (via Chargeverter)", data: cols[3], borderColor: palette.gen, backgroundColor: palette.gen + "33", fill: true },
    ], "W");
  }
  async function loadSocChart() {
    const s = await fetchSeries(["total/battery_state_of_charge", "total/battery_voltage"]);
    const { labels, cols } = align(s, ["total/battery_state_of_charge", "total/battery_voltage"]);
    upsert("chart-soc", labels, [{ label: "SOC %", data: cols[0], borderColor: palette.soc, backgroundColor: palette.soc + "22", fill: true }], "%");
  }
  async function loadBattChart() {
    const n = (state?.batteries || []).length || 3; const keys = [];
    for (let i = 1; i <= n; i++) keys.push(`battery_${i}/temperature`, `battery_${i}/cell_imbalance`);
    const s = await fetchSeries(keys); const { labels, cols } = align(s, keys); const ds = [];
    const cols2 = ["#2eaa5b", "#2a7de1", "#e0a300", "#8a5cd6", "#e5553c", "#1fa3a3"];
    for (let i = 0; i < n; i++) {
      ds.push({ label: `Pack ${i + 1} temp °C`, data: cols[2 * i], borderColor: cols2[i % 6], yAxisID: "y" });
      ds.push({ label: `Pack ${i + 1} Δcell V`, data: cols[2 * i + 1], borderColor: cols2[i % 6], borderDash: [4, 3], yAxisID: "y1" });
    }
    upsert("chart-batt", labels, ds, "");
    const c = charts["chart-batt"]; c.options.scales.y1 = { position: "right", grid: { drawOnChartArea: false } }; c.update();
  }
  async function loadCvChart() {
    const keys = ["total/chargeverter_power", "total/chargeverter_power_raw", "total/battery_power", "inverter/battery_power"];
    const s = await fetchSeries(keys); const { labels, cols } = align(s, keys);
    upsert("chart-cv", labels, [
      { label: "Chargeverter (derived)", data: cols[0], borderColor: palette.cv, backgroundColor: palette.cv + "33", fill: true },
      { label: "Raw difference", data: cols[1], borderColor: palette.cv, borderDash: [4, 3] },
      { label: "Pack power (BMS)", data: cols[2], borderColor: palette.batt },
      { label: "Battery power (inverter)", data: cols[3], borderColor: palette.load },
    ], "W");
  }

  // ---- forecast -------------------------------------------------------------------
  const fmtDT = (ts) => new Date(ts * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
  function renderWeather() {
    const w = state?.weather || {}, c = w.current;
    if (!c) { kv($("#weather-now"), [["Status", w.error || (w.station_id ? "waiting for data" : "not configured (weather.enabled: false)")]]); return; }
    kv($("#weather-now"), [
      ["Station", `${c.station_id} · ${ago(c.age_s)}`], ["Temperature", fmt(c.temperature_c, 1, "°C")], ["Humidity", fmt(c.humidity, 0, "%")],
      ["Solar radiation", c.solar_radiation != null ? fmt(c.solar_radiation, 0, "W/m²") : "no sensor"], ["UV", c.uv], ["Wind", `${fmt(c.wind_kph, 0, "km/h")} gust ${fmt(c.wind_gust_kph, 0)}`],
      ["Pressure", fmt(c.pressure_hpa, 1, "hPa")], ["Rain rate", fmt(c.precip_rate_mm_h, 1, "mm/h")], ["Rain today", fmt(c.precip_total_mm, 1, "mm")],
      ["Forecast", w.forecast?.length ? `${w.forecast.length} days, updated ${ago((Date.now() / 1000) - w.forecast_updated)}` : "none yet"],
    ]);
  }
  async function loadForecast() {
    const f = await (await fetch("/api/forecast")).json();
    const plan = f.plan;
    const badge = $("#plan-badge");
    if (!f.available) { $("#plan-reason").textContent = f.reason || "unavailable"; badge.textContent = "unavailable"; badge.className = "pill"; return; }
    if (!plan) { badge.textContent = "planner off"; badge.className = "pill"; $("#plan-reason").textContent = "Planner disabled or no SOC yet."; }
    else {
      badge.textContent = plan.stop_recommended ? "stop generator" : plan.recommended_now ? "start generator now" : plan.needed ? "run scheduled" : "no run needed";
      badge.className = "pill " + (plan.recommended_now || plan.stop_recommended ? "warn" : plan.needed ? "" : "ok");
      $("#plan-reason").textContent = plan.reason;
      kv($("#plan-kv"), [
        ["Start", plan.start_at ? fmtDT(plan.start_at) : "–"], ["Stop", plan.end_at ? fmtDT(plan.end_at) : "–"], ["Run length", plan.needed ? fmt(plan.run_h, 1, "h") : "–"],
        ["SOC start → end", plan.needed ? `${plan.start_soc ?? f.soc_now} → ${plan.end_soc} %` : "–"], ["Energy from generator", plan.needed ? fmt(plan.energy_kwh, 1, "kWh") : "–"],
        ["Lowest SOC without generator", `${plan.min_soc_without} % at ${plan.min_soc_at ? fmtDT(plan.min_soc_at) : "–"}`], ["Next run after this", plan.next_deficit_at ? fmtDT(plan.next_deficit_at) : "none in horizon"],
        ["Battery used for planning", fmt(f.params.battery_kwh, 1, "kWh")], ["Floor / target / storm target", `${f.params.soc_floor} / ${f.params.soc_target} / ${f.params.soc_target_storm} %`],
        ["Load profile in use", f.occupancy ? `${f.occupancy}${f.model.load_profiles && f.model.load_profiles[f.occupancy] ? " (learned)" : " (no data for this mode yet, using overall)"}` : "overall"],
      ]);
    }
    const src = f.sources || {}; const hb = src.hours_by_source || {};
    $("#forecast-source").textContent = hb.openmeteo ? `Open-Meteo irradiance ${hb.openmeteo} h${hb.wunderground ? ` · WU cloud ${hb.wunderground} h` : ""}${hb.climatology ? ` · assumed ${hb.climatology} h` : ""}` : hb.wunderground ? "Wunderground cloud cover" : (src.openmeteo_error ? "Open-Meteo error: " + src.openmeteo_error : "no forecast source");
    $("#forecast-source").className = "pill " + (hb.openmeteo ? "ok" : hb.wunderground ? "" : "warn");
    const m = f.model;
    kv($("#pv-model"), [
      ["Clear-sky capacity", `${m.k_clear} kW per kW/m² (${m.k_clear_source}, ${m.samples_clear} h)`], ["Forecast bias correction", `×${m.bias} (${m.samples_bias} h)`],
      ["Per measured W/m²", m.k_meas != null ? `${(m.k_meas * 1000).toFixed(2)} W per W/m² (${m.samples_meas} h)` : "no pyranometer data yet"],
      ["Expected now from irradiance", f.expected_now_kw != null ? `${f.expected_now_kw.toFixed(2)} kW vs actual ${f.actual_now_kw?.toFixed(2)} kW` : "–"],
      ["Load profile", `${m.load_profile_source}, ${m.load_days} days, avg ${(m.load_profile_kw.reduce((a, b) => a + b, 0) / 24).toFixed(2)} kW`], ["Weather days in forecast", f.weather_days],
      ["Model refreshed", ago((Date.now() / 1000) - m.fitted_at)],
    ]);
    $("#forecast-days").innerHTML = `<table><tr><th>Day</th><th>Cloud</th><th>Solar</th><th>Load</th><th>Net</th><th>Snow / low</th><th>Weather</th></tr>` + f.days.map((d, i) => {
      const w = (state?.weather?.forecast || []).find((x) => x.date === d.date);
      return `<tr><td>${new Date(d.date + "T12:00:00").toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })}${d.hours_covered < 24 ? ` <span class="muted">(${d.hours_covered} h)</span>` : ""}</td><td>${Math.round(d.cloud_avg * 100)} %</td><td>${d.pv_kwh} kWh</td><td>${d.load_kwh} kWh</td><td style="color:${d.net_kwh >= 0 ? "var(--ok)" : "var(--bad)"}">${d.net_kwh >= 0 ? "+" : ""}${d.net_kwh} kWh</td><td>${d.snow_cm ? d.snow_cm + " cm" + (d.snow_risk ? " ⚠ panels" : "") : "–"}${d.temp_min_c != null ? ` / ${d.temp_min_c.toFixed(0)} °C` : ""}</td><td class="muted">${w ? w.narrative : d.source}</td></tr>`;
    }).join("") + "</table>";
    loadScorecard();
    // chart
    const labels = f.hours.map((h) => h.ts);
    const hourly = (pts) => { const m = new Map(); (pts || []).forEach(([t, s]) => { const k = Math.round(t / 3600) * 3600; if (!m.has(k)) m.set(k, s); }); return m; };
    const base = hourly(plan?.baseline), withp = hourly(plan?.planned);
    const ds = [
      { type: "bar", label: "Solar forecast kW", data: f.hours.map((h) => h.pv_kw), backgroundColor: palette.pv + "99", yAxisID: "y" },
      { type: "line", label: "Load kW", data: f.hours.map((h) => h.load_kw), borderColor: palette.load, yAxisID: "y", borderDash: [2, 3] },
    ];
    if (plan) {
      ds.push({ type: "line", label: "SOC % without generator", data: labels.map((t) => base.get(t) ?? null), borderColor: palette.batt, borderDash: [6, 4], yAxisID: "y1" });
      ds.push({ type: "line", label: "SOC % with plan", data: labels.map((t) => withp.get(t) ?? null), borderColor: palette.batt, yAxisID: "y1", fill: false });
    }
    const ctx = document.getElementById("chart-forecast");
    const saveRange = range; range = "7d";
    if (charts["chart-forecast"]) { charts["chart-forecast"].data.labels = labels; charts["chart-forecast"].data.datasets = ds; charts["chart-forecast"].update(); }
    else {
      const o = chartOpts("kW"); o.scales.x = { ticks: { maxTicksLimit: 12, callback: function (v) { return fmtTs(this.getLabelForValue(v)); } }, grid: { display: false } };
      o.scales.y1 = { position: "right", min: 0, max: 100, grid: { drawOnChartArea: false }, ticks: { callback: (v) => v + " %" } };
      charts["chart-forecast"] = new Chart(ctx, { data: { labels, datasets: ds }, options: o });
    }
    range = saveRange;
  }
  async function loadScorecard() {
    try {
      const rows = await (await fetch("/api/scorecard?days=14")).json();
      if (!rows.length) { $("#scorecard").innerHTML = '<div class="muted">No history yet.</div>'; return; }
      const pct = (v) => (v == null ? "–" : Math.round(v * 100) + " %");
      $("#scorecard").innerHTML = `<table><tr><th>Day</th><th>Starts</th><th>Run</th><th>kWh</th><th>kWh / h</th><th>Full current</th><th>At floor</th><th>SOC min / max</th><th>Curtailed solar</th></tr>` +
        rows.map((r) => `<tr><td>${r.date}${r.partial_day ? ' <span class="muted">(so far)</span>' : ""}</td><td>${r.starts}</td><td>${r.run_h.toFixed(1)} h</td><td>${r.kwh}</td><td>${r.kwh_per_run_h ?? "–"}</td><td>${pct(r.full_current_share)}</td><td style="color:${r.hours_at_floor > 0 ? "var(--bad)" : "inherit"}">${r.hours_at_floor.toFixed(1)} h</td><td>${r.soc_min ?? "–"} / ${r.soc_max ?? "–"}</td><td style="color:${r.curtailed_kwh > 1 ? "var(--warn)" : "inherit"}">${r.curtailed_kwh} kWh</td></tr>`).join("") + "</table>";
    } catch (e) { $("#scorecard").innerHTML = '<div class="muted">unavailable</div>'; }
  }
  $("#forecast-recompute").addEventListener("click", async () => { const r = await fetch("/api/forecast/recompute", { method: "POST" }); toast(r.ok ? "Forecast recomputed" : "Recompute failed"); loadForecast(); });

  // ---- settings -----------------------------------------------------------------
  async function loadSettings() {
    const r = await fetch("/api/settings"); const d = await r.json(); settingsLoaded = true;
    $("#settings-mode").textContent = d.allow_writes ? "writes enabled" : "read-only"; $("#settings-mode").className = "pill " + (d.allow_writes ? "warn" : "");
    const groups = {}; d.settings.forEach((s) => (groups[s.group] = groups[s.group] || []).push(s));
    $("#settings").innerHTML = Object.entries(groups).map(([g, items]) => `<div class="sgroup"><h3>${g}</h3><table>${items.map(row).join("")}</table></div>`).join("");
    document.querySelectorAll("#settings .apply").forEach((b) => b.addEventListener("click", () => applySetting(b.dataset.key, d.settings.find((s) => s.key === b.dataset.key))));
    function row(s) {
      let ctl = "";
      const cur = s.value_label ?? (s.value == null ? "–" : s.value + (s.unit ? " " + s.unit : ""));
      if (s.writable) {
        if (s.kind === "bit") ctl = `<select id="in-${s.key}"><option value="false" ${s.value === false ? "selected" : ""}>${s.labels[0] || "Off"}</option><option value="true" ${s.value === true ? "selected" : ""}>${s.labels[1] || "On"}</option></select>`;
        else if (s.kind === "bits" || s.kind === "choice") ctl = `<select id="in-${s.key}">${s.options.map((o, i) => `<option value="${o}" ${o === s.value ? "selected" : ""}>${s.labels[i] ?? o}${s.unit && !s.labels.length ? " " + s.unit : ""}</option>`).join("")}</select>`;
        else if (s.kind === "time_window") ctl = `<input id="in-${s.key}" value="${s.value ?? ""}" placeholder="HH:MM-HH:MM" size="12">`;
        else ctl = `<input id="in-${s.key}" type="number" value="${s.value ?? ""}" min="${s.min ?? ""}" max="${s.max ?? ""}" step="${s.step}"> ${s.unit}`;
        ctl += `<button class="apply" data-key="${s.key}">Apply</button>`;
      }
      return `<tr class="setting ${s.danger ? "danger" : ""}" title="register ${s.register}${s.danger ? " — " + s.danger : ""}"><td>${s.label}</td><td>${cur}</td><td>${ctl}</td></tr>`;
    }
  }
  async function applySetting(key, s) {
    let v = document.getElementById("in-" + key).value;
    if (s.kind === "bit") v = v === "true"; else if (s.kind !== "time_window") v = Number(v);
    if (s.danger && !confirm(`${s.label}: ${s.danger}\n\nApply ${v}?`)) return;
    const r = await fetch(`/api/settings/${key}`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ value: v, confirm: true }) });
    const d = await r.json();
    toast(r.ok ? `${s.label} set to ${JSON.stringify(d[key])}` : `Failed: ${d.detail}`); if (r.ok) loadSettings();
  }
  $("#settings-refresh").addEventListener("click", async () => { const r = await fetch("/api/settings/refresh", { method: "POST" }); toast(r.ok ? "Settings re-read" : "Re-read failed"); loadSettings(); });

  // ---- system -------------------------------------------------------------------
  function renderDevices() {
    const dv = state?.devices || {};
    $("#devices").innerHTML = `<table><tr><th>Device</th><th>Port</th><th>State</th><th>Last OK</th><th>Polls / errors</th><th>Last error</th></tr>` +
      Object.values(dv).map((d) => `<tr><td>${d.name} <span class="muted">${d.kind}</span></td><td><code>${d.port}</code></td><td><span class="pill ${d.connected ? "ok" : "bad"}">${d.connected ? "connected" : "down"}</span></td><td>${ago(d.seconds_since_ok)}</td><td>${d.poll_count} / ${d.error_count}</td><td class="muted">${d.last_error || ""}</td></tr>`).join("") + "</table>";
  }
  async function loadSystem() {
    renderDevices();
    const ev = await (await fetch("/api/events?limit=60")).json();
    $("#events").innerHTML = ev.map((e) => `<div><span class="ts">${new Date(e.ts * 1000).toLocaleString()}</span><span class="pill ${e.level === "warn" ? "warn" : ""}">${e.source}</span><span>${e.message}</span></div>`).join("") || '<div class="muted">no events</div>';
    const st = await (await fetch("/api/history/stats")).json();
    kv($("#dbstats"), [["Raw samples", st.raw_rows], ["Hourly rows", st.hourly_rows], ["Size", (st.db_bytes / 1048576).toFixed(1) + " MB"], ["Raw retention", st.retention_days + " days"]]);
  }

  connect(); loadPowerChart(); loadSocChart(); loadGenTotals(); initOccupancy();
  setInterval(() => { if (document.querySelector("#tab-overview").classList.contains("active")) { loadPowerChart(); loadSocChart(); } }, 60000);
})();
