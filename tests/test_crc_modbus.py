from solardash.protocols import modbus_rtu as mb
from solardash.protocols.crc import append_crc, check_crc, crc16_modbus
from solardash.protocols.link import LoopbackLink


def test_crc_known_vector():
    # Chargeverter poll frame captured on a real bus (eg4mimic): 01 03 00 13 00 11 -> CRC 74 03
    assert append_crc(bytes.fromhex("01 03 00 13 00 11")) == bytes.fromhex("01 03 00 13 00 11 74 03")
    assert check_crc(bytes.fromhex("01 03 00 13 00 11 74 03"))
    assert not check_crc(bytes.fromhex("01 03 00 13 00 11 74 04"))
    assert crc16_modbus(b"123456789") == 0x4B37


def test_build_and_parse_read_request():
    f = mb.build_read_request(1, mb.READ_INPUT, 0, 40)
    r = mb.parse_read_request(f)
    assert r is not None and (r.unit, r.function, r.address, r.count) == (1, 4, 0, 40)
    assert mb.parse_read_request(f[:-1]) is None


def test_read_response_roundtrip():
    resp = mb.build_read_response(1, mb.READ_HOLDING, [1, 65535, 0x1234])
    assert mb.parse_read_response(resp, 1, mb.READ_HOLDING) == [1, 65535, 0x1234]


def test_client_read_and_exception():
    def responder(req: bytes) -> bytes:
        r = mb.parse_read_request(req)
        if r.address >= 200:
            return mb.build_exception_response(r.unit, r.function, 2)
        return mb.build_read_response(r.unit, r.function, list(range(r.address, r.address + r.count)))

    client = mb.ModbusRtuClient(LoopbackLink(responder), timeout=0.05, retries=0, inter_frame_gap=0)
    assert client.read_input(1, 10, 3) == [10, 11, 12]
    try:
        client.read_input(1, 200, 1)
        assert False, "expected exception"
    except mb.ModbusException as e:
        assert e.code == 2


def test_client_timeout_and_crc_error():
    client = mb.ModbusRtuClient(LoopbackLink(lambda req: b""), timeout=0.01, retries=1, inter_frame_gap=0)
    try:
        client.read_input(1, 0, 1)
        assert False
    except mb.ModbusTimeout:
        pass
    assert client.stats["timeouts"] == 2
    bad = mb.ModbusRtuClient(LoopbackLink(lambda req: bytes.fromhex("01 04 02 00 01 ff ff")), timeout=0.01, retries=0, inter_frame_gap=0)
    try:
        bad.read_input(1, 0, 1)
        assert False
    except mb.ModbusCrcError:
        pass


def test_write_single_echo():
    client = mb.ModbusRtuClient(LoopbackLink(lambda req: req), timeout=0.05, retries=0, inter_frame_gap=0)
    client.write_single(1, 64, 90)  # echo == request => ok
    assert client.stats["requests"] == 1


def test_frame_splitter_handles_glued_frames():
    poll = bytes.fromhex("01 03 00 13 00 11 74 03")
    reply = mb.build_read_response(1, 3, [0] * 17)
    s = mb.RtuFrameSplitter()
    frames = s.feed(poll + reply + poll)
    assert frames == [poll, reply, poll]
    # partial frame arrives in two chunks
    s = mb.RtuFrameSplitter()
    assert s.feed(reply[:10], burst_boundary=False) == []
    assert s.feed(reply[10:]) == [reply]
