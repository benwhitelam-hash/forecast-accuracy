"""Streamlit UI: monthly MAPE / MAE for AgilePredict vs Octopus Agile outturn.

Run with:
    streamlit run forecast_accuracy/app.py

Defaults to region G (London). Exposes a "Refresh data" button that runs both
collectors on demand.
"""
from __future__ import annotations

# Streamlit runs this as a top-level script (no package context), so relative
# imports fail. Prepend the project root to sys.path and use absolute imports.
import pathlib
import sys

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import altair as alt
import pandas as pd
import streamlit as st

from forecast_accuracy import analysis, storage
from forecast_accuracy.collectors import agilepredict, elexon, octopus

st.set_page_config(page_title="AgilePredict Accuracy", layout="wide")

st.title("AgilePredict accuracy tracker")
st.caption(
    "Monthly MAPE / MAE of AgilePredict vs Octopus Agile published prices. "
    "Forecast and outturn are both in p/kWh, inc-VAT, half-hourly."
)

# -- Sidebar controls ---------------------------------------------------------
with st.sidebar:
    st.header("Controls")
    region = st.selectbox("Region", ["G", "A", "B", "C", "D", "E", "F", "H", "J",
                                     "K", "L", "M", "N", "P"], index=0,
                          help="GSP region. G = London.")
    snapshot = st.radio("Snapshot selection",
                        ["latest", "all"], horizontal=True,
                        help="'latest' = for each target HH, keep only the freshest forecast "
                             "per horizon bucket. 'all' = include every snapshot.")
    outturn_source = st.selectbox("Outturn source",
                                  ["octopus_agile", "elexon_apx"],
                                  help="Octopus Agile is the apples-to-apples ground truth. "
                                       "Elexon APX is wholesale £/MWh (different units — not a "
                                       "valid MAPE compare unless you transform it first).")
    days_back = st.number_input("Days to backfill on refresh", 1, 180, 30)
    st.divider()
    refresh_forecasts = st.checkbox("AgilePredict", value=True)
    refresh_octopus = st.checkbox("Octopus Agile", value=True)
    refresh_elexon = st.checkbox("Elexon APX (day-ahead)", value=True)
    refresh_elexon_wd = st.checkbox("Elexon system price (within-day)", value=True)
    run_refresh = st.button("↻ Refresh data", type="primary")

# -- Refresh action -----------------------------------------------------------
if run_refresh:
    logs: list[str] = []
    with storage.connect() as conn:
        if refresh_forecasts:
            try:
                n = agilepredict.collect(conn, region=region)
                logs.append(f"AgilePredict ({region}): +{n} forecast rows")
            except Exception as exc:
                logs.append(f"AgilePredict ({region}): FAILED — {exc}")
        if refresh_octopus:
            try:
                n = octopus.collect(conn, region=region, days_back=int(days_back))
                logs.append(f"Octopus Agile ({region}): +{n} outturn rows")
            except Exception as exc:
                logs.append(f"Octopus Agile ({region}): FAILED — {exc}")
        if refresh_elexon:
            try:
                n = elexon.collect(conn, days_back=int(days_back))
                logs.append(f"Elexon APX: +{n} wholesale rows")
            except Exception as exc:
                logs.append(f"Elexon APX: FAILED — {exc}")
        if refresh_elexon_wd:
            try:
                # 7 days back + today + tomorrow is plenty for the 3-day chart.
                n = elexon.collect_system_price(conn, days_back=7)
                logs.append(f"Elexon system price: +{n} within-day rows")
            except Exception as exc:
                logs.append(f"Elexon system price: FAILED — {exc}")
    for line in logs:
        st.toast(line)

# -- Load and summarise -------------------------------------------------------
with storage.connect() as conn:
    summary = storage.summary(conn)
    df = analysis.load_joined(conn, outturn_source=outturn_source, region=region)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Forecast rows", f"{summary['forecast_rows']:,}")
c2.metric("Outturn rows", f"{summary['outturn_rows']:,}")
c3.metric("Joined rows", f"{len(df):,}")
c4.metric("Outturn span",
          (summary["outturn_span"][0] or "—")[:10] + " → " +
          (summary["outturn_span"][1] or "—")[:10])

# -- Yesterday / today / tomorrow HH chart -----------------------------------
st.subheader("Half-hourly prices — yesterday, today, tomorrow")
st.caption(
    "All series normalised to **£/MWh**. Retail series (Octopus Agile, "
    "AgilePredict) are p/kWh × 10 with VAT and retail margin left in — they "
    "will sit systematically above wholesale (Elexon APX day-ahead, Elexon "
    "system price within-day). AgilePredict line uses the freshest snapshot "
    "we hold per target half-hour. Dashed vertical line = now."
)

with storage.connect() as conn:
    recent = analysis.recent_prices(conn, region=region)

if recent.empty:
    st.info(
        "No half-hourly prices yet for the yesterday/today/tomorrow window. "
        "Hit **Refresh data** with AgilePredict, Octopus Agile, Elexon APX and "
        "Elexon within-day all ticked."
    )
