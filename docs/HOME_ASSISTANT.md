# solardash ↔ Home Assistant

Everything solardash knows is pushed to Home Assistant over MQTT with auto-discovery, and a few
things flow the other way (generator run signal, cabin occupancy, inverter settings). No custom
integration or YAML sensors are needed.

## 1. Broker

Use the Mosquitto broker add-on in Home Assistant (Settings → Add-ons → Mosquitto broker) and the
MQTT integration (Settings → Devices & services → MQTT). If you already ran Solar Assistant into HA,
this is the same broker; solardash defaults to Solar Assistant's topic prefix (`solar_assistant`) so
existing dashboards and automations keep their entity data.

## 2. solardash config

```yaml
mqtt:
  enabled: true
  host: 192.168.1.10        # HA / Mosquitto address
  port: 1883
  username: solardash       # a Mosquitto user (add-on → Configuration → logins)
  password: "..."
  prefix: solar_assistant   # topic root; keep for SA compatibility or change
  ha_discovery: true
  ha_prefix: homeassistant
  publish_interval: 5
```

Check it from the NAS before relying on it:

```bash
docker compose run --rm solardash solardash mqtt-test
```

This connects, publishes one full snapshot plus discovery, and prints the topics it sent.

## 3. What appears in Home Assistant

Devices (Settings → Devices & services → MQTT):

| Device | Entities (examples) |
|---|---|
| Inverter | PV power, load power, battery voltage/SOC/power, AC output V/Hz, temperatures, status, faults, every inverter setting as a read-only sensor |
| Battery 1..3 | voltage, current, power, state of charge, temperature(s), cycles, cell min/max/avg/imbalance, protection |
| Totals | bank SOC/voltage/current/power, cell spread, Chargeverter power (`generator_power`), `generator_running`, `occupancy` |
| Generator 1..n | `running` (binary), `active` (cam switch), `source`, `contactor`, `warming_up`, `runtime_today_h` |
| Weather station | temperature, humidity, solar radiation, UV, wind, pressure, rain |
| Forecast | `pv_today_kwh`, `pv_tomorrow_kwh`, `load_today_kwh`, `pv_expected_now_kw` |
| Plan | `needed`, `recommended_now`, `stop_recommended` (binary), `start_at`, `end_at`, `run_h`, `end_soc`, `reason` |

Topics follow `<prefix>/<device>/<metric>/state`, e.g. `solar_assistant/total/battery_state_of_charge/state`.

## 4. Automations

### Start/stop the outhouse generator from the plan

solardash never switches anything itself. Your existing automation keeps doing the start, the 90 s
warm-up and the contactor; it just takes its cue from the plan.

```yaml
automation:
  - alias: Generator - start on solardash plan
    trigger:
      - platform: state
        entity_id: binary_sensor.plan_recommended_now
        to: "on"
        for: "00:02:00"           # ignore a momentary flip
    condition:
      - condition: state
        entity_id: binary_sensor.outhouse_generator_running
        state: "off"
    action:
      - service: switch.turn_on
        target: { entity_id: switch.outhouse_generator_start }
      - wait_for_trigger:
          - platform: state
            entity_id: binary_sensor.outhouse_generator_running
            to: "on"
        timeout: "00:02:00"
      - delay: "00:01:30"         # warm-up
      - service: switch.turn_on
        target: { entity_id: switch.chargeverter_contactor }

  - alias: Generator - stop on solardash plan
    trigger:
      - platform: state
        entity_id: binary_sensor.plan_stop_recommended
        to: "on"
        for: "00:02:00"
    condition:
      - condition: state
        entity_id: binary_sensor.outhouse_generator_running
        state: "on"
    action:
      - service: switch.turn_off
        target: { entity_id: switch.chargeverter_contactor }
      - delay: "00:01:00"         # cool-down unloaded
      - service: switch.turn_off
        target: { entity_id: switch.outhouse_generator_start }
```

Keep your hard safety rules alongside it (for example: start regardless of plan if SOC < 25 %).
The plan is advisory and depends on the forecast.

### Tell solardash the generator is running (12 V sense wire) and the contactor state

```yaml
  - alias: solardash - generator signal
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

The topics are set per generator in `config.yaml` (`running_topic`, `contactor_topic`).

### Cabin occupancy from presence

```yaml
  - alias: solardash - occupancy
    trigger:
      - platform: state
        entity_id: zone.cabin
    action:
      - service: mqtt.publish
        data:
          topic: solar_assistant/occupancy/set
          payload: "{{ 'occupied' if states('zone.cabin') | int > 0 else 'away' }}"
```

### Change an inverter setting

Only works with `inverter.allow_writes: true`. Publish to `solar_assistant/inverter_1/<setting>/set`,
for example `solar_assistant/inverter_1/max_charge_current/set` with payload `80`. Setting keys are
listed on the Settings tab and at `/api/settings`.

## 5. Without MQTT (REST)

If you would rather not run a broker, HA's `rest` platform can poll `/api/state` every 30 s:

```yaml
rest:
  - resource: http://nas-ip:8080/api/state
    scan_interval: 30
    sensor:
      - name: Bank SOC
        value_template: "{{ value_json.total.battery_state_of_charge }}"
        unit_of_measurement: "%"
      - name: PV power
        value_template: "{{ value_json.inverter.pv_power }}"
        unit_of_measurement: W
      - name: Generator plan start
        value_template: "{{ value_json.forecast_summary.plan.start_at | default(none) }}"
    binary_sensor:
      - name: Generator recommended now
        value_template: "{{ value_json.forecast_summary.plan.recommended_now }}"
```

and `rest_command` for the signals (see README). MQTT is the better path: push, discovery, and
availability tracking for free.
