"""Estimated GB supplier cost stack for analysing Octopus Agile profitability per HH.

This is a *model*. The true supplier cost stack is proprietary. We build a
public proxy from Ofgem's default-tariff cap breakdown plus a couple of
industry-standard conventions.

Conventions
-----------
* All stack values are p/kWh **ex-VAT** unless otherwise noted.
* The domestic VAT rate is 5%. The customer pays the Agile inc-VAT rate;
  the supplier keeps the ex-VAT portion (VAT goes to HMRC).
* Wholesale is taken from the Elexon APX day-ahead market (£/MWh → p/kWh).
  For Agile specifically, day-ahead is the closest cheap public proxy for
  the supplier's marginal cost — Octopus bids volume into that auction,
  then trues up any imbalance against the within-day / system price.
* Losses are modelled as a multiplicative uplift on wholesale (default 8%,
  which is roughly the UK blended transmission+distribution loss rate for
  an LV domestic customer). This captures the extra kWh that has to be
  bought upstream to deliver one kWh to the meter.
* Network, policy and operating are modelled as **flat** p/kWh figures
  rather than time-varying. Real DUoS has red/amber/green time bands, but
  for domestic customers most of the network cost sits in the standing
  charge (not the unit rate), so a flat unit adder is a fair first cut.
* EBIT + headroom is treated as a **target** margin rather than a cost —
  i.e. it is what Ofgem's price cap assumes suppliers will earn, so we
  draw it as a reference band, not a deduction.

Default figures are anchored to Ofgem's Q2 2026 (April-June 2026) default
tariff cap decision, after the April 2026 levy removal (~£150/yr off the
typical dual-fuel bill, mostly from scrapping the RO and ECO charges on
electricity). They are *rough* apportionments of dual-fuel cap figures
onto the electricity unit rate — sliders in the UI let you push them
around to sanity-check sensitivity.

Sources
-------
* Ofgem, "Energy price cap (default tariff) update from 1 January 2026"
  (Annex 1, Direct Debit breakdown, £/year for typical dual-fuel customer)
* Ofgem price cap pages for Q2 2026 (1 April - 30 June 2026) headlines
* HMRC, reduced rate of VAT on domestic fuel and power: 5%
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd


# ---------------------------------------------------------------------------
# Default stack assumptions (Q2 2026, post April-2026 levy cut).
# ---------------------------------------------------------------------------

#: Transmission + distribution losses. GB LV domestic blended ~7-8%.
DEFAULT_LOSSES_PCT = 8.0

#: Network (TNUoS + DUoS) portion carried in the **unit rate** rather than
#: the standing charge. Most of the £~463/yr 2026 network cap allowance
#: (dual fuel) goes to the standing charge; the unit-rate residual is small.
DEFAULT_NETWORK_P_PER_KWH = 2.5

#: Policy costs loaded onto the unit rate post-April 2026. The automatic
#: ~3.5 p/kWh Agile cut on 1 Apr 2026 reflects RO + ECO being moved off
#: electricity bills; what's left (CfD, Capacity Market, FiT, AAHEDC,
#: Green Gas Levy, WHD share) is roughly this.
DEFAULT_POLICY_P_PER_KWH = 2.5

#: Operating, debt and industry costs allocated to the unit rate. Most of
#: Ofgem's £279/yr (Q1 2026 dual-fuel DD) operating allowance goes to the
#: standing charge; this is the small per-kWh residual.
DEFAULT_OPERATING_P_PER_KWH = 1.5

#: Target supplier margin (EBIT) + headroom per Ofgem's cap. Drawn as a
#: reference band above the stack, not deducted from margin.
DEFAULT_TARGET_MARGIN_P_PER_KWH = 1.5

#: Domestic VAT on fuel and power.
VAT_RATE = 0.05


@dataclass(frozen=True)
class CostStack:
    """Adjustable cost-stack assumptions. All additions are p/kWh ex-VAT."""

    losses_pct: float = DEFAULT_LOSSES_PCT
    network_p_per_kwh: float = DEFAULT_NETWORK_P_PER_KWH
    policy_p_per_kwh: float = DEFAULT_POLICY_P_PER_KWH
    operating_p_per_kwh: float = DEFAULT_OPERATING_P_PER_KWH
    target_margin_p_per_kwh: float = DEFAULT_TARGET_MARGIN_P_PER_KWH


# Order matters — this is bottom-to-top on the stacked area chart, and the
# colour scale below is aligned to it.
STACK_COMPONENTS: tuple[str, ...] = (
    "Wholesale",
    "Losses",
    "Network",
    "Policy",
    "Operating",
)

#: Stable colours for the stack (muted palette so the Agile retail line
#: draws the eye). Matches the order of STACK_COMPONENTS.
STACK_COLORS = {
    "Wholesale":  "#4C78A8",  # blue — the big variable one
    "Losses":     "#9ECAE1",  # pale blue — a follower of wholesale
    "Network":    "#8C6D31",  # brown — fixed wires
    "Policy":     "#B279A2",  # muted purple — fixed policy
    "Operating":  "#9C9C9C",  # grey — fixed ops
}


def build_stack_long(
    wholesale: pd.DataFrame,
    stack: CostStack,
    *,
    wholesale_col: str = "value_gbp_per_mwh",
    ts_col: str = "target_start",
) -> pd.DataFrame:
    """Return a long-format cost-stack DataFrame, one row per (HH, component).

    Parameters
    ----------
    wholesale
        Wide DataFrame with at least one timestamp column and one wholesale
        £/MWh column (default column names match ``analysis.recent_prices``).
    stack
        Cost-stack assumptions.
    wholesale_col
        Name of the wholesale £/MWh column in ``wholesale``.
    ts_col
        Name of the timestamp column in ``wholesale``.

    Returns
    -------
    pd.DataFrame
        Columns: ``[ts_col]``, ``component`` (str, one of STACK_COMPONENTS),
        ``value_p_per_kwh`` (float, ex-VAT). Rows are unioned across
        components so an Altair stacked-area chart can consume directly.
    """
    if wholesale.empty:
        return pd.DataFrame(columns=[ts_col, "component", "value_p_per_kwh"])

    # £/MWh → p/kWh (÷10). The ex-VAT convention holds because Elexon APX is
    # a wholesale index quoted ex-VAT.
    w_p = wholesale[wholesale_col].astype(float) / 10.0
    losses = w_p * (stack.losses_pct / 100.0)

    frames: list[pd.DataFrame] = []
    # Dynamic components first — each row varies with wholesale.
    frames.append(pd.DataFrame({
        ts_col: wholesale[ts_col].values,
        "component": "Wholesale",
        "value_p_per_kwh": w_p.values,
    }))
    frames.append(pd.DataFrame({
        ts_col: wholesale[ts_col].values,
        "component": "Losses",
        "value_p_per_kwh": losses.values,
    }))
    # Flat components — one value broadcast across every HH in the window.
    for name, v in (
        ("Network", stack.network_p_per_kwh),
        ("Policy", stack.policy_p_per_kwh),
        ("Operating", stack.operating_p_per_kwh),
    ):
        frames.append(pd.DataFrame({
            ts_col: wholesale[ts_col].values,
            "component": name,
            "value_p_per_kwh": float(v),
        }))
    out = pd.concat(frames, ignore_index=True)
    # Stable ordering — handy for Altair's sort and for downstream slicing.
    out["component"] = pd.Categorical(out["component"],
                                      categories=list(STACK_COMPONENTS),
                                      ordered=True)
    return out.sort_values([ts_col, "component"]).reset_index(drop=True)


def total_cost_ex_vat(stack_long: pd.DataFrame, *,
                      ts_col: str = "target_start") -> pd.DataFrame:
    """Sum a long-format stack back down to a single total-cost-per-HH series."""
    if stack_long.empty:
        return pd.DataFrame(columns=[ts_col, "total_cost_p_per_kwh"])
    totals = (stack_long
              .groupby(ts_col, as_index=False)["value_p_per_kwh"]
              .sum()
              .rename(columns={"value_p_per_kwh": "total_cost_p_per_kwh"}))
    return totals


def margin_series(
    agile_inc_vat_p_per_kwh: pd.Series,
    total_cost_ex_vat_p_per_kwh: pd.Series,
) -> pd.Series:
    """Compute Octopus's estimated margin per HH (p/kWh).

    Revenue is the Agile rate **ex-VAT** (customer pays inc-VAT; the 5%
    goes to HMRC, so Octopus keeps inc-VAT / 1.05). Cost is the
    total cost-to-supply (also ex-VAT). Positive = Octopus wins on that HH.
    """
    revenue_ex_vat = agile_inc_vat_p_per_kwh.astype(float) / (1.0 + VAT_RATE)
    return revenue_ex_vat - total_cost_ex_vat_p_per_kwh.astype(float)


def agile_ex_vat(agile_inc_vat_p_per_kwh: Iterable[float]) -> list[float]:
    """Small helper for when a list is more convenient than a Series."""
    return [float(x) / (1.0 + VAT_RATE) for x in agile_inc_vat_p_per_kwh]
