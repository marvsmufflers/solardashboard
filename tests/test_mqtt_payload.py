from solardash.mqtt.publisher import parse_bool_payload


def test_parse_bool_payload():
    for p in ("on", "ON", "true", "1", "running", '"on"', " yes "):
        assert parse_bool_payload(p) is True
    for p in ("off", "false", "0", "stopped", "unavailable", "open"):
        assert parse_bool_payload(p) is False
    assert parse_bool_payload("12.0") is True
    assert parse_bool_payload("maybe") is None
