/* solardash UI — vanilla JS, no build step */
(() => {
  const $ = (s) => document.querySelector(s);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const fmtW = (w) => (w == null ? "–" : Math.abs(w) >= 10000 ? (w / 1000).toFixed(1) + " kW" : Math.abs(w) >= 1000 ? (w / 1000).toFixed(2) + " kW" : Math.round(w) + " W");
  const fmt = (v, d = 1, u = "") => (v == null || Number.isNaN(v) ? "–" : (typeof v === "number" ? v.toFixed(d) : v) + (u ? " " + u : ""));
  const ago = (s) => (s == null ? "" : s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${(s / 3600).toFixed(1)}h ago`);
  const fmtDT = (ts) => new Date(ts * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
  // display units (Setup → Display), from the live state; default Fahrenheit
  const U = () => state?.display || { temperature: "F", wind: "mph", rain: "in" };
  const tC = (c) => (c == null || Number.isNaN(+c) ? null : U().temperature === "F" ? c * 9 / 5 + 32 : +c);
  const TU = () => (U().temperature === "F" ? "°F" : "°C");
  const fT = (c, d = 0) => (tC(c) == null ? "–" : tC(c).toFixed(d) + " " + TU());
  const fTr = (a, b) => (a == null ? null : `${tC(a).toFixed(0)}–${tC(b).toFixed(0)} ${TU()}`);
  const hours = (h) => (h == null ? "–" : h < 1 ? `${Math.round(h * 60)} min` : `${h.toFixed(1)} h`);
  const TABS = [["home", "i-home", "Home"], ["battery", "i-battery", "Battery"], ["generator", "i-bolt", "Generator"], ["inverter", "i-wave", "Inverter"], ["system", "i-sliders", "Setup"]];
  let state = null, range = "24h", settingsLoaded = false, ws, tab = "home";
  const charts = {};
  const palette = () => ({ pv: css("--pv"), load: css("--load"), batt: css("--batt"), gen: css("--gen"), cv: css("--cv"), muted: css("--muted"), line: css("--line"), text: css("--text") });

  // ---------- navigation ----------
  function buildNav() {
    const mk = (cls) => TABS.map(([id, ic, label]) => `<button data-tab="${id}" class="${id === tab ? "active" : ""}">${cls === "tabbar" ? `<svg class="ic"><use href="#${ic}"/></svg>` : ""}${label}</button>`).join("");
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
    $("#h-soc-sub").textContent = bp == null ? "battery" : "idle";
    // % per hour, like Solar Assistant: amps over amp-hours when the BMS gives both, else watts over kWh
    let rate = null;
    if (tot.battery_current != null && tot.battery_capacity_ah) rate = tot.battery_current / tot.battery_capacity_ah * 100;
    else if (bp != null) rate = bp / (((state.forecast_summary?.params?.battery_kwh) || 15.36) * 1000) * 100;
    const re = $("#h-soc-rate");
    re.textContent = rate == null ? "" : `${rate >= 0 ? "+" : "−"}${Math.abs(rate).toFixed(1)} %/h`;
    re.className = "gauge-rate " + (rate == null || Math.abs(rate) < 0.05 ? "" : rate > 0 ? "up" : "down");
    $("#h-batt").textContent = tot.battery_voltage != null ? `${tot.battery_voltage.toFixed(1)} V` : "–";
    $("#h-batt-sub").textContent = tot.battery_current != null ? `${tot.battery_current.toFixed(1)} A · ${fT(tot.battery_temperature)} · ${tot.battery_count ?? 0} packs` : (tot.battery_source === "inverter" ? "inverter estimate" : "no BMS data");
    // runway: time to 30 % at current rate (or to full)
    const kwh = (state.forecast_summary?.params?.battery_kwh) || (tot.battery_capacity_ah ? tot.battery_capacity_ah * 51.2 / 1000 : 15.36);
    if (bp != null && soc != null && Math.abs(bp) > 30) {
      const target = bp < 0 ? floor : 100;
      const h = Math.abs((target - soc) / 100 * kwh * 1000 / bp);
      const ht = h > 48 ? "over 2 days" : h < 1 ? `${Math.round(h * 60)} min` : `${h.toFixed(1)} h`;
      const sub = $("#h-soc-sub");
      sub.textContent = bp > 0 ? `${ht} to full` : `${ht} to ${floor} %`;
      sub.className = "gauge-sub" + (bp < 0 && h < 2 ? " crit" : bp < 0 && h < 6 ? " low" : "");
    }
    // flow
    const pv = invOk ? inv.pv_power : null, load = invOk ? tot.load_power : null, gen = tot.generator_power;
    $("#fn-pv").textContent = fmtW(pv); $("#fn-load").textContent = fmtW(load); $("#fn-gen").textContent = tot.generator_running ? fmtW(gen) : (gen == null ? "–" : "off"); $("#fn-batt").textContent = bp == null ? "–" : fmtW(Math.abs(bp));
    $("#fn-gen-label").textContent = (tot.generator_supplying || tot.generator_active || "generator").split(" ")[0].toLowerCase();
    // ring gauges: solar vs array peak, generator vs charger output, load vs inverter rating, battery power vs 6 kW
    const pp = state.forecast_summary?.params || {};
    arc("fa-pv", pv, (pp.pv_kw_peak || 6) * 1000);
    arc("fa-gen", tot.generator_running ? gen : 0, (pp.generator_dc_kw || 2.4) * 1000);
    arc("fa-load", load, 6000);
    arc("fa-batt", bp, 6000);
    // battery glyph: fills with the charge, bolt while charging (like Solar Assistant)
    { const f = $("#fb-fill"), lvl = soc == null ? 0 : Math.max(0, Math.min(100, soc)), h = 19 * lvl / 100;
      f.setAttribute("height", h.toFixed(2)); f.setAttribute("y", (5.5 + 19 - h).toFixed(2));
      const fl = state.forecast_summary?.params?.soc_floor ?? 25;
      f.setAttribute("class", "b-fill" + (soc != null && soc <= fl ? " bad" : soc != null && soc <= fl + 15 ? " warn" : ""));
      $("#fb-bolt").style.display = bp != null && bp > 20 ? "" : "none";
      const v = tot.battery_voltage;
      $("#fn-batt-label").textContent = soc == null ? "battery" : `${Math.round(soc)} %${v != null ? ` · ${v.toFixed(1)} V` : ""}`; }
    const idle = (sel, on) => document.querySelector(sel)?.classList.toggle("idle", !on);
    idle(".n-pv", pv > 20); idle(".n-gen", tot.generator_running); idle(".n-batt", bp != null && Math.abs(bp) > 20);
    const line = (id, cls, p, rev) => { const el = $("#" + id); el.className = `fl ${cls} ${p != null && Math.abs(p) > 20 ? "on" : ""} ${rev ? "rev" : ""}`; };
    line("fl-pv", "pv", pv, false); line("fl-gen", "gen", tot.generator_running ? gen : 0, false); line("fl-batt", "batt", bp, bp > 0);
    // tiles
    $("#t-pv-e").textContent = fmt(tot.pv_energy_today || inv.pv_energy_today, 1, "kWh"); $("#t-pv-s").textContent = invOk ? `${fmtW(inv.pv1_power)} + ${fmtW(inv.pv2_power)} now` : "inverter offline";
    $("#t-load-e").textContent = fmt(tot.load_energy_today || inv.load_energy_today || inv.eps_energy_today, 1, "kWh"); $("#t-load-s").textContent = invOk ? `L1 ${fmtW(inv.eps_l1_power)} · L2 ${fmtW(inv.eps_l2_power)}` : "";
    const g = (tot.generators || []).find((x) => x.active) || (tot.generators || [])[0];
    $("#t-gen").textContent = tot.generator_running ? "running" : "off";
    $("#t-gen-s").textContent = g ? `${g.name.split(" (")[0]} · ${hours(g.runtime_today_h)} today${g.warming_up ? " · warming up" : ""}` : "";
    const w = state.weather?.current;
    $("#t-wx").textContent = w ? fT(w.temperature_c) : (state.weather?.station_id ? "…" : "–");
    renderSky(state.sky, w);
    $("#t-wx-s").textContent = w ? `${w.solar_radiation != null ? Math.round(w.solar_radiation) + " W/m² · " : ""}${w.humidity != null ? Math.round(w.humidity) + " % RH" : ""}` : (state.weather?.error ? "station error" : "no station");
    // plan mini
    const fp = state.forecast_summary?.plan;
    const pb = $("#pm-badge");
    if (!fp) { pb.textContent = "plan"; pb.className = "badge subtle"; $("#pm-title").textContent = "No forecast yet"; $("#pm-text").textContent = state.forecast_summary?.reason || ""; }
    else {
      const [cls, title, badge] = fp.stop_recommended ? ["warn", "Stop the generator", "stop"] : fp.recommended_now ? ["warn", "Start the generator now", "start"] : fp.needed ? ["", `Generator ${when(fp.start_at)}`, "plan"] : ["ok", "No generator needed", "plan"];
      pb.textContent = badge; pb.className = "badge " + cls; $("#pm-title").textContent = title;
      $("#pm-text").textContent = fp.needed ? `Run ${hours(fp.run_h)} · ${Math.round(fp.start_soc ?? state.total?.battery_state_of_charge ?? 0)} → ${Math.round(fp.end_soc)} %` : `Lowest point ${Math.round(fp.min_soc_without)} % ${fp.min_soc_at ? when(fp.min_soc_at) : ""}`;
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
      ["Temperature", fTr(tot.battery_temperature_min, tot.battery_temperature_max)],
      ["Protection", (tot.battery_protection_active || []).join(", ") || "none", (tot.battery_protection_active || []).length ? "bad" : "ok"],
    ]);
    $("#packs").innerHTML = (state.batteries || []).map((b) => {
      const cells = b.cells || [], mn = b.cell_min, mx = b.cell_max;
      const flag = (b.cell_imbalance || 0) >= 0.03;
      const bars = cells.map((c, i) => `<i class="${flag && c === mn ? "lo" : flag && c === mx ? "hi" : ""}${(b.balancing || []).includes(i + 1) ? " bal" : ""}" style="height:${Math.max(8, Math.min(100, ((c - 3.0) / 0.6) * 100))}%" title="cell ${i + 1}: ${c.toFixed(3)} V"></i>`).join("");
      const stale = b.age_s > 60;
      return `<div class="pack"><div class="pack-head"><span class="name">${b.name}</span><span class="badge ${stale ? "bad" : "ok"}">${stale ? "stale " + ago(b.age_s) : fmtW(b.power)}</span></div>
        <div class="pack-soc">${b.soc ?? "–"} %<small>${fmt(b.voltage, 2, "V")} · ${fmt(b.current, 1, "A")}</small></div>
        <div class="bar"><i style="width:${b.soc || 0}%"></i></div>
        <div class="cells">${bars}</div>
        <div class="pack-meta"><span>cells <b>${fmt(mn, 3)}–${fmt(mx, 3)}</b> Δ <b>${fmt(b.cell_imbalance, 3)}</b></span><span>temp <b>${(b.temperatures || []).length ? fTr(Math.min(...b.temperatures), Math.max(...b.temperatures)) : "–"}</b></span><span>cycles <b>${b.cycles ?? "–"}</b></span><span>${b.remaining_ah != null ? `<b>${b.remaining_ah}</b> / ${b.nominal_ah} Ah` : ""}</span>${(b.protection || []).length ? `<span style="color:var(--bad)">${b.protection.join(", ")}</span>` : ""}${!b.charge_fet_on || !b.discharge_fet_on ? `<span style="color:var(--warn)">FET chg ${b.charge_fet_on ? "on" : "OFF"} / dis ${b.discharge_fet_on ? "on" : "OFF"}</span>` : ""}</div></div>`;
    }).join("") || '<div class="card muted">No batteries configured.</div>';
  }

  function arc(id, v, max) {
    const el = document.getElementById(id); if (!el) return;
    const r = +el.getAttribute("r"), c = 2 * Math.PI * r;
    const f = v == null || !(max > 0) ? 0 : Math.max(0, Math.min(1, Math.abs(v) / max));
    el.style.strokeDasharray = `${(f * c).toFixed(1)} ${c.toFixed(1)}`; el.style.opacity = f > 0.005 ? 1 : 0;
  }

  // ---------- weather icon ----------
  function skyIcon(sky, w) {
    if (!sky && !w) return null;
    const day = sky ? sky.is_day : (w?.solar_radiation ?? 0) > 5, c = sky?.code;
    if (w?.precip_rate_mm_h > 0.1 || (c >= 51 && c <= 67) || (c >= 80 && c <= 82)) return (w?.temperature_c ?? 5) <= 0 && c == null ? "snow" : "rain";
    if ((c >= 71 && c <= 77) || c === 85 || c === 86) return "snow";
    if (c >= 95) return "storm";
    if (c === 45 || c === 48) return "fog";
    const cloud = sky?.cloud ?? (c === 3 ? 1 : c === 2 ? 0.5 : c != null ? 0.1 : null);
    if (cloud != null && cloud >= 0.8) return "cloud";
    if (cloud != null && cloud >= 0.35) return day ? "partly-day" : "partly-night";
    return day ? "sun" : "moon";
  }
  function renderSky(sky, w) {
    const el = $("#t-wx-ic"); if (!el) return;
    const k = skyIcon(sky, w);
    el.hidden = !k;
    if (k && el.getAttribute("data-k") !== k) {
      const sym = document.getElementById("w-" + k);  // copy the shapes in so the tile's CSS colours apply
      el.setAttribute("viewBox", sym.getAttribute("viewBox")); el.innerHTML = sym.innerHTML; el.setAttribute("data-k", k);
    }
  }

  // ---------- inverter ----------
  function renderInverter(inv) {
    const tot = (state && state.total) || {};
    $("#inv-status").textContent = inv.status || "offline"; $("#inv-status").className = "badge " + ((inv.faults || []).length ? "bad" : "subtle");
    kv($("#inv-live"), [
      ["PV1", `${fmt(inv.pv1_voltage, 0, "V")} · ${fmtW(inv.pv1_power)}`], ["PV2", `${fmt(inv.pv2_voltage, 0, "V")} · ${fmtW(inv.pv2_power)}`], ["Battery (inverter)", `${fmt(inv.battery_voltage, 1, "V")} · ${inv.battery_soc ?? "–"} % · ${fmtW(inv.battery_power)}`],
      ["AC output", `${fmt(inv.eps_voltage, 0, "V")} ${fmt(inv.eps_frequency, 2, "Hz")} · ${fmtW(inv.eps_power)}`], ["L1 / L2", `${fmt(inv.eps_l1_voltage, 0, "V")} ${fmtW(inv.eps_l1_power)} · ${fmt(inv.eps_l2_voltage, 0, "V")} ${fmtW(inv.eps_l2_power)}`], ["Load", fmtW(inv.load_power)],
      ["Temperatures", `in ${fT(inv.temperature_inner)} · rad ${fT(inv.temperature_radiator_1)} / ${fT(inv.temperature_radiator_2)}`],
      ["Faults", (inv.faults || []).join(", ") || "none", (inv.faults || []).length ? "bad" : "ok"], ["Warnings", (inv.warnings || []).join(", ") || "none", (inv.warnings || []).length ? "warn" : ""],
    ]);
    kv($("#inv-energy"), [
      ["PV today / lifetime", `${fmt(tot.pv_energy_today || inv.pv_energy_today, 1)} / ${fmt(inv.pv_energy_total, 0)} kWh`], ["Load today", fmt(tot.load_energy_today || inv.load_energy_today, 1, "kWh")], ["EPS today / lifetime", `${fmt(inv.eps_energy_today, 1)} / ${fmt(inv.eps_energy_total, 0)} kWh`],
      ["Charged today / lifetime", `${fmt(inv.battery_charge_energy_today, 1)} / ${fmt(inv.battery_charge_energy_total, 0)} kWh`], ["Discharged today / lifetime", `${fmt(inv.battery_discharge_energy_today, 1)} / ${fmt(inv.battery_discharge_energy_total, 0)} kWh`],
    ]);
    kv($("#inv-bms"), [
      ["Packs / capacity", `${inv.battery_parallel_count ?? "–"} · ${fmt(inv.battery_capacity_ah, 0, "Ah")}`], ["Max charge / discharge", `${fmt(inv.bms_max_charge_current, 0)} / ${fmt(inv.bms_max_discharge_current, 0)} A`], ["Charge V ref / cut-off", `${fmt(inv.bms_charge_voltage_ref, 1)} / ${fmt(inv.bms_discharge_cutoff_voltage, 1)} V`],
      ["Cell min / max", `${fmt(inv.bms_cell_voltage_min, 3)} / ${fmt(inv.bms_cell_voltage_max, 3)} V`], ["Cell temp min / max", `${fT(inv.bms_cell_temp_min, 1)} / ${fT(inv.bms_cell_temp_max, 1)}`], ["BMS fault / warning", `${inv.bms_fault_code ?? "–"} / ${inv.bms_warning_code ?? "–"}`],
    ]);
    const s = state.settings || {};
    kv($("#inv-diag"), [
      ["Serial", s.serial_number], ["Inverter clock", s.inverter_time], ["Modbus address / type", `${s.com_addr ?? "–"} / ${s.device_type ?? "–"}`],
      ["Status code", inv.status_code != null ? "0x" + inv.status_code.toString(16) : null], ["Bus voltages", `${fmt(inv.bus_voltage_1, 0)} / ${fmt(inv.bus_voltage_2, 0)} V`], ["Runtime", inv.runtime_seconds != null ? (inv.runtime_seconds / 86400).toFixed(1) + " days" : null], ["Data age", ago(state.inverter_age_s)],
    ]);
  }

  // ---------- charts ----------
  const STEP = { "6h": 300, "24h": 600, "48h": 900, "7d": 3600, "30d": 3 * 3600, "1y": 86400 };
  const kW = (v) => (v == null ? "–" : Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(Math.abs(v) >= 10000 ? 0 : 1)} kW` : `${Math.round(v)} W`);
  function timeTick(value, index) {
    const ch = this.chart, ts = ch.data.labels[index]; if (ts == null) return "";
    const step = ch.$step || 600, labels = ch.data.labels, span = labels[labels.length - 1] - labels[0];
    const d = new Date(ts * 1000), sec = d.getHours() * 3600 + d.getMinutes() * 60;
    const iv = span <= 8 * 3600 ? 3600 : span <= 30 * 3600 ? 4 * 3600 : span <= 3.5 * 86400 ? 12 * 3600 : 86400;
    if (iv < 86400) {
      if (sec % iv >= step) return "";
      if (sec < step && span > 30 * 3600) return d.toLocaleDateString([], { weekday: "short" });
      return d.toLocaleTimeString([], { hour: "numeric" }).replace(":00", "");
    }
    if (span > 100 * 86400) return d.getUTCDate() === 1 ? new Date(ts * 1000 + 43200000).toLocaleDateString([], { month: "short" }) : "";
    if (sec >= step) return "";
    if (span > 10 * 86400) return d.getDate() % 5 === 0 ? d.toLocaleDateString([], { month: "short", day: "numeric" }) : "";
    return d.toLocaleDateString([], { weekday: "short" });
  }
  function baseOpts({ fmtY = (v) => v, yMin, yMax, legend = true, extraScales = {}, tooltipFmt } = {}) {
    const p = palette();
    Chart.defaults.color = p.muted; Chart.defaults.borderColor = p.line; Chart.defaults.font.family = getComputedStyle(document.body).fontFamily; Chart.defaults.font.size = 11.5;
    return {
      animation: false, responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      layout: { padding: { top: 4, right: 2 } },
      plugins: {
        legend: { display: legend, position: "bottom", labels: { usePointStyle: true, pointStyle: "rectRounded", boxWidth: 9, boxHeight: 9, padding: 14, color: p.text, filter: (it) => !it.text.startsWith("_") } },
        tooltip: {
          backgroundColor: css("--card-2"), titleColor: p.text, bodyColor: p.text, borderColor: p.line, borderWidth: 1, padding: 10, boxPadding: 4, usePointStyle: true,
          filter: (it) => !it.dataset.label.startsWith("_") && it.raw != null,
          callbacks: {
            title: (items) => (items[0] ? new Date(items[0].label * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }) : ""),
            label: (it) => ` ${it.dataset.label}: ${(tooltipFmt || fmtY)(it.raw)}`,
          },
        },
      },
      scales: {
        x: { ticks: { autoSkip: false, maxRotation: 0, callback: timeTick, color: p.muted }, grid: { display: false }, border: { display: false } },
        y: { min: yMin, max: yMax, ticks: { callback: fmtY, maxTicksLimit: 5, color: p.muted }, grid: { color: (c) => (c.tick && c.tick.value === 0 ? p.muted + "88" : p.line + "99") }, border: { display: false } },
        ...extraScales,
      },
      elements: { point: { radius: 0, hoverRadius: 4, hitRadius: 8 }, line: { borderWidth: 2, tension: 0.35, borderJoinStyle: "round" } },
    };
  }
  async function fetchSeries(metrics, r = range) {
    const res = await fetch(`/api/history?metrics=${encodeURIComponent(metrics.join(","))}&range=${r}&step=${STEP[r] || 600}`);
    return (await res.json()).series;
  }
  function draw(id, labels, datasets, opts, step, type = "line") {
    if (typeof Chart === "undefined") return;
    let ch = charts[id];
    if (!ch) ch = charts[id] = new Chart(document.getElementById(id), { type, data: { labels, datasets }, options: opts });
    else { ch.data.labels = labels; ch.data.datasets = datasets; ch.options = opts; }
    ch.$step = step; ch.update();
    return ch;
  }
  function align(series, keys) {
    const ts = new Set(); keys.forEach((k) => (series[k] || []).forEach(([t]) => ts.add(t)));
    const labels = [...ts].sort((a, b) => a - b);
    const maps = keys.map((k) => new Map(series[k] || []));
    return { labels, cols: maps.map((m) => labels.map((t) => (m.has(t) ? m.get(t) : null))) };
  }
  const alpha = (c, a) => c + Math.round(a * 255).toString(16).padStart(2, "0");
  async function loadPowerChart() {
    const p = palette(); const keys = ["total/pv_power", "total/load_power", "total/battery_power", "total/generator_power"];
    const s = await fetchSeries(keys); const { labels, cols } = align(s, keys);
    const gen = cols[3].map((v) => (v ? v : null));
    draw("chart-power", labels, [
      { label: "Solar", data: cols[0], borderColor: p.pv, backgroundColor: alpha(p.pv, 0.22), fill: "origin", order: 3 },
      { label: "Generator", data: gen, borderColor: p.gen, backgroundColor: alpha(p.gen, 0.22), fill: "origin", order: 4, spanGaps: false },
      { label: "Battery", data: cols[2], borderColor: alpha(p.batt, 0.9), borderWidth: 1.5, fill: { target: "origin", above: alpha(p.batt, 0.12), below: alpha(p.cv, 0.12) }, order: 2 },
      { label: "Load", data: cols[1], borderColor: p.load, borderWidth: 2.2, order: 1 },
    ], baseOpts({ fmtY: kW, tooltipFmt: (v) => (v == null ? "–" : kW(v)) }), STEP[range]);
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
    const p = palette(), r = "48h";
    const s = await fetchSeries(["total/battery_state_of_charge", "total/generator_power"], r);
    const { labels, cols } = align(s, ["total/battery_state_of_charge", "total/generator_power"]);
    const prm = state?.forecast_summary?.params || {}, floor = prm.soc_floor ?? 25, target = prm.soc_ceiling ?? 90;
    draw("chart-soc", labels, [
      { label: "State of charge", data: cols[0], borderColor: p.batt, backgroundColor: alpha(p.batt, 0.16), fill: "origin", order: 1 },
      { label: "Generator running", data: cols[1].map((v) => (v > 0 ? 100 : null)), borderWidth: 0, backgroundColor: alpha(p.gen, 0.16), fill: "origin", stepped: true, spanGaps: false, order: 3 },
      { label: `_floor`, data: labels.map(() => floor), borderColor: alpha(css("--bad"), 0.7), borderDash: [5, 5], borderWidth: 1.2, order: 2 },
      { label: `_target`, data: labels.map(() => target), borderColor: alpha(p.muted, 0.7), borderDash: [2, 4], borderWidth: 1.2, order: 2 },
    ], baseOpts({ yMin: 0, yMax: 100, fmtY: (v) => `${v} %`, tooltipFmt: (v) => `${Math.round(v)} %` }), STEP[r]);
    $("#soc-note").innerHTML = `<span class="key k-floor"></span> floor ${floor} % <span class="key k-target"></span> generator never charges past ${target} %`;
  }
  async function loadBattChart() {
    const p = palette(), r = "48h", n = (state?.batteries || []).length || 3;
    const tk = [], ik = []; for (let i = 1; i <= n; i++) { tk.push(`battery_${i}/temperature`); ik.push(`battery_${i}/cell_imbalance`); }
    const s = await fetchSeries([...tk, ...ik], r);
    const colors = [p.batt, p.load, p.pv, p.gen, p.cv, "#2bb3b3"];
    const t = align(s, tk), im = align(s, ik);
    draw("chart-temp", t.labels, t.cols.map((c, i) => ({ label: `Pack ${i + 1}`, data: c.map(tC), borderColor: colors[i % 6] })),
      baseOpts({ fmtY: (v) => `${Math.round(v)}${TU()}`, tooltipFmt: (v) => `${v.toFixed(1)} ${TU()}` }), STEP[r]);
    draw("chart-spread", im.labels, im.cols.map((c, i) => ({ label: `Pack ${i + 1}`, data: c.map((v) => (v == null ? null : v * 1000)), borderColor: colors[i % 6] })),
      baseOpts({ yMin: 0, fmtY: (v) => `${Math.round(v)} mV`, tooltipFmt: (v) => `${v.toFixed(0)} mV` }), STEP[r]);
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
    $("#gen-list").innerHTML = gens.map((g) => {
      const fuelCls = g.fuel_pct == null ? "" : g.fuel_pct <= 15 ? "bad" : g.fuel_pct <= 35 ? "warn" : "ok";
      const oilCls = g.oil_due_in_h == null ? "" : g.oil_due_in_h <= 0 ? "bad" : g.oil_due_in_h <= 5 ? "warn" : "";
      return `<div class="gen"><div class="n">${g.name}<small>${g.running ? (g.warming_up ? "running · warming up" : "running") : "off"} · ${g.source === "signal" ? "sense wire" : "inferred"}${g.contactor != null ? ` · contactor ${g.contactor ? "closed" : "open"}` : ""}${g.active ? " · on cam switch" : ""}</small></div><div class="h">${hours(g.runtime_today_h)}<small>today · ${genTotals[g.name] != null ? genTotals[g.name].toFixed(1) + " h total" : ""}</small></div>
        ${g.fuel_pct != null ? `<div class="gen-svc"><div class="fuel"><div class="fuel-k">Fuel <b>${g.fuel_pct} %</b> · about ${hours(g.fuel_left_h)} left</div><div class="fuel-bar"><span class="${fuelCls}" style="width:${g.fuel_pct}%"></span></div></div>
          <div class="oil ${oilCls}">Oil change ${g.oil_due_in_h > 0 ? `in <b>${Math.round(g.oil_due_in_h)} h</b>` : `<b>due now</b>`} <small>every ${g.oil_change_h} h</small></div>
          <div class="svc-btns"><button class="btn sm" data-svc="refilled" data-gen="${g.name}">Tank refilled</button><button class="btn sm" data-svc="oil_changed" data-gen="${g.name}">Oil changed</button></div></div>` : ""}</div>`;
    }).join("")
      + ((tot.generator_warnings || []).map((w) => `<div class="gen" style="color:var(--warn)">⚠ ${w}</div>`).join(""));
  }
  document.addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-svc]"); if (!b) return;
    const what = b.dataset.svc, name = b.dataset.gen;
    if (!confirm(`${what === "refilled" ? "Mark the tank as full" : "Record an oil change"} for ${name}?`)) return;
    const r = await fetch(`/api/generators/${encodeURIComponent(name)}/${what}`, { method: "POST" });
    toast(r.ok ? (what === "refilled" ? "Tank marked full" : "Oil change recorded") : "Could not save");
  });
  async function loadGenerators() {
    try { const d = await (await fetch("/api/generators")).json(); genTotals = Object.fromEntries(d.generators.map((g) => [g.name, g.runtime_total_h])); if (state) renderGenLive(state.total || {}); } catch (e) {}
    loadScorecard();
  }
  const when = (ts) => {
    const d = new Date(ts * 1000), now = new Date(), tm = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const days = Math.round((new Date(d).setHours(0, 0, 0, 0) - new Date(now).setHours(0, 0, 0, 0)) / 86400000);
    return days === 0 ? `today ${tm}` : days === 1 ? `tomorrow ${tm}` : `${d.toLocaleDateString([], { weekday: "long" })} ${tm}`;
  };
  async function loadForecast() {
    const f = await (await fetch("/api/forecast")).json();
    const plan = f.plan, badge = $("#plan-badge");
    if (!f.available) {
      $("#plan-head").textContent = "No plan yet"; $("#plan-sub").textContent = f.reason || ""; $("#plan-warn").textContent = "";
      badge.textContent = "waiting"; badge.className = "badge subtle"; return;
    }
    const prm = f.params;
    if (!plan) { $("#plan-head").textContent = "Planner is off"; $("#plan-sub").textContent = ""; badge.textContent = "off"; badge.className = "badge subtle"; }
    else {
      let head, sub, cls;
      if (plan.stop_recommended) { head = "Stop the generator"; sub = `Battery has what it needs (${Math.round(f.soc_now)} %).`; cls = "warn"; }
      else if (plan.recommended_now) { head = "Start the generator now"; sub = `Run about ${hours(plan.run_h)}, to ${Math.round(plan.end_soc)} %`; cls = "warn"; }
      else if (plan.needed) { head = `Start ${when(plan.start_at)}`; sub = `Run ${hours(plan.run_h)} · ${Math.round(plan.start_soc)} → ${Math.round(plan.end_soc)} % · stops ${when(plan.end_at)}`; cls = ""; }
      else { head = "No generator needed"; sub = `Lowest point ${Math.round(plan.min_soc_without)} % ${when(plan.min_soc_at)} · next ${prm.horizon_h} h`; cls = "ok"; }
      $("#plan-head").textContent = head; $("#plan-sub").textContent = sub;
      badge.textContent = plan.stop_recommended ? "stop" : plan.recommended_now ? "start" : plan.needed ? "scheduled" : "all good"; badge.className = "badge " + cls;
      const manual = plan.manual_generator ? `<div class="plan-manual">Cam switch is on <b>${plan.manual_generator.split(" (")[0]}</b>: no start or stop signals go to Home Assistant.</div>` : "";
      $("#plan-warn").innerHTML = manual + (plan.needed && plan.floor_at ? `Without it the bank reaches the <b>${Math.round(prm.soc_floor)} %</b> floor ${when(plan.floor_at)}.` : "");
      $("#plan-next").textContent = plan.next_deficit_at ? `Another run will be needed ${when(plan.next_deficit_at)}.` : "";
      $("#plan-reason").textContent = plan.reason;
      kv($("#plan-kv"), [
        ["Energy from generator", plan.needed ? fmt(plan.energy_kwh, 1, "kWh") : "–"], ["Floor / ceiling", `${prm.soc_floor} % / ${prm.soc_ceiling} %`], ["Run length", `${prm.min_run_h}–${prm.max_run_h} h`],
        ["Battery used for planning", fmt(prm.battery_kwh, 1, "kWh")], ["Load profile", f.occupancy ? `${f.occupancy}${f.model.load_profiles?.[f.occupancy] ? "" : " (learning)"}` : "overall"],
      ]);
    }
    const src = f.sources || {}, hb = src.hours_by_source || {};
    $("#forecast-source").textContent = hb.openmeteo ? "Open-Meteo" : (hb.wunderground ? "Wunderground" : "assumed sky") + (src.openmeteo_error ? " · Open-Meteo failed" : "");
    $("#forecast-source").title = src.openmeteo_error || "";
    $("#forecast-error").textContent = !hb.openmeteo && src.openmeteo_error ? `Open-Meteo: ${src.openmeteo_error}. Retrying automatically.` : "";
    $("#forecast-source").className = "badge " + (hb.openmeteo ? "subtle" : "warn");
    const m = f.model;
    kv($("#pv-model"), [
      ["Array output on a clear day", `${m.k_clear} kW per kW/m² (${m.k_clear_source})`], ["Forecast correction", `×${m.bias}`],
      ["Expected now", f.expected_now_kw != null ? `${f.expected_now_kw.toFixed(2)} kW (actual ${f.actual_now_kw?.toFixed(2)} kW)` : state?.weather?.current?.solar_radiation != null ? `learning (${m.samples_meas} of 10 sunny hours)` : "your station has no sunlight sensor"],
      ["Typical load", `${(m.load_profile_kw.reduce((a, b) => a + b, 0) / 24).toFixed(2)} kW avg · ${m.load_profile_source}`], ["Updated", ago((Date.now() / 1000) - m.fitted_at)],
    ]);
    // outlook
    const maxE = Math.max(1, ...f.days.map((d) => Math.max(d.pv_kwh, d.load_kwh)));
    $("#forecast-days").innerHTML = f.days.filter((d, i) => i === 0 || d.hours_covered >= 12).map((d, i) => {
      const w = (state?.weather?.forecast || []).find((x) => x.date === d.date), dt = new Date(d.date + "T12:00:00");
      const pos = d.net_kwh >= 0;
      return `<div class="day">
        <div class="day-top"><span class="d">${dt.toLocaleDateString([], { weekday: "short" })} <small>${dt.toLocaleDateString([], { month: "short", day: "numeric" })}${i === 0 && d.hours_covered < 24 ? " · rest of today" : ""}</small></span>
          <span class="net ${pos ? "pos" : "neg"}">${pos ? "+" : "−"}${Math.abs(d.net_kwh).toFixed(1)} kWh</span></div>
        <div class="day-bars"><span class="lab">Solar</span><div class="bar"><i class="pv" style="width:${d.pv_kwh / maxE * 100}%"></i></div><span class="val">${d.pv_kwh.toFixed(1)}</span>
          <span class="lab">Use</span><div class="bar"><i class="ld" style="width:${d.load_kwh / maxE * 100}%"></i></div><span class="val">${d.load_kwh.toFixed(1)}</span></div>
        <div class="day-wx">${Math.round(d.cloud_avg * 100)} % cloud${d.snow_risk ? " · snow on panels likely" : ""}${d.temp_min_c != null ? ` · low ${fT(d.temp_min_c)}` : ""}${w ? ` · ${w.narrative}` : ""}</div></div>`;
    }).join("");
    // 72 h chart
    const p = palette(), labels = f.hours.map((h) => h.ts);
    const hourly = (pts) => { const mm = new Map(); (pts || []).forEach(([t, v]) => { const k = Math.floor(t / 3600) * 3600; if (!mm.has(k)) mm.set(k, v); }); return mm; };
    const base = hourly(plan?.baseline), withp = hourly(plan?.planned);
    const run = (t) => plan?.needed && plan.start_at && t + 3600 > plan.start_at && t < plan.end_at;
    const ds = [
      { type: "bar", label: "Solar forecast", data: f.hours.map((h) => h.pv_kw * 1000), backgroundColor: alpha(p.pv, 0.75), borderRadius: 3, yAxisID: "y", order: 4, barPercentage: 1, categoryPercentage: 0.82 },
      { type: "line", label: "Expected use", data: f.hours.map((h) => h.load_kw * 1000), borderColor: alpha(p.load, 0.85), borderWidth: 1.6, yAxisID: "y", order: 3, tension: 0.4 },
    ];
    if (plan) {
      ds.push({ type: "bar", label: "Generator run", data: labels.map((t) => (run(t) ? 100 : null)), backgroundColor: alpha(p.gen, 0.22), yAxisID: "y1", order: 5, barPercentage: 1, categoryPercentage: 1 });
      ds.push({ type: "line", label: "Battery, with plan", data: labels.map((t) => withp.get(t) ?? null), borderColor: p.batt, borderWidth: 2.4, yAxisID: "y1", order: 1 });
      ds.push({ type: "line", label: "Battery, no generator", data: labels.map((t) => base.get(t) ?? null), borderColor: alpha(p.muted, 0.8), borderDash: [5, 4], borderWidth: 1.4, yAxisID: "y1", order: 2 });
      ds.push({ type: "line", label: "_floor", data: labels.map(() => prm.soc_floor), borderColor: alpha(css("--bad"), 0.6), borderDash: [3, 4], borderWidth: 1, yAxisID: "y1", order: 2 });
    }
    const o = baseOpts({ fmtY: kW, tooltipFmt: kW, yMin: 0, extraScales: {
      y1: { position: "right", min: 0, max: 100, grid: { drawOnChartArea: false }, border: { display: false }, ticks: { callback: (v) => `${v}%`, maxTicksLimit: 5, color: p.muted } } } });
    o.plugins.tooltip.callbacks.label = (it) => ` ${it.dataset.label}: ${it.dataset.yAxisID === "y1" ? (it.dataset.label === "Generator run" ? "running" : Math.round(it.raw) + " %") : kW(it.raw)}`;
    draw("chart-forecast", labels, ds, o, 3600, "bar");
  }
  async function loadScorecard() {
    try {
      const rows = await (await fetch("/api/scorecard?days=14")).json();
      if (!rows.length) { $("#scorecard").innerHTML = '<div class="muted small">No history yet.</div>'; return; }
      const pct = (v) => (v == null ? "–" : Math.round(v * 100) + " %");
      $("#scorecard").innerHTML = `<table><tr><th>Day</th><th class="num">Starts</th><th class="num">Run</th><th class="num">kWh</th><th class="num wide">kWh/h</th><th class="num wide">Full current</th><th class="num">At floor</th><th class="num wide">SOC</th><th class="num">Wasted sun</th></tr>` +
        rows.map((r) => `<tr><td>${r.date.slice(5)}${r.partial_day ? "*" : ""}</td><td class="num">${r.starts}</td><td class="num">${r.run_h.toFixed(1)} h</td><td class="num">${r.kwh}</td><td class="num wide">${r.kwh_per_run_h ?? "–"}</td><td class="num wide">${pct(r.full_current_share)}</td><td class="num" style="color:${r.hours_at_floor > 0 ? "var(--bad)" : "inherit"}">${r.hours_at_floor.toFixed(1)} h</td><td class="num wide">${r.soc_min != null ? Math.round(r.soc_min) : "–"}–${r.soc_max != null ? Math.round(r.soc_max) : "–"} %</td><td class="num" style="color:${r.curtailed_kwh > 1 ? "var(--warn)" : "inherit"}">${r.curtailed_kwh} kWh</td></tr>`).join("") + "</table>";
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

  // ---------- setup (one form, one Save) ----------
  let setupLoaded = null;
  const esc = (v) => String(v ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
  const fld = (sec, key, label, kind, v, opt = {}) => {
    const id = `su-${sec}-${key}`, d = `data-sec="${sec}" data-key="${key}" data-kind="${kind}"`;
    if (kind === "bool") return `<div class="field check ${opt.wide ? "wide" : ""}"><input type="checkbox" id="${id}" ${d} ${v ? "checked" : ""}><label for="${id}">${label}</label></div>`;
    const type = kind === "number" ? 'type="number" inputmode="decimal" step="any"' : kind === "password" ? 'type="password" autocomplete="off"' : "";
    const val = kind === "password" ? "" : kind === "list" ? (v || []).join(", ") : v ?? "";
    return `<div class="field ${opt.wide ? "wide" : ""}"><label for="${id}">${label}</label><input id="${id}" ${d} ${type} value="${esc(val)}" placeholder="${esc(opt.ph ?? (kind === "password" && v ? "•••••• (saved)" : ""))}" ${opt.list ? `list="${opt.list}"` : ""}></div>`;
  };
  async function loadSetup() {
    const d = await (await fetch("/api/setup")).json(); setupLoaded = d.sections;
    const w = d.sections.weather.values, pl = d.sections.planner.values, m = d.sections.mqtt.values, inv = d.sections.inverter.values, oc = d.sections.occupancy.values;
    const batRow = (b = {}) => `<div class="su-row" data-row="battery"><div class="field"><label>Name</label><input data-k="name" value="${esc(b.name)}"></div><div class="field"><label>Port</label><input data-k="port" list="port-list" value="${esc(b.port)}" placeholder="/dev/ttyACM1"></div><button class="btn rm" type="button">✕</button></div>`;
    const genRow = (g = {}) => `<div class="su-row" data-row="generator" data-rt="${esc(g.running_topic)}" data-ct="${esc(g.contactor_topic)}" data-notes="${esc(g.notes)}"><div class="field wide" style="grid-column:1/3"><label>Name</label><input data-k="name" value="${esc(g.name)}"></div><button class="btn rm" type="button">✕</button></div>`;
    $("#setup").innerHTML = `
      <div class="su-group"><h3>Cabin location</h3><div class="su-grid">
        ${fld("weather", "latitude", "Latitude", "number", w.latitude, { ph: "39.1234" })}${fld("weather", "longitude", "Longitude", "number", w.longitude, { ph: "-105.1234" })}</div>
        <div class="su-note">Needed for the solar forecast and generator plan.</div></div>
      <div class="su-group"><h3>Display units</h3><div class="su-grid">
        <div class="field"><label for="su-display-temperature">Temperature</label><select id="su-display-temperature" data-sec="display" data-key="temperature" data-kind="text">${["F", "C"].map((u) => `<option value="${u}" ${d.sections.display.values.temperature === u ? "selected" : ""}>°${u}</option>`).join("")}</select></div>
        <div class="field"><label for="su-display-wind">Wind</label><select id="su-display-wind" data-sec="display" data-key="wind" data-kind="text">${["mph", "km/h"].map((u) => `<option ${d.sections.display.values.wind === u ? "selected" : ""}>${u}</option>`).join("")}</select></div>
        <div class="field"><label for="su-display-rain">Rain</label><select id="su-display-rain" data-sec="display" data-key="rain" data-kind="text">${["in", "mm"].map((u) => `<option ${d.sections.display.values.rain === u ? "selected" : ""}>${u}</option>`).join("")}</select></div></div></div>
      <div class="su-group"><h3>Solar, battery, generator</h3><div class="su-grid">
        ${fld("planner", "pv_kw_peak", "Solar array (kW)", "number", pl.pv_kw_peak)}<div class="field"><label for="su-gen-amps">Chargeverter charge current (A)</label><input id="su-gen-amps" type="number" inputmode="decimal" step="any" data-sec="planner" data-key="generator_dc_kw" data-kind="amps" value="${pl.generator_dc_kw != null ? Math.round(pl.generator_dc_kw * 1000 / 52) : ""}" placeholder="46"></div>
        ${fld("planner", "soc_floor", "Lowest battery charge (%)", "number", pl.soc_floor)}${fld("planner", "soc_ceiling", "Generator never charges past (%)", "number", pl.soc_ceiling)}
        ${fld("planner", "min_run_h", "Shortest run (h)", "number", pl.min_run_h)}${fld("planner", "quiet_hours", "Quiet hours", "list", pl.quiet_hours, { ph: "22:00-07:00" })}</div>
        <p class="su-note">The planner picks when to start and how long to run to use the least fuel and engine hours: fewer, longer runs, and never charging what the sun will cover.</p></div>
      <div class="su-group"><h3>Devices</h3>
        <div class="su-grid">${fld("inverter", "port", "Inverter port", "text", inv.port, { list: "port-list", wide: true })}</div>
        <div class="su-rows" id="su-bats" style="margin-top:10px">${d.sections.batteries.values.map(batRow).join("")}</div>
        <button class="btn" type="button" id="su-bat-add">+ Battery</button>
        <div class="su-note" id="port-scan"></div>
        <div style="margin-top:10px">${fld("inverter", "allow_writes", "Allow changing inverter settings (off = read-only)", "bool", inv.allow_writes)}</div></div>
      <div class="su-group"><h3>Generators on the cam switch</h3><div class="su-rows" id="su-gens">${d.sections.generators.values.map(genRow).join("")}</div><button class="btn" type="button" id="su-gen-add">+ Generator</button></div>
      <div class="su-group"><h3>Weather Underground station (optional)</h3>${fld("weather", "enabled", "Use my station", "bool", w.enabled)}
        <div class="su-grid" style="margin-top:8px">${fld("weather", "station_id", "Station ID", "text", w.station_id, { ph: "KCOxxxxx" })}${fld("weather", "api_key", "API key", "password", w.api_key)}</div></div>
      <div class="su-group"><h3>Home Assistant / MQTT (optional)</h3>${fld("mqtt", "enabled", "Publish to MQTT", "bool", m.enabled)}<div class="su-note" id="mqtt-status"></div>
        <div class="su-grid" style="margin-top:8px">${fld("mqtt", "host", "Broker address (Home Assistant's IP)", "text", m.host, { ph: "192.168.1.50" })}${fld("mqtt", "port", "Port", "number", m.port)}${fld("mqtt", "username", "Username", "text", m.username)}${fld("mqtt", "password", "Password", "password", m.password)}</div></div>
      <div class="su-group su-adv"><details><summary>Advanced</summary><div class="su-grid" style="margin-top:8px">
        ${fld("inverter", "baud", "Inverter baud", "number", inv.baud)}${fld("inverter", "unit", "Inverter Modbus ID", "number", inv.unit)}${fld("inverter", "poll_interval", "Inverter poll (s)", "number", inv.poll_interval)}${fld("planner", "battery_kwh", "Battery kWh (blank = auto)", "number", pl.battery_kwh)}
        ${fld("planner", "soc_margin", "Planning margin (%)", "number", pl.soc_margin)}${fld("planner", "storm_lookahead_h", "Lookahead for extra starts (h)", "number", pl.storm_lookahead_h)}${fld("planner", "start_cost_h", "Cost of one start (engine h)", "number", pl.start_cost_h)}${fld("planner", "max_run_h", "Longest run (h)", "number", pl.max_run_h)}
        ${fld("planner", "horizon_h", "Forecast horizon (h)", "number", pl.horizon_h)}${fld("planner", "charge_efficiency", "Charge efficiency", "number", pl.charge_efficiency)}${fld("weather", "units", "WU units (e/m)", "text", w.units)}${fld("mqtt", "prefix", "MQTT topic prefix", "text", m.prefix)}
        ${fld("occupancy", "modes", "Occupancy modes", "list", oc.modes)}${fld("occupancy", "default", "Default occupancy", "text", oc.default)}
        ${fld("weather", "openmeteo_enabled", "Open-Meteo forecast", "bool", w.openmeteo_enabled, { wide: true })}${fld("mqtt", "ha_discovery", "Home Assistant discovery", "bool", m.ha_discovery, { wide: true })}${fld("planner", "enabled", "Generator planner", "bool", pl.enabled, { wide: true })}
      </div></details></div>`;
    const wire = (boxId, addId, rowFn, blank) => {
      const box = $(boxId);
      box.addEventListener("click", (e) => { if (e.target.classList.contains("rm")) e.target.closest(".su-row").remove(); });
      $(addId).addEventListener("click", () => box.insertAdjacentHTML("beforeend", rowFn(blank(box.children.length + 1))));
    };
    wire("#su-bats", "#su-bat-add", batRow, (n) => ({ name: `Battery ${n}`, port: "" }));
    wire("#su-gens", "#su-gen-add", genRow, (n) => ({ name: `Generator ${n}` }));
    scanPorts();
  }
  async function scanPorts() {
    const out = $("#port-scan"); if (!out) return;
    try {
      const d = await (await fetch("/api/setup/ports")).json();
      let dl = document.getElementById("port-list"); if (!dl) { dl = document.createElement("datalist"); dl.id = "port-list"; document.body.appendChild(dl); }
      dl.innerHTML = d.ports.filter((p) => p.device).map((p) => `<option value="${p.device}">`).join("");
      const devs = d.ports.filter((p) => p.device && !/ttyS\d/.test(p.device));
      out.innerHTML = devs.length ? "Adapters found: " + devs.map((p) => `<code>${p.device}</code>${p.used_by ? " " + p.used_by : ""}`).join(" · ") : "No USB serial adapters visible to the app.";
    } catch (e) { out.textContent = ""; }
  }
  async function saveSetupAll() {
    if (!setupLoaded) return;
    const btn = $("#setup-save"); btn.disabled = true; btn.textContent = "Saving…";
    const bySec = {};
    document.querySelectorAll("#setup [data-sec]").forEach((i) => {
      const { sec, key, kind } = i.dataset; let v;
      if (kind === "bool") v = i.checked;
      else if (kind === "password") { if (!i.value) return; v = i.value; }
      else if (kind === "list") v = i.value.split(",").map((x) => x.trim()).filter(Boolean);
      else if (kind === "number") v = i.value === "" ? null : Number(i.value);
      else if (kind === "amps") { v = i.value === "" ? null : Math.round(Number(i.value) * 52) / 1000; if (v != null && Math.abs(v - setupLoaded[sec].values[key]) < 0.03) return; }
      else v = i.value.trim();
      const cur = setupLoaded[sec].values[key];
      if (JSON.stringify(v) !== JSON.stringify(cur)) (bySec[sec] = bySec[sec] || {})[key] = v;
    });
    const bats = [...document.querySelectorAll('#su-bats .su-row')].map((r, i) => { const o = { ...(setupLoaded.batteries.values[i] || {}) }; r.querySelectorAll("input").forEach((x) => (o[x.dataset.k] = x.value.trim())); o.enabled = true; return o; });
    if (JSON.stringify(bats) !== JSON.stringify(setupLoaded.batteries.values)) bySec.batteries = bats;
    const gens = [...document.querySelectorAll('#su-gens .su-row')].map((r) => ({ name: r.querySelector("input").value.trim(), notes: r.dataset.notes || "", running_topic: r.dataset.rt || "", contactor_topic: r.dataset.ct || "" }));
    const curG = setupLoaded.generators.values.map((g) => ({ name: g.name, notes: g.notes || "", running_topic: g.running_topic || "", contactor_topic: g.contactor_topic || "" }));
    if (JSON.stringify(gens) !== JSON.stringify(curG)) bySec.generators = gens.map((g, i) => ({ ...(setupLoaded.generators.values.find((x) => x.name === g.name) || {}), ...g }));
    const errors = []; let saved = 0;
    for (const [section, values] of Object.entries(bySec)) {
      const r = await fetch("/api/setup", { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ section, values }) });
      if (r.ok) saved++; else errors.push(`${section}: ${(await r.json()).detail}`);
    }
    btn.disabled = false; btn.textContent = "Save";
    toast(errors.length ? errors.join(" · ") : saved ? "Saved" : "Nothing changed", errors.length ? 7000 : 2500);
    if (saved) { await loadSetup(); setTimeout(() => fetch("/api/forecast/recompute", { method: "POST" }).catch(() => {}), 2500); }
  }
  $("#setup-save").addEventListener("click", saveSetupAll);

  // ---------- system ----------
  function renderMqttStatus() {
    const el = $("#mqtt-status"); if (!el) return;
    const d = state?.devices?.["mqtt:broker"];
    if (!d) { el.textContent = ""; return; }
    el.innerHTML = d.connected ? `<span style="color:var(--ok)">● Connected to ${d.port}</span> · ${d.stats?.discovered ?? 0} entities sent to Home Assistant`
      : `<span style="color:var(--bad)">● Not connected</span>${d.last_error ? ` · ${d.last_error}` : " · trying…"}`;
  }
  function renderSystem() {
    renderMqttStatus();
    const dv = state?.devices || {};
    $("#devices").innerHTML = Object.values(dv).map((d) => `<div class="dev ${d.connected ? "ok" : ""}"><span class="dot"></span><div class="n">${d.name}<small>${d.kind} · <code>${d.port}</code>${d.last_error ? ` · ${d.last_error}` : ""}</small></div><div class="m">${d.connected ? ago(d.seconds_since_ok) : "down"}<br>${d.poll_count} / ${d.error_count} err</div></div>`).join("");
    const w = state?.weather || {}, c = w.current;
    kv($("#weather-now"), c ? [
      ["Station", `${c.station_id} · ${ago(c.age_s)}`], ["Temperature", fT(c.temperature_c, 1)], ["Humidity", fmt(c.humidity, 0, "%")], ["Solar radiation", c.solar_radiation != null ? fmt(c.solar_radiation, 0, "W/m²") : "no sensor"],
      ["UV", c.uv], ["Wind", U().wind === "mph" ? `${fmt(c.wind_kph != null ? c.wind_kph / 1.609 : null, 0, "mph")} gust ${fmt(c.wind_gust_kph != null ? c.wind_gust_kph / 1.609 : null, 0)}` : `${fmt(c.wind_kph, 0, "km/h")} gust ${fmt(c.wind_gust_kph, 0)}`],
      ["Pressure", U().temperature === "F" ? fmt(c.pressure_hpa != null ? c.pressure_hpa / 33.8639 : null, 2, "inHg") : fmt(c.pressure_hpa, 1, "hPa")],
      ["Rain today", U().rain === "in" ? fmt(c.precip_total_mm != null ? c.precip_total_mm / 25.4 : null, 2, "in") : fmt(c.precip_total_mm, 1, "mm")],
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
