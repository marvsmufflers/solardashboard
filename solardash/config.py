"""Configuration: YAML file with environment-variable overrides."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml


@dataclass
class InverterConfig:
    enabled: bool = True
    port: str = "/dev/ttyACM0"
    baud: int = 19200
    unit: int = 1
    poll_interval: float = 5.0
    settings_poll_interval: float = 60.0
    allow_writes: bool = False
    timeout: float = 1.0
    name: str = "EG4 6000XP"


@dataclass
class BatteryConfig:
    name: str = "Battery 1"
    port: str = "/dev/ttyACM1"
    baud: int = 9600
    poll_interval: float = 5.0
    timeout: float = 1.0
    capacity_ah: float = 100.0
    enabled: bool = True


@dataclass
class ChargeverterEmulateConfig:
    soh: int = 100
    charge_voltage: float = 56.0
    charge_limit_a: float = 100.0
    discharge_limit_a: float = 100.0
    # if no battery data has been seen for this long, stop answering so the
    # Chargeverter falls back to its own voltage-based logic
    stale_after: float = 30.0
    # optional SOC override floor/ceiling clamps when answering
    soc_offset: int = 0


@dataclass
class ChargeverterConfig:
    enabled: bool = False  # RS485 link to the Chargeverter's BMS port is optional and normally not needed
    mode: str = "off"  # off | sniff | emulate
    port: str = "/dev/ttyACM3"
    baud: int = 9600
    parity: str = "N"
    name: str = "EG4 Chargeverter GC"
    # power inference: minimum derived power (W) to report as Chargeverter output
    power_threshold_w: float = 150.0
    emulate: ChargeverterEmulateConfig = field(default_factory=ChargeverterEmulateConfig)


@dataclass
class GeneratorConfig:
    name: str = "Generator"
    notes: str = ""
    # Optional external "running" signal (e.g. a 12 V sense wire read by a
    # Waveshare Modbus relay in Home Assistant). When set, run hours for this
    # generator come from the signal instead of Chargeverter-output inference.
    # Either subscribe to an MQTT topic (payload on/off, true/false, 1/0) ...
    running_topic: str = ""
    # ... and/or let Home Assistant call PUT /api/generators/<name>/signal.
    # Optional second signal: is the contactor to the Chargeverter closed?
    contactor_topic: str = ""
    # a signal older than this is ignored (falls back to inference)
    signal_stale_s: float = 6 * 3600


@dataclass
class WeatherConfig:
    enabled: bool = False  # Weather Underground station (observations + narrative forecast)
    provider: str = "wunderground"
    # Open-Meteo hourly irradiance forecast (free, no key). Needs a location:
    # weather.latitude/longitude or the WU station's reported position.
    openmeteo_enabled: bool = True
    openmeteo_interval: float = 3600.0
    openmeteo_days: int = 7
    api_key: str = ""  # or SOLARDASH_WU_API_KEY
    station_id: str = ""
    latitude: float | None = None  # defaults to the station's reported position
    longitude: float | None = None
    units: str = "e"  # 'e' imperial (US stations) or 'm'
    poll_interval: float = 300.0
    forecast_interval: float = 1800.0


@dataclass
class PlannerConfig:
    enabled: bool = True
    soc_floor: float = 30.0  # never plan to go below this
    soc_target: float = 50.0  # normal stop point (the owner's 30 -> 50 % routine)
    soc_target_storm: float = 80.0  # allowed stop point when the forecast shows no solar surplus coming
    storm_lookahead_h: float = 36.0  # ... i.e. when the bank would need another run within this many hours
    soc_margin: float = 5.0  # planning margin above the floor
    generator_dc_kw: float = 2.4  # Chargeverter DC output from one EU3000is (~2.8 kW AC)
    charge_efficiency: float = 0.94
    min_run_h: float = 1.0
    max_run_h: float = 8.0
    quiet_hours: list[str] = field(default_factory=list)  # e.g. ["22:00-07:00"]
    horizon_h: int = 72
    pv_kw_peak: float = 6.0  # array size; used until enough history has been learned
    battery_kwh: float | None = None  # default: bank Ah × 51.2 V
    recompute_interval: float = 300.0


@dataclass
class DisplayConfig:
    temperature: str = "F"  # F or C
    wind: str = "mph"  # mph or km/h
    rain: str = "in"  # in or mm


@dataclass
class OccupancyConfig:
    enabled: bool = True
    modes: list[str] = field(default_factory=lambda: ["occupied", "away"])
    default: str = "away"


@dataclass
class HistoryConfig:
    path: str = "data/solardash.db"
    sample_interval: float = 15.0
    retention_days: int = 14


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    # Optional password for the UI/API (or $SOLARDASH_PASSWORD). Empty = open.
    # Use it as a second layer behind Cloudflare Access / Tailscale.
    password: str = ""


@dataclass
class MqttConfig:
    enabled: bool = False
    host: str = ""
    port: int = 1883
    username: str = ""
    password: str = ""
    prefix: str = "solar_assistant"  # Solar Assistant-compatible topic layout
    ha_discovery: bool = True
    ha_prefix: str = "homeassistant"
    publish_interval: float = 5.0
    client_id: str = "solardash"


@dataclass
class Config:
    simulate: bool = False
    log_level: str = "INFO"
    inverter: InverterConfig = field(default_factory=InverterConfig)
    batteries: list[BatteryConfig] = field(default_factory=list)
    chargeverter: ChargeverterConfig = field(default_factory=ChargeverterConfig)
    # The generators that can feed the Chargeverter (one at a time, via the cam switch).
    generators: list[GeneratorConfig] = field(default_factory=lambda: [
        GeneratorConfig("Outhouse generator (auto-start)", "Honda EU3000is, remote start",
                        running_topic="solardash/generator/outhouse/running",
                        contactor_topic="solardash/generator/outhouse/contactor"),
        GeneratorConfig("Porch generator", "Honda EU3000is, plugged in during winter stays"),
    ])
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    occupancy: OccupancyConfig = field(default_factory=OccupancyConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    web: WebConfig = field(default_factory=WebConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)


def _build(cls, data: dict[str, Any] | None):
    data = dict(data or {})
    kwargs = {}
    for f in cls.__dataclass_fields__.values():  # type: ignore[attr-defined]
        if f.name not in data:
            continue
        v = data.pop(f.name)
        if f.name == "emulate":
            v = _build(ChargeverterEmulateConfig, v)
        kwargs[f.name] = v
    if data:
        raise ValueError(f"unknown keys for {cls.__name__}: {sorted(data)}")
    return cls(**kwargs)


# ---------------------------------------------------------------------------
# Overrides saved from the Setup page (data/config.overrides.json). They sit on
# top of config.yaml; env vars only fill secrets that are still empty.
# ---------------------------------------------------------------------------

OVERRIDABLE_SECTIONS: dict[str, Any] = {
    "inverter": InverterConfig,
    "batteries": BatteryConfig,  # list section
    "weather": WeatherConfig,
    "planner": PlannerConfig,
    "occupancy": OccupancyConfig,
    "display": DisplayConfig,
    "mqtt": MqttConfig,
    "generators": GeneratorConfig,  # list section
}
SECRET_FIELDS = {("weather", "api_key"), ("mqtt", "password")}


def overrides_path(cfg: "Config") -> Path | None:
    if cfg.history.path == ":memory:":
        return None
    return Path(cfg.history.path).parent / "config.overrides.json"


def load_overrides(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        with path.open() as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_overrides(path: Path | None, overrides: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as fh:
        json.dump(overrides, fh, indent=2, sort_keys=True)
    tmp.replace(path)


def _check_types(cls, values: dict[str, Any]) -> dict[str, Any]:
    """Type-check (and lightly coerce) plain values against a dataclass's annotations."""
    out = dict(values)
    for f in fields(cls):
        if f.name not in out:
            continue
        v = out[f.name]
        t = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", str(f.type))
        optional = "None" in t
        if v is None or (v == "" and optional and "str" not in t):
            if optional:
                out[f.name] = None
                continue
            raise ValueError(f"{f.name} is required")
        if "bool" in t:
            if not isinstance(v, bool):
                raise ValueError(f"{f.name} must be true or false")
        elif "list" in t:
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ValueError(f"{f.name} must be a list of text values")
        elif "float" in t or "int" in t:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"{f.name} must be a number")
            out[f.name] = int(v) if "int" in t and "float" not in t else float(v)
        elif not isinstance(v, str):
            raise ValueError(f"{f.name} must be text")
    return out


