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

from forecast_accuracy import analysis, costs, storage
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

# -- Half-hourly prices chart (sliding window over last 30 days → tomorrow) --
RECENT_DAYS_BACK = 30
RECENT_DAYS_FORWARD = 2  # today + tomorrow (exclusive end at end of tomorrow)

st.subheader("Half-hourly prices")
st.caption(
    "All series normalised to **£/MWh**. Retail series (Octopus Agile, "
    "AgilePredict) are p/kWh × 10 with VAT and retail margin left in — they "
    "will sit systematically above wholesale (Elexon APX day-ahead, Elexon "
    "system price within-day). AgilePredict line uses the freshest snapshot "
    "we hold per target half-hour. X-axis is UK local time; background "
    "shading goes grey overnight → yellowest at local noon. Dashed vertical "
    "line = now."
)

with storage.connect() as conn:
    recent_all = analysis.recent_prices(
        conn, region=region,
        days_back=RECENT_DAYS_BACK,
        days_forward=RECENT_DAYS_FORWARD,
    )

# Work out UK-local day bounds for the slider (covers the whole pre-loaded
# window end-to-end; today = UK midnight of today).
from datetime import date as _date, datetime as _dt, time as _time, timedelta as _td, timezone as _tz
from zoneinfo import ZoneInfo as _ZI
_UK = _ZI("Europe/London")
_today_uk_date: _date = _dt.now(_tz.utc).astimezone(_UK).date()
_min_date = _today_uk_date - _td(days=RECENT_DAYS_BACK)
_max_date = _today_uk_date + _td(days=RECENT_DAYS_FORWARD - 1)   # tomorrow
_default_start = _today_uk_date - _td(days=1)                    # yesterday
_default_end = _max_date                                         # tomorrow

slider_start, slider_end = st.slider(
    "Date range (UK local, inclusive)",
    min_value=_min_date,
    max_value=_max_date,
    value=(_default_start, _default_end),
    format="YYYY-MM-DD",
    help=(
        f"Drag to pan or resize the window. Data is pre-loaded for the "
        f"last {RECENT_DAYS_BACK} days plus tomorrow; filtering is instant."
    ),
)

# -- Per-series toggles --------------------------------------------------
# Short checkbox labels; full labels stay in the chart/legend/tooltip.
SERIES_SHORT = {
    "Day-ahead (Elexon APX)":                       "Day-ahead",
    "Within-day (Elexon system price)":             "Within-day",
    "Confirmed (Octopus Agile)":                    "Octopus",
    "Predicted (AgilePredict, freshest snapshot)":  "AgilePredict",
}
# Keep a single source of truth for per-series colour — the Altair scale
# is then built from whichever subset is visible, so toggled-off series
# disappear from the legend *and* colours stay stable across toggles.
SERIES_COLOR = {
    "Day-ahead (Elexon APX)":                       "#E1A800",
    "Within-day (Elexon system price)":             "#6A3D9A",
    "Confirmed (Octopus Agile)":                    "#1F77B4",
    "Predicted (AgilePredict, freshest snapshot)":  "#D62728",
}
_toggle_cols = st.columns(len(analysis.RECENT_SERIES_LABELS))
_visible_series = [
    label for col, label in zip(_toggle_cols, analysis.RECENT_SERIES_LABELS)
    if col.checkbox(SERIES_SHORT.get(label, label), value=True,
                    key=f"series_toggle_{label}")
]


def _uk_date_to_utc_naive(d: _date, end_of_day: bool = False) -> pd.Timestamp:
    """UK-local midnight (or next-day midnight) → naive UTC Timestamp to
    match the DataFrame's `target_start` dtype (which read_sql_query parses
    from 'Z'-suffixed ISO strings as naive UTC)."""
    if end_of_day:
        d = d + _td(days=1)
    dt_uk = _dt.combine(d, _time(0, 0, tzinfo=_UK))
    return pd.Timestamp(dt_uk.astimezone(_tz.utc).replace(tzinfo=None))


_win_start_utc = _uk_date_to_utc_naive(slider_start, end_of_day=False)
_win_end_utc = _uk_date_to_utc_naive(slider_end, end_of_day=True)

if recent_all.empty:
    recent = recent_all
