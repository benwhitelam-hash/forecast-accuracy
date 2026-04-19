"""CLI entrypoint for scheduled collection.

Usage:
    python -m forecast_accuracy.cli refresh              # all collectors
    python -m forecast_accuracy.cli refresh --no-elexon  # skip Elexon
    python -m forecast_accuracy.cli summary
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import storage
from .collectors import agilepredict, elexon, octopus

LOG = logging.getLogger("forecast_accuracy")


def _safe(name: str, fn) -> tuple[str, int | None, str | None]:
    """Run a collector; never propagate exceptions out of cmd_refresh.

    Returns (name, rows_added, error_message). A flaky API shouldn't prevent
    the other collectors from running in the same cron cycle — each collector
    has its own record_run() entry in the DB for postmortem.
    """
    try:
        rows = fn()
        return name, int(rows or 0), None
    except Exception as exc:  # pragma: no cover — network flake paths
        LOG.exception("%s: collector failed", name)
        return name, None, f"{type(exc).__name__}: {exc}"


def cmd_refresh(args: argparse.Namespace) -> int:
    results: list[tuple[str, int | None, str | None]] = []
    with storage.connect() as conn:
        if args.agilepredict:
            results.append(_safe("agilepredict",
                                 lambda: agilepredict.collect(conn, region=args.region)))
        if args.octopus:
            results.append(_safe("octopus",
                                 lambda: octopus.collect(conn, region=args.region,
                                                         days_back=args.days_back)))
        if args.elexon:
            results.append(_safe("elexon_apx",
                                 lambda: elexon.collect(conn, days_back=args.days_back)))
        if args.elexon_wd:
            results.append(_safe("elexon_system_price",
                                 lambda: elexon.collect_system_price(
                                     conn, days_back=min(args.days_back, 14))))

    failures = [r for r in results if r[2] is not None]
    for name, rows, err in results:
        if err:
            print(f"[FAIL] {name}: {err}")
        else:
            print(f"[OK]   {name}: +{rows} rows")
    # Non-zero exit if *every* collector failed; partial success is still a
    # successful run — the commit step in CI will push whatever did come in.
    if results and len(failures) == len(results):
        return 2
    return 0


def cmd_summary(_args: argparse.Namespace) -> int:
    with storage.connect() as conn:
        s = storage.summary(conn)
    print(f"forecasts: {s['forecast_rows']:,}  span {s['forecast_span']}")
    print(f"outturn:   {s['outturn_rows']:,}  span {s['outturn_span']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="forecast_accuracy")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("refresh", help="Run collectors")
    pr.add_argument("--region", default="G")
    pr.add_argument("--days-back", type=int, default=30)
    pr.add_argument("--no-agilepredict", dest="agilepredict", action="store_false")
    pr.add_argument("--no-octopus",       dest="octopus",       action="store_false")
    pr.add_argument("--no-elexon",        dest="elexon",        action="store_false")
    pr.add_argument("--no-elexon-wd",     dest="elexon_wd",     action="store_false",
                    help="Skip Elexon system price (within-day) collector.")
    pr.set_defaults(agilepredict=True, octopus=True, elexon=True, elexon_wd=True,
                    func=cmd_refresh)

    ps = sub.add_parser("summary", help="Show DB row counts / extents")
    ps.set_defaults(func=cmd_summary)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
