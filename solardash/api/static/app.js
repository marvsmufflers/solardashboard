/* solardash UI — vanilla JS, no build step */
(() => {
  const $ = (s) => document.querySelector(s);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const fmtW = (w) => (w == null ? "–" : Math.abs(w) >= 10000 ? (w / 1000).toFixed(1) + " kW" : Math.abs(w) >= 1000 ? (w / 1000).toFixed(2) + " kW" : Math.round(w) + " W");
  const fmt = (v, d = 1, u = "") => (v == null || Number.isNaN(v) ? "–" : (typeof v === "number" ? v.toFixed(d) : v) + (u ? " " + u : ""));
  const ago = (s) => (s == null ? "" : s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${(s / 3600).toFixed(1)}h ago`);
  const fmtDT = (ts) => new Date(ts * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
  const hours = (h) => (h == null ? "–" : h < 1 ? `${Math.round(h * 60)} min` : `${h.toFixed(1)} h`);
  const TABS = [["home", "⌂", "Home"], ["battery", "▮", "Battery"], ["generator", "⚡", "Generator"], ["inverter", "≋", "Inverter"], ["system", "⚙", "System"]];
  let state = null, range = "24h", settingsLoaded = false, ws, tab = "home";
  const charts = {};
  const palette = () => ({ pv: css("--pv"), load: css("--load"), batt: css("--batt"), gen: css("--gen"), cv: css("--cv"), muted: css("--muted"), line: css("--line"), text: css("--text") });

  // ---------- navigation ----------
  function buildNav() {
    const mk = (cls) => TABS.map(([id, ic, label]) => `<button data-tab="${id}" class="${id === tab ? "active" : ""}">${cls === "tabbar" ? `<span class="ic">${ic}</span>` : ""}${label}</button>`).join("");
    $("#tabbar").innerHTML = mk("tabbar");
    $("#nav-desktop").innerHTML = mk("desktop");
    document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  }
  function showTab(id) {
    tab = id;
    document.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === id));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + id));
    try { localStorage.setItem("tab", id); } catch (e) {}
    window.scrollTo({ top: 0 });
    if (id === "home") { loadPowerChart(); }
    if (id === "battery") { loadSocChart(); loadBattChart(); }
    if (id === "generator") { loadForecast(); loadGenerators(); }
    if (id === "inverter" && !settingsLoaded) loadSettings();
    if (id === "system") loadSystem();
    render();
  }
  try { const t = localStorage.getItem("tab"); if (t && TABS.some(([id]) => id === t)) tab = t; } catch (e) {}
  buildNav();
  document.querySelectorAll("#ranges button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("#ranges button").forEach((x) => x.classList.toggle("active", x === b));
    range = b.dataset.range; loadPowerChart();
  }));

  function toast(msg, ms = 3000) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove("show"), ms); }
  function kv(el, rows) {
    el.innerHTML = rows.filter((r) => r && r[1] !== undefined && r[1] !== null && r[1] !== "").map(([k, v, cls]) => `<div class="kv-item"><div class="k">${k}</div><div class="v ${cls || ""}">${v}</div></div>`).join("");
  }

  // ---------- live state ----------
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { $("#conn").className = "conn ok"; $("#conn").title = "live"; };
    ws.onclose = () => { $("#conn").className = "conn"; $("#conn").title = "disconnected"; setTimeout(connect, 3000); };
    ws.onmessage = (ev) => { state = JSON.parse(ev.data); render(); };
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden && ws && ws.readyState !== 1) connect(); });

  function render() {
    if (!state) return;
    const inv = state.inverter || {}, tot = state.total || {};
    const invOk = state.inverter_age_s != null && state.inverter_age_s < 60;
    if (tab === "home") renderHome(inv, tot, invOk);
    if (tab === "battery") renderBattery(tot);
    if (tab === "inverter") renderInverter(inv);
    if (tab === "generator") renderGenLive(tot);
    if (tab === "system") renderSystem();
    renderOccupancy(tot);
  }

  // ---------- home ----------
  function setGauge(soc, floor) {
    const total = 405; // 270° of 2πr(540)
    $("#gauge-fill").style.strokeDasharray = `${Math.max(0, Math.min(100, soc || 0)) / 100 * total} 540`;
    // thin marker at the planner's floor
    const f = $("#gauge-floor"); f.style.strokeDasharray = "1.6 538.4"; f.style.strokeDashoffset = `${-((floor || 0) / 100 * total - 0.8)}`;
  }
  function renderHome(inv, tot, invOk) {
    const soc = tot.battery_state_of_charge;
    $("#h-soc").textContent = soc == null ? "–" : Math.round(soc);
    const floor = state.forecast_summary?.params?.soc_floor ?? 30;
    setGauge(soc, floor);
    const bp = tot.battery_power;
    $("#h-soc-sub").textContent = bp == null ? "battery" : bp >= 20 ? `charging ${fmtW(bp)}` : bp <= -20 ? `discharging ${fmtW(-bp)}` : "idle";
    $("#h-batt").textContent = tot.battery_voltage != null ? `${tot.battery_voltage.toFixed(1)} V` : "–";
    $("#h-batt-sub").textContent = tot.battery_current != null ? `${tot.battery_current.toFixed(1)} A · ${fmt(tot.battery_temperature, 0, "°C")} · ${tot.battery_count ?? 0} packs` : (tot.battery_source === "inverter" ? "inverter estimate" : "no BMS data");
    // runway: time to 30 % at current rate (or to full)
    const kwh = (state.forecast_summary?.params?.battery_kwh) || (tot.battery_capacity_ah ? tot.battery_capacity_ah * 51.2 / 1000 : 15.36);
    if (bp != null && soc != null && Math.abs(bp) > 30) {
      const target = bp < 0 ? floor : 100;
      const h = Math.abs((target - soc) / 100 * kwh * 1000 / bp);
      $("#h-runway").textContent = h > 48 ? "> 2 days" : hours(h);
      $("#h-runway-sub").textContent = bp < 0 ? `to ${floor} % at this load` : "to full at this rate";
    } else { $("#h-runway").textContent = "–"; $("#h-runway-sub").textContent = soc != null ? "battery idle" : ""; }
    // flow
    const pv = invOk ? inv.pv_power : null, load = invOk ? tot.load_power : null, gen = tot.generator_power;
    $("#fn-pv").textContent = fmtW(pv); $("#fn-load").textContent = fmtW(load); $("#fn-gen").textContent = tot.generator_running ? fmtW(gen) : (gen == null ? "–" : "off"); $("#fn-batt").textContent = bp == null ? "–" : fmtW(Math.abs(bp));
    $("#fn-gen-label").textContent = (tot.generator_active || "generator").split(" ")[0].toLowerCase();
    $("#fn-batt-label").textContent = soc == null ? "battery" : `battery ${Math.round(soc)} %`;
    const line = (id, cls, p, rev) => { const el = $("#" + id); el.className = `fl ${cls} ${p != null && Math.abs(p) > 20 ? "on" : ""} ${rev ? "rev" : ""}`; };
    line("fl-pv", "pv", pv, false); line("fl-gen", "gen", tot.generator_running ? gen : 0, false); line("fl-batt", "batt", bp, bp > 0);
    // tiles
    $("#t-pv-e").textContent = fmt(inv.pv_energy_today, 1, "kWh"); $("#t-pv-s").textContent = invOk ? `${fmtW(inv.pv1_power)} + ${fmtW(inv.pv2_power)} now` : "inverter offline";
    $("#t-load-e").textContent = fmt(inv.load_energy_today ?? inv.eps_energy_today, 1, "kWh"); $("#t-load-s").textContent = invOk ? `L1 ${fmtW(inv.eps_l1_power)} · L2 ${fmtW(inv.eps_l2_power)}` : "";
    const g = (tot.generators || []).find((x) => x.active) || (tot.generators || [])[0];
    $("#t-gen").textContent = tot.generator_running ? "running" : "off";
    $("#t-gen-s").textContent = g ? `${g.name.split(" (")[0]} · ${hours(g.runtime_today_h)} today${g.warming_up ? " · warming up" : ""}` : "";
    const w = state.weather?.current;
    $("#t-wx").textContent = w ? `${w.temperature_c?.toFixed(0)} °C` : (state.weather?.station_id ? "…" : "–");
    $("#t-wx-s").textContent = w ? `${w.solar_radiation != null ? Math.round(w.solar_radiation) + " W/m² · " : ""}${w.humidity != null ? Math.round(w.humidity) + " % RH" : ""}` : (state.weather?.error ? "station error" : "no station");
    // plan mini
    const fp = state.forecast_summary?.plan;
    const pb = $("#pm-badge");
    if (!fp) { pb.textContent = "plan"; pb.className = "badge subtle"; $("#pm-title").textContent = "No forecast yet"; $("#pm-text").textContent = state.forecast_summary?.reason || ""; }
    else {
      const [cls, title] = fp.stop_recommended ? ["warn", "Stop the generator"] : fp.recommended_now ? ["warn", "Start the generator now"] : fp.needed ? ["", `Generator ${fmtDT(fp.start_at)} for ${hours(fp.run_h)}`] : ["ok", "No generator needed"];
      pb.textContent = fp.needed ? "plan" : "plan ✓"; pb.className = "badge " + cls; $("#pm-title").textContent = title;
      $("#pm-text").textContent = fp.needed ? `${fp.start_soc ?? "–"} → ${fp.end_soc} % · lowest without it ${fp.min_soc_without} % ${fp.min_soc_at ? fmtDT(fp.min_soc_at) : ""}` : `bottoms out at ${fp.min_soc_without} % ${fp.min_soc_at ? fmtDT(fp.min_soc_at) : ""}`;
    }
    $("#plan-mini").onclick = () => showTab("generator");
  }

  // ---------- occupancy ----------
  let occBuilt = false;
  async function initOccupancy() {
    try {
      const d = await (await fetch("/api/occupancy")).json();
      const sel = $("#occ-select");
      if (!d.enabled) { sel.parentElement.style.display = "none"; return; }
      sel.innerHTML = d.modes.map((m) => `<option value="${m}">${m}</option>`).join(""); sel.value = d.mode; occBuilt = true;
      sel.addEventListener("change", async () => {
        const r = await fetch("/api/occupancy", { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ mode: sel.value }) });
        toast(r.ok ? `Cabin ${sel.value}` : "Could not set occupancy");
      });
    } catch (e) {}
  }
  function renderOccupancy(tot) { if (occBuilt && tot.occupancy && document.activeElement !== $("#occ-select")) $("#occ-select").value = tot.occupancy; }

  // ---------- battery ----------
  function renderBattery(tot) {
    $("#bank-badge").textContent = `${tot.battery_count ?? 0}/${tot.battery_count_configured ?? 0} packs`;
    $("#bank-badge").className = "badge " + ((tot.battery_count || 0) === (tot.battery_count_configured || 0) ? "ok" : "bad");
    kv($("#bank-stats"), [
      ["State of charge", fmt(tot.battery_state_of_charge, 1, "%")], ["Voltage", fmt(tot.battery_voltage, 2, "V")], ["Current", fmt(tot.battery_current, 1, "A")],
      ["Power", fmtW(tot.battery_power)], ["Remaining", tot.battery_remaining_ah != null ? `${tot.battery_remaining_ah} / ${tot.battery_capacity_ah} Ah` : null],
      ["SOC spread", tot.battery_soc_min != null ? `${tot.battery_soc_min}–${tot.battery_soc_max} %` : null],
      ["Cell min / max", tot.battery_cell_voltage_min != null ? `${tot.battery_cell_voltage_min.toFixed(3)} / ${tot.battery_cell_voltage_max.toFixed(3)} V` : null],
      ["Worst imbalance", fmt(tot.battery_cell_imbalance_max, 3, "V"), tot.battery_cell_imbalance_max > 0.05 ? "warn" : ""],
      ["Temperature", tot.battery_temperature_min != null ? `${tot.battery_temperature_min}–${tot.battery_temperature_max} °C` : null],
      ["Protection", (tot.battery_protection_active || []).join(", ") || "none", (tot.battery_protection_active || []).length ? "bad" : "ok"],
    ]);
    $("#packs").innerHTML = (state.batteries || []).map((b) => {
      const cells = b.cells || [], mn = b.cell_min, mx = b.cell_max;
      const bars = cells.map((c, i) => `<i class="${c === mn ? "lo" : c === mx ? "hi" : ""}${(b.balancing || []).includes(i + 1) ? " bal" : ""}" style="height:${Math.max(8, Math.min(100, ((c - 3.0) / 0.6) * 100))}%" title="cell ${i + 1}: ${c.toFixed(3)} V"></i>`).join("");
      const stale = b.age_s > 60;
      return `<div class="pack"><div class="pack-head"><span class="name">${b.name}</span><span class="badge ${stale ? "bad" : "ok"}">${stale ? "stale " + ago(b.age_s) : fmtW(b.power)}</span></div>
        <div class="pack-soc">${b.soc ?? "–"} %<small>${fmt(b.voltage, 2, "V")} · ${fmt(b.current, 1, "A")}</small></div>
        <div class="bar"><i style="width:${b.soc || 0}%"></i></div>
        <div class="cells">${bars}</div>
        <div class="pack-meta"><span>cells <b>${fmt(mn, 3)}–${fmt(mx, 3)}</b> Δ <b>${fmt(b.cell_imbalance, 3)}</b></span><span>temp <b>${(b.temperatures || []).length ? Math.min(...b.temperatures).toFixed(0) + "–" + Math.max(...b.temperatures).toFixed(0) : "–"} °C</b></span><span>cycles <b>${b.cycles ?? "–"}</b></span><span>${b.remaining_ah != null ? `<b>${b.remaining_ah}</b> / ${b.nominal_ah} Ah` : ""}</span>${(b.protection || []).length ? `<span style="color:var(--bad)">${b.protection.join(", ")}</span>` : ""}${!b.charge_fet_on || !b.discharge_fet_on ? `<span style="color:var(--warn)">FET chg ${b.charge_fet_on ? "on" : "OFF"} / dis ${b.discharge_fet_on ? "on" : "OFF"}</span>` : ""}</div></div>`;
    }).join("") || '<div class="card muted">No batteries configured.</div>';
  }

  // ---------- inverter ----------
  function renderInverter(inv) {
    $("#inv-status").textContent = inv.status || "offline"; $("#inv-status").className = "badge " + ((inv.faults || []).length ? "bad" : "subtle");
    kv($("#inv-live"), [
      ["PV1", `${fmt(inv.pv1_voltage, 0, "V")} · ${fmtW(inv.pv1_power)}`], ["PV2", `${fmt(inv.pv2_voltage, 0, "V")} · ${fmtW(inv.pv2_power)}`], ["Battery (inverter)", `${fmt(inv.battery_voltage, 1, "V")} · ${inv.battery_soc ?? "–"} % · ${fmtW(inv.battery_power)}`],
      ["AC output", `${fmt(inv.eps_voltage, 0, "V")} ${fmt(inv.eps_frequency, 2, "Hz")} · ${fmtW(inv.eps_power)}`], ["L1 / L2", `${fmt(inv.eps_l1_voltage, 0, "V")} ${fmtW(inv.eps_l1_power)} · ${fmt(inv.eps_l2_voltage, 0, "V")} ${fmtW(inv.eps_l2_power)}`], ["Load", fmtW(inv.load_power)],
      ["AC input (unused)", `${inv.ac_input_live ? "LIVE" : "dead"} · ${fmt(inv.ac_input_voltage, 0, "V")}`, inv.ac_input_live ? "warn" : ""], ["Temperatures", `in ${fmt(inv.temperature_inner, 0)} · rad ${fmt(inv.temperature_radiator_1, 0)} / ${fmt(inv.temperature_radiator_2, 0)} °C`],
      ["Faults", (inv.faults || []).join(", ") || "none", (inv.faults || []).length ? "bad" : "ok"], ["Warnings", (inv.warnings || []).join(", ") || "none", (inv.warnings || []).length ? "warn" : ""],
    ]);
    kv($("#inv-energy"), [
      ["PV today / lifetime", `${fmt(inv.pv_energy_today, 1)} / ${fmt(inv.pv_energy_total, 0)} kWh`], ["Load today", fmt(inv.load_energy_today, 1, "kWh")], ["EPS today / lifetime", `${fmt(inv.eps_energy_today, 1)} / ${fmt(inv.eps_energy_total, 0)} kWh`],
      ["Charged today / lifetime", `${fmt(inv.battery_charge_energy_today, 1)} / ${fmt(inv.battery_charge_energy_total, 0)} kWh`], ["Discharged today / lifetime", `${fmt(inv.battery_discharge_energy_today, 1)} / ${fmt(inv.battery_discharge_energy_total, 0)} kWh`], ["AC input today / lifetime", `${fmt(inv.ac_input_total_energy_today, 1)} / ${fmt(inv.ac_input_total_energy_total, 0)} kWh`],
    ]);
    kv($("#inv-bms"), [
      ["Packs / capacity", `${inv.battery_parallel_count ?? "–"} · ${fmt(inv.battery_capacity_ah, 0, "Ah")}`], ["Max charge / discharge", `${fmt(inv.bms_max_charge_current, 0)} / ${fmt(inv.bms_max_discharge_current, 0)} A`], ["Charge V ref / cut-off", `${fmt(inv.bms_charge_voltage_ref, 1)} / ${fmt(inv.bms_discharge_cutoff_voltage, 1)} V`],
      ["Cell min / max", `${fmt(inv.bms_cell_voltage_min, 3)} / ${fmt(inv.bms_cell_voltage_max, 3)} V`], ["Cell temp min / max", `${fmt(inv.bms_cell_temp_min, 1)} / ${fmt(inv.bms_cell_temp_max, 1)} °C`], ["BMS fault / warning", `${inv.bms_fault_code ?? "–"} / ${inv.bms_warning_code ?? "–"}`],
    ]);
    const s = state.settings || {};
    kv($("#inv-diag"), [
      ["Serial", s.serial_number], ["Inverter clock", s.inverter_time], ["Modbus address / type", `${s.com_addr ?? "–"} / ${s.device_type ?? "–"}`],
      ["Status code", inv.status_code != null ? "0x" + inv.status_code.toString(16) : null], ["Bus voltages", `${fmt(inv.bus_voltage_1, 0)} / ${fmt(inv.bus_voltage_2, 0)} V`], ["Runtime", inv.runtime_seconds != null ? (inv.runtime_seconds / 86400).toFixed(1) + " days" : null], ["Data age", ago(state.inverter_age_s)],
    ]);
  }

  // ---------- charts ----------
  function chartOpts(unit, extra = {}) {
    const p = palette();
    Chart.defaults.color = p.muted; Chart.defaults.borderColor = p.line; Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    return {
      animation: false, responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true, pointStyle: "circle", padding: 14 } }, tooltip: { backgroundColor: css("--card-2"), titleColor: p.text, bodyColor: p.text, borderColor: p.line, borderWidth: 1 } },
      scales: { x: { ticks: { maxTicksLimit: 6, callback: function (v) { return fmtTs(this.getLabelForValue(v)); } }, grid: { display: false } }, y: { ticks: { callback: (v) => +(+v).toFixed(3) + " " + unit, maxTicksLimit: 6 }, grid: { color: p.line } } },
      elements: { point: { radius: 0, hoverRadius: 4 }, line: { borderWidth: 2, tension: 0.3 } }, ...extra,
    };
  }
  const fmtTs = (ts) => { const d = new Date(ts * 1000); return range === "6h" || range === "24h" ? d.toLocaleTimeString([], { hour: "numeric" }) : d.toLocaleDateString([], { weekday: "short" }) + " " + d.toLocaleTimeString([], { hour: "numeric" }); };
  async function fetchSeries(metrics, r = range) {
    const res = await fetch(`/api/history?metrics=${encodeURIComponent(metrics.join(","))}&range=${r}`);
    return (await res.json()).series;
  }
  function upsert(id, labels, datasets, unit, extra = {}) {
    if (typeof Chart === "undefined") return;
    if (charts[id]) { charts[id].data.labels = labels; charts[id].data.datasets = datasets; charts[id].update(); return charts[id]; }
    charts[id] = new Chart(document.getElementById(id), { type: "line", data: { labels, datasets }, options: chartOpts(unit, extra) });
    return charts[id];
  }
  function align(series, keys) {
    const ts = new Set(); keys.forEach((k) => (series[k] || []).forEach(([t]) => ts.add(t)));
    const labels = [...ts].sort((a, b) => a - b);
    const maps = keys.map((k) => new Map(series[k] || []));
    return { labels, cols: maps.map((m) => labels.map((t) => (m.has(t) ? m.get(t) : null))) };
  }
  const area = (c) => c + "33";
  async function loadPowerChart() {
    const p = palette(); const keys = ["total/pv_power", "total/load_power", "total/battery_power", "total/generator_power"];
    const s = await fetchSeries(keys); const { labels, cols } = align(s, keys);
    upsert("chart-power", labels, [
      { label: "Solar", data: cols[0], borderColor: p.pv, backgroundColor: area(p.pv), fill: true },
      { label: "Load", data: cols[1], borderColor: p.load },
      { label: "Battery", data: cols[2], borderColor: p.batt },
      { label: "Generator", data: cols[3], borderColor: p.gen, backgroundColor: area(p.gen), fill: true },
    ], "W");
    sparkline("sp-pv", s["total/pv_power"], p.pv); sparkline("sp-load", s["total/load_power"], p.load);
  }
  function sparkline(id, series, color) {
    const c = document.getElementById(id); if (!c || !series || !series.length) return;
    const dpr = window.devicePixelRatio || 1; const w = c.clientWidth, h = 34; c.width = w * dpr; c.height = h * dpr;
    const ctx = c.getContext("2d"); ctx.scale(dpr, dpr); ctx.clearRect(0, 0, w, h);
    const vals = series.map(([, v]) => v); const max = Math.max(...vals, 1), min = Math.min(...vals, 0);
    ctx.beginPath(); series.forEach(([, v], i) => { const x = (i / (series.length - 1)) * w, y = h - 3 - ((v - min) / (max - min || 1)) * (h - 6); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.stroke();
    ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath(); ctx.fillStyle = color + "22"; ctx.fill();
  }
  async function loadSocChart() {
    const p = palette(); const s = await fetchSeries(["total/battery_state_of_charge"], "24h");
    const { labels, cols } = align(s, ["total/battery_state_of_charge"]);
    const saved = range; range = "24h";
    upsert("chart-soc", labels, [{ label: "SOC", data: cols[0], borderColor: p.batt, backgroundColor: area(p.batt), fill: true }], "%", { scales: { y: { min: 0, max: 100, ticks: { callback: (v) => v + " %" } }, x: { ticks: { maxTicksLimit: 6, callback: function (v) { return fmtTs(this.getLabelForValue(v)); } }, grid: { display: false } } } });
    range = saved;
  }
  async function loadBattChart() {
    const p = palette(); const n = (state?.batteries || []).length || 3; const keys = [];
    for (let i = 1; i <= n; i++) keys.push(`battery_${i}/temperature`, `battery_${i}/cell_imbalance`);
    const s = await fetchSeries(keys, "24h"); const { labels, cols } = align(s, keys); const ds = [];
    const colors = [p.batt, p.load, p.pv, p.gen, p.cv, "#1fa3a3"];
    for (let i = 0; i < n; i++) {
      ds.push({ label: `Pack ${i + 1} °C`, data: cols[2 * i], borderColor: colors[i % 6], yAxisID: "y" });
      ds.push({ label: `Pack ${i + 1} Δ`, data: cols[2 * i + 1], borderColor: colors[i % 6], borderDash: [4, 3], yAxisID: "y1" });
    }
    const saved = range; range = "24h";
    upsert("chart-batt", labels, ds, "", { scales: { y: { ticks: { callback: (v) => +(+v).toFixed(1) + " °C" } }, y1: { position: "right", grid: { drawOnChartArea: false }, ticks: { callback: (v) => +(+v).toFixed(3) + " V" } }, x: { ticks: { maxTicksLimit: 6, callback: function (v) { return fmtTs(this.getLabelForValue(v)); } }, grid: { display: false } } } });
    range = saved;
  }

  // ---------- generator / forecast ----------
  let genTotals = {}, genSelectBuilt = false;
  function renderGenLive(tot) {
    const sel = $("#gen-select"); const gens = tot.generators || [];
    if (!genSelectBuilt && gens.length) {
      sel.innerHTML = gens.map((g) => `<option value="${g.name}">${g.name.split(" (")[0]}</option>`).join("");
      sel.addEventListener("change", async () => {
        const r = await fetch("/api/generators/active", { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ name: sel.value }) });
        toast(r.ok ? `Run hours now go to ${sel.value}` : "Could not change generator"); loadGenerators();
      });
      genSelectBuilt = true;
    }
    if (document.activeElement !== sel && tot.generator_active) sel.value = tot.generator_active;
    $("#gen-list").innerHTML = gens.map((g) => `<div class="gen"><div class="n">${g.name}<small>${g.running ? (g.warming_up ? "running · warming up" : "running") : "off"} · ${g.source === "signal" ? "sense wire" : "inferred"}${g.contactor != null ? ` · contactor ${g.contactor ? "closed" : "open"}` : ""}${g.active ? " · on cam switch" : ""}</small></div><div class="h">${hours(g.runtime_today_h)}<small>today · ${genTotals[g.name] != null ? genTotals[g.name].toFixed(1) + " h total" : ""}</small></div></div>`).join("")
      + ((tot.generator_warnings || []).map((w) => `<div class="gen" style="color:var(--warn)">⚠ ${w}</div>`).join(""));
  }
  async function loadGenerators() {
    try { const d = await (await fetch("/api/generators")).json(); genTotals = Object.fromEntries(d.generators.map((g) => [g.name, g.runtime_total_h])); if (state) renderGenLive(state.total || {}); } catch (e) {}
    loadScorecard();
  }
  async function loadForecast() {
    const f = await (await fetch("/api/forecast")).json();
    const plan = f.plan; const badge = $("#plan-badge");
    if (!f.available) { $("#plan-reason").textContent = f.reason || "unavailable"; badge.textContent = "unavailable"; badge.className = "badge"; kv($("#plan-kv"), []); return; }
    if (!plan) { badge.textContent = "planner off"; badge.className = "badge"; $("#plan-reason").textContent = "Planner disabled or no SOC yet."; }
    else {
      badge.textContent = plan.stop_recommended ? "stop now" : plan.recommended_now ? "start now" : plan.needed ? "scheduled" : "no run needed";
      badge.className = "badge " + (plan.recommended_now || plan.stop_recommended ? "warn" : plan.needed ? "" : "ok");
      $("#plan-reason").textContent = plan.reason;
      kv($("#plan-kv"), [
        ["Start", plan.start_at ? fmtDT(plan.start_at) : "–"], ["Stop", plan.end_at ? fmtDT(plan.end_at) : "–"], ["Run", plan.needed ? hours(plan.run_h) : "–"],
        ["SOC", plan.needed ? `${plan.start_soc ?? f.soc_now} → ${plan.end_soc} %` : "–"], ["Energy", plan.needed ? fmt(plan.energy_kwh, 1, "kWh") : "–"], ["Lowest without", `${plan.min_soc_without} % ${plan.min_soc_at ? fmtDT(plan.min_soc_at) : ""}`, plan.min_soc_without < f.params.soc_floor ? "bad" : ""],
        ["Next run after", plan.next_deficit_at ? fmtDT(plan.next_deficit_at) : "none in horizon"], ["Floor / target / storm", `${f.params.soc_floor} / ${f.params.soc_target} / ${f.params.soc_target_storm} %`], ["Load profile", f.occupancy ? `${f.occupancy}${f.model.load_profiles?.[f.occupancy] ? "" : " (overall until learned)"}` : "overall"],
      ]);
    }
    const src = f.sources || {}; const hb = src.hours_by_source || {};
    $("#forecast-source").textContent = hb.openmeteo ? "Open-Meteo" : hb.wunderground ? "Wunderground" : (src.openmeteo_error ? "no forecast" : "assumed sky");
    $("#forecast-source").className = "badge " + (hb.openmeteo ? "ok" : hb.wunderground ? "subtle" : "warn");
    const m = f.model;
    kv($("#pv-model"), [
      ["Clear-sky capacity", `${m.k_clear} kW/(kW/m²) · ${m.k_clear_source}, ${m.samples_clear} h`], ["Bias correction", `×${m.bias} · ${m.samples_bias} h`],
      ["Per measured W/m²", m.k_meas != null ? `${(m.k_meas * 1000).toFixed(2)} W · ${m.samples_meas} h` : "no pyranometer yet"],
      ["Expected now", f.expected_now_kw != null ? `${f.expected_now_kw.toFixed(2)} kW vs ${f.actual_now_kw?.toFixed(2)} kW actual` : "–"],
      ["Load profile", `${m.load_profile_source} · ${m.load_days} d · avg ${(m.load_profile_kw.reduce((a, b) => a + b, 0) / 24).toFixed(2)} kW`], ["Model refreshed", ago((Date.now() / 1000) - m.fitted_at)],
    ]);
    const maxE = Math.max(1, ...f.days.map((d) => Math.max(d.pv_kwh, d.load_kwh)));
    $("#forecast-days").innerHTML = f.days.map((d) => {
      const w = (state?.weather?.forecast || []).find((x) => x.date === d.date);
      return `<div class="day"><div class="d">${new Date(d.date + "T12:00:00").toLocaleDateString([], { weekday: "short" })}<small>${new Date(d.date + "T12:00:00").toLocaleDateString([], { month: "short", day: "numeric" })}${d.hours_covered < 24 ? ` · ${d.hours_covered} h` : ""}</small></div>
        <div class="bars"><div class="bar"><i class="pv" style="width:${d.pv_kwh / maxE * 100}%"></i></div><div class="bar"><i class="ld" style="width:${d.load_kwh / maxE * 100}%"></i></div><small class="muted">☀ ${d.pv_kwh} · ⌂ ${d.load_kwh} kWh · ${Math.round(d.cloud_avg * 100)} % cloud${d.snow_risk ? " · ❄ snow" : ""}${w ? " · " + w.narrative : ""}</small></div>
        <div class="net" style="color:${d.net_kwh >= 0 ? "var(--ok)" : "var(--bad)"}">${d.net_kwh >= 0 ? "+" : ""}${d.net_kwh}<small>kWh net</small></div></div>`;
    }).join("");
    // chart
    const p = palette();
    const labels = f.hours.map((h) => h.ts);
    const hourly = (pts) => { const mm = new Map(); (pts || []).forEach(([t, s]) => { const k = Math.round(t / 3600) * 3600; if (!mm.has(k)) mm.set(k, s); }); return mm; };
    const base = hourly(plan?.baseline), withp = hourly(plan?.planned);
    const ds = [
      { type: "bar", label: "Solar kW", data: f.hours.map((h) => h.pv_kw), backgroundColor: p.pv + "99", yAxisID: "y", borderRadius: 2 },
      { type: "line", label: "Load kW", data: f.hours.map((h) => h.load_kw), borderColor: p.load, yAxisID: "y", borderDash: [3, 3], borderWidth: 1.5 },
    ];
    if (plan) {
      ds.push({ type: "line", label: "SOC without", data: labels.map((t) => base.get(t) ?? null), borderColor: p.batt, borderDash: [6, 4], yAxisID: "y1", borderWidth: 1.5 });
      ds.push({ type: "line", label: "SOC with plan", data: labels.map((t) => withp.get(t) ?? null), borderColor: p.batt, yAxisID: "y1" });
    }
    const saved = range; range = "7d";
    const o = chartOpts("kW", { scales: { y: { ticks: { callback: (v) => v + " kW" }, grid: { color: p.line } }, y1: { position: "right", min: 0, max: 100, grid: { drawOnChartArea: false }, ticks: { callback: (v) => v + " %" } }, x: { ticks: { maxTicksLimit: 6, callback: function (v) { return fmtTs(this.getLabelForValue(v)); } }, grid: { display: false } } } });
    if (charts["chart-forecast"]) { charts["chart-forecast"].data.labels = labels; charts["chart-forecast"].data.datasets = ds; charts["chart-forecast"].update(); }
    else charts["chart-forecast"] = new Chart(document.getElementById("chart-forecast"), { data: { labels, datasets: ds }, options: o });
    range = saved;
  }
  async function loadScorecard() {
    try {
      const rows = await (await fetch("/api/scorecard?days=14")).json();
      if (!rows.length) { $("#scorecard").innerHTML = '<div class="muted small">No history yet.</div>'; return; }
      const pct = (v) => (v == null ? "–" : Math.round(v * 100) + " %");
      $("#scorecard").innerHTML = `<table><tr><th>Day</th><th class="num">Starts</th><th class="num">Run</th><th class="num">kWh</th><th class="num">kWh/h</th><th class="num">Full</th><th class="num">Floor</th><th class="num">SOC</th><th class="num">Curtailed</th></tr>` +
        rows.map((r) => `<tr><td>${r.date.slice(5)}${r.partial_day ? "*" : ""}</td><td class="num">${r.starts}</td><td class="num">${r.run_h.toFixed(1)} h</td><td class="num">${r.kwh}</td><td class="num">${r.kwh_per_run_h ?? "–"}</td><td class="num">${pct(r.full_current_share)}</td><td class="num" style="color:${r.hours_at_floor > 0 ? "var(--bad)" : "inherit"}">${r.hours_at_floor.toFixed(1)} h</td><td class="num">${r.soc_min ?? "–"}–${r.soc_max ?? "–"}</td><td class="num" style="color:${r.curtailed_kwh > 1 ? "var(--warn)" : "inherit"}">${r.curtailed_kwh}</td></tr>`).join("") + "</table>";
    } catch (e) { $("#scorecard").innerHTML = '<div class="muted small">unavailable</div>'; }
  }
  $("#forecast-recompute").addEventListener("click", async () => { const r = await fetch("/api/forecast/recompute", { method: "POST" }); toast(r.ok ? "Forecast recomputed" : "Recompute failed"); loadForecast(); });

  // ---------- settings ----------
  async function loadSettings() {
    const r = await fetch("/api/settings"); const d = await r.json(); settingsLoaded = true;
    $("#settings-mode").textContent = d.allow_writes ? "writes enabled" : "read-only"; $("#settings-mode").className = "badge " + (d.allow_writes ? "warn" : "subtle");
    const groups = {}; d.settings.forEach((s) => (groups[s.group] = groups[s.group] || []).push(s));
    $("#settings").innerHTML = Object.entries(groups).map(([g, items], i) => `<details class="sgroup" ${i === 0 ? "open" : ""}><summary>${g}</summary>${items.map(row).join("")}</details>`).join("");
    document.querySelectorAll("#settings .apply").forEach((b) => b.addEventListener("click", () => applySetting(b.dataset.key, d.settings.find((s) => s.key === b.dataset.key))));
    function row(s) {
      const cur = s.value_label ?? (s.value == null ? "–" : s.value + (s.unit ? " " + s.unit : ""));
      let ctl = "";
      if (s.writable) {
        if (s.kind === "bit") ctl = `<select id="in-${s.key}"><option value="false" ${s.value === false ? "selected" : ""}>${s.labels[0] || "Off"}</option><option value="true" ${s.value === true ? "selected" : ""}>${s.labels[1] || "On"}</option></select>`;
        else if (s.kind === "bits" || s.kind === "choice") ctl = `<select id="in-${s.key}">${s.options.map((o, i) => `<option value="${o}" ${o === s.value ? "selected" : ""}>${s.labels[i] ?? o}${s.unit && !s.labels.length ? " " + s.unit : ""}</option>`).join("")}</select>`;
        else if (s.kind === "time_window") ctl = `<input id="in-${s.key}" value="${s.value ?? ""}" placeholder="HH:MM-HH:MM">`;
        else ctl = `<input id="in-${s.key}" type="number" inputmode="decimal" value="${s.value ?? ""}" min="${s.min ?? ""}" max="${s.max ?? ""}" step="${s.step}"><span class="muted small">${s.unit}</span>`;
        ctl = `<div class="ctl">${ctl}<button class="btn apply" data-key="${s.key}">Apply</button></div>`;
      }
      return `<div class="setting ${s.danger ? "danger" : ""}"><div class="l">${s.label}<small>reg ${s.register}${s.min != null ? ` · ${s.min}–${s.max} ${s.unit}` : ""}</small></div><div class="cur">${cur}</div>${ctl}</div>`;
    }
  }
  async function applySetting(key, s) {
    let v = document.getElementById("in-" + key).value;
    if (s.kind === "bit") v = v === "true"; else if (s.kind !== "time_window") v = Number(v);
    if (s.danger && !confirm(`${s.label}: ${s.danger}\n\nApply ${v}?`)) return;
    const r = await fetch(`/api/settings/${key}`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ value: v, confirm: true }) });
    const d = await r.json();
    toast(r.ok ? `${s.label} → ${JSON.stringify(d[key])}` : `Failed: ${d.detail}`); if (r.ok) loadSettings();
  }
  $("#settings-refresh").addEventListener("click", async () => { const r = await fetch("/api/settings/refresh", { method: "POST" }); toast(r.ok ? "Settings re-read" : "Re-read failed"); loadSettings(); });

  // ---------- setup ----------
  const SETUP = {
    weather: { title: "Location & weather", fields: {
      latitude: ["Latitude", "cabin, decimal degrees (needed for any forecast)"], longitude: ["Longitude", "negative for west"],
      openmeteo_enabled: ["Open-Meteo irradiance forecast", "free, no key"], openmeteo_days: ["Open-Meteo days", "1–16"], openmeteo_interval: ["Open-Meteo refresh (s)"],
      enabled: ["Weather Underground station", "measured conditions + narrative"], station_id: ["Station ID", "e.g. KCOxxxx"], api_key: ["WU API key", "wunderground.com/member/api-keys"],
      units: ["Units (e/m)", "e for US stations"], poll_interval: ["Station poll (s)"], forecast_interval: ["WU forecast refresh (s)"], provider: null } },
    planner: { title: "Generator planner", fields: {
      enabled: ["Planner enabled"], pv_kw_peak: ["Array size (kW)", "used until learned"], battery_kwh: ["Battery (kWh)", "blank = bank Ah × 51.2 V"],
      soc_floor: ["SOC floor (%)"], soc_target: ["Normal stop (%)"], soc_target_storm: ["Storm stop (%)", "allowed when no surplus is coming"], storm_lookahead_h: ["Storm lookahead (h)"],
      soc_margin: ["Margin above floor (%)"], generator_dc_kw: ["Generator → battery (kW)", "Chargeverter DC output"], charge_efficiency: ["Charge efficiency"],
      min_run_h: ["Min run (h)"], max_run_h: ["Max run (h)"], quiet_hours: ["Quiet hours", "comma separated, e.g. 22:00-07:00"], horizon_h: ["Horizon (h)"], recompute_interval: ["Recompute (s)"] } },
    occupancy: { title: "Occupancy", fields: { enabled: ["Enabled"], modes: ["Modes", "comma separated"], default: ["Default mode"] } },
    generators: { title: "Generators (feed the Chargeverter via the cam switch)" },
    mqtt: { title: "MQTT / Home Assistant", fields: {
      enabled: ["Enabled"], host: ["Broker host"], port: ["Port"], username: ["Username"], password: ["Password"], prefix: ["Topic prefix", "solar_assistant keeps SA-era entities"],
      ha_discovery: ["Home Assistant discovery"], ha_prefix: ["Discovery prefix"], publish_interval: ["Publish interval (s)"], client_id: ["Client ID"] } },
  };
  async function loadSetup() {
    const d = await (await fetch("/api/setup")).json();
    const el = $("#setup"); el.innerHTML = "";
    for (const [sec, meta] of Object.entries(SETUP)) {
      const s = d.sections[sec]; if (!s) continue;
      const det = document.createElement("details"); det.className = "sgroup"; det.innerHTML = `<summary>${meta.title}</summary>`;
      const form = document.createElement("div"); form.className = "setup-form";
      if (sec === "generators") {
        form.innerHTML = `<div class="gen-edit" id="gen-edit"></div><div class="setup-actions"><button class="btn" id="gen-add">+ Add generator</button><button class="btn primary" data-save="generators">Save</button></div>`;
        det.appendChild(form); el.appendChild(det);
        const box = form.querySelector("#gen-edit");
        const row = (g = {}) => { const r = document.createElement("div"); r.className = "gen-row"; r.innerHTML = `
          <div class="field"><label>Name</label><input data-k="name" value="${g.name || ""}"></div><div class="field"><label>Notes</label><input data-k="notes" value="${g.notes || ""}"></div>
          <div class="field"><label>Running topic <small>MQTT from HA</small></label><input data-k="running_topic" value="${g.running_topic || ""}" placeholder="solardash/generator/x/running"></div>
          <div class="field"><label>Contactor topic</label><input data-k="contactor_topic" value="${g.contactor_topic || ""}"></div><button class="btn rm">✕</button>`;
          r.querySelector(".rm").addEventListener("click", () => r.remove()); box.appendChild(r); };
        s.values.forEach(row); form.querySelector("#gen-add").addEventListener("click", () => row());
        form.querySelector("[data-save]").addEventListener("click", async () => {
          const values = [...box.querySelectorAll(".gen-row")].map((r) => Object.fromEntries([...r.querySelectorAll("input")].map((i) => [i.dataset.k, i.value.trim()])));
          saveSetup("generators", values);
        });
        continue;
      }
      const kinds = Object.fromEntries(s.schema.map((f) => [f.key, f.kind]));
      for (const [key, label] of Object.entries(meta.fields)) {
        if (label === null) continue;
        const v = s.values[key], kind = kinds[key] || "text", id = `su-${sec}-${key}`;
        const f = document.createElement("div"); f.className = "field" + (kind === "bool" ? " check" : "");
        const lab = `<label for="${id}">${label[0]}${label[1] ? ` <small>· ${label[1]}</small>` : ""}</label>`;
        if (kind === "bool") f.innerHTML = `<input type="checkbox" id="${id}" ${v ? "checked" : ""}>${lab}`;
        else if (kind === "password") f.innerHTML = `${lab}<input type="password" id="${id}" value="" placeholder="${v ? "•••••• (set)" : "not set"}" autocomplete="off">`;
        else if (kind === "list") f.innerHTML = `${lab}<input id="${id}" value="${(v || []).join(", ")}">`;
        else if (kind === "number") f.innerHTML = `${lab}<input type="number" inputmode="decimal" step="any" id="${id}" value="${v ?? ""}">`;
        else f.innerHTML = `${lab}<input id="${id}" value="${v ?? ""}">`;
        f.dataset.key = key; f.dataset.kind = kind; form.appendChild(f);
      }
      const act = document.createElement("div"); act.className = "setup-actions"; act.innerHTML = `<button class="btn primary">Save</button>`;
      act.querySelector("button").addEventListener("click", () => {
        const values = {};
        form.querySelectorAll(".field").forEach((f) => {
          const i = f.querySelector("input"); const k = f.dataset.key, kind = f.dataset.kind;
          if (kind === "bool") values[k] = i.checked;
          else if (kind === "password") { if (i.value) values[k] = i.value; }
          else if (kind === "list") values[k] = i.value.split(",").map((x) => x.trim()).filter(Boolean);
          else if (kind === "number") values[k] = i.value === "" ? null : Number(i.value);
          else values[k] = i.value;
        });
        saveSetup(sec, values);
      });
      form.appendChild(act); det.appendChild(form); el.appendChild(det);
    }
  }
  async function saveSetup(section, values) {
    const r = await fetch("/api/setup", { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ section, values }) });
    const d = await r.json();
    toast(r.ok ? `Saved · ${d.applied}` : `Not saved: ${d.detail}`, 4000);
    if (r.ok) { loadSetup(); if (section !== "mqtt") setTimeout(loadForecast, 2500); }
  }

  // ---------- system ----------
  function renderSystem() {
    const dv = state?.devices || {};
    $("#devices").innerHTML = Object.values(dv).map((d) => `<div class="dev ${d.connected ? "ok" : ""}"><span class="dot"></span><div class="n">${d.name}<small>${d.kind} · <code>${d.port}</code>${d.last_error ? ` · ${d.last_error}` : ""}</small></div><div class="m">${d.connected ? ago(d.seconds_since_ok) : "down"}<br>${d.poll_count} / ${d.error_count} err</div></div>`).join("");
    const w = state?.weather || {}, c = w.current;
    kv($("#weather-now"), c ? [
      ["Station", `${c.station_id} · ${ago(c.age_s)}`], ["Temperature", fmt(c.temperature_c, 1, "°C")], ["Humidity", fmt(c.humidity, 0, "%")], ["Solar radiation", c.solar_radiation != null ? fmt(c.solar_radiation, 0, "W/m²") : "no sensor"],
      ["UV", c.uv], ["Wind", `${fmt(c.wind_kph, 0, "km/h")} gust ${fmt(c.wind_gust_kph, 0)}`], ["Pressure", fmt(c.pressure_hpa, 1, "hPa")], ["Rain today", fmt(c.precip_total_mm, 1, "mm")],
      ["Forecast", w.forecast?.length ? `${w.forecast.length} days · ${ago((Date.now() / 1000) - w.forecast_updated)}` : "none yet"],
    ] : [["Status", w.error || (w.station_id ? "waiting for data" : "not configured")]]);
    const cv = state?.chargeverter || {}; const tot = state?.total || {};
    $("#cv-card").style.display = cv.mode ? "" : "none";
    kv($("#cv"), [["Mode", cv.mode], ["Charger polling", cv.charger_online ? "yes" : "no"], ["Polls / replies", `${cv.poll_count} / ${cv.reply_count}`], ["SOC presented", cv.presented_soc != null ? cv.presented_soc + " %" : null], ["Derived output", fmtW(tot.chargeverter_power)]]);
  }
  async function loadSystem() {
    renderSystem(); loadSetup();
    const ev = await (await fetch("/api/events?limit=60")).json();
    $("#events").innerHTML = ev.map((e) => `<div><span class="ts">${new Date(e.ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</span><span><b>${e.source}</b> ${e.message}</span></div>`).join("") || '<div class="muted">no events</div>';
    const st = await (await fetch("/api/history/stats")).json();
    kv($("#dbstats"), [["Raw samples", st.raw_rows], ["Hourly rows", st.hourly_rows], ["Size", (st.db_bytes / 1048576).toFixed(1) + " MB"], ["Raw retention", st.retention_days + " days"]]);
  }

  // ---------- boot ----------
  connect(); initOccupancy(); showTab(tab);
  setInterval(() => { if (tab === "home") loadPowerChart(); }, 60000);
})();
