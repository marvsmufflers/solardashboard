# solardash – design notes

A self-hosted replacement for Solar Assistant for one specific **off-grid cabin**. There is no utility
connection, so nothing in solardash is modelled as "grid": the 6000XP's AC input is a generator input.

Generators: three Honda EU3000is. One is modified for remote start and lives in the outhouse (the
auto-start unit), one is plugged in on the porch during winter stays when it runs for long stretches.
**Both feed the Chargeverter** through a cam switch in the cabin that selects one or the other; nothing
is connected to the inverter's own AC input.

solardash therefore has one generator *path* (the Chargeverter) and a list of generators
(`generators:` in the config). The cam-switch position cannot be read, so it is a setting in the UI
(`PUT /api/generators/active`, remembered in SQLite). Chargeverter output above the noise threshold
means "generator running", and run time accrues to the selected generator: per-day hours in the
`daily_energy` table (`generator_runtime_h/<name>`), lifetime totals on `/api/generators`, start/stop
and cam-switch events in the event log. Handy for EU3000is oil-change intervals.

The auto-start generator has a 12 V run-sense wire read by a Waveshare Modbus relay in Home Assistant;
HA waits ~90 s for warm-up before closing the contactor to the Chargeverter. solardash accepts both
states per generator (`running_topic` / `contactor_topic` over MQTT, or `PUT
/api/generators/<name>/signal`). A fresh signal wins over inference for that generator, which gives
true engine hours including warm-up, and a "warming up" state while running with the contactor open.
Cross-checks raise warnings: signal says running but the cam switch selects the other generator; or
contactor closed but no Chargeverter output is inferred. Signals older than `signal_stale_s` are
ignored so a dead HA link falls back to inference.

If the inverter's AC input ever shows voltage, the dashboard flags it as unexpected.

Devices:

| Device | Role | Interface | Protocol | Status in solardash |
|---|---|---|---|---|
| EG4 6000XP | off-grid inverter / charger | RS485 (dongle / INV485 port) | Modbus RTU 19200 8N1, unit 1, Luxpower register map | read all live data, read/write settings |
| 3 × Eco-Worthy 48V 100Ah rack | LiFePO4 packs, JBD UP16S015 BMS | RS485-1 RJ45, one USB adapter channel per pack | JBD "Xiaoxiang" 0xDD protocol, 9600 8N1 | read everything (cells, temps, SOC, protection) |
| EG4 Chargeverter GC | generator → battery charger | RJ45 "BMS" port, pins 1 (B) / 2 (A) | **Modbus RTU master** at 9600 8N1 polling a battery | sniff the bus; optionally answer as the battery; infer its output power |

## What the current Solar Assistant install tells us

The existing site (Docker on a Synology DS225+) uses:

* Inverter driver **"Luxpower SNA"**, serial, model 740, serial number 3473740399, on `ttyACM0` at 19200 baud.
* Battery driver **"USB JBD RS485" × 3** – one serial channel per pack.
* A single 4-port USB CDC-ACM serial adapter (`ttyACM0..3`, `usb 1-2.2`) carries all four links.
  `dmesg` on that box shows the adapter disconnecting and re-enumerating every ~2 s around 06:52 with
  `device descriptor read/64, error -32`. That is a USB-level problem (cable, hub power or the adapter
  itself), not software. solardash reopens ports after errors, but a flapping adapter will still show
  as repeated "lost/connected" events in the System tab.