else:
    # Normalise target_start to naive UTC so the slider comparison is clean.
    # read_sql_query + parse_dates usually gives us a tz-naive UTC datetime64
    # column, but if pandas returned object dtype (e.g. from a mixed-empty
    # concat) or tz-aware, coerce it here.
    ts = recent_all["target_start"]
    if not pd.api.types.is_datetime64_any_dtype(ts):
        ts = pd.to_datetime(ts, utc=True, errors="coerce")
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert("UTC").dt.tz_localize(None)
    recent_all = recent_all.assign(target_start=ts)
    recent = recent_all[
        (recent_all["target_start"] >= _win_start_utc) &
        (recent_all["target_start"] <  _win_end_utc) &
        (recent_all["series"].isin(_visible_series))
    ].copy()

if not _visible_series:
    st.info("All series are toggled off — tick one of the boxes above to draw a chart.")
elif recent.empty:
    st.info(
        "No half-hourly prices for the selected window and series. Widen the "
        "slider, tick more series, or hit **Refresh data** with AgilePredict, "
        "Octopus Agile, Elexon APX and Elexon within-day all ticked."
    )
else:
    import math as _math
    now_utc_ts = pd.Timestamp.utcnow().tz_localize(None)
    window_span_days = (slider_end - slider_start).days + 1
    # Dots only read as dots when there's room for them — above ~5 days the
    # line-only rendering is much easier on the eye.
    show_points = window_span_days <= 5

    # Convert target_start (naive UTC) → naive UK-local for display. Vega
    # treats all timestamps as UTC on the wire, so the trick is to plot the
    # *UK local* wall-clock time and call it UTC — day boundaries then land
    # on UK midnight, matching the slider and the user's mental model.
    recent = recent.assign(
        target_start_uk=(recent["target_start"]
                         .dt.tz_localize("UTC")
                         .dt.tz_convert(_UK)
                         .dt.tz_localize(None))
    )
    # pandas < 2.2 returns naive UTC; ≥ 2.2 returns tz-aware. Normalise.
    _now = pd.Timestamp.utcnow()
    if _now.tzinfo is None:
        _now = _now.tz_localize("UTC")
    now_uk_ts = _now.tz_convert(_UK).tz_localize(None)
    _win_start_uk = pd.Timestamp(_dt.combine(slider_start, _time(0, 0)))
    _win_end_uk = pd.Timestamp(_dt.combine(slider_end + _td(days=1), _time(0, 0)))

    # Daylight background: half-hourly buckets, "warmth" cosines from 0 at
    # 06:00/18:00 up to 1 at local noon; clamped to 0 overnight. Colour
    # interpolates grey→pale yellow→saturated yellow; kept desaturated so
    # the actual price lines still dominate.
    _bg_grid = pd.date_range(_win_start_uk, _win_end_uk, freq="30min",
                             inclusive="left")
    if len(_bg_grid):
        _h = _bg_grid.hour + _bg_grid.minute / 60.0
        _warmth = [max(0.0, _math.cos((x - 12.0) * _math.pi / 12.0)) for x in _h]
        bg_df = pd.DataFrame({
            "start": _bg_grid,
            "end": _bg_grid + pd.Timedelta("30min"),
            "warmth": _warmth,
        })
        background = (
            alt.Chart(bg_df)
            .mark_rect(opacity=0.55)
            .encode(
                x=alt.X("start:T"),
                x2="end:T",
                color=alt.Color(
                    "warmth:Q",
                    scale=alt.Scale(
                        domain=[0.0, 0.15, 1.0],
                        range=["#E8E8E8", "#FFF4CC", "#FFD24D"],
                    ),
                    legend=None,
                ),
            )
        )
    else:
        background = None

    # Colour scale is restricted to visible series so the legend only shows
    # what is actually drawn — but each colour is still taken from the
    # master map, so toggling a series off and on doesn't shuffle colours.
    color_scale = alt.Scale(
        domain=_visible_series,
        range=[SERIES_COLOR[l] for l in _visible_series],
    )
    _mark_kwargs = {"strokeWidth": 1.8}
    if show_points:
        _mark_kwargs["point"] = alt.OverlayMarkDef(size=18, filled=True)

    # Tiered x-axis: HH:MM on the top row, and the date on a second line,
    # rendered only at the 12:00 tick so the day label sits centred under
    # the day's data (rather than on the midnight boundary). We supply the
    # tick positions explicitly — every 6 hours across the visible window
    # — because Streamlit's bundled Vega doesn't accept the TickCountObject
    # {interval, step} form cleanly, and explicit `values` also guarantees
    # that 12:00 is always a tick (needed for the date label to render).
    _tick_positions = pd.date_range(
        _win_start_uk, _win_end_uk, freq="6h", inclusive="left"
    )
    _tiered_label_expr = (
        "[timeFormat(datum.value, '%H:%M'), "
        "timeFormat(datum.value, '%H:%M') == '12:00' "
        "? timeFormat(datum.value, '%a %d %b') : '']"
    )
    x_axis = alt.Axis(
        title=None,
        labelExpr=_tiered_label_expr,
        values=list(_tick_positions),
        labelFontSize=11,
        labelPadding=2,
    )
    base = (
        alt.Chart(recent)
        .mark_line(**_mark_kwargs)
        .encode(
            x=alt.X("target_start_uk:T", axis=x_axis),
            y=alt.Y("value_gbp_per_mwh:Q", title="£/MWh"),
            color=alt.Color("series:N", title="Series",
                            scale=color_scale,
                            sort=_visible_series),
            tooltip=[
                alt.Tooltip("target_start_uk:T", title="HH (UK)",
                            format="%a %d %b %H:%M"),
                alt.Tooltip("series:N", title="Series"),
                alt.Tooltip("value_gbp_per_mwh:Q", title="£/MWh", format=".2f"),
            ],
        )
    )
    # Only draw the "now" rule when now is actually inside the selected
    # window — otherwise Altair would stretch the X axis to include it and
    # compress the data into a corner.
    layers = []
    if background is not None:
        layers.append(background)
    layers.append(base)
    if _win_start_uk <= now_uk_ts < _win_end_uk:
        now_df = pd.DataFrame({"now": [now_uk_ts]})
        layers.append(
            alt.Chart(now_df).mark_rule(strokeDash=[4, 4], color="#444").encode(x="now:T")
        )
    chart = layers[0] if len(layers) == 1 else alt.layer(*layers)
    st.altair_chart(chart.interactive(bind_y=False), width="stretch")
    # Only call out series the user *wanted* to see but we have no rows for
    # — toggled-off series are deliberately hidden, not missing.
    present = set(recent["series"].unique())
    missing = [s for s in _visible_series if s not in present]
    if missing:
        st.caption("Missing series (no rows in DB for this window): "
                   + ", ".join(missing))

