"""Collectors for Elexon BMRS wholesale price feeds.

Two feeds live here:

1) **Market Index Data (MID) — day-ahead wholesale.**
   Endpoint: https://data.elexon.co.uk/bmrs/api/v1/datasets/MID?from=YYYY-MM-DD&to=YYYY-MM-DD
   Providers:
     - APXMIDP — APX (now EPEX UK) volume-weighted market index (what we use)
     - N2EXMIDP — N2EX volume-weighted market index (currently ~zero volume)
   Values are in GBP/MWh; half-hourly (settlementPeriod 1-48). Stored as
   source='elexon_apx'. Placeholder for ENTSO-E — swap once we have a token.

2) **System Prices (DISEBSP) — within-day / imbalance settlement price.**
   Endpoint: https://data.elexon.co.uk/bmrs/api/v1/balancing/settlement/system-prices/{YYYY-MM-DD}
   Publishes the cash-out / imbalance price per settlement period after the
   period has cleared. It's the standard GB within-day price proxy (the
   day-ahead auction clears pre-delivery; the imbalance/system price reflects
   actual same-day market conditions). GBP/MWh; stored as source='elexon_system_price'.
   Fields we care about: `startTime`, `systemSellPrice`, `systemBuyPrice`
   (under dual-cashout they're usually equal; we take the average).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterator

import requests

from ..storage import insert_outturn, record_run, utcnow_iso

LOG = logging.getLogger(__name__)

URL = "https://data.elexon.co.uk/bmrs/api/v1/datasets/MID"
SYSTEM_PRICE_URL = "https://data.elexon.co.uk/bmrs/api/v1/balancing/settlement/system-prices/{date}"
TIMEOUT_S = 30
DEFAULT_PROVIDER = "APXMIDP"


def _settlement_to_utc(settlement_date: str, settlement_period: int) -> str:
    """Convert (date, period 1-48) to UTC ISO-8601. Period 1 starts 00:00 UTC on settlement_date."""
    base = datetime.fromisoformat(settlement_date).replace(tzinfo=timezone.utc)
    # Period 1 covers [23:00, 23:30] the previous day in local UK time — but
    # Elexon's convention for settlement_date is the *settlement* date, with
    # period 1 = 00:00-00:30 on that date in UTC. We trust startTime from API.
    return (base + timedelta(minutes=30 * (settlement_period - 1))).isoformat().replace("+00:00", "Z")


def fetch(from_date: str, to_date: str, provider: str = DEFAULT_PROVIDER) -> list[dict]:
    params = {"from": from_date, "to": to_date, "format": "json"}
    resp = requests.get(URL, params=params, timeout=TIMEOUT_S)
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data", [])
    return [r for r in data if r.get("dataProvider") == provider]


def _rows_from_payload(results: list[dict]) -> Iterator[dict]:
    fetched = utcnow_iso()
    for r in results:
        # Prefer the API's startTime if present; it is already UTC ISO-8601.
        target_start = r.get("startTime")
        if target_start and target_start.endswith("Z"):
            target = target_start
        elif target_start:
            target = target_start.replace("+00:00", "Z")
        else:
            target = _settlement_to_utc(r["settlementDate"], int(r["settlementPeriod"]))
        yield {
            "target_start": target,
            "region": "_",  # wholesale is not region-specific
            "source": "elexon_apx",
            "value": float(r["price"]),
            "unit": "gbp/mwh",
            "fetched_at": fetched,
        }


def collect(conn, days_back: int = 30, provider: str = DEFAULT_PROVIDER) -> int:
    started = utcnow_iso()
    try:
        today = datetime.now(timezone.utc).date()
        rows_added_total = 0
        # Elexon caps per-call range — chunk in 7-day windows to be safe.
        cursor = today - timedelta(days=days_back)
        while cursor <= today:
            chunk_end = min(cursor + timedelta(days=7), today)
            results = fetch(cursor.isoformat(), chunk_end.isoformat(), provider)
            rows = list(_rows_from_payload(results))
            rows_added_total += insert_outturn(conn, rows) if rows else 0
            cursor = chunk_end + timedelta(days=1)
        record_run(conn, f"elexon:{provider}", started, utcnow_iso(), rows_added_total, "ok",
                   f"{days_back}d back")
        LOG.info("Elexon %s: +%d rows (%dd back)", provider, rows_added_total, days_back)
        return rows_added_total
    except Exception as exc:
        record_run(conn, f"elexon:{provider}", started, utcnow_iso(), 0, "error", str(exc))
        raise


# ---------------------------------------------------------------------------
# Within-day / imbalance system price
# ---------------------------------------------------------------------------


def fetch_system_prices(settlement_date: str) -> list[dict]:
    """Return the DISEBSP (system prices) records for one settlement date.

    Each record covers one settlement period (1-48). Fields of interest:
    `startTime` (UTC ISO-8601), `systemSellPrice`, `systemBuyPrice`.
    """
    url = SYSTEM_PRICE_URL.format(date=settlement_date)
    resp = requests.get(url, params={"format": "json"}, timeout=TIMEOUT_S)
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("data", []) or []


def _rows_from_system_prices(results: list[dict]) -> Iterator[dict]:
    fetched = utcnow_iso()
    for r in results:
        start = r.get("startTime")
        if not start:
            continue
        target = start if start.endswith("Z") else start.replace("+00:00", "Z")
        # Under dual-cashout (single imbalance price) sell == buy. Average
        # defensively in case we ever see a regime where they diverge.
        sell = r.get("systemSellPrice")
        buy = r.get("systemBuyPrice")
        if sell is None and buy is None:
            continue
        if sell is None:
            price = float(buy)
        elif buy is None:
            price = float(sell)
        else:
            price = (float(sell) + float(buy)) / 2.0
        yield {
            "target_start": target,
            "region": "_",  # not region-specific
            "source": "elexon_system_price",
            "value": price,
            "unit": "gbp/mwh",
            "fetched_at": fetched,
        }


def collect_system_price(conn, days_back: int = 7) -> int:
    """Fetch Elexon system prices (within-day / imbalance) for the last N days + today + tomorrow.

    The system price is post-hoc (published after each settlement period), so
    tomorrow's data will simply be absent — that's expected, not an error.
    """
    started = utcnow_iso()
    try:
        today = datetime.now(timezone.utc).date()
        rows_added_total = 0
        # Pull [today - days_back, today + 1] so we catch yesterday cleanly and
        # any early-published periods for tomorrow if Elexon has them.
        cursor = today - timedelta(days=days_back)
        end = today + timedelta(days=1)
        while cursor <= end:
            try:
                results = fetch_system_prices(cursor.isoformat())
            except requests.HTTPError as exc:
                # Future dates (e.g. tomorrow) may 404 before publication — skip.
                if exc.response is not None and exc.response.status_code in (400, 404):
                    results = []
                else:
                    raise
            rows = list(_rows_from_system_prices(results))
            if rows:
                rows_added_total += insert_outturn(conn, rows)
            cursor += timedelta(days=1)
        record_run(conn, "elexon:system_price", started, utcnow_iso(),
                   rows_added_total, "ok", f"{days_back}d back through tomorrow")
        LOG.info("Elexon system_price: +%d rows (%dd back)", rows_added_total, days_back)
        return rows_added_total
    except Exception as exc:
        record_run(conn, "elexon:system_price", started, utcnow_iso(), 0, "error", str(exc))
        raise
