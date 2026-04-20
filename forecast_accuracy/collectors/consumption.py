"""Collector for the user's own Octopus consumption data (half-hourly kWh).

Endpoint
--------
``GET /v1/electricity-meter-points/{mpan}/meters/{serial}/consumption/``

Auth: HTTP Basic — the Octopus API key goes in the *username* field, with an
empty password. The key, MPAN and meter serial are read from environment
variables so secrets never land in the repo:

* ``OCTOPUS_API_KEY``   (GH Actions *secret*)
* ``OCTOPUS_MPAN``      (GH Actions *variable* — 13-digit meter-point ID)
* ``OCTOPUS_SERIAL``    (GH Actions *variable* — meter serial number)

The collector is a no-op if any of the three are missing, so the cron keeps
running even on forks without creds configured.

Response shape
--------------
``{"count": N, "next": url|null, "results": [{"consumption": float kWh,
"interval_start": iso, "interval_end": iso}, ...]}``

We normalise ``interval_start`` to UTC and write into a dedicated
``consumption`` table keyed on (target_start, mpan, serial).
"""
from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Iterator

import requests

from ..storage import insert_consumption, record_run, utcnow_iso

LOG = logging.getLogger(__name__)

URL_TEMPLATE = (
    "https://api.octopus.energy/v1/electricity-meter-points/{mpan}/"
    "meters/{serial}/consumption/"
)
TIMEOUT_S = 30
PAGE_SIZE = 1500  # Octopus caps at 25k; 1500 is comfortable and fast.


class MissingCredentials(RuntimeError):
    """Raised when one of OCTOPUS_API_KEY/MPAN/SERIAL is unset."""


def _credentials() -> tuple[str, str, str]:
    key = os.environ.get("OCTOPUS_API_KEY", "").strip()
    mpan = os.environ.get("OCTOPUS_MPAN", "").strip()
    serial = os.environ.get("OCTOPUS_SERIAL", "").strip()
    missing = [n for n, v in (("OCTOPUS_API_KEY", key),
                               ("OCTOPUS_MPAN", mpan),
                               ("OCTOPUS_SERIAL", serial)) if not v]
    if missing:
        raise MissingCredentials(
            "Missing environment variables: " + ", ".join(missing)
            + ". Set them in a local .env or as GH Actions secrets/vars."
        )
    return key, mpan, serial


def _iso_utc(dt_str: str) -> str:
    """Normalise any Octopus-returned ISO timestamp to UTC-with-Z."""
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _auth_header(api_key: str) -> dict[str, str]:
    token = base64.b64encode(f"{api_key}:".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _paginate(start_url: str, headers: dict[str, str]) -> Iterator[dict]:
    url = start_url
    while url:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT_S)
        resp.raise_for_status()
        payload = resp.json()
        yield from payload.get("results", [])
        url = payload.get("next")


def fetch(days_back: int = 90) -> tuple[list[dict], str, str]:
    """Return all HH consumption rows for the last `days_back` days.

    Returns ``(results, mpan, serial)`` so the caller can record which meter
    the numbers came from without re-reading the env.
    """
    api_key, mpan, serial = _credentials()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(days=int(days_back))
    url = (
        URL_TEMPLATE.format(mpan=mpan, serial=serial)
        + f"?period_from={start.isoformat().replace('+00:00', 'Z')}"
        + f"&period_to={now.isoformat().replace('+00:00', 'Z')}"
        + f"&page_size={PAGE_SIZE}&order_by=period"
    )
    results = list(_paginate(url, _auth_header(api_key)))
    return results, mpan, serial


def _rows_from_payload(results: list[dict], mpan: str, serial: str) -> Iterator[dict]:
    fetched = utcnow_iso()
    for r in results:
        yield {
            "target_start": _iso_utc(r["interval_start"]),
            "mpan": mpan,
            "serial": serial,
            "kwh": float(r["consumption"]),
            "fetched_at": fetched,
        }


def collect(conn, days_back: int = 90) -> int:
    """Fetch + insert; returns rows added. Safe no-op if creds missing."""
    started = utcnow_iso()
    try:
        results, mpan, serial = fetch(days_back=days_back)
        rows = list(_rows_from_payload(results, mpan, serial))
        added = insert_consumption(conn, rows) if rows else 0
        record_run(conn, f"consumption:{mpan[-4:]}/{serial}", started, utcnow_iso(),
                   added, "ok", f"{len(results)} rows fetched, {added} new")
        LOG.info("Consumption %s...%s: +%d rows (of %d)",
                 mpan[:4], mpan[-4:], added, len(results))
        return added
    except MissingCredentials as exc:
        # Skip cleanly rather than raise — lets shared CI runs work without creds.
        record_run(conn, "consumption", started, utcnow_iso(), 0, "skip", str(exc))
        LOG.warning("Consumption: skipped — %s", exc)
        return 0
    except Exception as exc:
        record_run(conn, "consumption", started, utcnow_iso(), 0, "error", str(exc))
        raise