st.divider()

# -- Agile profitability for Octopus -----------------------------------------
#
# For each HH in the visible window we compare what Octopus *receives* from
# the customer on Agile (retail rate ex-VAT) against a modelled cost-to-supply
# stack. The wholesale leg is the Elexon APX day-ahead market price, uplifted
# for transmission+distribution losses, plus flat network / policy / operating
# allowances anchored to Ofgem's Q2 2026 price cap decision. Everything
# expressed in p/kWh ex-VAT unless the chart says otherwise.
st.subheader("Agile profitability for Octopus (estimated)")
st.caption(
    "Cost stack compared to what Octopus actually receives from an Agile "
    "customer per half-hour. Wholesale varies with Elexon APX day-ahead; "
    "everything else is modelled as flat p/kWh. All values are **ex-VAT** "
    "(the 5% VAT on the retail price goes to HMRC, not the supplier). "
    "Positive gap above the stack = Octopus margin for that HH; negative = "
    "loss. This is a **model**, not Octopus's real P&L — sliders below let "
    "you stress-test the assumptions."
)

with st.expander("Cost-stack assumptions (drag to edit)", expanded=False):
    _asm_cols = st.columns(5)
    _losses_pct = _asm_cols[0].slider(
        "Losses uplift (%)", min_value=0.0, max_value=15.0,
        value=costs.DEFAULT_LOSSES_PCT, step=0.5,
        help="Multiplicative uplift on wholesale for T+D losses. "
             "GB LV domestic blended ~7-8%.",
    )
    _network_p = _asm_cols[1].slider(
        "Network (p/kWh)", min_value=0.0, max_value=8.0,
        value=costs.DEFAULT_NETWORK_P_PER_KWH, step=0.1,
        help="TNUoS + DUoS residual carried in the unit rate. Most of "
             "Ofgem's network allowance sits in the standing charge, so "
             "the unit-rate component is small.",
    )
    _policy_p = _asm_cols[2].slider(
        "Policy (p/kWh)", min_value=0.0, max_value=10.0,
        value=costs.DEFAULT_POLICY_P_PER_KWH, step=0.1,
        help="CfD + Capacity Market + FiT + AAHEDC + Green Gas Levy + WHD. "
             "Defaults to post-April-2026 levels (RO + ECO moved off "
             "electricity bills — hence the automatic ~3.5 p/kWh Agile cut).",
    )
    _operating_p = _asm_cols[3].slider(
        "Operating (p/kWh)", min_value=0.0, max_value=6.0,
        value=costs.DEFAULT_OPERATING_P_PER_KWH, step=0.1,
        help="Billing, metering, debt, customer service unit-rate share.",
    )
    _target_margin_p = _asm_cols[4].slider(
        "Target margin (p/kWh)", min_value=0.0, max_value=4.0,
        value=costs.DEFAULT_TARGET_MARGIN_P_PER_KWH, step=0.1,
        help="Ofgem EBIT + headroom allowance — drawn as a reference band "
             "above the cost stack, not subtracted from margin.",
    )
