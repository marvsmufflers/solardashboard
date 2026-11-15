# solardash

Local, self-hosted monitoring and control for an **off-grid cabin** built from an **EG4 6000XP**,
**Eco-Worthy 48V 100Ah server-rack batteries** (JBD BMS), an **EG4 Chargeverter GC** and Honda
EU3000is generators – a replacement for Solar Assistant that runs in Docker on a NAS or on a
Raspberry Pi. There is no grid anywhere in the data model; the generators feed the battery through the
Chargeverter.

* Live dashboard (power flow, SOC, per-cell battery view, inverter details) with history charts
* Generator tracking for the Hondas that feed the Chargeverter: pick which one is on the cam switch in
  the UI, and solardash logs start/stop events and run hours per generator (per day and lifetime)
* Reads the inverter over Modbus RTU (RS485) and each battery over the JBD protocol – no cloud
* Inverter settings read/write with range checks and confirmation for risky ones (opt-in)
* Chargeverter support: listen to its battery bus, infer its output power, and optionally answer its
  polls with real pack data so it stops showing *LOST* and can start/stop the generator on SOC
* SQLite history with hourly rollups; MQTT output using Solar Assistant's topic names plus
  Home Assistant discovery
* Simulation mode and a serial probe tool, so you can try everything before touching hardware

See [docs/DESIGN.md](docs/DESIGN.md) for protocol details, wiring and architecture.

## Quick start (no hardware)

```bash
pip install -e .[dev]
solardash serve --simulate --port 8080
```

Open http://localhost:8080.

## Quick start (real hardware, Docker on Synology)

1. Plug in the USB-RS485 adapters and find them:

   ```bash
   docker compose run --rm solardash solardash probe list
   docker compose run --rm solardash solardash probe all
   ```

   `probe all` tries the Luxpower/EG4 inverter protocol and then the JBD protocol on every port and
   prints what answered. To identify a single port:

   ```bash
   solardash probe inverter --port /dev/ttyACM0
   solardash probe battery  --port /dev/ttyACM1
   solardash probe sniff    --port /dev/ttyUSB0 --seconds 20   # Chargeverter bus
   ```

2. `cp config.example.yaml config.yaml` and set the ports (prefer `/dev/serial/by-id/...` names).
3. `docker compose up -d` and open http://nas-ip:8080.

Settings writes are off by default. Set `inverter.allow_writes: true` only once the read-only data
matches the inverter's display.

## Wiring summary

| Link | Connector | Pins | Serial |
|---|---|---|---|
| 6000XP | 4-pin dongle plug (black A / white B) or INV485 RJ45 | RJ45 pin 8 = A, pin 7 = B | 19200 8N1, unit 1 |
| Eco-Worthy pack | RS485-1 RJ45 (one adapter channel per pack) | pin 8 = A, pin 7 = B | 9600 8N1 |
| Chargeverter GC | BMS RJ45 | pin 2 = A, pin 1 = B | 9600 8N1, it is the master |

If a bus stays silent after checking pins and baud, swap the USB adapter: a dead adapter looks
exactly like wrong wiring.

## Configuration

Everything is in `config.yaml` (see `config.example.yaml`). Environment overrides:
`SOLARDASH_CONFIG`, `SOLARDASH_PORT`, `SOLARDASH_DB`, `SOLARDASH_LOG_LEVEL`, `SOLARDASH_SIMULATE`.

### Chargeverter modes

* `off` – not connected.
* `sniff` – read-only. Shows whether the charger is polling and what SOC is on the bus.
* `emulate` – solardash answers the Chargeverter's battery polls with the aggregated JBD data.
  It stops answering when battery data is older than `emulate.stale_after` seconds, so a failure
  degrades to the charger's normal *LOST* behaviour. Use a dedicated RS485 adapter for this bus.

## Home Assistant and the auto-start generator

The outhouse generator's 12 V run signal (Waveshare Modbus relay in Home Assistant) is the best
"running" source there is, so solardash takes it from HA and uses it for that generator's run hours
and for a "warming up" state until the contactor closes. Either publish it over MQTT (retained) with
an automation like this:

```yaml
automation:
  - alias: solardash generator signal
    trigger:
      - platform: state
        entity_id: binary_sensor.outhouse_generator_running
      - platform: state
        entity_id: switch.chargeverter_contactor
      - platform: homeassistant
        event: start
    action:
      - service: mqtt.publish
        data:
          topic: solardash/generator/outhouse/running
          payload: "{{ 'on' if is_state('binary_sensor.outhouse_generator_running', 'on') else 'off' }}"
          retain: true
      - service: mqtt.publish
        data:
          topic: solardash/generator/outhouse/contactor
          payload: "{{ 'on' if is_state('switch.chargeverter_contactor', 'on') else 'off' }}"
          retain: true
```

or call the REST endpoint from a `rest_command`:

```yaml
rest_command:
  solardash_gen_signal:
    url: "http://nas-ip:8080/api/generators/Outhouse%20generator%20(auto-start)/signal"
    method: put
    content_type: application/json
    payload: '{"running": {{ running | lower }}, "contactor": {{ contactor | lower }}}'
```

In the other direction, solardash publishes bank SOC, per-pack data and the inferred Chargeverter
output with HA discovery, so the "start the generator at X %" automation can run on BMS SOC instead
of the inverter's estimate. The porch generator has no sense wire; its run hours are inferred from
Chargeverter output while it is selected on the cam switch in the UI.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/state` | full snapshot: inverter, batteries, totals, chargeverter, device health |
| `GET /api/history?metrics=total/pv_power,total/load_power&range=24h` | time series (`range` or `start`/`end`, optional `step`) |
| `GET /api/history/metrics` | available metric names |
| `GET /api/energy/daily` | per-day energy counters |
| `GET /api/settings` / `PUT /api/settings/{key}` `{"value": …, "confirm": true}` | inverter settings |
| `POST /api/settings/refresh` | re-read holding registers |
| `GET /api/generators` / `PUT /api/generators/active` `{"name": …}` | generator run hours, cam-switch selection |
| `PUT /api/generators/{name}/signal` `{"running": true, "contactor": false}` | external run/contactor signal (Home Assistant) |
| `GET /api/devices`, `GET /api/events`, `GET /api/chargeverter`, `GET /api/inverter/raw` | diagnostics |
| `WS /ws` | pushes the snapshot about once a second |

## MQTT

With `mqtt.enabled: true` solardash publishes `solar_assistant/inverter_1/<metric>/state`,
`solar_assistant/battery_<n>/<metric>/state`, `solar_assistant/total/<metric>/state` and
`solar_assistant/chargeverter_1/<metric>/state` (prefix configurable) and Home Assistant discovery
messages. Writing `solar_assistant/inverter_1/<setting>/set` changes a setting when writes are enabled.

## Development

```bash
pip install -e .[dev]
pytest
```

Tests cover the codecs with frames captured from real hardware (JBD, Chargeverter poll) and run the
full stack against the simulator.

## Credits

Register maps and protocol details were assembled from the lxp-bridge, lux-mon, solar-monitor,
jsharkey/lxp-esphome, eg4_web_monitor, syssi/esphome-jbd-bms, eg4mimic and yambms-chargeverter
projects. Chart.js (MIT) is vendored in `solardash/api/static`.

## License

MIT