def _check_ranges(section: str, obj) -> None:
    if section == "planner":
        if not (0 <= obj.soc_floor < obj.soc_target <= 100):
            raise ValueError("need 0 <= soc_floor < soc_target <= 100")
        if not (obj.soc_target <= obj.soc_target_storm <= 100):
            raise ValueError("soc_target_storm must be between soc_target and 100")
        if not 0.3 <= obj.generator_dc_kw <= 8:
            raise ValueError("generator charge must be between 6 and 150 A (0.3–8 kW); an EU3000is into a Chargeverter is about 40–55 A")
        if obj.generator_dc_kw <= 0 or obj.pv_kw_peak <= 0 or obj.max_run_h < obj.min_run_h or obj.horizon_h < 12:
            raise ValueError("generator_dc_kw and pv_kw_peak must be positive, max_run_h >= min_run_h, horizon_h >= 12")
        for q in obj.quiet_hours:
            a, _, b = q.partition("-")
            if not (a[:2].isdigit() and b[:2].isdigit() and 0 <= int(a[:2]) <= 23 and 0 <= int(b[:2]) <= 23):
                raise ValueError(f"quiet hours entry {q!r} must look like 22:00-07:00")
    if section == "weather":
        if obj.latitude is not None and not -90 <= obj.latitude <= 90:
            raise ValueError("latitude must be between -90 and 90")
        if obj.longitude is not None and not -180 <= obj.longitude <= 180:
            raise ValueError("longitude must be between -180 and 180")
        if obj.units not in ("e", "m"):
            raise ValueError("units must be e or m")
        if (obj.latitude is None) != (obj.longitude is None):
            raise ValueError("set both latitude and longitude")
    if section == "occupancy":
        if not obj.modes or obj.default not in obj.modes:
            raise ValueError("default mode must be one of the modes")
    if section == "display":
        if obj.temperature not in ("F", "C") or obj.wind not in ("mph", "km/h") or obj.rain not in ("in", "mm"):
            raise ValueError("temperature F/C, wind mph or km/h, rain in or mm")
    if section == "mqtt" and not 1 <= obj.port <= 65535:
        raise ValueError("port must be 1-65535")
    if section == "inverter":
        if not obj.port.strip():
            raise ValueError("inverter port is required")
        if not 0 <= obj.unit <= 247 or obj.baud not in (9600, 19200, 38400, 57600, 115200):
            raise ValueError("unit must be 0-247 and baud one of 9600/19200/38400/57600/115200")