_stack_cfg = costs.CostStack(
    losses_pct=_losses_pct,
    network_p_per_kwh=_network_p,
    policy_p_per_kwh=_policy_p,
    operating_p_per_kwh=_operating_p,
    target_margin_p_per_kwh=_target_margin_p,
)

# Pull the two series we need from the pre-loaded window (not the user's
# chart toggles — the profitability view needs both regardless). Convert to
# UK-local axis to match the top chart and the slider.
if recent_all.empty:
    st.info(
        "No wholesale or Agile data loaded yet — hit **Refresh data** with "
        "Elexon APX and Octopus Agile both ticked."
    )
else:
    _win_mask = (
        (recent_all["target_start"] >= _win_start_utc) &
        (recent_all["target_start"] <  _win_end_utc)
    )
    _win_df = recent_all[_win_mask].copy()

    _wholesale = _win_df[
        _win_df["series"] == "Day-ahead (Elexon APX)"
    ][["target_start", "value_gbp_per_mwh"]].copy()
    _agile = _win_df[
        _win_df["series"] == "Confirmed (Octopus Agile)"
    ][["target_start", "value_gbp_per_mwh"]].copy()

    if _wholesale.empty or _agile.empty:
        _missing = []
        if _wholesale.empty:
            _missing.append("Elexon APX day-ahead")
        if _agile.empty:
            _missing.append("Octopus Agile")
        st.info(
            "Profitability view needs both wholesale and Agile data. Missing: "
            + ", ".join(_missing)
            + ". Widen the slider or hit **Refresh data**."
        )
    else:
        # Build cost stack from wholesale — long format, one row per (HH,
        # component). Also keep a separate running-total series for the
        # Agile line-vs-stack comparison and margin chart.
        stack_long = costs.build_stack_long(_wholesale, _stack_cfg)
        totals = costs.total_cost_ex_vat(stack_long)

        # Agile retail in the DB is p/kWh **inc VAT** (per octopus.py). For
        # the chart we want the ex-VAT revenue line (what Octopus keeps) and
        # keep inc-VAT around for tooltips so the user sees both.
        _agile = _agile.assign(
            agile_inc_vat_p=_agile["value_gbp_per_mwh"] / 10.0,
        )
        _agile["agile_ex_vat_p"] = (
            _agile["agile_inc_vat_p"] / (1.0 + costs.VAT_RATE)
        )

        # Margin = Agile ex-VAT revenue − cost-to-supply ex-VAT. Merge on
        # target_start so both series align HH-by-HH; drop any HHs missing
        # from either side so the margin chart never lies about coverage.
        margin_df = _agile[["target_start", "agile_ex_vat_p", "agile_inc_vat_p"]].merge(
            totals, on="target_start", how="inner"
        )
        margin_df["margin_p_per_kwh"] = (
            margin_df["agile_ex_vat_p"] - margin_df["total_cost_p_per_kwh"]
        )

        # Convert all timestamp columns to UK-local naive so Vega draws them
        # aligned with the top chart's X axis.
        def _to_uk(ts: pd.Series) -> pd.Series:
            return (ts.dt.tz_localize("UTC")
                      .dt.tz_convert(_UK)
                      .dt.tz_localize(None))

        stack_long = stack_long.assign(target_start_uk=_to_uk(stack_long["target_start"]))
        _agile = _agile.assign(target_start_uk=_to_uk(_agile["target_start"]))
        margin_df = margin_df.assign(target_start_uk=_to_uk(margin_df["target_start"]))

        # Re-use the top-chart's tiered date+time x-axis so the two charts
        # read as a single vertical stack of the same window.
        _stack_tick_positions = pd.date_range(
            _win_start_uk, _win_end_uk, freq="6h", inclusive="left"
        )
        _stack_label_expr = (
            "[timeFormat(datum.value, '%H:%M'), "
            "timeFormat(datum.value, '%H:%M') == '12:00' "
            "? timeFormat(datum.value, '%a %d %b') : '']"
        )
        _stack_x_axis = alt.Axis(
            title=None,
            labelExpr=_stack_label_expr,
            values=list(_stack_tick_positions),
            labelFontSize=11,
            labelPadding=2,
        )

        _component_colour_scale = alt.Scale(
            domain=list(costs.STACK_COMPONENTS),
            range=[costs.STACK_COLORS[c] for c in costs.STACK_COMPONENTS],
        )

        stack_area = (
            alt.Chart(stack_long)
            .mark_area(opacity=0.85)
            .encode(
                x=alt.X("target_start_uk:T", axis=_stack_x_axis),
                y=alt.Y(
                    "value_p_per_kwh:Q",
                    stack="zero",
                    title="p/kWh (ex-VAT)",
                ),
                color=alt.Color(
                    "component:N",
                    title="Cost component",
                    scale=_component_colour_scale,
                    sort=list(costs.STACK_COMPONENTS),
                ),
                order=alt.Order("component:N", sort="ascending"),
                tooltip=[
                    alt.Tooltip("target_start_uk:T", title="HH (UK)",
                                format="%a %d %b %H:%M"),
                    alt.Tooltip("component:N", title="Component"),
                    alt.Tooltip("value_p_per_kwh:Q", title="p/kWh",
                                format=".2f"),
                ],
            )
        )

        agile_line = (
            alt.Chart(_agile)
            .mark_line(color="#1F77B4", strokeWidth=2.2,
                       point=alt.OverlayMarkDef(size=18, filled=True,
                                                color="#1F77B4"))
            .encode(
                x=alt.X("target_start_uk:T", axis=_stack_x_axis),
                y=alt.Y("agile_ex_vat_p:Q"),
                tooltip=[
                    alt.Tooltip("target_start_uk:T", title="HH (UK)",
                                format="%a %d %b %H:%M"),
                    alt.Tooltip("agile_ex_vat_p:Q",
                                title="Agile (ex-VAT, p/kWh)", format=".2f"),
                    alt.Tooltip("agile_inc_vat_p:Q",
                                title="Agile (inc-VAT, p/kWh)", format=".2f"),
                ],
            )
        )

        # Optional "now" rule — only within window.
        _stack_layers = [stack_area, agile_line]
        if _win_start_uk <= now_uk_ts < _win_end_uk:
            _now_df = pd.DataFrame({"now": [now_uk_ts]})
            _stack_layers.append(
                alt.Chart(_now_df).mark_rule(strokeDash=[4, 4], color="#444")
                .encode(x="now:T")
            )

        stack_chart = (
            alt.layer(*_stack_layers)
            .resolve_scale(y="shared")
            .properties(height=330)
        )
        st.altair_chart(stack_chart.interactive(bind_y=False),
                        width="stretch")
        st.caption(
            "Solid line = Agile retail **ex-VAT** (what Octopus actually "
            "receives). Stacked area = modelled cost-to-supply. Wholesale is "
            "the Elexon APX day-ahead market price per HH; the other layers "
            "are flat allowances from the sliders above."
        )

        # ---- Margin chart (profit vs loss per HH) -----------------------
        # Signed area below the Agile line is Octopus's margin. A dedicated
        # chart makes the sign (and magnitude) much clearer than eyeballing
        # the gap between two series on different scales.
        if margin_df.empty:
            st.caption(
                "(Margin series unavailable — no HHs with both wholesale "
                "and Agile data in this window.)"
            )
        else:
            # Split into positive and negative segments so we can shade
            # profit green and loss red, each against a zero baseline.
            pos_df = margin_df.assign(
                margin_pos=margin_df["margin_p_per_kwh"].clip(lower=0.0),
            )
            neg_df = margin_df.assign(
                margin_neg=margin_df["margin_p_per_kwh"].clip(upper=0.0),
            )
            margin_pos_area = (
                alt.Chart(pos_df)
                .mark_area(opacity=0.45, color="#2CA02C")
                .encode(
                    x=alt.X("target_start_uk:T", axis=_stack_x_axis),
                    y=alt.Y("margin_pos:Q", title="Margin (p/kWh, ex-VAT)"),
                )
            )
            margin_neg_area = (
                alt.Chart(neg_df)
                .mark_area(opacity=0.45, color="#D62728")
                .encode(
                    x=alt.X("target_start_uk:T", axis=_stack_x_axis),
                    y="margin_neg:Q",
                )
            )
            margin_line = (
                alt.Chart(margin_df)
                .mark_line(color="#333", strokeWidth=1.4,
                           point=alt.OverlayMarkDef(size=14, filled=True,
                                                    color="#333"))
                .encode(
                    x="target_start_uk:T",
                    y="margin_p_per_kwh:Q",
                    tooltip=[
                        alt.Tooltip("target_start_uk:T", title="HH (UK)",
                                    format="%a %d %b %H:%M"),
                        alt.Tooltip("margin_p_per_kwh:Q",
                                    title="Margin (p/kWh)", format=".2f"),
                        alt.Tooltip("agile_ex_vat_p:Q",
                                    title="Agile ex-VAT (p/kWh)",
                                    format=".2f"),
                        alt.Tooltip("total_cost_p_per_kwh:Q",
                                    title="Cost stack (p/kWh)", format=".2f"),
                    ],
                )
            )
            zero_rule = (
                alt.Chart(pd.DataFrame({"y": [0.0]}))
                .mark_rule(color="#888", strokeDash=[3, 3])
                .encode(y="y:Q")
            )
            target_rule = (
                alt.Chart(pd.DataFrame({"y": [_stack_cfg.target_margin_p_per_kwh]}))
                .mark_rule(color="#8C564B", strokeDash=[2, 4])
                .encode(y="y:Q")
            )
            margin_layers = [margin_pos_area, margin_neg_area,
                             zero_rule, target_rule, margin_line]
            if _win_start_uk <= now_uk_ts < _win_end_uk:
                _now_df2 = pd.DataFrame({"now": [now_uk_ts]})
                margin_layers.append(
                    alt.Chart(_now_df2)
                    .mark_rule(strokeDash=[4, 4], color="#444")
                    .encode(x="now:T")
                )
            margin_chart = (
                alt.layer(*margin_layers)
                .properties(height=200)
            )
            st.altair_chart(margin_chart.interactive(bind_y=False),
                            width="stretch")

            # Headline metrics for the visible window.
            n_hh = len(margin_df)
            mean_margin = margin_df["margin_p_per_kwh"].mean()
            loss_frac = (margin_df["margin_p_per_kwh"] < 0).mean() * 100.0
            worst = margin_df["margin_p_per_kwh"].min()
            best = margin_df["margin_p_per_kwh"].max()
            mcol1, mcol2, mcol3, mcol4 = st.columns(4)
            mcol1.metric("Mean margin", f"{mean_margin:+.2f} p/kWh",
                         help="Simple mean of per-HH margin across the "
                              "visible window (not consumption-weighted).")
            mcol2.metric("Loss-making HHs", f"{loss_frac:.0f}%",
                         help="Share of half-hours where the Agile retail "
                              "rate ex-VAT is below the modelled cost stack.")
            mcol3.metric("Worst HH", f"{worst:+.2f} p/kWh",
                         help="Largest per-HH loss in the window.")
            mcol4.metric("Best HH", f"{best:+.2f} p/kWh",
                         help="Largest per-HH margin in the window.")
            st.caption(
                f"Green band = HHs where Agile > cost stack (Octopus wins). "
                f"Red band = HHs where Agile < cost stack (Octopus loses). "
                f"Brown dashed = target margin ({_stack_cfg.target_margin_p_per_kwh:.1f} "
                f"p/kWh, Ofgem EBIT + headroom). Window covers {n_hh} HHs."
            )

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
