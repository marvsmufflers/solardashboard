"""Derived / aggregated values.

Everything the UI shows under "total" (Solar Assistant naming) plus the one
piece of inference the hardware can't give us directly: the Chargeverter's
DC output.

Sign conventions used throughout solardash:
* battery current/power: **positive = charging**, negative = discharging
* the inverter's AC input is the generator (off-grid system)
"""

from __future__ import annotations

import time
from typing import Any


def aggregate_batteries(batteries: dict[int, dict[str, Any]], max_age_s: float = 30.0, now: float | None = None) -> dict:
    """Combine per-pack JBD data into bank totals."""
    now = now or time.time()
    packs = [b for b in batteries.values() if b and (now - b.get("_updated", 0)) <= max_age_s]
    out: dict[str, Any] = {"battery_count": len(packs), "battery_count_configured": len(batteries)}
    if not packs:
        return out
    voltages = [p["voltage"] for p in packs if p.get("voltage") is not None]
    currents = [p["current"] for p in packs if p.get("current") is not None]
    socs = [p["soc"] for p in packs if p.get("soc") is not None]
    temps = [t for p in packs for t in p.get("temperatures", [])]
    cells = [c for p in packs for c in p.get("cells", [])]
    rem = [p["remaining_ah"] for p in packs if p.get("remaining_ah") is not None]
    nom = [p["nominal_ah"] for p in packs if p.get("nominal_ah") is not None]
    if voltages:
        out["battery_voltage"] = round(sum(voltages) / len(voltages), 2)
    if currents:
        out["battery_current"] = round(sum(currents), 2)
        if voltages:
            out["battery_power"] = round(sum(p["voltage"] * p["current"] for p in packs if p.get("voltage") is not None and p.get("current") is not None), 1)
    if socs:
        # capacity-weighted if we know Ah, else plain mean
        if rem and nom and len(rem) == len(packs) and sum(nom) > 0:
            out["battery_state_of_charge"] = round(100.0 * sum(rem) / sum(nom), 1)
        else:
            out["battery_state_of_charge"] = round(sum(socs) / len(socs), 1)
        out["battery_soc_min"] = min(socs)
        out["battery_soc_max"] = max(socs)
    if rem:
        out["battery_remaining_ah"] = round(sum(rem), 1)
    if nom:
        out["battery_capacity_ah"] = round(sum(nom), 1)
    if temps:
        out["battery_temperature"] = round(sum(temps) / len(temps), 1)
        out["battery_temperature_max"] = max(temps)
        out["battery_temperature_min"] = min(temps)
    if cells:
        out["battery_cell_voltage_min"] = min(cells)
        out["battery_cell_voltage_max"] = max(cells)
        out["battery_cell_voltage_avg"] = round(sum(cells) / len(cells), 4)
        out["battery_cell_imbalance_max"] = round(max(p.get("cell_imbalance", 0) for p in packs), 3)
    prot = [x for p in packs for x in p.get("protection", [])]
    out["battery_protection_active"] = sorted(set(prot))
    out["battery_cycles_avg"] = round(sum(p.get("cycles", 0) for p in packs) / len(packs))
    return out


def infer_chargeverter_power(bank: dict, inverter: dict, threshold_w: float = 150.0) -> dict:
    """Estimate Chargeverter DC output.

    The Chargeverter is a second charger on the same DC bus, so

        pack_net_power = inverter_battery_power + chargeverter_power

    Both terms on the right are measured independently (BMS sums vs inverter
    registers 10/11), so the difference is the Chargeverter. Noise and timing
    skew make small values meaningless, hence ``threshold_w``.
    """
    out: dict[str, Any] = {"chargeverter_power": None, "chargeverter_power_raw": None, "chargeverter_power_method": None}
    pack_p = bank.get("battery_power")
    inv_p = inverter.get("battery_power")
    if pack_p is None or inv_p is None:
        return out
    diff = pack_p - inv_p
    out["chargeverter_power_raw"] = round(diff, 1)
    out["chargeverter_power"] = round(diff, 0) if diff >= threshold_w else 0.0
    out["chargeverter_power_method"] = "bms_minus_inverter"
    return out


def compute_totals(inverter: dict, batteries: dict[int, dict], chargeverter: dict, threshold_w: float = 150.0, now: float | None = None) -> dict:
    """Build the ``total/*`` view (Solar Assistant naming where it exists)."""
    now = now or time.time()
    bank = aggregate_batteries(batteries, now=now)
    t: dict[str, Any] = {}
    # battery: prefer BMS data; fall back to inverter's view
    for k in ("battery_voltage", "battery_current", "battery_power", "battery_state_of_charge", "battery_temperature",
              "battery_capacity_ah", "battery_remaining_ah", "battery_cell_voltage_min", "battery_cell_voltage_max",
              "battery_cell_voltage_avg", "battery_cell_imbalance_max", "battery_soc_min", "battery_soc_max",
              "battery_temperature_max", "battery_temperature_min", "battery_count", "battery_count_configured",
              "battery_protection_active", "battery_cycles_avg"):
        if k in bank:
            t[k] = bank[k]
    t["battery_source"] = "bms" if bank.get("battery_count") else "inverter"
    if not bank.get("battery_count"):
        if "battery_voltage" in inverter:
            t["battery_voltage"] = inverter["battery_voltage"]
        if "battery_soc" in inverter:
            t["battery_state_of_charge"] = inverter["battery_soc"]
        if "battery_power" in inverter:
            t["battery_power"] = inverter["battery_power"]
        if "bms_battery_current" in inverter:
            t["battery_current"] = inverter["bms_battery_current"]
    # inverter-side totals
    for src, dst in (
        ("pv_power", "pv_power"),
        ("load_power", "load_power"),
        ("eps_power", "ac_output_power"),
        ("eps_voltage", "ac_output_voltage"),
        ("eps_frequency", "ac_output_frequency"),
        ("ac_input_live", "ac_input_live"),
        ("ac_input_voltage", "ac_input_voltage"),
        ("ac_input_total_power", "ac_input_power"),
        ("pv_energy_today", "pv_energy_today"),
        ("load_energy_today", "load_energy_today"),
        ("battery_charge_energy_today", "battery_energy_in_today"),
        ("battery_discharge_energy_today", "battery_energy_out_today"),
        ("battery_power", "inverter_battery_power"),
        ("status", "inverter_status"),
        ("temperature_inner", "inverter_temperature"),
    ):
        if src in inverter:
            t[dst] = inverter[src]
    # if the inverter has no load register, fall back to EPS power
    if "load_power" not in t and "ac_output_power" in t:
        t["load_power"] = t["ac_output_power"]
    t.update(infer_chargeverter_power(bank, inverter, threshold_w))
    # The generators only reach the battery through the Chargeverter.
    t["generator_power"] = t.get("chargeverter_power")
    t["generator_running"] = bool((t.get("chargeverter_power") or 0) > 0)  # raw; service debounces/overrides
    if chargeverter:
        t["chargeverter_online"] = chargeverter.get("charger_online")
        t["chargeverter_soc_reported"] = (chargeverter.get("last_reply") or {}).get("soc")
    t["_updated"] = now
    return t
