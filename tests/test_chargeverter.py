import struct

from solardash.protocols import chargeverter as cv
from solardash.protocols.modbus_rtu import check_crc, parse_read_request, parse_read_response


def test_poll_frame_is_valid_modbus():
    req = parse_read_request(cv.POLL_FRAME)
    assert req is not None
    assert (req.unit, req.function, req.address, req.count) == (1, 3, 0x13, 17)


def test_emulated_response_layout():
    img = cv.BatteryImage(voltage=52.34, current=-12.3, soc=47, soh=100, temp_max=18.3, temp_min=13.1,
                          capacity_ah=300, charge_limit_a=100, discharge_limit_a=100, charge_voltage_v=56.0, cycles=70000)
    req = parse_read_request(cv.POLL_FRAME)
    resp = cv.build_response(req, img)
    assert resp is not None and check_crc(resp)
    values = parse_read_response(resp, 1, 3)
    assert len(values) == 17
    regs = {0x13 + i: v for i, v in enumerate(values)}
    assert regs[0x15] == 47 and regs[0x19] == 47
    assert regs[0x16] == 5234
    assert struct.unpack(">h", struct.pack(">H", regs[0x14]))[0] == -123
    assert regs[0x18] == 100
    assert regs[0x1A] == 183 and regs[0x1B] == 131
    assert regs[0x1C] == 30000
    assert regs[0x1F] == 5600
    assert regs[0x20] | (regs[0x21] << 16) == 70000
    # decode the same reply the way the sniffer would
    sn = cv.decode_reply_registers(values)
    assert (sn.soc, sn.voltage, sn.current, sn.temp_max) == (47, 52.34, -12.3, 18.3)


def test_response_refused_for_other_requests():
    from solardash.protocols.modbus_rtu import build_read_request

    img = cv.BatteryImage(voltage=52.0, current=0, soc=50)
    assert cv.build_response(parse_read_request(build_read_request(2, 3, 0x13, 17)), img) is None
    assert cv.build_response(parse_read_request(build_read_request(1, 4, 0x13, 17)), img) is None
    assert cv.build_response(parse_read_request(build_read_request(1, 3, 0x00, 10)), img) is None


def test_sniffer_pairs_poll_and_reply():
    img = cv.BatteryImage(voltage=51.9, current=5.0, soc=66)
    req = parse_read_request(cv.POLL_FRAME)
    resp = cv.build_response(req, img)
    s = cv.Sniffer()
    s.on_frame(cv.POLL_FRAME)
    assert s.state.poll_count == 1 and s.state.charger_online and not s.state.battery_answering
    s.on_frame(resp)
    assert s.state.reply_count == 1 and s.state.battery_answering
    assert s.state.last_reply.soc == 66
    d = s.state.to_dict()
    assert d["last_reply"]["voltage"] == 51.9
