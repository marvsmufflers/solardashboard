import pytest

from solardash.protocols import luxpower as lx


def test_decode_inputs_basic():
    regs = {a: 0 for a in range(0, 200)}
    regs[0] = 0x40
    regs[1], regs[2] = 2345, 0
    regs[4] = 521
    regs[5] = 31 | (100 << 8)
    regs[7], regs[8] = 1200, 800
    regs[10], regs[11] = 0, 256
    regs[12], regs[15] = 2401, 6000
    regs[17] = 1500
    regs[20], regs[23], regs[24] = 2401, 6002, 297
    regs[27] = 400
    regs[32] = 21
    regs[37] = 9
    regs[28], regs[29] = 45, 30
    regs[40], regs[41] = 0x1234, 0x0001  # u32 low word first
    regs[64] = 22
    regs[67] = 0xFFFB  # -5 °C
    regs[77] = 1
    regs[81], regs[82] = 3000, 3000
    regs[98] = 0xFFCE  # -5.0 A
    regs[101], regs[102] = 3261, 3252
    regs[103] = 183
    regs[129], regs[130] = 210, 21
    regs[170] = 231
    out = lx.decode_inputs(regs)
    assert out["status"] == "Battery off-grid"
    assert out["pv1_voltage"] == 234.5
    assert out["battery_voltage"] == 52.1
    assert out["battery_soc"] == 31 and out["battery_soh"] == 100
    assert out["pv_power"] == 2000
    assert out["battery_power"] == -256
    assert out["eps_voltage"] == 240.1 and out["eps_frequency"] == 60.02 and out["eps_power"] == 297
    assert out["pv_energy_today"] == 7.5
    assert out["pv1_energy_total"] == pytest.approx((0x10000 + 0x1234) * 0.1)
    assert out["temperature_battery"] == -5
    assert out["ac_input_is_generator"] is True
    assert out["ac_input_voltage"] == 240.1 and out["ac_input_live"] is True
    assert out["ac_input_total_power"] == 1900
    assert out["ac_input_total_energy_today"] == 3.0
    assert "grid_power" not in out and "generator_power" not in out
    assert out["bms_max_discharge_current"] == 300.0
    assert out["bms_battery_current"] == -5.0
    assert out["bms_cell_voltage_max"] == 3.261 and out["bms_cell_temp_max"] == 18.3
    assert out["eps_l1_power"] == 210 and out["load_power"] == 231
    assert out["faults"] == [] and out["warnings"] == []


def test_fault_bits_decode():
    assert lx.decode_bitfield(1 << 21 | 1 << 25, lx.FAULT_CODES) == ["PV overvoltage", "Heatsink temperature out of range"]


def test_decode_settings_and_labels():
    regs = {a: 0 for a in range(0, 240)}
    for i, w in enumerate((0x3433, 0x3337, 0x3437, 0x3330, 0x3939)):  # "3473740399" little-endian words
        regs[2 + i] = w
    regs[12] = (11 << 8) | 26  # Nov 2026
    regs[13] = (14 << 8) | 15
    regs[14] = (30 << 8) | 7
    regs[21] = (1 << 9) | (1 << 7)
    regs[64] = 100
    regs[68] = 1 | (30 << 8)
    regs[69] = 5 | (0 << 8)
    regs[90], regs[91] = 240, 60
    regs[99] = 574
    regs[120] = (3 << 1) | (1 << 7)
    regs[145], regs[146] = 0, 2
    regs[196], regs[197] = 50, 90
    s = lx.decode_settings(regs)
    assert s["serial_number"] == "3473740399"
    assert s["inverter_time"] == "2026-11-15 14:07:30"
    assert s["inverter_state"] is True and s["ac_charge"] is True and s["eps_enabled"] is False
    assert s["ac_charge_slot_1"] == "01:30-05:00"
    assert s["absorption_charge_voltage"] == 57.4
    assert s["ac_charge_according_to"] == 3
    assert lx.setting_label(lx.SETTINGS_BY_KEY["ac_charge_according_to"], 3) == "SOC"
    assert s["generator_charge_according_to"] is True
    assert lx.setting_label(lx.SETTINGS_BY_KEY["generator_charge_according_to"], True) == "SOC"
    assert s["line_mode"] == 2 and lx.setting_label(lx.SETTINGS_BY_KEY["line_mode"], 2).startswith("GEN")
    assert s["generator_charge_start_capacity"] == 50 and s["generator_charge_stop_capacity"] == 90


def test_encode_number_range_checks():
    s = lx.SETTINGS_BY_KEY["generator_charge_stop_capacity"]
    assert lx.encode_setting_write(s, 85, {}) == [(197, 85)]
    with pytest.raises(lx.SettingValueError):
        lx.encode_setting_write(s, 10, {})
    v = lx.SETTINGS_BY_KEY["float_charge_voltage"]
    assert lx.encode_setting_write(v, 54.0, {}) == [(144, 540)]
    with pytest.raises(lx.SettingValueError):
        lx.encode_setting_write(v, 60.0, {})


def test_encode_bit_read_modify_write():
    s = lx.SETTINGS_BY_KEY["ac_charge"]
    assert lx.encode_setting_write(s, True, {21: 0x0201}) == [(21, 0x0281)]
    assert lx.encode_setting_write(s, False, {21: 0x0281}) == [(21, 0x0201)]
    with pytest.raises(lx.SettingValueError):
        lx.encode_setting_write(s, True, {})  # refuses blind write


def test_encode_bits_and_choice_and_window():
    b = lx.SETTINGS_BY_KEY["ac_charge_according_to"]
    assert lx.encode_setting_write(b, 3, {120: 0x00F1}) == [(120, (0x00F1 & ~0x0E) | (3 << 1))]
    with pytest.raises(lx.SettingValueError):
        lx.encode_setting_write(b, 9, {120: 0})
    c = lx.SETTINGS_BY_KEY["eps_output_voltage"]
    assert lx.encode_setting_write(c, 240, {}) == [(90, 240)]
    with pytest.raises(lx.SettingValueError):
        lx.encode_setting_write(c, 250, {})
    w = lx.SETTINGS_BY_KEY["ac_charge_slot_1"]
    assert lx.encode_setting_write(w, "01:30-05:00", {68: 0, 69: 0}) == [(68, 1 | (30 << 8)), (69, 5)]
    with pytest.raises(lx.SettingValueError):
        lx.encode_setting_write(w, "25:00-05:00", {68: 0, 69: 0})
    ro = lx.SETTINGS_BY_KEY["com_addr"]
    with pytest.raises(lx.SettingValueError):
        lx.encode_setting_write(ro, 2, {15: 1})


def test_setting_keys_unique_and_registers_sane():
    keys = [s.key for s in lx.SETTINGS]
    assert len(keys) == len(set(keys))
    assert not [k for k in keys if "grid" in k and k != "micro_grid"]
    assert not [r.key for r in lx.INPUT_REGISTERS if "grid" in r.key]
    for s in lx.SETTINGS:
        assert 0 <= s.reg < 240
        if s.kind == "time_window":
            assert s.end_reg == s.reg + 1
    addrs = [r.addr for r in lx.INPUT_REGISTERS]
    assert len(addrs) == len(set(addrs))