* The inverter is in closed-loop with the batteries (BMS max current 300 A, cell temps, "discharge
  according to SOC" are visible in the inverter), so the packs talk to the inverter over CAN/RS485
  on their inverter-protocol port while the JBD protocol is read from RS485-1.

## 1. EG4 6000XP (Luxpower)

* Plain Modbus RTU; the "SN-framed" Luxpower TCP dialect is only used by the WiFi dongle.
* Input registers (FC04) 0–199 are read in five 40-register blocks. Decoders live in
  `solardash/protocols/luxpower.py::INPUT_REGISTERS`. Notable quirks:
  * reg 5 packs SOC (low byte) and SOH (high byte);
  * 32-bit energy counters are low-word first;
  * reg 77 bit 0 says whether the AC input is configured as **generator**;
  * regs 12/15/27 (vendor names "grid voltage/frequency/power to user") are exposed as
    `ac_input_voltage`, `ac_input_frequency`, `ac_input_power` and kept only as diagnostics, since
    nothing is connected to that input here;
  * reg 123 ("generator power") and 124–126 are not real measurements on the XP firmware – not used;
    generator power in solardash is the inferred Chargeverter output;
  * settings that only mean something with a utility connection (export, on-grid working mode,
    on-grid end-of-discharge) are not exposed; "AC charge" settings are kept because they govern
    charging from the generator on the AC input;
  * reg 98 battery current is 0.1 A/LSB (verified by others on a 6000XP; some docs say 0.01).
* Holding registers (FC03) are decoded into a curated settings table (`SETTINGS`) with documented
  ranges. Writes (FC06) are range checked, use read-modify-write for bit fields (reg 21, 110, 120, 179),
  and are read back for verification. Writes are disabled unless `inverter.allow_writes: true`.
  Settings marked `danger` require an explicit confirmation from the UI/API.
* Status codes, fault bits and warning bits are decoded to text.

### Wiring

Either the 4-pin dongle connector (black = A, white = B) or the **INV485** RJ45 jack:
pin 7 = B, pin 8 = A (T568B: white/brown and brown). The WiFi dongle shares this bus; it is fine to
leave it plugged in only if it is not also being polled by another master – EG4's cloud dongle does poll.
Enable 120 Ω termination on the adapter if it has a switch.

## 2. Eco-Worthy packs (JBD)

* Request `DD A5 03 00 FF FD 77` → basic info (pack V, I, remaining/nominal Ah, cycles, protection
  bits, SOC, FET state, NTC temperatures); `DD A5 04 00 FF FC 77` → 16 cell voltages;
  `DD A5 05 00 FF FB 77` → hardware version string.
* Checksum = two's complement of the sum of (cmd, len, data). Current is signed, positive = charging.
* Each pack is its own point-to-point link, exactly like Solar Assistant's "USB JBD RS485" driver.
  RS485-1 RJ45 pins 7/8 carry A/B on these packs (same as the inverter's INV485 jack).
* Bank totals (`total/*`) are computed from the packs: capacity-weighted SOC, summed current,
  min/max cell and temperature, union of active protections.

## 3. Chargeverter GC

In this system the Chargeverter is just an AC→DC charger: Home Assistant starts the generator (12 V
run-sense feedback) and closes the contactor after warm-up, so the charger's own generator-start
relay and SOC-reading port are unused. solardash needs no connection to it; its output is inferred
(see "Output power" below). The RS485 sniff/emulate support that follows is optional and off by
default.

There is no register to read *from* the Chargeverter. It is a Modbus **master**: every ~500 ms it
sends `01 03 00 13 00 11 74 03` (read 17 holding registers from unit 1 starting at 0x13) to whatever
is on its BMS RJ45 (pin 1 = RS485-B, pin 2 = RS485-A, 9600 8N1) and uses the SOC in the reply to
start/stop the generator. With Eco-Worthy packs (which speak Pylontech/Growatt-style protocols, not
EG4's) it gets no answer and shows **LOST**, falling back to voltage-only control.

solardash therefore offers two modes on a dedicated RS485 adapter wired to that jack:

* **sniff** – listen only. Shows whether the charger is powered/polling and what SOC (if anything) is
  on the bus. Safe; it never transmits.
* **emulate** – answer the polls with a battery image built from the real JBD data
  (`protocols/chargeverter.py::BatteryImage`). Register layout (verified by two independent
  projects, SOC and voltage confirmed to be used by the charger):

  | reg | meaning | unit |
  |---|---|---|
  | 0x14 | current (signed) | 0.1 A |
  | 0x15 | **SOC** | % |
  | 0x16 | **voltage** | 0.01 V |
  | 0x17 / 0x1D | charge current limit | 0.1 A |
  | 0x18 | SOH | % |
  | 0x19 | SOC mirror | % |
  | 0x1A / 0x1B | max / min temperature (signed) | 0.1 °C |
  | 0x1C | capacity | 0.01 Ah |
  | 0x1E | discharge current limit | 0.1 A |
  | 0x1F | charge voltage limit | 0.01 V |
  | 0x20 / 0x21 | cycles low / high word | |

  Fail-safe: if no pack has reported within `emulate.stale_after` seconds, solardash stops answering
  and the Chargeverter reverts to its own LOST/voltage behaviour. In emulate mode nothing else may be
  on that bus (the real battery is not connected to it anyway).

* **Output power** is inferred, not measured: the BMSs see the total current into the bank and the
  inverter reports its own charge/discharge power, so
  `chargeverter_power = Σ(pack V·I) − (inverter charge − discharge)`. Below `power_threshold_w`
  (default 150 W) it is reported as 0 to hide sensor noise. A Shelly/CT on the Chargeverter's AC
  input would be the measured alternative.

## 4. Weather, PV forecast and generator planning

* **Open-Meteo** (`weather/openmeteo.py`): hourly `shortwave_radiation` (GHI), direct/diffuse,
  cloud, temperature, snowfall for 7 days plus 2 past days, free and keyless. Values are means over
  the preceding hour and are re-keyed to hour start. This is the primary PV input: `pv = k_clear ×
  GHI × bias`, no cloud-to-irradiance heuristic needed. Forecast GHI is logged (`weather/ghi_forecast`).
* **Weather Underground** (`weather/wunderground.py`): the cabin's PWS gives current conditions every
  5 min (including `solarRadiation` W/m² when the station has a pyranometer) and a 5-day forecast with
  day/night cloud cover, used as the fallback source and for the narrative. Field names follow TWC's
  schemas; `solardash probe weather` dumps raw responses to verify against a real account
  (api.weather.com was not reachable from the development environment).
* **Occupancy** (`occupancy:` config, kv `occupancy_mode`, sample `total/occupancy`): load profiles are
  learned per mode over 60 days; the planner uses the current mode's profile. Load is the dominant
  uncertainty for a cabin, so this matters more than solar model refinements.
* **Clear-sky model** (`weather/solar.py`): NOAA solar position + Haurwitz GHI, hourly averages.
* **PV model** (`forecast/model.py`): `pv = k_clear × GHI_clear × (1 − 0.75·cloud^3.4) × bias`.
  `k_clear` is the 90th percentile of pv/GHI_clear over 30 days of daylight hours (what the array does
  when it is clear), clamped around the configured array size; `bias` is actual/predicted over the
  last 7 days using the predictions logged at forecast time (`forecast/pv_power_pred`); `k_meas`
  (per measured W/m²) gives an "expected now" so under-performance (snow, shading) is visible.
  Load profile: median load by local hour over 14 days.
* **Planner** (`forecast/planner.py`): 15-minute SOC simulation over 72 h. The generator's load is fixed
  by the Chargeverter, so the objective is engine hours (fuel and wear). If SOC would cross floor +
  margin, the latest safe start is found (a minimum run from there keeps SOC above the floor and stays
  out of quiet hours); then every stop point from a minimum run up to `soc_ceiling` is scored:
  run hours + later run hours + `start_cost_h` per later start (later starts beyond `storm_lookahead_h`
  weigh half) + charge left above the floor when the sun refills the bank (fuel the sun would have
  replaced). The cheapest wins; ties go to the lower stop. While a generator runs, the same search
  advises the stop, never before `min_run_h`. Output is advisory: `plan/recommended_now`,
  `plan/stop_recommended`, times and reason on the API and MQTT.
* **Fuel and oil**: engine hours since the last refill / oil change per generator (kv `generator_wear`),
  tank level = `tank_hours` − hours since refill; reset from the app, `POST /api/generators/<name>/refilled`
  or `/oil_changed`, or the Home Assistant buttons.
* **Scorecard** (`forecast/scorecard.py`): per-day starts, run hours, inferred kWh, kWh/h, share of
  run time ≥ 85 % of peak Chargeverter output, hours at/below the floor, curtailed solar (full bank
  while the clear-sky model says the array could have made ≥ 30 % more).

## Architecture

```
          ┌──────────────┐  Modbus RTU 19200  ┌────────────┐
          │ 6000XP       │◄──────────────────►│ ttyACM0    │
          └──────────────┘                    │            │   ┌───────────────┐
          ┌──────────────┐  JBD 9600          │ 4-port USB │   │  solardash    │
          │ pack 1..3    │◄──────────────────►│ serial     │──►│  (asyncio)    │
          └──────────────┘                    │ ttyACM1..3 │   │               │
          ┌──────────────┐  Modbus master 9600└────────────┘   │ pollers → state → derived
          │ Chargeverter │◄──────────────────► ttyUSB0 ───────►│   ├─ SQLite history
          └──────────────┘ (sniff / emulate)                   │   ├─ FastAPI REST + WS + UI
                                                               │   └─ MQTT (SA-compatible topics)
                                                               └───────────────┘
```

* `protocols/` – pure, testable codecs (Modbus RTU, Luxpower map, JBD, Chargeverter image).
* `devices/` – one poller per serial link; blocking pyserial I/O runs in worker threads; ports are
  reopened lazily after any error (USB replug survives); backoff up to 60 s.
* `core/state.py` – latest snapshot + event bus. `core/derived.py` – totals and Chargeverter inference.
* `storage/sqlite.py` – `samples` (raw, default 15 s, kept 14 days) and `hourly` rollups (forever),
  `daily_energy` from the inverter's today counters, `events`.
* `api/` – `/api/state`, `/api/history`, `/api/settings`, `/api/devices`, `/api/chargeverter`, `/ws`;
  the dashboard is static HTML/JS with a vendored Chart.js.
* `mqtt/` – publishes `solar_assistant/inverter_1/<metric>/state`, `battery_<n>/…`, `total/…`,
  `chargeverter_1/…` with Home Assistant discovery, and accepts `inverter_1/<setting>/set`.
* `devices/simulated.py` – a consistent fake system (PV by time of day, wandering load, three packs,
  a Chargeverter that starts the "generator" below 50 % and stops above 90 %) so everything runs and
  is tested without hardware.

## Open questions / next steps

1. **Chargeverter wiring**: a fifth RS485 channel is needed (USB adapter or a spare port). Start in
   `sniff` mode and run `solardash probe sniff` to confirm the `01 03 00 13 00 11 74 03` polls.
2. **Which battery protocol port is the inverter using** (CAN vs RS485) – only matters if the 6000XP
   closed loop ever needs to be re-plumbed; solardash does not touch it.
3. Register scaling that could not be verified without hardware: input reg 98 (0.1 vs 0.01 A) and
   81/82. Compare the Inverter tab against the inverter's own screen once running and adjust
   `luxpower.py` if needed.
4. Writing the inverter clock (holding 12–14) and quick-charge style automations are not implemented
   yet; the settings engine supports adding them.
5. USB stability on the Synology: the reconnect storm in `dmesg` should be fixed at the hardware level
   (powered hub / different port / cable) regardless of which software reads the bus.