def build_section(section: str, values: Any):
    """Validate and build one overridable section from plain data."""
    cls = OVERRIDABLE_SECTIONS.get(section)
    if cls is None:
        raise ValueError(f"section {section!r} is not editable")
    if section == "generators":
        if not isinstance(values, list):
            raise ValueError("generators must be a list")
        gens = [_build(GeneratorConfig, _check_types(GeneratorConfig, g)) for g in values]
        names = [g.name.strip() for g in gens]
        if any(not n for n in names) or len(set(names)) != len(names):
            raise ValueError("generator names must be unique and non-empty")
        return gens
    if section == "batteries":
        if not isinstance(values, list):
            raise ValueError("batteries must be a list")
        bats = [_build(BatteryConfig, _check_types(BatteryConfig, b)) for b in values]
        ports = [b.port.strip() for b in bats if b.enabled]
        if any(not p for p in ports) or len(set(ports)) != len(ports):
            raise ValueError("each enabled battery needs its own serial port")
        return bats
    if not isinstance(values, dict):
        raise ValueError(f"{section} must be an object")
    obj = _build(cls, _check_types(cls, values))
    _check_ranges(section, obj)
    return obj


def apply_overrides(cfg: "Config", overrides: dict[str, Any]) -> "Config":
    """Apply saved Setup values. A value that no longer passes validation is
    skipped with a warning (the default stays) instead of stopping startup."""
    import logging

    log = logging.getLogger(__name__)
    for section, values in overrides.items():
        if section not in OVERRIDABLE_SECTIONS:
            continue
        try:
            if section in ("generators", "batteries"):
                setattr(cfg, section, build_section(section, values))
                continue
            current = asdict(getattr(cfg, section))
            wanted = {k: v for k, v in values.items() if k in current}
            try:
                setattr(cfg, section, build_section(section, {**current, **wanted}))
            except (ValueError, TypeError):
                # keep every saved value that is fine on its own, drop the bad ones
                good = dict(current)
                for k, v in wanted.items():
                    try:
                        build_section(section, {**good, k: v})
                        good[k] = v
                    except (ValueError, TypeError) as e:
                        log.warning("setup: ignoring saved %s.%s=%r (%s)", section, k, v, e)
                setattr(cfg, section, build_section(section, good))
        except (ValueError, TypeError) as e:
            log.warning("setup: ignoring saved %s settings (%s)", section, e)
    return cfg


