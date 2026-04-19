"""Collector for Octopus Agile published HH prices (primary ground truth).

Endpoint: https://api.octopus.energy/v1/products/{PRODUCT}/electricity-tariffs/
         E-1R-{PRODUCT}-{REGION}/standard-unit-rates/?period_from=...&period_to=...

Returns p/kWh inc/ex VAT at half-hourly granularity. Apples-to-apples with
AgilePredict's `agile_pred` (both are Agile retail, p/kWh).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterator

import requests

from ..storage import insert_outturn, record_run, utcnow_iso

LOG = logging.getLogger(__name__)

PRODUCT_CODE = "AGILE-24-10-01"
URL_TEMPLATE = (
    "https://api.octopus.energy/v1/products/{product}/electricity-tariffs/"
    "E-1R-{product}-{region}/standard-unit-rates/"
)
TIMEOUT_S = 20


def _iso_utc(dt_str: str) -> str:
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _paginate(start_url: str) -> Iterator[dict]:
    url = start_url
    while url:
        resp = requests.get(url, timeout=TIMEOUT_S)
        resp.raise_for_status()
        payload = resp.json()
        yield from payload.get("results", [])
        url = payload.get("next")


def fetch(region: str = "G", days_back: int = 30,
          product: str = PRODUCT_CODE) -> list[dict]:
    """Return Agile unit-rate rows for `days_back` days up to now."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(days=days_back)
    url = (
        URL_TEMPLATE.format(product=product, region=region)
        + f"?period_from={start.isoformat().replace('+00:00', 'Z')}"
        + f"&period_to={now.isoformat().replace('+00:00', 'Z')}"
        + "&page_size=1500"
    )
    return list(_paginate(url))


def _rows_from_payload(results: list[dict], region: str) -> Iterator[dict]:
    fetched = utcnow_iso()
    for r in results:
        yield {
            "target_start": _iso_utc(r["valid_from"]),
            "region": region,
            "source": "octopus_agile",
            "value": float(r["value_inc_vat"]),
            "unit": "p/kWh",
            "fetched_at": fetched,
        }


def collect(conn, region: str = "G", days_back: int = 30,
            product: str = PRODUCT_CODE) -> int:
    started = utcnow_iso()
    try:
        results = fetch(region=region, days_back=days_back, product=product)
        rows = list(_rows_from_payload(results, region))
        added = insert_outturn(conn, rows) if rows else 0
        record_run(conn, f"octopus:{region}", started, utcnow_iso(), added, "ok",
                   f"{len(results)} rows fetched")
        LOG.info("Octopus %s: +%d rows", region, added)
        return added
    except Exception as exc:
        record_run(conn, f"octopus:{region}", started, utcnow_iso(), 0, "error", str(exc))
        raise
