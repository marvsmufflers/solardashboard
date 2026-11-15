"""Configuration: YAML file with environment-variable overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
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
    enabled: bool = False
    mode: str = "sniff"  # sniff | emulate | off
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


@dataclass
class HistoryConfig:
    path: str = "data/solardash.db"
    sample_interval: float = 15.0
    retention_days: int = 14


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class MqttConfig:
    enabled: bool = False
    host: str = "localhost"
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
        GeneratorConfig("Outhouse generator (auto-start)", "Honda EU3000is, remote start"),
        GeneratorConfig("Porch generator", "Honda EU3000is, plugged in during winter stays"),
    ])
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


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load ``config.yaml`` (or ``$SOLARDASH_CONFIG``). Missing file => defaults."""
    p = Path(path or os.environ.get("SOLARDASH_CONFIG", "config.yaml"))
    raw: dict[str, Any] = {}
    if p.exists():
        with p.open() as fh:
            raw = yaml.safe_load(fh) or {}
    cfg = Config(
        simulate=bool(raw.get("simulate", False)),
        log_level=str(raw.get("log_level", "INFO")),
        inverter=_build(InverterConfig, raw.get("inverter")),
        batteries=[_build(BatteryConfig, b) for b in raw.get("batteries", [])],
        chargeverter=_build(ChargeverterConfig, raw.get("chargeverter")),
        generators=[_build(GeneratorConfig, g) for g in raw["generators"]] if "generators" in raw else Config.__dataclass_fields__["generators"].default_factory(),  # type: ignore[misc]
        history=_build(HistoryConfig, raw.get("history")),
        web=_build(WebConfig, raw.get("web")),
        mqtt=_build(MqttConfig, raw.get("mqtt")),
    )
    # env overrides for the common things people set in docker-compose
    if os.environ.get("SOLARDASH_SIMULATE"):
        cfg.simulate = os.environ["SOLARDASH_SIMULATE"].lower() in ("1", "true", "yes")
    if os.environ.get("SOLARDASH_PORT"):
        cfg.web.port = int(os.environ["SOLARDASH_PORT"])
    if os.environ.get("SOLARDASH_DB"):
        cfg.history.path = os.environ["SOLARDASH_DB"]
    if os.environ.get("SOLARDASH_LOG_LEVEL"):
        cfg.log_level = os.environ["SOLARDASH_LOG_LEVEL"]
    if cfg.simulate and not cfg.batteries:
        cfg.batteries = [BatteryConfig(name=f"Battery {i}", port=f"sim{i}") for i in (1, 2, 3)]
    return cfg