def section_values(cfg: "Config", section: str, mask_secrets: bool = True) -> Any:
    if section == "generators":
        return [asdict(g) for g in cfg.generators]
    if section == "batteries":
        return [asdict(b) for b in cfg.batteries]
    d = asdict(getattr(cfg, section))
    if mask_secrets:
        for (sec, key) in SECRET_FIELDS:
            if sec == section and d.get(key):
                d[key] = "***"
    return d


def section_schema(section: str) -> list[dict[str, Any]]:
    cls = OVERRIDABLE_SECTIONS[section]
    out = []
    for f in fields(cls):
        t = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", str(f.type))
        kind = "bool" if "bool" in t else "number" if ("float" in t or "int" in t) else "list" if "list" in t else "text"
        if (section, f.name) in SECRET_FIELDS:
            kind = "password"
        out.append({"key": f.name, "kind": kind})
    return out


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load ``config.yaml`` (or ``$SOLARDASH_CONFIG``). Missing file => defaults."""
    p = Path(path or os.environ.get("SOLARDASH_CONFIG", "config.yaml"))
    raw: dict[str, Any] = {}
    if p.is_file():
        with p.open() as fh:
            raw = yaml.safe_load(fh) or {}
    cfg = Config(
        simulate=bool(raw.get("simulate", False)),
        log_level=str(raw.get("log_level", "INFO")),
        inverter=_build(InverterConfig, raw.get("inverter")),
        batteries=[_build(BatteryConfig, b) for b in raw.get("batteries", [])],
        chargeverter=_build(ChargeverterConfig, raw.get("chargeverter")),
        generators=[_build(GeneratorConfig, g) for g in raw["generators"]] if "generators" in raw else Config.__dataclass_fields__["generators"].default_factory(),  # type: ignore[misc]
        weather=_build(WeatherConfig, raw.get("weather")),
        planner=_build(PlannerConfig, raw.get("planner")),
        occupancy=_build(OccupancyConfig, raw.get("occupancy")),
        display=_build(DisplayConfig, raw.get("display")),
        history=_build(HistoryConfig, raw.get("history")),
        web=_build(WebConfig, raw.get("web")),
        mqtt=_build(MqttConfig, raw.get("mqtt")),
    )
    # env overrides for the common things people set in docker-compose
    if os.environ.get("SOLARDASH_SIMULATE"):
        cfg.simulate = os.environ["SOLARDASH_SIMULATE"].lower() in ("1", "true", "yes")
    if os.environ.get("SOLARDASH_PORT"):
        cfg.web.port = int(os.environ["SOLARDASH_PORT"])
    if os.environ.get("SOLARDASH_PASSWORD"):
        cfg.web.password = os.environ["SOLARDASH_PASSWORD"]
    if os.environ.get("SOLARDASH_DB"):
        cfg.history.path = os.environ["SOLARDASH_DB"]
    # Setup-page overrides win over the file; env only fills secrets still empty
    apply_overrides(cfg, load_overrides(overrides_path(cfg)))
    if os.environ.get("SOLARDASH_WU_API_KEY") and not cfg.weather.api_key:
        cfg.weather.api_key = os.environ["SOLARDASH_WU_API_KEY"]
    if os.environ.get("SOLARDASH_LOG_LEVEL"):
        cfg.log_level = os.environ["SOLARDASH_LOG_LEVEL"]
    if cfg.simulate and not cfg.batteries:
        cfg.batteries = [BatteryConfig(name=f"Battery {i}", port=f"sim{i}") for i in (1, 2, 3)]
    elif "batteries" not in raw and "batteries" not in load_overrides(overrides_path(cfg)):
        # no config at all: assume the Solar Assistant layout (inverter on ACM0, three packs after it)
        cfg.batteries = [BatteryConfig(name=f"Battery {i}", port=f"/dev/ttyACM{i}") for i in (1, 2, 3)]
    return cfg
