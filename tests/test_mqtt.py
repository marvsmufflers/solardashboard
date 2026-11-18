

def test_weather_sensor_names_and_units():
    from solardash.mqtt.publisher import _pretty_name, _unit_and_class

    assert _pretty_name("dewpoint_c") == "Dew point" and _unit_and_class("dewpoint_c")[:2] == ("°C", "temperature")
    assert _pretty_name("precip_rate_mm_h") == "Rain rate" and _unit_and_class("precip_rate_mm_h")[0] == "mm/h"
    assert _pretty_name("fuel_left_h") == "Fuel left" and _pretty_name("pv_today_kwh") == "PV today"
