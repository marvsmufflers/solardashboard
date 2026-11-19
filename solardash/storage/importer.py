"""Import older history (the cleaned Solar Assistant export) into the app's database.

Drop ``cabin_1min.csv.gz`` (or any CSV with the same columns) into ``<data>/import/`` and run the
import from Setup or ``POST /api/history/import``. Only minutes *before* the app's own first reading
are imported, so nothing the app recorded itself is ever overwritten. Imported files are renamed
to ``*.imported`` so they are not read twice.
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from .sqlite import HistoryStore

log = logging.getLogger(__name__)

# CSV column -> stored metric
COLUMNS = {
    "pv_w": "total/pv_power",
    "load_w": "total/load_power",
    "battery_w": "total/battery_power",
    "soc": "total/battery_state_of_charge",
    "battery_v": "total/battery_voltage",
    "battery_a": "total/battery_current",
    "battery_temp_c": "total/battery_temperature",
    "chargeverter_w_inferred": "total/chargeverter_power",
    "generator_w_inferred": "total/generator_power",
    "pv1_w": "inverter/pv1_power",
    "pv2_w": "inverter/pv2_power",
    "pv1_v": "inverter/pv1_voltage",
    "pv2_v": "inverter/pv2_voltage",
    "inverter_temp_c": "inverter/temperature_inner",
    "ac_input_w": "inverter/ac_input_total_power",
    "outside_temp_c": "weather/temperature",
}
DAILY = {"pv_w": "pv_energy_today", "load_w": "load_energy_today"}


def import_dir(cfg_history_path: str) -> Path:
    return Path(cfg_history_path).parent / "import"


def _open(path: Path):
    if path.suffix == ".gz":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", newline="")
    return open(path, encoding="utf-8", newline="")


def _parse_ts(s: str) -> int | None:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def first_own_sample(store: HistoryStore) -> int | None:
    with store._lock:  # noqa: SLF001
        row = store._conn.execute("SELECT MIN(ts) FROM samples WHERE metric = 'total/battery_state_of_charge'").fetchone()  # noqa: SLF001
    return int(row[0]) if row and row[0] is not None else None


def import_file(store: HistoryStore, path: Path, before: int | None, batch: int = 5000) -> dict:
    """Insert one CSV. Returns counts and the covered time range."""
    rows = minutes = 0
    first = last = None
    daily: dict[str, dict[str, float]] = {}
    pending: list[tuple[int, str, float]] = []

    def flush():
        if pending:
            with store._lock:  # noqa: SLF001
                store._conn.executemany("INSERT OR IGNORE INTO samples(ts, metric, value) VALUES (?,?,?)", pending)  # noqa: SLF001
            pending.clear()

    with _open(path) as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "time_utc" not in reader.fieldnames:
            raise ValueError(f"{path.name}: no time_utc column; is this the cleaned Solar Assistant export?")
        known = [c for c in COLUMNS if c in reader.fieldnames]
        if not known:
            raise ValueError(f"{path.name}: none of the expected columns ({', '.join(COLUMNS)})")
        for rec in reader:
            if rec.get("data_present", "1") in ("0", "0.0"):
                continue
            ts = _parse_ts(rec["time_utc"])
            if ts is None or (before is not None and ts >= before):
                continue
            got = False
            for col in known:
                v = rec.get(col)
                if v in (None, "", "nan", "NaN"):
                    continue
                try:
                    pending.append((ts, COLUMNS[col], float(v)))
                    got = True
                except ValueError:
                    continue
            if not got:
                continue
            minutes += 1
            first = ts if first is None else min(first, ts)
            last = ts if last is None else max(last, ts)
            day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")  # container local time, like the app's own days
            e = daily.setdefault(day, {})
            for col, key in DAILY.items():
                v = rec.get(col)
                if v not in (None, "", "nan", "NaN"):
                    e[key] = e.get(key, 0.0) + max(0.0, float(v)) / 1000.0 / 60.0  # W for one minute -> kWh
            if len(pending) >= batch:
                rows += len(pending)
                flush()
    rows += len(pending)
    flush()
    for day, vals in daily.items():
        existing = store.daily_energy(400).get(day, {})
        store.set_daily_energy(day, {k: round(v, 2) for k, v in vals.items() if k not in existing})
    if first is not None:
        rollup_range(store, first, last)
    return {"file": path.name, "minutes": minutes, "values": rows, "first": first, "last": last, "days": len(daily)}


def rollup_range(store: HistoryStore, start: int, end: int) -> None:
    """Hourly rollups for an imported (older) range; the regular rollup only moves forward."""
    with store._lock:  # noqa: SLF001
        store._conn.execute(  # noqa: SLF001
            """
            INSERT OR REPLACE INTO hourly(ts, metric, avg, min, max, n)
            SELECT (ts/3600)*3600, metric, AVG(value), MIN(value), MAX(value), COUNT(*)
            FROM samples WHERE ts BETWEEN ? AND ?
            GROUP BY (ts/3600), metric
            """,
            ((start // 3600) * 3600, end),
        )


def run_import(store: HistoryStore, history_path: str) -> dict:
    """Import every CSV waiting in <data>/import/. Safe to run repeatedly."""
    folder = import_dir(history_path)
    files = sorted([p for p in folder.glob("*.csv")] + [p for p in folder.glob("*.csv.gz")]) if folder.is_dir() else []
    if not files:
        return {"folder": str(folder), "imported": [], "message": f"No files waiting in {folder.name}/."}
    before = first_own_sample(store)
    done, errors = [], []
    t0 = time.time()
    for f in files:
        try:
            res = import_file(store, f, before)
            f.rename(f.with_name(f.name + ".imported"))
            done.append(res)
            store.add_event("info", "import", f"imported {res['minutes']} minutes ({res['days']} days) from {f.name}")
        except Exception as e:  # noqa: BLE001
            log.warning("import %s: %s", f.name, e)
            errors.append(f"{f.name}: {e}")
    msg = (f"Imported {sum(d['days'] for d in done)} days of history in {time.time() - t0:.0f} s."
           if done else "Nothing imported.")
    return {"folder": str(folder), "imported": done, "errors": errors, "message": msg}
