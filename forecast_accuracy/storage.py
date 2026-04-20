"""SQLite storage for forecasts, outturn, and consumption.

Schema:
  forecasts   — one row per (predicted_at, target_start, region, source)
  outturn     — one row per (target_start, region, source)
  consumption — one row per (target_start, mpan, serial) — user's own kWh
  All timestamps stored as ISO-8601 UTC strings ending in 'Z'.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

# DB path resolution order:
#   1. FORECAST_ACCURACY_DB env var (explicit override)
#   2. ./data/forecast_accuracy.sqlite next to the package (default for local use)
_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "forecast_accuracy.sqlite"
DEFAULT_DB_PATH = Path(os.environ.get("FORECAST_ACCURACY_DB", _DEFAULT_PATH))


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS forecasts (
    predicted_at    TEXT NOT NULL,   -- ISO-8601 UTC, when the forecast snapshot was produced
    target_start    TEXT NOT NULL,   -- ISO-8601 UTC, HH period start
    region          TEXT NOT NULL,   -- single-letter GSP region, e.g. 'G'
    source          TEXT NOT NULL,   -- e.g. 'agilepredict'
    value_p_per_kwh REAL NOT NULL,   -- point prediction, p/kWh (inc VAT by convention)
    value_low       REAL,            -- lower CI bound if provided
    value_high      REAL,            -- upper CI bound if provided
    PRIMARY KEY (predicted_at, target_start, region, source)
);

CREATE INDEX IF NOT EXISTS idx_forecasts_target    ON forecasts(target_start);
CREATE INDEX IF NOT EXISTS idx_forecasts_predicted ON forecasts(predicted_at);

CREATE TABLE IF NOT EXISTS outturn (
    target_start    TEXT NOT NULL,   -- ISO-8601 UTC, HH period start
    region          TEXT NOT NULL,   -- single-letter GSP region; '_' for wholesale (not region-specific)
    source          TEXT NOT NULL,   -- 'octopus_agile' | 'elexon_apx'
    value           REAL NOT NULL,   -- price in `unit`
    unit            TEXT NOT NULL,   -- 'p/kWh' (octopus_agile) | 'gbp/mwh' (elexon_apx)
    fetched_at      TEXT NOT NULL,   -- ISO-8601 UTC
    PRIMARY KEY (target_start, region, source)
);

CREATE INDEX IF NOT EXISTS idx_outturn_target ON outturn(target_start);

CREATE TABLE IF NOT EXISTS consumption (
    target_start    TEXT NOT NULL,   -- ISO-8601 UTC, HH period start
    mpan            TEXT NOT NULL,   -- 13-digit meter point administration number
    serial          TEXT NOT NULL,   -- meter serial (so meter swaps don't collide)
    kwh             REAL NOT NULL,   -- consumption for that HH (kWh)
    fetched_at      TEXT NOT NULL,   -- ISO-8601 UTC
    PRIMARY KEY (target_start, mpan, serial)
);

CREATE INDEX IF NOT EXISTS idx_consumption_target ON consumption(target_start);

CREATE TABLE IF NOT EXISTS collector_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    collector   TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    rows_added  INTEGER,
    status      TEXT,   -- 'ok' | 'error'
    message     TEXT
);
"""


def utcnow_iso() -> str:
    """ISO-8601 UTC with Z suffix, seconds precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@contextmanager
def connect(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_forecasts(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Insert forecast rows; returns number of rows actually inserted (ignores duplicates)."""
    sql = """
        INSERT OR IGNORE INTO forecasts
            (predicted_at, target_start, region, source, value_p_per_kwh, value_low, value_high)
        VALUES (:predicted_at, :target_start, :region, :source, :value_p_per_kwh, :value_low, :value_high)
    """
    cur = conn.executemany(sql, rows)
    return cur.rowcount or 0


def insert_outturn(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    sql = """
        INSERT OR REPLACE INTO outturn
            (target_start, region, source, value, unit, fetched_at)
        VALUES (:target_start, :region, :source, :value, :unit, :fetched_at)
    """
    cur = conn.executemany(sql, rows)
    return cur.rowcount or 0


def insert_consumption(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Insert/refresh HH consumption rows; returns rowcount.

    REPLACE rather than IGNORE so a re-published HH (Octopus sometimes
    restates) lands with the latest value.
    """
    sql = """
        INSERT OR REPLACE INTO consumption
            (target_start, mpan, serial, kwh, fetched_at)
        VALUES (:target_start, :mpan, :serial, :kwh, :fetched_at)
    """
    cur = conn.executemany(sql, rows)
    return cur.rowcount or 0


def record_run(conn: sqlite3.Connection, collector: str, started_at: str,
               ended_at: str, rows_added: int, status: str, message: str = "") -> None:
    conn.execute(
        "INSERT INTO collector_runs (collector, started_at, ended_at, rows_added, status, message) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (collector, started_at, ended_at, rows_added, status, message),
    )


def summary(conn: sqlite3.Connection) -> dict:
    def _count(table: str) -> int:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _extent(table: str, col: str) -> tuple[str | None, str | None]:
        row = conn.execute(f"SELECT MIN({col}), MAX({col}) FROM {table}").fetchone()
        return (row[0], row[1]) if row else (None, None)

    fc_min, fc_max = _extent("forecasts", "target_start")
    ot_min, ot_max = _extent("outturn", "target_start")
    cs_min, cs_max = _extent("consumption", "target_start")
    return {
        "forecast_rows": _count("forecasts"),
        "outturn_rows": _count("outturn"),
        "consumption_rows": _count("consumption"),
        "forecast_span": (fc_min, fc_max),
        "outturn_span": (ot_min, ot_max),
        "consumption_span": (cs_min, cs_max),
    }
