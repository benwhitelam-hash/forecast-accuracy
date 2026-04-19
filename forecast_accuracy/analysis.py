"""MAPE / MAE analysis joining AgilePredict forecasts to Octopus Agile outturn.

Design notes
------------
- **Primary comparison**: AgilePredict p/kWh ↔ Octopus Agile p/kWh, same region.
- **Horizon bucketing**: (target_start - predicted_at) in hours, bucketed as
  0–24h, 24–48h, 48–72h, 72–120h, 120h+.
- **MAPE edge case**: Agile prices can be near zero or negative on windy nights,
  which makes MAPE explode. We:
    * compute MAPE over rows where |outturn| >= MAPE_MIN_ABS (default 2 p/kWh);
    * report the percentage of rows excluded;
    * also expose MAE (p/kWh) which is stable across the full range.
- For each (target_start, region, source) we keep **all** AgilePredict
  snapshots: the UI lets the user either pick the *latest* snapshot per
  target, or aggregate over horizons.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

UK = ZoneInfo("Europe/London")

MAPE_MIN_ABS = 2.0  # p/kWh — rows with |outturn| below this are excluded from MAPE


HORIZON_BUCKETS = [
    (0,    24,  "0–24h"),
    (24,   48,  "24–48h"),
    (48,   72,  "48–72h"),
    (72,  120,  "72–120h"),
    (120, 10_000, "120h+"),
]


def _bucket(hours: float) -> str:
    for lo, hi, label in HORIZON_BUCKETS:
        if lo <= hours < hi:
            return label
    return "?"


def load_joined(conn: sqlite3.Connection,
                outturn_source: str = "octopus_agile",
                region: str = "G") -> pd.DataFrame:
    """Return a DataFrame with all forecasts joined to matching outturn rows.

    Columns: predicted_at, target_start, region, forecast, outturn,
             horizon_hours, horizon_bucket, month, abs_error, ape.
    """
    sql = """
        SELECT
            f.predicted_at     AS predicted_at,
            f.target_start     AS target_start,
            f.region           AS region,
            f.source           AS forecast_source,
            f.value_p_per_kwh  AS forecast,
            o.value            AS outturn
        FROM forecasts f
        INNER JOIN outturn o
                ON o.target_start = f.target_start
               AND o.region = f.region
               AND o.source = ?
        WHERE f.region = ?
    """
    df = pd.read_sql_query(sql, conn, params=(outturn_source, region),
                           parse_dates=["predicted_at", "target_start"])
    if df.empty:
        return df
    df["horizon_hours"] = (df["target_start"] - df["predicted_at"]).dt.total_seconds() / 3600.0
    df["horizon_bucket"] = df["horizon_hours"].apply(_bucket)
    df["month"] = df["target_start"].dt.strftime("%Y-%m")
    df["abs_error"] = (df["forecast"] - df["outturn"]).abs()
    df["ape"] = df.apply(
        lambda r: abs(r["forecast"] - r["outturn"]) / abs(r["outturn"]) * 100.0
                  if abs(r["outturn"]) >= MAPE_MIN_ABS else None,
        axis=1,
    )
    return df


def monthly_accuracy(df: pd.DataFrame,
                     snapshot: Literal["latest", "all"] = "latest") -> pd.DataFrame:
    """Aggregate joined data to monthly MAPE / MAE per horizon bucket.

    - `snapshot="latest"`: for each (target_start, horizon_bucket) keep only
      the most recent forecast snapshot before the target. This answers
      "how accurate was the forecast at that horizon, using the freshest data
      available at the time?"
    - `snapshot="all"`: include every snapshot. Useful if you want to see
      whether repeated refinements improve things.
    """
    if df.empty:
        return pd.DataFrame(columns=["month", "horizon_bucket", "n", "mae_p_per_kwh",
                                     "mape_pct", "mape_sample_n"])
    if snapshot == "latest":
        # For each target_start, within each horizon bucket, keep the latest predicted_at.
        df = (df.sort_values("predicted_at")
                .groupby(["target_start", "horizon_bucket"], as_index=False)
                .tail(1))
    out = (df.groupby(["month", "horizon_bucket"])
             .agg(n=("abs_error", "size"),
                  mae_p_per_kwh=("abs_error", "mean"),
                  mape_pct=("ape", "mean"),
                  mape_sample_n=("ape", "count"))
             .reset_index())
    return out.sort_values(["month", "horizon_bucket"])


def day_window(days_back: int = 1,
               days_forward: int = 2,
               now: datetime | None = None) -> tuple[str, str, datetime]:
    """Return (utc_start_iso, utc_end_iso, uk_today_midnight) covering
    `days_back` days before UK today-midnight through `days_forward` days
    ahead (exclusive end). `days_back=1, days_forward=2` reproduces the
    yesterday → end-of-tomorrow window. ISO strings end in 'Z'.
    """
    now_utc = now or datetime.now(timezone.utc)
    now_uk = now_utc.astimezone(UK)
    today_uk = now_uk.replace(hour=0, minute=0, second=0, microsecond=0)
    start_uk = today_uk - timedelta(days=int(days_back))
    end_uk = today_uk + timedelta(days=int(days_forward))
    start_utc = start_uk.astimezone(timezone.utc).replace(microsecond=0)
    end_utc = end_uk.astimezone(timezone.utc).replace(microsecond=0)

    def _z(dt: datetime) -> str:
        return dt.isoformat().replace("+00:00", "Z")
    return _z(start_utc), _z(end_utc), today_uk


def yesterday_today_tomorrow_window(now: datetime | None = None) -> tuple[str, str, datetime]:
    """Back-compat shim: yesterday 00:00 UK → tomorrow 23:59 UK."""
    return day_window(days_back=1, days_forward=2, now=now)


# Display labels for the recent-prices chart, ordered for legend stability.
RECENT_SERIES_LABELS = [
    "Day-ahead (Elexon APX)",
    "Within-day (Elexon system price)",
    "Confirmed (Octopus Agile)",
    "Predicted (AgilePredict, freshest snapshot)",
]


def recent_prices(conn: sqlite3.Connection,
                  region: str = "G",
                  now: datetime | None = None,
                  days_back: int = 1,
                  days_forward: int = 2) -> pd.DataFrame:
    """Long-format DataFrame of HH prices over a UK-aligned day window,
    one row per (target_start, series). All values normalised to GBP/MWh
    (Octopus & AgilePredict are p/kWh × 10 — VAT and retail margin are
    *not* removed, so retail series will sit systematically above wholesale).

    Defaults to yesterday / today / tomorrow. Pass a larger `days_back` /
    `days_forward` to pre-load a wider window (e.g. for a client-side slider).

    Columns: target_start (UTC datetime), series (str), value_gbp_per_mwh (float).
    """
    start_iso, end_iso, _today = day_window(days_back=days_back,
                                            days_forward=days_forward,
                                            now=now)

    # Forecasts: keep the freshest snapshot per target_start (whatever horizon).
    fc_sql = """
        SELECT target_start, value_p_per_kwh
        FROM forecasts f
        WHERE source = 'agilepredict'
          AND region = ?
          AND target_start >= ?
          AND target_start <  ?
          AND predicted_at = (
              SELECT MAX(predicted_at) FROM forecasts f2
              WHERE f2.target_start = f.target_start
                AND f2.source      = f.source
                AND f2.region      = f.region
          )
    """
    fc = pd.read_sql_query(fc_sql, conn, params=(region, start_iso, end_iso),
                           parse_dates=["target_start"])
    fc["series"] = "Predicted (AgilePredict, freshest snapshot)"
    fc["value_gbp_per_mwh"] = fc["value_p_per_kwh"] * 10.0
    fc = fc[["target_start", "series", "value_gbp_per_mwh"]]

    # Octopus confirmed: p/kWh × 10 → £/MWh-equivalent.
    oc = pd.read_sql_query(
        "SELECT target_start, value FROM outturn "
        "WHERE source = 'octopus_agile' AND region = ? "
        "  AND target_start >= ? AND target_start < ?",
        conn, params=(region, start_iso, end_iso), parse_dates=["target_start"],
    )
    oc["series"] = "Confirmed (Octopus Agile)"
    oc["value_gbp_per_mwh"] = oc["value"] * 10.0
    oc = oc[["target_start", "series", "value_gbp_per_mwh"]]

    # Elexon day-ahead (already £/MWh).
    da = pd.read_sql_query(
        "SELECT target_start, value FROM outturn "
        "WHERE source = 'elexon_apx' "
        "  AND target_start >= ? AND target_start < ?",
        conn, params=(start_iso, end_iso), parse_dates=["target_start"],
    )
    da["series"] = "Day-ahead (Elexon APX)"
    da["value_gbp_per_mwh"] = da["value"]
    da = da[["target_start", "series", "value_gbp_per_mwh"]]

    # Elexon within-day / system imbalance price (already £/MWh).
    wd = pd.read_sql_query(
        "SELECT target_start, value FROM outturn "
        "WHERE source = 'elexon_system_price' "
        "  AND target_start >= ? AND target_start < ?",
        conn, params=(start_iso, end_iso), parse_dates=["target_start"],
    )
    wd["series"] = "Within-day (Elexon system price)"
    wd["value_gbp_per_mwh"] = wd["value"]
    wd = wd[["target_start", "series", "value_gbp_per_mwh"]]

    out = pd.concat([fc, oc, da, wd], ignore_index=True)
    if not out.empty:
        out = out.sort_values(["series", "target_start"]).reset_index(drop=True)
    return out


def overall_accuracy(df: pd.DataFrame,
                     snapshot: Literal["latest", "all"] = "latest") -> pd.DataFrame:
    """Same as monthly_accuracy but aggregated across all months — one row per horizon."""
    if df.empty:
        return pd.DataFrame(columns=["horizon_bucket", "n", "mae_p_per_kwh",
                                     "mape_pct", "mape_sample_n"])
    if snapshot == "latest":
        df = (df.sort_values("predicted_at")
                .groupby(["target_start", "horizon_bucket"], as_index=False)
                .tail(1))
    return (df.groupby("horizon_bucket")
              .agg(n=("abs_error", "size"),
                   mae_p_per_kwh=("abs_error", "mean"),
                   mape_pct=("ape", "mean"),
                   mape_sample_n=("ape", "count"))
              .reset_index()
              .sort_values("horizon_bucket"))
