"""Serial diagnostics: find which port is which before configuring the app.

    solardash probe list
    solardash probe inverter --port /dev/ttyACM0 [--bauds 19200,9600] [--units 1,2,3]
    solardash probe battery  --port /dev/ttyACM1 [--baud 9600]
    solardash probe sniff    --port /dev/ttyUSB0 [--baud 9600] [--seconds 15]
    solardash probe raw      --port /dev/ttyUSB0 --baud 9600 [--seconds 10]
    solardash probe all      [--bauds ...]      # try inverter+battery on every port
"""

from __future__ import annotations

import argparse
import sys
import time

from ..protocols import chargeverter as cv
from ..protocols import jbd
from ..protocols import luxpower as lx
from ..protocols.link import SerialLink
from ..protocols.modbus_rtu import ModbusError, ModbusRtuClient, RtuFrameSplitter


def add_probe_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("what", choices=["list", "inverter", "battery", "sniff", "raw", "all", "weather"])
    p.add_argument("--port")
    p.add_argument("--baud", type=int, default=None)
    p.add_argument("--bauds", default="19200,9600")
    p.add_argument("--units", default="1,2,3,0")
    p.add_argument("--seconds", type=float, default=15.0)


def list_ports() -> list[tuple[str, str]]:
    from serial.tools import list_ports

    out = []
    for p in sorted(list_ports.comports(), key=lambda x: x.device):
        desc = f"{p.description} | {p.hwid}"
        if p.serial_number:
            desc += f" | SN {p.serial_number}"
        out.append((p.device, desc))
    return out


def probe_inverter(port: str, bauds: list[int], units: list[int], verbose: bool = True) -> dict | None:
    for baud in bauds:
        link = SerialLink(port, baud)
        client = ModbusRtuClient(link, timeout=0.8, retries=0)
        try:
            for unit in units:
                try:
                    regs = client.read_input(unit, 0, 40)
                except ModbusError as e:
                    if verbose:
                        print(f"  {port} @ {baud} unit {unit}: {e}")
                    continue
                decoded = lx.decode_inputs({i: v for i, v in enumerate(regs)})
                try:
                    hold = client.read_holding(unit, 0, 20)
                    settings = lx.decode_settings({i: v for i, v in enumerate(hold)})
                except ModbusError:
                    settings = {}
                info = {"port": port, "baud": baud, "unit": unit, "status": decoded.get("status"),
                        "battery_voltage": decoded.get("battery_voltage"), "soc": decoded.get("battery_soc"),
                        "pv_power": decoded.get("pv_power"), "eps_power": decoded.get("eps_power"),
                        "serial": settings.get("serial_number")}
                print(f"  FOUND Luxpower/EG4 inverter: {info}")
                return info
        finally:
            link.close()
    return None


def probe_battery(port: str, baud: int, verbose: bool = True) -> dict | None:
    link = SerialLink(port, baud)
    client = jbd.JbdClient(link, timeout=0.8, retries=1)
    try:
        info = client.basic_info()
        cells = client.cell_voltages()
        try:
            hw = client.hw_version()
        except Exception:  # noqa: BLE001
            hw = "?"
        out = {"port": port, "baud": baud, "hw": hw, "voltage": info.voltage, "current": info.current, "soc": info.soc,
               "cells": len(cells), "cell_min": min(cells), "cell_max": max(cells), "temps": info.temperatures,
               "cycles": info.cycles, "protection": info.protection}
        print(f"  FOUND JBD BMS: {out}")
        return out
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"  {port} @ {baud}: no JBD reply ({e})")
        return None
    finally:
        link.close()


def sniff(port: str, baud: int, seconds: float, parity: str = "N") -> None:
    link = SerialLink(port, baud, parity=parity)
    splitter = RtuFrameSplitter()
    sn = cv.Sniffer()
    print(f"listening on {port} @ {baud} 8{parity}1 for {seconds:.0f}s (Chargeverter polls look like 01 03 00 13 00 11 74 03)")
    t_end = time.monotonic() + seconds
    try:
        while time.monotonic() < t_end:
            data = link.read(64, 0.5)
            if not data:
                continue
            frames = splitter.feed(data)
            if not frames:
                print(f"  ?? {data.hex(' ')}")
            for f in frames:
                sn.on_frame(f)
                print(f"  {time.strftime('%H:%M:%S')}  {f.hex(' ')}")
    finally:
        link.close()
    st = sn.state.to_dict()
    print("summary:", st)
    if st["poll_count"] and not st["reply_count"]:
        print("  -> Chargeverter is polling but nothing answers: the batteries don't speak its protocol. "
              "Run solardash with chargeverter.mode: emulate to answer with the JBD data.")


def raw_dump(port: str, baud: int, seconds: float) -> None:
    link = SerialLink(port, baud)
    print(f"raw dump {port} @ {baud} for {seconds:.0f}s")
    t_end = time.monotonic() + seconds
    try:
        while time.monotonic() < t_end:
            data = link.read(256, 0.5)
            if data:
                print(f"  {time.strftime('%H:%M:%S')} [{len(data):3d}] {data.hex(' ')}")
    finally:
        link.close()


def run_probe(args: argparse.Namespace) -> int:
    bauds = [int(b) for b in args.bauds.split(",") if b]
    units = [int(u) for u in args.units.split(",") if u != ""]
    if args.what == "weather":
        import json

        from ..config import load_config
        from ..weather.wunderground import WundergroundClient

        cfg = load_config(getattr(args, "config", None))
        w = cfg.weather
        if not w.api_key or not w.station_id:
            print("set weather.api_key and weather.station_id in config.yaml (or SOLARDASH_WU_API_KEY)", file=sys.stderr)
            return 2
        c = WundergroundClient(w.api_key, w.station_id, units=w.units)
        obs = c.current()
        print("current:", json.dumps(obs.to_dict(), indent=1, default=str))
        print("raw observation keys:", sorted(obs.raw.keys()))
        lat, lon = (w.latitude, w.longitude) if w.latitude is not None else (obs.lat, obs.lon)
        days = c.forecast(lat, lon)
        for d in days:
            print(f"{d.date}: {d.narrative}  day cloud {None if d.day is None else d.day.cloud_cover}  night cloud {None if d.night is None else d.night.cloud_cover}  qpf {d.qpf_mm} mm")
        return 0
    if args.what == "list":
        ports = list_ports()
        if not ports:
            print("no serial ports found (in Docker: did you pass --device / devices: ?)")
        for dev, desc in ports:
            print(f"{dev}\t{desc}")
        return 0
    if args.what == "all":
        for dev, desc in list_ports():
            print(f"{dev}  ({desc})")
            if probe_inverter(dev, bauds, units, verbose=False):
                continue
            probe_battery(dev, args.baud or 9600, verbose=False)
        return 0
    if not args.port:
        print("--port is required", file=sys.stderr)
        return 2
    if args.what == "inverter":
        return 0 if probe_inverter(args.port, [args.baud] if args.baud else bauds, units) else 1
    if args.what == "battery":
        return 0 if probe_battery(args.port, args.baud or 9600) else 1
    if args.what == "sniff":
        sniff(args.port, args.baud or 9600, args.seconds)
        return 0
    if args.what == "raw":
        raw_dump(args.port, args.baud or 9600, args.seconds)
        return 0
    return 1
