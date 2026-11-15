import time

from solardash.core.derived import aggregate_batteries, compute_totals, infer_chargeverter_power
from solardash.storage.sqlite import HistoryStore


def _pack(v, i, soc, rem, temps, cells):
    return {"voltage": v, "current": i, "soc": soc, "remaining_ah": rem, "nominal_ah": 100.0, "temperatures": temps,
            "cells": cells, "cell_imbalance": round(max(cells) - min(cells), 3), "protection": [], "cycles": 90, "_updated": time.time()}


def test_aggregate_three_packs():
    b = {
        1: _pack(52.1, -2.5, 23, 24, [13.9, 14.0], [3.256, 3.261]),
        2: _pack(52.1, -2.2, 45, 45, [14.3, 15.1], [3.252, 3.258]),
        3: _pack(52.1, -1.7, 27, 27, [18.7, 21.4], [3.252, 3.257]),
    }
    a = aggregate_batteries(b)
    assert a["battery_count"] == 3
    assert a["battery_voltage"] == 52.1
    assert a["battery_current"] == -6.4
    assert a["battery_state_of_charge"] == 32.0  # capacity weighted (96/300)
    assert a["battery_power"] == round(52.1 * -6.4, 1)
    assert a["battery_cell_voltage_max"] == 3.261 and a["battery_temperature_max"] == 21.4
    # stale pack is ignored
    b[3]["_updated"] -= 1000
    assert aggregate_batteries(b)["battery_count"] == 2


def test_chargeverter_inference():
    bank = {"battery_power": 4100.0}
    inv = {"battery_power": -300.0}  # inverter discharging 300 W
    r = infer_chargeverter_power(bank, inv, threshold_w=150)
    assert r["chargeverter_power"] == 4400.0
    assert infer_chargeverter_power({"battery_power": -290.0}, {"battery_power": -300.0})["chargeverter_power"] == 0.0
    assert infer_chargeverter_power({}, inv)["chargeverter_power"] is None


def test_compute_totals_prefers_bms_and_falls_back():
    inv = {"pv_power": 1000, "load_power": 500, "battery_power": -300, "battery_voltage": 52.0, "battery_soc": 40,
           "eps_power": 520, "grid_power": 0, "generator_power": 0, "status": "Battery off-grid"}
    t = compute_totals(inv, {}, {})
    assert t["battery_source"] == "inverter" and t["battery_state_of_charge"] == 40 and t["pv_power"] == 1000
    b = {1: _pack(52.1, 80.0, 50, 50, [20.0], [3.3, 3.31])}
    t = compute_totals(inv, b, {"charger_online": True, "last_reply": {"soc": 50}})
    assert t["battery_source"] == "bms"
    assert t["chargeverter_power"] == round(52.1 * 80.0 + 300, 0)
    assert t["chargeverter_online"] is True and t["chargeverter_soc_reported"] == 50


def test_history_store_roundtrip_and_rollup(tmp_path):
    st = HistoryStore(tmp_path / "h.db", retention_days=1)
    now = int(time.time())
    for k in range(10):
        st.insert(now - 3600 * 3 + k * 60, {"total/pv_power": 100.0 + k, "total/load_power": 50.0, "x": None, "flag": True})
    rows = st.query("total/pv_power", now - 4 * 3600, now)
    assert len(rows) == 10 and rows[0][1] == 100.0
    bucketed = st.query("total/pv_power", now - 4 * 3600, now, step=600)
    assert 1 <= len(bucketed) <= 10
    assert "flag" not in st.metrics()
    st.insert(now - 3 * 86400, {"total/pv_power": 5.0})
    deleted = st.rollup_and_prune(now)
    assert deleted == 1
    assert st.query("total/pv_power", now - 4 * 86400, now - 2 * 86400) == [((now - 3 * 86400) // 3600 * 3600, 5.0)]
    st.set_daily_energy("2026-11-15", {"pv_energy_today": 12.3})
    assert st.daily_energy()["2026-11-15"]["pv_energy_today"] == 12.3
    st.add_event("info", "test", "hello")
    assert st.events()[0]["message"] == "hello"
    assert st.stats()["raw_rows"] == 20
    st.close()
