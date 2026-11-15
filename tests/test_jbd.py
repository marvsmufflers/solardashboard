import pytest

from solardash.protocols import jbd
from solardash.protocols.link import LoopbackLink


def test_request_frames_match_documented_bytes():
    assert jbd.REQ_BASIC_INFO == bytes.fromhex("dd a5 03 00 ff fd 77")
    assert jbd.REQ_CELL_VOLTAGES == bytes.fromhex("dd a5 04 00 ff fc 77")
    assert jbd.REQ_HW_VERSION == bytes.fromhex("dd a5 05 00 ff fb 77")


def test_parse_real_basic_info_frame():
    # captured from a 4S JBD BMS (syssi/esphome-jbd-bms README)
    frame = bytes.fromhex("DD 03 00 1D 06 0B 00 00 01 ED 01 F4 00 00 2C 7C 00 00 00 00 10 00 80 63 02 04 03 0B A0 0B 9D 0B 98 FA 55 77")
    cmd, data = jbd.parse_reply(frame)
    assert cmd == 3
    info = jbd.decode_basic_info(data)
    assert info.voltage == 15.47
    assert info.current == 0.0
    assert info.remaining_ah == 4.93
    assert info.nominal_ah == 5.0
    assert info.cycles == 0
    assert info.soc == 99
    assert info.cell_count == 4
    assert info.temperatures == [24.5, 24.2, 23.7]
    assert info.protection == ["MOSFET software lock"]
    assert info.software_version == "8.0"
    # FET byte is 0x02 in this capture: charge MOSFET off (software lock), discharge on
    assert info.charge_fet_on is False and info.discharge_fet_on is True


def test_parse_real_cell_frame():
    frame = bytes.fromhex("DD 04 00 08 0F 23 0F 1C 0F 12 0F 1D FF 4E 77")
    cmd, data = jbd.parse_reply(frame)
    assert cmd == 4
    assert jbd.decode_cell_voltages(data) == [3.875, 3.868, 3.858, 3.869]


def test_bad_checksum_rejected():
    frame = bytearray(bytes.fromhex("DD 04 00 08 0F 23 0F 1C 0F 12 0F 1D FF 4E 77"))
    frame[5] ^= 0x01
    with pytest.raises(jbd.JbdError):
        jbd.parse_reply(bytes(frame))


def test_encode_decode_roundtrip_16s():
    temps = [13.5, 15.3, 13.4, 13.2, 14.1, 14.0]
    data = jbd.encode_basic_info(52.1, -2.5, 24.0, 100.0, 91, 23, temps, cell_count=16)
    reply = jbd.build_reply(jbd.CMD_BASIC_INFO, data)
    cmd, payload = jbd.parse_reply(reply)
    info = jbd.decode_basic_info(payload)
    assert (info.voltage, info.current, info.remaining_ah, info.nominal_ah) == (52.1, -2.5, 24.0, 100.0)
    assert info.cycles == 91 and info.soc == 23 and info.cell_count == 16
    assert info.temperatures == temps
    assert info.power == round(52.1 * -2.5, 1)


def test_client_two_stage_read():
    data = jbd.encode_basic_info(52.0, 1.0, 50, 100, 5, 50, [20.0], cell_count=16)
    reply = jbd.build_reply(jbd.CMD_BASIC_INFO, data)
    client = jbd.JbdClient(LoopbackLink(lambda req: reply if req[2] == 3 else b""), timeout=0.01, retries=0)
    assert client.basic_info().soc == 50
