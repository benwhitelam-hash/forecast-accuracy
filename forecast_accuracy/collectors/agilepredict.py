"""Collector for AgilePredict forecasts (https://agilepredict.com).

Endpoint: https://prices.fly.dev/api/{REGION}/
Returns a list of forecast snapshots. Each snapshot has `created_at` (when the
model was run) and `prices[]` with `date_time`, `agile_pred`, `agile_low`,
`agile_high`. Values are in p/kWh (Agile retail price, inc VAT by convention).

The endpoint typically returns one snapshot (the most recent). Previous
snapshots are *not* re-served — so to build a history we must poll periodically
and insert (predicted_at, target_start) rows each time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterator

import requests

from ..storage import insert_forecasts, record_run, utcnow_iso

LOG = logging.getLogger(__name__)

BASE_URL = "https://prices.fly.dev/api/{region}/"
DEFAULT_REGION = "G"
TIMEOUT_S = 20


def _iso_utc(dt_str: str) -> str:
    """AgilePredict returns +01:00/+00:00 local times; normalise to UTC Z."""
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _rows_from_snapshot(snapshot: dict, region: str) -> Iterator[dict]:
    predicted_at = _iso_utc(snapshot["created_at"])
    for pt in snapshot.get("prices", []):
        yield {
            "predicted_at": predicted_at,
            "target_start": _iso_utc(pt["date_time"]),
            "region": region,
            "source": "agilepredict",
            "value_p_per_kwh": float(pt["agile_pred"]),
            "value_low": float(pt["agile_low"]) if pt.get("agile_low") is not None else None,
            "value_high": float(pt["agile_high"]) if pt.get("agile_high") is not None else None,
        }


def fetch(region: str = DEFAULT_REGION) -> list[dict]:
    """Fetch raw JSON from AgilePredict."""
    url = BASE_URL.format(region=region)
    resp = requests.get(url, timeout=TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"Unexpected AgilePredict response shape: {type(data)}")
    return data


def collect(conn, region: str = DEFAULT_REGION) -> int:
    """Fetch latest snapshot(s) and insert into forecasts table. Returns rows inserted."""
    started = utcnow_iso()
    try:
        snapshots = fetch(region)
        rows = [r for snap in snapshots for r in _rows_from_snapshot(snap, region)]
        added = insert_forecasts(conn, rows) if rows else 0
        record_run(conn, f"agilepredict:{region}", started, utcnow_iso(), added, "ok",
                   f"{len(snapshots)} snapshots, {len(rows)} points")
        LOG.info("AgilePredict %s: +%d rows (of %d points)", region, added, len(rows))
        return added
    except Exception as exc:  # pragma: no cover
        record_run(conn, f"agilepredict:{region}", started, utcnow_iso(), 0, "error", str(exc))
        raise