else:
    now_utc_ts = pd.Timestamp.utcnow().tz_localize(None)
    now_df = pd.DataFrame({"now": [now_utc_ts]})

    # Friendly fixed colour order: day-ahead gold, within-day purple,
    # confirmed blue, predicted red.
    color_scale = alt.Scale(
        domain=analysis.RECENT_SERIES_LABELS,
        range=["#E1A800", "#6A3D9A", "#1F77B4", "#D62728"],
    )
    base = (
        alt.Chart(recent)
        .mark_line(point=alt.OverlayMarkDef(size=18, filled=True), strokeWidth=1.8)
        .encode(
            x=alt.X("target_start:T", title="Half-hour (UTC)"),
            y=alt.Y("value_gbp_per_mwh:Q", title="£/MWh"),
            color=alt.Color("series:N", title="Series",
                            scale=color_scale,
                            sort=analysis.RECENT_SERIES_LABELS),
            tooltip=[
                alt.Tooltip("target_start:T", title="HH (UTC)"),
                alt.Tooltip("series:N", title="Series"),
                alt.Tooltip("value_gbp_per_mwh:Q", title="£/MWh", format=".2f"),
            ],
        )
    )
    now_rule = (
        alt.Chart(now_df)
        .mark_rule(strokeDash=[4, 4], color="#888")
        .encode(x="now:T")
    )
    st.altair_chart((base + now_rule).interactive(bind_y=False),
                    width="stretch")
    present = set(recent["series"].unique())
    missing = [s for s in analysis.RECENT_SERIES_LABELS if s not in present]
    if missing:
        st.caption("Missing series (no rows in DB for this window): "
                   + ", ".join(missing))

st.divider()

if df.empty:
    st.info("No joined data yet. Hit **Refresh data** (or wait for "
            "AgilePredict snapshots to accumulate — it typically takes a few "
            "days for forecast targets to actually pass and become comparable).")
    st.stop()

if outturn_source != "octopus_agile":
    st.warning("MAPE below compares AgilePredict (p/kWh) to Elexon wholesale "
               "(£/MWh) directly — the units don't match, so numbers here are "
               "**not meaningful** without transforming wholesale → retail. "
               "Switch to `octopus_agile` for a valid comparison.")

# -- Monthly table ------------------------------------------------------------
monthly = analysis.monthly_accuracy(df, snapshot=snapshot)
overall = analysis.overall_accuracy(df, snapshot=snapshot)

st.subheader("Monthly accuracy")
st.caption(f"Horizon buckets = hours between forecast and target. MAPE excludes "
           f"rows with |outturn| < {analysis.MAPE_MIN_ABS} p/kWh "
           f"to avoid divide-by-near-zero blow-up (see `mape_sample_n` vs `n`).")

st.dataframe(
    monthly.rename(columns={
        "month": "Month",
        "horizon_bucket": "Horizon",
        "n": "N (rows)",
        "mae_p_per_kwh": "MAE (p/kWh)",
        "mape_pct": "MAPE (%)",
        "mape_sample_n": "N (MAPE)",
    }).style.format({
        "MAE (p/kWh)": "{:.2f}",
        "MAPE (%)": "{:.1f}",
    }),
    width="stretch",
    hide_index=True,
)

# -- Chart --------------------------------------------------------------------
st.subheader("MAPE by month × horizon")
chart_df = monthly.dropna(subset=["mape_pct"])
if not chart_df.empty:
    chart = (alt.Chart(chart_df)
             .mark_line(point=True)
             .encode(x=alt.X("month:O", title="Month"),
                     y=alt.Y("mape_pct:Q", title="MAPE (%)"),
                     color=alt.Color("horizon_bucket:N", title="Horizon"),
                     tooltip=["month", "horizon_bucket", "mape_pct", "mae_p_per_kwh", "n"]))
    st.altair_chart(chart, width="stretch")
else:
    st.caption("(MAPE chart unavailable — all rows filtered by the near-zero guard.)")

st.subheader("Overall (all months)")
st.dataframe(
    overall.rename(columns={
        "horizon_bucket": "Horizon",
        "n": "N (rows)",
        "mae_p_per_kwh": "MAE (p/kWh)",
        "mape_pct": "MAPE (%)",
        "mape_sample_n": "N (MAPE)",
    }).style.format({"MAE (p/kWh)": "{:.2f}", "MAPE (%)": "{:.1f}"}),
    width="stretch",
    hide_index=True,
)

# -- Footnote -----------------------------------------------------------------
st.divider()
st.caption(
    "**Notes.** AgilePredict snapshots do not carry historical predictions back "
    "in time — to build a full accuracy record you need to poll the feed "
    "regularly (AgilePredict itself updates four times daily). A cron or GH "
    "Action that runs the refresh at, say, 06:20 / 10:20 / 16:20 / 22:20 UK "
    "will populate the forecast history; Octopus Agile prices appear at "
    "~16:00 UK for the next day and are then immutable. ENTSO-E is the "
    "intended long-term wholesale cross-check — the current Elexon APX row "
    "is a drop-in placeholder and uses the same underlying N2EX/EPEX market."
)
