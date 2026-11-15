"""Luxpower / EG4 6000XP Modbus RTU register map.

The EG4 6000XP is a rebadged Luxpower SNA. Its RS485 interface (the 4-pin
"dongle" port where the WiFi dongle normally plugs in, A/B = black/white; the
same signals are on RJ45 pins 7/8 of the INV485 jack) speaks **plain Modbus
RTU at 19200 8N1, unit ID 1**:

* FC 0x04 input registers  – live data (this file: ``INPUT_REGISTERS``)
* FC 0x03 holding registers – settings (``SETTINGS``), FC 0x06 / 0x10 to write

Register numbers below are decimal. Sources: lxp-bridge LXP_REGISTERS,
jsharkey/lxp-esphome 12000xp.tsv, circuitdojo/solar-monitor eg4_6000xp.rs
(which cites the vendor PDF "6kXP-Modbus 2023.10.28"), eg4_web_monitor
DATA_MAPPING.md. Where sources disagree the choice is noted inline.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Literal

DEFAULT_BAUD = 19200
DEFAULT_UNIT = 1

# ---------------------------------------------------------------------------
# Input registers (FC04)
# ---------------------------------------------------------------------------

Kind = Literal["u16", "s16", "u32", "soc_soh", "status", "bits"]


@dataclass(frozen=True)
class InputReg:
    addr: int
    key: str
    name: str
    kind: Kind = "u16"
    scale: float = 1.0
    unit: str = ""
    group: str = "status"  # status | energy | bms | generator | diagnostic


I = InputReg
INPUT_REGISTERS: list[InputReg] = [
    I(0, "status_code", "Operating status", "status", group="diagnostic"),
    I(1, "pv1_voltage", "PV1 voltage", scale=0.1, unit="V"),
    I(2, "pv2_voltage", "PV2 voltage", scale=0.1, unit="V"),
    I(3, "pv3_voltage", "PV3 voltage", scale=0.1, unit="V", group="diagnostic"),
    I(4, "battery_voltage", "Battery voltage", scale=0.1, unit="V"),
    I(5, "soc_soh", "Battery SOC/SOH", "soc_soh", unit="%"),
    I(6, "internal_fault", "Internal fault code", group="diagnostic"),
    I(7, "pv1_power", "PV1 power", unit="W"),
    I(8, "pv2_power", "PV2 power", unit="W"),
    I(9, "pv3_power", "PV3 power", unit="W", group="diagnostic"),
    I(10, "battery_charge_power", "Battery charge power", unit="W"),
    I(11, "battery_discharge_power", "Battery discharge power", unit="W"),
    I(12, "ac_input_voltage", "AC input (generator) voltage", scale=0.1, unit="V"),
    I(13, "ac_input_voltage_s", "AC input voltage S", scale=0.1, unit="V", group="diagnostic"),
    I(14, "ac_input_voltage_t", "AC input voltage T", scale=0.1, unit="V", group="diagnostic"),
    I(15, "ac_input_frequency", "AC input (generator) frequency", scale=0.01, unit="Hz"),
    I(16, "inverter_power", "Inverter power", unit="W"),
    I(17, "rectifier_power", "Rectifier (AC charge) power", unit="W"),
    I(18, "inverter_current", "Inverter RMS current", scale=0.01, unit="A", group="diagnostic"),
    I(19, "power_factor", "Power factor", "s16", scale=0.001, group="diagnostic"),
    I(20, "eps_voltage", "AC output (EPS) voltage", scale=0.1, unit="V"),
    I(21, "eps_voltage_s", "EPS voltage S", scale=0.1, unit="V", group="diagnostic"),
    I(22, "eps_voltage_t", "EPS voltage T", scale=0.1, unit="V", group="diagnostic"),
    I(23, "eps_frequency", "AC output (EPS) frequency", scale=0.01, unit="Hz"),
    I(24, "eps_power", "AC output (EPS) power", unit="W"),
    I(25, "eps_apparent_power", "AC output apparent power", unit="VA", group="diagnostic"),
    I(26, "ac_backfeed_power", "Power back out of the AC input", unit="W", group="diagnostic"),
    I(27, "ac_input_power", "AC input (generator) pass-through power", unit="W"),
    I(28, "pv1_energy_today", "PV1 energy today", scale=0.1, unit="kWh", group="energy"),
    I(29, "pv2_energy_today", "PV2 energy today", scale=0.1, unit="kWh", group="energy"),
    I(30, "pv3_energy_today", "PV3 energy today", scale=0.1, unit="kWh", group="energy"),
    I(31, "inverter_energy_today", "Inverter energy today", scale=0.1, unit="kWh", group="energy"),
    I(32, "rectifier_energy_today", "AC charge energy today", scale=0.1, unit="kWh", group="energy"),
    I(33, "battery_charge_energy_today", "Battery charged today", scale=0.1, unit="kWh", group="energy"),
    I(34, "battery_discharge_energy_today", "Battery discharged today", scale=0.1, unit="kWh", group="energy"),
    I(35, "eps_energy_today", "AC output energy today", scale=0.1, unit="kWh", group="energy"),
    I(36, "ac_backfeed_energy_today", "AC input backfeed today", scale=0.1, unit="kWh", group="diagnostic"),
    I(37, "ac_input_energy_today", "AC input pass-through today", scale=0.1, unit="kWh", group="energy"),
    I(38, "bus_voltage_1", "Bus voltage 1", scale=0.1, unit="V", group="diagnostic"),
    I(39, "bus_voltage_2", "Bus voltage 2", scale=0.1, unit="V", group="diagnostic"),
    I(40, "pv1_energy_total", "PV1 energy lifetime", "u32", scale=0.1, unit="kWh", group="energy"),
    I(42, "pv2_energy_total", "PV2 energy lifetime", "u32", scale=0.1, unit="kWh", group="energy"),
    I(44, "pv3_energy_total", "PV3 energy lifetime", "u32", scale=0.1, unit="kWh", group="energy"),
    I(46, "inverter_energy_total", "Inverter energy lifetime", "u32", scale=0.1, unit="kWh", group="energy"),
    I(48, "rectifier_energy_total", "AC charge energy lifetime", "u32", scale=0.1, unit="kWh", group="energy"),
    I(50, "battery_charge_energy_total", "Battery charged lifetime", "u32", scale=0.1, unit="kWh", group="energy"),
    I(52, "battery_discharge_energy_total", "Battery discharged lifetime", "u32", scale=0.1, unit="kWh", group="energy"),
    I(54, "eps_energy_total", "AC output energy lifetime", "u32", scale=0.1, unit="kWh", group="energy"),
    I(56, "ac_backfeed_energy_total", "AC input backfeed lifetime", "u32", scale=0.1, unit="kWh", group="diagnostic"),
    I(58, "ac_input_energy_total", "AC input pass-through lifetime", "u32", scale=0.1, unit="kWh", group="energy"),
    I(60, "fault_code", "Fault code", "u32", group="diagnostic"),
    I(62, "warning_code", "Warning code", "u32", group="diagnostic"),
    I(64, "temperature_inner", "Internal temperature", "s16", unit="°C"),
    I(65, "temperature_radiator_1", "Radiator 1 temperature", "s16", unit="°C"),
    I(66, "temperature_radiator_2", "Radiator 2 temperature", "s16", unit="°C"),
    I(67, "temperature_battery", "Battery temperature (inverter)", "s16", unit="°C"),
    I(69, "runtime_seconds", "Runtime", "u32", unit="s", group="diagnostic"),
    I(77, "ac_input_flags", "AC input flags (bit0 = generator)", "bits", group="diagnostic"),
    I(80, "battery_comms_type", "Battery brand / comms type", group="bms"),
    # scale 0.1 A per lxp-esphome TSV; lxp-bridge says 0.01. SA shows 300 A for
    # three 100 A packs, which matches 0.1.
    I(81, "bms_max_charge_current", "BMS max charge current", scale=0.1, unit="A", group="bms"),
    I(82, "bms_max_discharge_current", "BMS max discharge current", scale=0.1, unit="A", group="bms"),
    I(83, "bms_charge_voltage_ref", "BMS requested charge voltage", scale=0.1, unit="V", group="bms"),
    I(84, "bms_discharge_cutoff_voltage", "BMS discharge cut-off voltage", scale=0.1, unit="V", group="bms"),
    I(95, "bms_status_inverter", "BMS status (inverter view)", group="bms"),
    I(96, "battery_parallel_count", "Batteries in parallel", group="bms"),
    I(97, "battery_capacity_ah", "Battery capacity", unit="Ah", group="bms"),
    # 0.1 A/LSB verified live on a 6000XP by solar-monitor (TSV says 0.01).
    I(98, "bms_battery_current", "Battery current (BMS)", "s16", scale=0.1, unit="A", group="bms"),
    I(99, "bms_fault_code", "BMS fault code", group="bms"),
    I(100, "bms_warning_code", "BMS warning code", group="bms"),
    I(101, "bms_cell_voltage_max", "Max cell voltage (BMS)", scale=0.001, unit="V", group="bms"),
    I(102, "bms_cell_voltage_min", "Min cell voltage (BMS)", scale=0.001, unit="V", group="bms"),
    I(103, "bms_cell_temp_max", "Max cell temperature (BMS)", "s16", scale=0.1, unit="°C", group="bms"),
    I(104, "bms_cell_temp_min", "Min cell temperature (BMS)", "s16", scale=0.1, unit="°C", group="bms"),
    I(106, "bms_cycle_count", "Cycle count (BMS)", group="bms"),
    I(107, "battery_voltage_inverter_sample", "Battery voltage (inverter sample)", scale=0.1, unit="V", group="diagnostic"),
    I(120, "half_bus_voltage", "Half bus voltage", scale=0.1, unit="V", group="diagnostic"),
    I(121, "generator_voltage", "Generator voltage", scale=0.1, unit="V", group="generator"),
    I(122, "generator_frequency", "Generator frequency", scale=0.01, unit="Hz", group="generator"),
    # On the off-grid XP family reg 123 has been shown (12000XP firmware) to be
    # a free-running counter rather than power. Exposed as raw; gated below.
    I(123, "generator_power_raw", "Generator power (raw, unreliable on XP)", unit="W", group="generator"),
    I(124, "generator_energy_today_raw", "Generator energy today (raw, unreliable on XP)", scale=0.1, unit="kWh", group="diagnostic"),
    I(125, "generator_energy_total_raw", "Generator energy lifetime (raw, unreliable on XP)", "u32", scale=0.1, unit="kWh", group="diagnostic"),
    I(127, "eps_l1_voltage", "AC output L1-N voltage", scale=0.1, unit="V"),
    I(128, "eps_l2_voltage", "AC output L2-N voltage", scale=0.1, unit="V"),
    I(129, "eps_l1_power", "AC output L1 power", unit="W"),
    I(130, "eps_l2_power", "AC output L2 power", unit="W"),
    I(131, "eps_l1_apparent_power", "AC output L1 apparent power", unit="VA", group="diagnostic"),
    I(132, "eps_l2_apparent_power", "AC output L2 apparent power", unit="VA", group="diagnostic"),
    I(133, "eps_l1_energy_today", "AC output L1 energy today", scale=0.1, unit="kWh", group="energy"),
    I(134, "eps_l2_energy_today", "AC output L2 energy today", scale=0.1, unit="kWh", group="energy"),
    I(135, "eps_l1_energy_total", "AC output L1 energy lifetime", "u32", scale=0.1, unit="kWh", group="energy"),
    I(137, "eps_l2_energy_total", "AC output L2 energy lifetime", "u32", scale=0.1, unit="kWh", group="energy"),
    I(170, "load_power", "Load power", unit="W"),
    I(171, "load_energy_today", "Load energy today", scale=0.1, unit="kWh", group="energy"),
    I(195, "generator_l1_voltage", "Generator L1-N voltage", scale=0.1, unit="V", group="generator"),
    I(196, "generator_l2_voltage", "Generator L2-N voltage", scale=0.1, unit="V", group="generator"),
]
INPUT_BY_KEY = {r.key: r for r in INPUT_REGISTERS}

# Read in 40-register aligned blocks: 0..199.
INPUT_BLOCKS: list[tuple[int, int]] = [(0, 40), (40, 40), (80, 40), (120, 40), (160, 40)]

STATUS_CODES = {
    0x00: "Standby",
    0x02: "Firmware updating",
    0x04: "PV with AC input",
    0x08: "PV charge",
    0x0C: "PV charge with AC input",
    0x10: "Battery with AC input",
    0x11: "Bypass",
    0x14: "PV & battery with AC input",
    0x19: "PV charge + bypass",
    0x20: "AC charge",
    0x28: "PV & AC charge",
    0x40: "Battery off-grid",
    0x80: "PV off-grid",
    0xC0: "PV & battery off-grid",
    0x88: "PV charge off-grid",
}

FAULT_CODES = {
    0: "Internal communication failure 1",
    1: "Model fault",
    8: "Parallel CAN communication failure",
    9: "The host is missing",
    10: "Inconsistent rated power",
    11: "Inconsistent AC or safety settings",
    12: "UPS short circuit",
    13: "UPS reverse current",
    14: "BUS short circuit",
    15: "Abnormal phase in three-phase system",
    16: "Relay failure",
    17: "Internal communication failure 2",
    18: "Internal communication failure 3",
    19: "BUS overvoltage",
    20: "EPS connection fault",
    21: "PV overvoltage",
    22: "Overcurrent protection",
    23: "Neutral fault",
    24: "PV short circuit",
    25: "Heatsink temperature out of range",
    26: "Internal failure",
    27: "Consistency failure",
    28: "Inconsistent generator connection",
    29: "Parallel sync signal loss",
    31: "Internal communication failure 4",
}

WARNING_CODES = {
    0: "Battery communication failed",
    1: "AFCI communication failure",
    2: "Battery low temperature",
    3: "Meter communication failed",
    4: "Battery cannot be charged/discharged",
    5: "Automated test failed",
    6: "RSD active",
    7: "LCD communication failure",
    8: "Software version mismatch",
    9: "Fan is stuck",
    10: "AC input overload",
    11: "Parallel secondaries exceed limit",
    12: "Battery reverse MOS abnormal",
    13: "Radiator temperature out of range",
    14: "Multiple primary units in parallel",
    15: "Battery reverse",
    16: "No AC input",
    17: "AC input voltage out of range",
    18: "AC input frequency out of range",
    20: "Insulation resistance low",
    21: "Leakage current too high",
    22: "DCI exceeded standard",
    23: "PV short circuit",
    25: "Battery overvoltage",
    26: "Battery undervoltage",
    27: "Battery open circuit",
    28: "EPS overload",
    29: "EPS voltage high",
    30: "Meter reversed",
    31: "DCV exceeded standard",
}


def decode_bitfield(value: int, table: dict[int, str]) -> list[str]:
    return [name for bit, name in table.items() if value >> bit & 1]


def _s16(v: int) -> int:
    return struct.unpack(">h", struct.pack(">H", v & 0xFFFF))[0]


def decode_inputs(regs: dict[int, int]) -> dict:
    """Decode a ``{address: raw}`` map of input registers into named values.

    Missing registers are simply skipped, so partial reads still decode.
    """
    out: dict = {}
    for r in INPUT_REGISTERS:
        if r.addr not in regs:
            continue
        raw = regs[r.addr]
        if r.kind == "u32":
            if r.addr + 1 not in regs:
                continue
            raw = raw | (regs[r.addr + 1] << 16)  # low word first
            val = raw * r.scale
        elif r.kind == "s16":
            val = _s16(raw) * r.scale
        elif r.kind == "soc_soh":
            out["battery_soc"] = raw & 0xFF
            out["battery_soh"] = raw >> 8
            continue
        elif r.kind == "status":
            out["status_code"] = raw
            out["status"] = STATUS_CODES.get(raw, f"Unknown (0x{raw:02X})")
            continue
        elif r.kind == "bits":
            out[r.key] = raw
            continue
        else:
            val = raw * r.scale
        if r.scale != 1.0:
            val = round(val, 3)
        else:
            val = int(val)
        out[r.key] = val

    # derived conveniences
    if "pv1_power" in out:
        out["pv_power"] = out.get("pv1_power", 0) + out.get("pv2_power", 0) + out.get("pv3_power", 0)
    if "battery_charge_power" in out and "battery_discharge_power" in out:
        out["battery_power"] = out["battery_charge_power"] - out["battery_discharge_power"]  # +charging
    if "pv1_energy_today" in out:
        out["pv_energy_today"] = round(
            out.get("pv1_energy_today", 0) + out.get("pv2_energy_today", 0) + out.get("pv3_energy_today", 0), 1
        )
    if "pv1_energy_total" in out:
        out["pv_energy_total"] = round(
            out.get("pv1_energy_total", 0) + out.get("pv2_energy_total", 0) + out.get("pv3_energy_total", 0), 1
        )
    if "ac_input_flags" in out:
        out["ac_input_is_generator"] = bool(out["ac_input_flags"] & 1)
    if "fault_code" in out:
        out["faults"] = decode_bitfield(out["fault_code"], FAULT_CODES)
    if "warning_code" in out:
        out["warnings"] = decode_bitfield(out["warning_code"], WARNING_CODES)
    # Off-grid system: the AC input *is* the generator. Reg 123/124/125 are not
    # trustworthy on the XP family, so generator power/energy is what the
    # inverter actually takes from the AC input: battery charging through the
    # rectifier plus load pass-through.
    if "rectifier_power" in out or "ac_input_power" in out:
        out["generator_power"] = out.get("rectifier_power", 0) + out.get("ac_input_power", 0)
    if "rectifier_energy_today" in out or "ac_input_energy_today" in out:
        out["generator_energy_today"] = round(out.get("rectifier_energy_today", 0) + out.get("ac_input_energy_today", 0), 1)
    if "rectifier_energy_total" in out or "ac_input_energy_total" in out:
        out["generator_energy_total"] = round(out.get("rectifier_energy_total", 0) + out.get("ac_input_energy_total", 0), 1)
    out["generator_running"] = out.get("ac_input_voltage", 0) > 80
    return out


# ---------------------------------------------------------------------------
# Holding registers (FC03) – settings
# ---------------------------------------------------------------------------

FUNC_EN = 21  # function-enable bit field
FUNC_EN_1 = 110  # second function-enable bit field


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    group: str
    kind: Literal["number", "bit", "bits", "choice", "time_window"]
    reg: int
    scale: float = 1.0
    minimum: float | None = None
    maximum: float | None = None
    step: float = 1.0
    unit: str = ""
    bit: int = 0  # kind=bit
    shift: int = 0  # kind=bits
    width: int = 1  # kind=bits
    options: tuple[int, ...] = ()  # kind=choice / bits
    labels: tuple[str, ...] = ()  # kind=choice / bits / bit
    end_reg: int = 0  # kind=time_window
    writable: bool = True
    danger: str = ""  # non-empty => UI must confirm before writing


S = Setting
SETTINGS: list[Setting] = [
    # -- System
    S("inverter_state", "Inverter state", "System", "bit", FUNC_EN, bit=9, labels=("Standby", "Powered on"),
      danger="Switching the inverter state cuts output power."),
    S("output_priority", "Output priority", "System", "choice", 145, options=(0, 1, 2),
      labels=("Battery first", "PV first", "AC first")),
    S("line_mode", "AC input range", "System", "choice", 146, options=(0, 1, 2),
      labels=("APL (90-280 V)", "UPS (170-280 V)", "GEN (90-280 V)")),
    S("buzzer_enabled", "Buzzer", "System", "bit", FUNC_EN_1, bit=7),
    S("eco_mode", "Eco mode", "System", "bit", FUNC_EN_1, bit=15),
    S("green_mode", "Green mode", "System", "bit", FUNC_EN_1, bit=14),
    S("ac_first_window_1", "AC-first window 1", "System", "time_window", 152, end_reg=153),
    S("ac_first_window_2", "AC-first window 2", "System", "time_window", 154, end_reg=155),
    S("ac_first_window_3", "AC-first window 3", "System", "time_window", 156, end_reg=157),
    # -- Charging
    S("charge_power_percent", "Charge power", "Charging", "number", 64, minimum=0, maximum=100, unit="%"),
    S("max_charge_current", "Max charge current", "Charging", "number", 101, minimum=0, maximum=140, unit="A"),
    S("absorption_charge_voltage", "Absorption (lead-acid) charge voltage", "Charging", "number", 99, scale=0.1,
      minimum=50, maximum=59, step=0.1, unit="V",
      danger="Set strictly per the battery documentation; ignored for closed-loop lithium."),
    S("float_charge_voltage", "Float charge voltage", "Charging", "number", 144, scale=0.1, minimum=50, maximum=56,
      step=0.1, unit="V"),
    # -- AC charge (charging from the generator on the AC input)
    S("ac_charge", "AC charge from generator input", "AC charge", "bit", FUNC_EN, bit=7),
    S("ac_charge_according_to", "AC charge control", "AC charge", "bits", 120, shift=1, width=3,
      options=(0, 1, 2, 3, 4, 5), labels=("Disabled", "Time", "Voltage", "SOC", "Voltage + time", "SOC + time")),
    S("ac_charge_power_percent", "AC charge power", "AC charge", "number", 66, minimum=0, maximum=100, unit="%"),
    S("ac_charge_soc_limit", "AC charge SOC limit", "AC charge", "number", 67, minimum=0, maximum=100, unit="%"),
    S("ac_charge_current", "AC charge current", "AC charge", "number", 168, minimum=0, maximum=140, unit="A"),
    S("ac_charge_start_voltage", "AC charge start voltage", "AC charge", "number", 158, scale=0.1, minimum=38.5,
      maximum=52, step=0.1, unit="V"),
    S("ac_charge_stop_voltage", "AC charge stop voltage", "AC charge", "number", 159, scale=0.1, minimum=48,
      maximum=59, step=0.1, unit="V"),
    S("ac_charge_start_capacity", "AC charge start SOC", "AC charge", "number", 160, minimum=0, maximum=90, unit="%"),
    S("ac_charge_stop_capacity", "AC charge stop SOC", "AC charge", "number", 161, minimum=20, maximum=100, unit="%"),
    S("ac_charge_slot_1", "AC charge window 1", "AC charge", "time_window", 68, end_reg=69),
    S("ac_charge_slot_2", "AC charge window 2", "AC charge", "time_window", 70, end_reg=71),
    S("ac_charge_slot_3", "AC charge window 3", "AC charge", "time_window", 72, end_reg=73),
    # -- Charge priority (forced charge)
    S("charge_priority", "Charge priority", "Charge priority", "bit", FUNC_EN, bit=11),
    S("charge_priority_power_percent", "Charge priority power", "Charge priority", "number", 74, minimum=0,
      maximum=100, unit="%"),
    S("charge_priority_soc_limit", "Charge priority SOC limit", "Charge priority", "number", 75, minimum=0,
      maximum=100, unit="%"),
    S("charge_priority_voltage_limit", "Charge priority voltage limit", "Charge priority", "number", 201, scale=0.1,
      minimum=48, maximum=59, step=0.1, unit="V"),
    S("charge_priority_slot_1", "Charge priority window 1", "Charge priority", "time_window", 76, end_reg=77),
    S("charge_priority_slot_2", "Charge priority window 2", "Charge priority", "time_window", 78, end_reg=79),
    S("charge_priority_slot_3", "Charge priority window 3", "Charge priority", "time_window", 80, end_reg=81),
    # -- Discharging
    S("discharge_power_percent", "Discharge power", "Discharging", "number", 65, minimum=0, maximum=100, unit="%"),
    S("max_discharge_current", "Max discharge current", "Discharging", "number", 102, minimum=0, maximum=140, unit="A"),
    S("discharge_according_to", "Discharge control", "Discharging", "bits", 120, shift=4, width=2,
      options=(0, 1, 2), labels=("Voltage", "SOC", "Both")),
    S("stop_discharge_capacity", "Stop discharge SOC (EOD)", "Discharging", "number", 105, minimum=10, maximum=90,
      unit="%"),
    S("stop_discharge_voltage", "Stop discharge voltage (lead-acid)", "Discharging", "number", 100, scale=0.1,
      minimum=40, maximum=52, step=0.1, unit="V"),
    S("eps_discharge_soc_floor", "Discharge SOC floor", "Discharging", "number", 125, minimum=0, maximum=90,
      unit="%"),
    # -- Forced discharge
    S("forced_discharge", "Forced discharge", "Forced discharge", "bit", FUNC_EN, bit=10),
    S("forced_discharge_power_percent", "Forced discharge power", "Forced discharge", "number", 82, minimum=0,
      maximum=100, unit="%"),
    S("forced_discharge_soc_limit", "Forced discharge SOC limit", "Forced discharge", "number", 83, minimum=0,
      maximum=100, unit="%"),
    S("forced_discharge_voltage_limit", "Forced discharge voltage limit", "Forced discharge", "number", 202,
      scale=0.1, minimum=40, maximum=56, step=0.1, unit="V"),
    S("forced_discharge_slot_1", "Forced discharge window 1", "Forced discharge", "time_window", 84, end_reg=85),
    S("forced_discharge_slot_2", "Forced discharge window 2", "Forced discharge", "time_window", 86, end_reg=87),
    S("forced_discharge_slot_3", "Forced discharge window 3", "Forced discharge", "time_window", 88, end_reg=89),
    # -- Battery protection
    S("warning_start_voltage", "Battery low warning voltage", "Battery protection", "number", 162, scale=0.1,
      minimum=40, maximum=50, step=0.1, unit="V"),
    S("warning_recovery_voltage", "Battery low recovery voltage", "Battery protection", "number", 163, scale=0.1,
      minimum=42, maximum=52, step=0.1, unit="V"),
    S("warning_start_capacity", "Battery low warning SOC", "Battery protection", "number", 164, minimum=0,
      maximum=90, unit="%"),
    S("warning_recovery_capacity", "Battery low recovery SOC", "Battery protection", "number", 165, minimum=20,
      maximum=100, unit="%"),
    S("shutdown_battery_voltage", "Switch to AC input (low battery) voltage", "Battery protection", "number", 166,
      scale=0.1, minimum=44.4, maximum=51.4, step=0.1, unit="V"),
    S("shutdown_battery_capacity", "Switch to AC input (low battery) SOC", "Battery protection", "number", 167,
      minimum=0, maximum=100, unit="%"),
    # -- Battery (lead-acid / unmatched)
    S("battery_capacity", "Battery capacity (unmatched)", "Battery (lead-acid)", "number", 147, minimum=0,
      maximum=10000, unit="Ah"),
    S("battery_nominal_voltage", "Battery nominal voltage (unmatched)", "Battery (lead-acid)", "number", 148,
      scale=0.1, minimum=40, maximum=59, step=0.1, unit="V"),
    S("equalization_voltage", "Equalization voltage", "Battery (lead-acid)", "number", 149, scale=0.1, minimum=50,
      maximum=59, step=0.1, unit="V", danger="Equalization is for flooded lead-acid only."),
    S("equalization_interval_days", "Equalization interval", "Battery (lead-acid)", "number", 150, minimum=0,
      maximum=365, unit="days"),
    S("equalization_duration_hours", "Equalization duration", "Battery (lead-acid)", "number", 151, minimum=0,
      maximum=24, unit="h"),
    # -- Generator
    S("gen_port_mode", "GEN port mode", "Generator", "bit", 179, bit=13, labels=("Generator", "Smart load")),
    S("generator_charge_according_to", "Generator charge control", "Generator", "bit", 120, bit=7,
      labels=("Voltage", "SOC")),
    S("generator_charge_start_capacity", "Generator charge start SOC", "Generator", "number", 196, minimum=0,
      maximum=90, unit="%"),
    S("generator_charge_stop_capacity", "Generator charge stop SOC", "Generator", "number", 197, minimum=20,
      maximum=100, unit="%"),
    S("generator_charge_start_voltage", "Generator charge start voltage", "Generator", "number", 194, scale=0.1,
      minimum=38.4, maximum=52, step=0.1, unit="V"),
    S("generator_charge_stop_voltage", "Generator charge stop voltage", "Generator", "number", 195, scale=0.1,
      minimum=48, maximum=59, step=0.1, unit="V"),
    S("generator_charge_current", "Generator charge current", "Generator", "number", 198, minimum=0, maximum=60,
      unit="A"),
    S("generator_peak_shaving", "Generator peak shaving", "Generator", "bit", 179, bit=8),
    S("max_generator_input_power", "Max generator input power", "Generator", "number", 177, unit="W",
      writable=False),
    # -- Backup output
    S("eps_enabled", "Inverter output (EPS)", "Backup output", "bit", FUNC_EN, bit=0,
      danger="Toggling backup output cuts power to backup loads."),
    S("seamless_eps_switching", "Seamless AC input / inverter switching", "Backup output", "bit", FUNC_EN, bit=8),
    S("micro_grid", "AC-coupled micro-grid", "Backup output", "bit", FUNC_EN_1, bit=2),
    S("eps_output_voltage", "Backup output voltage", "Backup output", "choice", 90, options=(208, 220, 230, 240, 277),
      unit="V", danger="Every backup load must support the new voltage."),
    S("eps_output_frequency", "Backup output frequency", "Backup output", "choice", 91, options=(50, 60), unit="Hz",
      danger="Every backup load must support the new frequency."),
    # -- Info (read-only)
    S("com_addr", "Modbus address", "Info", "number", 15, writable=False),
    S("device_type", "Device type code", "Info", "number", 19, writable=False),
    S("pv_input_mode", "PV input mode", "Info", "number", 20, writable=False),
]
SETTINGS_BY_KEY = {s.key: s for s in SETTINGS}

# Holding register blocks needed to evaluate every setting above (aligned 40s).
HOLDING_BLOCKS: list[tuple[int, int]] = [(0, 40), (40, 40), (80, 40), (120, 40), (160, 40), (200, 40)]


def _time_window(start: int, end: int) -> str:
    # hour in the low byte, minute in the high byte
    return f"{start & 0xFF:02d}:{start >> 8:02d}-{end & 0xFF:02d}:{end >> 8:02d}"


def decode_setting(s: Setting, regs: dict[int, int]):
    """Return the decoded value for one setting, or None if registers missing."""
    if s.reg not in regs:
        return None
    raw = regs[s.reg]
    if s.kind == "number":
        v = raw * s.scale
        return round(v, 3) if s.scale != 1.0 else int(v)
    if s.kind == "bit":
        return bool(raw >> s.bit & 1)
    if s.kind == "bits":
        return (raw >> s.shift) & ((1 << s.width) - 1)
    if s.kind == "choice":
        return raw
    if s.kind == "time_window":
        if s.end_reg not in regs:
            return None
        return _time_window(raw, regs[s.end_reg])
    return raw


def decode_settings(regs: dict[int, int]) -> dict:
    out = {}
    for s in SETTINGS:
        v = decode_setting(s, regs)
        if v is not None:
            out[s.key] = v
    # time
    if all(a in regs for a in (12, 13, 14)):
        yr, mo = regs[12] & 0xFF, regs[12] >> 8
        day, hr = regs[13] & 0xFF, regs[13] >> 8
        mi, sec = regs[14] & 0xFF, regs[14] >> 8
        out["inverter_time"] = f"20{yr:02d}-{mo:02d}-{day:02d} {hr:02d}:{mi:02d}:{sec:02d}"
    if all(a in regs for a in range(2, 7)):
        raw = b"".join(struct.pack("<H", regs[a]) for a in range(2, 7))
        out["serial_number"] = raw.decode("ascii", errors="replace").strip("\x00 ")
    return out


def setting_label(s: Setting, value) -> str | None:
    if s.kind == "bit" and s.labels and value is not None:
        return s.labels[1 if value else 0]
    if s.kind in ("bits", "choice") and s.labels and value is not None and value in s.options:
        return s.labels[s.options.index(value)]
    return None


class SettingValueError(ValueError):
    pass


def encode_setting_write(s: Setting, value, current_regs: dict[int, int]) -> list[tuple[int, int]]:
    """Return ``[(register, new_raw), ...]`` needed to apply ``value``.

    Performs range checks and read-modify-write for bit fields using the
    register values in ``current_regs`` (must contain ``s.reg``).
    """
    if not s.writable:
        raise SettingValueError(f"{s.key} is read-only")
    if s.kind == "number":
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise SettingValueError(f"{s.key}: not a number: {value!r}")
        if s.minimum is not None and v < s.minimum:
            raise SettingValueError(f"{s.key}: {v} below minimum {s.minimum}")
        if s.maximum is not None and v > s.maximum:
            raise SettingValueError(f"{s.key}: {v} above maximum {s.maximum}")
        raw = int(round(v / s.scale))
        return [(s.reg, raw & 0xFFFF)]
    if s.kind == "choice":
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise SettingValueError(f"{s.key}: not an integer: {value!r}")
        if v not in s.options:
            raise SettingValueError(f"{s.key}: {v} not one of {s.options}")
        return [(s.reg, v & 0xFFFF)]
    if s.reg not in current_regs:
        raise SettingValueError(f"{s.key}: current value of register {s.reg} unknown; refusing blind write")
    cur = current_regs[s.reg]
    if s.kind == "bit":
        on = value in (True, 1, "1", "true", "True", "on")
        new = (cur | (1 << s.bit)) if on else (cur & ~(1 << s.bit) & 0xFFFF)
        return [(s.reg, new)]
    if s.kind == "bits":
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise SettingValueError(f"{s.key}: not an integer: {value!r}")
        if v not in s.options:
            raise SettingValueError(f"{s.key}: {v} not one of {s.options}")
        mask = ((1 << s.width) - 1) << s.shift
        new = (cur & ~mask & 0xFFFF) | (v << s.shift)
        return [(s.reg, new)]
    if s.kind == "time_window":
        # "HH:MM-HH:MM"
        try:
            start, end = str(value).split("-")
            sh, sm = (int(x) for x in start.split(":"))
            eh, em = (int(x) for x in end.split(":"))
        except ValueError:
            raise SettingValueError(f"{s.key}: expected HH:MM-HH:MM, got {value!r}")
        for h, m in ((sh, sm), (eh, em)):
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise SettingValueError(f"{s.key}: invalid time {h}:{m}")
        return [(s.reg, sh | (sm << 8)), (s.end_reg, eh | (em << 8))]
    raise SettingValueError(f"{s.key}: unsupported kind {s.kind}")
