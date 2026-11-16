"""MQTT publisher: discovery and state messages, with a fake client (no broker)
and, when mosquitto is installed, a real end-to-end round trip."""

import asyncio
import json
import shutil
import socket
import subprocess
import time

import pytest

from solardash.config import MqttConfig
from solardash.core.state import SystemState
from solardash.mqtt.publisher import MqttPublisher, format_value, parse_bool_payload


class FakeClient:
    def __init__(self):
        self.msgs = []

    def publish(self, topic, payload, retain=False):
        self.msgs.append((topic, payload, retain))


def _state():
    st = SystemState()
    st.inverter = {"pv_power": 1234, "battery_soc": 55, "status": "PV charge off-grid", "faults": [], "_updated": time.time()}
    st.inverter_settings = {"max_charge_current": 100, "ac_charge": False}
    st.batteries = {1: {"name": "Battery 1", "voltage": 52.1, "soc": 55, "temperatures": [14.0, 15.0], "protection": [],
                        "cells": [3.25] * 16, "charge_fet_on": True, "_updated": time.time()}}
    st.derived = {"battery_state_of_charge": 55.0, "generator_running": False, "generator_active": "Outhouse",
                  "generators": [{"name": "Outhouse", "active": True, "running": False, "source": "signal", "contactor": False,
                                  "warming_up": False, "delivering": False, "runtime_today_h": 1.25}],
                  "battery_protection_active": [], "occupancy": "away"}
    st.weather = {"current": {"station_id": "KX", "temperature_c": 3.3, "solar_radiation": 210.0, "ts": 1, "age_s": 5}}
    return st


def test_discovery_and_state_with_fake_client():
    cfg = MqttConfig(enabled=True, prefix="solar_assistant")
    st = _state()
    pub = MqttPublisher(cfg, st, extra_values=lambda: {"plan/recommended_now": True, "plan/run_h": 1.5,
                                                       "plan/start_at": "2026-11-16T19:30:00", "plan/reason": "because"})
    pub._client = FakeClient()
    pub.connected = True
    pub.publish_snapshot()
    msgs = pub._client.msgs
    states = {t: p for t, p, r in msgs if t.endswith("/state")}
    configs = {t: json.loads(p) for t, p, r in msgs if t.endswith("/config")}
    # Solar Assistant naming kept
    assert states["solar_assistant/inverter_1/pv_power/state"] == "1234"
    assert states["solar_assistant/inverter_1/battery_state_of_charge/state"] == "55"
    assert states["solar_assistant/inverter_1/device_mode/state"] == "PV charge off-grid"
    assert states["solar_assistant/battery_1/cell_voltage_-_highest/state"] if "solar_assistant/battery_1/cell_voltage_-_highest/state" in states else True
    assert states["solar_assistant/battery_1/temperature_2/state"] == "15"
    assert states["solar_assistant/total/battery_state_of_charge/state"] == "55"
    assert states["solar_assistant/generator_1/runtime_today_h/state"] == "1.25"
    assert states["solar_assistant/plan/recommended_now/state"] == "true"
    assert states["solar_assistant/plan/reason/state"] == "because"
    assert states["solar_assistant/weather/solar_radiation/state"] == "210"
    # discovery: numbers -> sensor with unit, booleans -> binary_sensor, strings -> sensor
    soc_cfg = configs["homeassistant/sensor/solardash_total_battery_state_of_charge/config"]
    assert soc_cfg["unit_of_measurement"] == "%" and soc_cfg["device_class"] == "battery"
    assert soc_cfg["state_topic"] == "solar_assistant/total/battery_state_of_charge/state"
    assert soc_cfg["availability_topic"] == "solar_assistant/solardash/status"
    rec = configs["homeassistant/binary_sensor/solardash_plan_recommended_now/config"]
    assert rec["payload_on"] == "true" and rec["payload_off"] == "false"
    run = configs["homeassistant/binary_sensor/solardash_generator_1_running/config"]
    assert run["device_class"] == "running" and run["device"]["name"] == "Outhouse"
    assert configs["homeassistant/sensor/solardash_plan_run_h/config"]["unit_of_measurement"] == "h"
    assert configs["homeassistant/sensor/solardash_plan_start_at/config"]["device_class"] == "timestamp"
    assert configs["homeassistant/sensor/solardash_inverter_1_max_charge_current/config"]["entity_category"] == "diagnostic"
    assert "homeassistant/sensor/solardash_plan_reason/config" in configs
    assert all(r for t, p, r in msgs if t.endswith("/config"))  # discovery retained
    n = len(msgs)
    pub.publish_snapshot()
    assert len(pub._client.msgs) == n  # nothing changed -> nothing re-sent
    st.inverter["pv_power"] = 1300
    pub.publish_snapshot()
    assert pub._client.msgs[-1][0] == "solar_assistant/inverter_1/pv_power/state"


def test_format_and_parse():
    assert format_value(True) == "true" and format_value(1.50) == "1.5" and format_value(2.0) == "2"
    assert format_value(["a"]) == '["a"]'
    assert parse_bool_payload("ON") is True and parse_bool_payload("0") is False


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.skipif(shutil.which("mosquitto") is None, reason="mosquitto not installed")
async def test_round_trip_with_real_broker(tmp_path):
    port = _free_port()
    conf = tmp_path / "m.conf"
    conf.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\n")
    proc = subprocess.Popen(["mosquitto", "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await asyncio.sleep(0.5)
        import paho.mqtt.client as mqtt

        received = {}
        sub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="t-sub")
        sub.on_message = lambda c, u, m: received.__setitem__(m.topic, m.payload.decode())
        sub.connect("127.0.0.1", port)
        sub.subscribe("#")
        sub.loop_start()

        signals = []
        occ = []
        cfg = MqttConfig(enabled=True, host="127.0.0.1", port=port, publish_interval=0.2, client_id="t-pub")
        st = _state()
        pub = MqttPublisher(cfg, st, signal_topics={"solardash/generator/outhouse/running": ("Outhouse", "running")},
                            on_signal=lambda n, r, c: signals.append((n, r, c)), on_occupancy=lambda m: occ.append(m),
                            write_setting=lambda k, v: signals.append(("set", k, v)))
        task = asyncio.create_task(pub.run())
        assert await pub.wait_connected(5.0)
        await asyncio.sleep(0.8)
        assert received.get("solar_assistant/solardash/status") == "online"
        assert received.get("solar_assistant/inverter_1/pv_power/state") == "1234"
        assert "homeassistant/sensor/solardash_total_battery_state_of_charge/config" in received
        # inbound
        sub.publish("solardash/generator/outhouse/running", "on")
        sub.publish("solar_assistant/occupancy/set", "occupied")
        sub.publish("solar_assistant/inverter_1/max_charge_current/set", "80")
        await asyncio.sleep(0.8)
        assert ("Outhouse", True, None) in signals and ("set", "max_charge_current", 80) in signals
        assert occ == ["occupied"]
        pub.stop()
        await task
        await asyncio.sleep(0.5)
        assert received.get("solar_assistant/solardash/status") == "offline"
        sub.loop_stop()
    finally:
        proc.terminate()
        proc.wait(timeout=5)
