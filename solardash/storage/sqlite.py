"""SQLite time-series store.

Two tables:

* ``samples(ts, metric, value)`` — raw samples at the configured interval,
  kept for ``retention_days``.
* ``hourly(ts, metric, avg, min, max, n)`` — hourly rollups, kept forever.

``query`` picks the right table for the requested range and optionally
re-buckets to ``step`` seconds.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts     INTEGER NOT NULL,
    metric TEXT    NOT NULL,
    value  REAL,
    PRIMARY KEY (metric, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hourly (
    ts     INTEGER NOT NULL,
    metric TEXT    NOT NULL,
    avg    REAL, min REAL, max REAL, n INTEGER,
    PRIMARY KEY (metric, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS daily_energy (
    day    TEXT NOT NULL,
    metric TEXT NOT NULL,
    value  REAL,
    PRIMARY KEY (metric, day)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    level   TEXT NOT NULL,
    source  TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""


class HistoryStore:
    def __init__(self, path: str | Path, retention_days: int = 14) -> None:
        self.path = str(path)
        self.retention_days = retention_days
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._last_rollup = 0.0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes ---------------------------------------------------------------
    def insert(self, ts: int, metrics: dict[str, float | int | None]) -> None:
        rows = [(int(ts), k, float(v)) for k, v in metrics.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not rows:
            return
        with self._lock:
            self._conn.executemany("INSERT OR REPLACE INTO samples(ts, metric, value) VALUES (?,?,?)", rows)

    def set_daily_energy(self, day: str, metrics: dict[str, float]) -> None:
        rows = [(day, k, float(v)) for k, v in metrics.items() if isinstance(v, (int, float))]
        if rows:
            with self._lock:
                self._conn.executemany("INSERT OR REPLACE INTO daily_energy(day, metric, value) VALUES (?,?,?)", rows)

    def add_event(self, level: str, source: str, message: str, ts: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(ts, level, source, message) VALUES (?,?,?,?)",
                (int(ts or time.time()), level, source, message),
            )

    # -- maintenance -----------------------------------------------------------
    def rollup_and_prune(self, now: float | None = None) -> int:
        """Fold completed hours into ``hourly`` and delete expired raw rows.
        Returns the number of raw rows deleted."""
        now = now or time.time()
        hour_cutoff = int(now // 3600) * 3600  # start of the current hour
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO hourly(ts, metric, avg, min, max, n)
                SELECT (ts/3600)*3600, metric, AVG(value), MIN(value), MAX(value), COUNT(*)
                FROM samples
                WHERE ts < ? AND ts >= COALESCE((SELECT MAX(ts) FROM hourly), 0)
                GROUP BY (ts/3600), metric
                """,
                (hour_cutoff,),
            )
            deleted = 0
            if self.retention_days > 0:
                cutoff = int(now - self.retention_days * 86400)
                cur = self._conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
                deleted = cur.rowcount
            self._conn.execute("DELETE FROM events WHERE ts < ?", (int(now - 90 * 86400),))
        self._last_rollup = now
        return deleted

    def maybe_rollup(self, every: float = 3600.0) -> None:
        if time.time() - self._last_rollup > every:
            self.rollup_and_prune()

    # -- reads -----------------------------------------------------------------
    def query(self, metric: str, start: float, end: float, step: int | None = None) -> list[tuple[int, float]]:
        """Return ``[(ts, value), ...]`` for ``metric`` between start and end.

        Uses raw samples when they cover the range, hourly rollups otherwise.
        ``step`` (seconds) averages raw samples into buckets.
        """
        start, end = int(start), int(end)
        raw_floor = int(time.time() - self.retention_days * 86400) if self.retention_days > 0 else 0
        with self._lock:
            if start >= raw_floor:
                if step and step > 1:
                    cur = self._conn.execute(
                        "SELECT (ts/?)*?, AVG(value) FROM samples WHERE metric=? AND ts BETWEEN ? AND ? "
                        "GROUP BY ts/? ORDER BY 1",
                        (step, step, metric, start, end, step),
                    )
                else:
                    cur = self._conn.execute(
                        "SELECT ts, value FROM samples WHERE metric=? AND ts BETWEEN ? AND ? ORDER BY ts",
                        (metric, start, end),
                    )
                rows = [(int(t), float(v)) for t, v in cur.fetchall() if v is not None]
                if rows:
                    return rows
            cur = self._conn.execute(
                "SELECT ts, avg FROM hourly WHERE metric=? AND ts BETWEEN ? AND ? ORDER BY ts",
                (metric, start, end),
            )
            return [(int(t), float(v)) for t, v in cur.fetchall() if v is not None]

    def query_many(self, metrics: list[str], start: float, end: float, step: int | None = None) -> dict[str, list]:
        return {m: self.query(m, start, end, step) for m in metrics}

    def daily_energy(self, days: int = 30) -> dict[str, dict[str, float]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT day, metric, value FROM daily_energy ORDER BY day DESC LIMIT ?", (days * 20,)
            )
            out: dict[str, dict[str, float]] = {}
            for day, metric, value in cur.fetchall():
                out.setdefault(day, {})[metric] = value
            return out

    def events(self, limit: int = 100) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT ts, level, source, message FROM events ORDER BY id DESC LIMIT ?", (limit,)
            )
            return [{"ts": t, "level": lv, "source": s, "message": m} for t, lv, s, m in cur.fetchall()]

    def metrics(self) -> list[str]:
        with self._lock:
            cur = self._conn.execute("SELECT DISTINCT metric FROM samples UNION SELECT DISTINCT metric FROM hourly")
            return sorted(r[0] for r in cur.fetchall())

    def stats(self) -> dict:
        with self._lock:
            n_raw = self._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            n_hourly = self._conn.execute("SELECT COUNT(*) FROM hourly").fetchone()[0]
            size = Path(self.path).stat().st_size if self.path != ":memory:" and Path(self.path).exists() else 0
        return {"raw_rows": n_raw, "hourly_rows": n_hourly, "db_bytes": size, "retention_days": self.retention_days}
