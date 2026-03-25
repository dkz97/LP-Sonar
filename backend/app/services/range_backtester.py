"""
Range Backtester: replay each candidate range against a historical price path.

For each candidate, simulates forward over the last `horizon_bars` bars and
estimates realised performance metrics.

Metrics computed:
  in_range_time_ratio      fraction of bars where price is inside [lower, upper]
  cumulative_fee_proxy     Σ(vol[t] × fee_rate × in_range[t] × capital_efficiency) / capital
  il_cost_proxy            CLMM IL formula at final price vs entry price (fraction)
  first_breach_bar         index of first bar where price exits range (None if never)
  breach_count             number of distinct range exits (price leaves then re-enters)
  rebalance_count          estimated number of rebalances (each sustained breach = 1)
  realized_net_pnl_proxy   cumulative_fee_proxy − il_cost_proxy

Capital efficiency for concentrated liquidity:
  A full-range position has efficiency = 1. A range of width w centred at p has
  higher efficiency. Approximated here as:
    eff = 1 / width_pct   (capped at 20× to avoid infinity for sub-tick ranges)

IL formula for concentrated liquidity (simplified):
  When price P is inside [P_lo, P_hi], the LP holds both tokens.
  At expiry:
    if P is still in range: standard CLMM IL ≈ V2 IL (P_t / P_0 ratio)
    if P breached and never came back: position is single-sided; IL = full single leg

We use the V2 IL formula as an approximation for in-range IL at final price,
and an amplified penalty for positions that breached and stayed out.
"""
from __future__ import annotations
import logging
import math
from typing import Optional

from app.models.schemas import BacktestResult, CandidateRange

logger = logging.getLogger(__name__)

# Capital efficiency cap (concentrated range vs full range)
_MAX_CAPITAL_EFFICIENCY = 20.0

# When price is out of range for a sustained period (> this fraction of horizon),
# apply an additional single-leg IL amplification
_SUSTAINED_OOR_THRESHOLD = 0.30  # > 30% of bars out-of-range is "sustained"

# Number of consecutive OOR bars that trigger a rebalance event
_REBALANCE_OOR_STREAK = 3


def _clmm_il_fraction(price_ratio: float) -> float:
    """
    Concentrated liquidity IL approximation.

    Uses Uniswap V2 formula (2√k/(1+k) - 1) as base IL for the in-range portion.
    price_ratio = P_final / P_entry

    Returns IL as a negative fraction (e.g. -0.057 for 50% move).
    Clipped to [-1, 0].
    """
    if price_ratio <= 0:
        return -1.0
    k = price_ratio
    il = (2.0 * math.sqrt(k) / (1.0 + k)) - 1.0
    return max(il, -1.0)


def _capital_efficiency(width_pct: float) -> float:
    """
    Approximate capital efficiency multiplier for a concentrated range.
    width_pct is the fractional width: (upper - lower) / center.
    """
    if width_pct <= 0:
        return _MAX_CAPITAL_EFFICIENCY
    eff = 1.0 / width_pct
    return min(eff, _MAX_CAPITAL_EFFICIENCY)


def backtest_candidate(
    ohlcv_bars: list[dict],
    candidate: CandidateRange,
    horizon_bars: int,
    fee_rate: float,
    tvl_usd: float = 1.0,
    volume_scale: float = 1.0,
) -> BacktestResult:
    """
    Replay a candidate range against the most recent `horizon_bars` of price history.

    Parameters
    ----------
    ohlcv_bars     Full OHLCV bar list (oldest → newest). We use the last horizon_bars.
    candidate      CandidateRange to evaluate.
    horizon_bars   Number of bars to replay (e.g. 48 for 48h at 1h resolution).
    fee_rate       Pool fee rate as decimal (e.g. 0.003 for 0.3%).
    tvl_usd        Pool TVL in USD (used only for rebalance cost normalisation).
    volume_scale   Multiply each bar's volume by this factor before computing fee proxy.
                   Use to correct token-level OKX volumes to pool-specific fractions.
                   Default 1.0 (no scaling).

    Returns
    -------
    BacktestResult with all replay metrics.
    """
    if not ohlcv_bars:
        return BacktestResult(
            in_range_time_ratio=0.0,
            cumulative_fee_proxy=0.0,
            il_cost_proxy=0.0,
            first_breach_bar=None,
            breach_count=0,
            rebalance_count=0,
            realized_net_pnl_proxy=0.0,
        )

    # Use the last horizon_bars bars (most recent history = most relevant)
    replay_bars = ohlcv_bars[-horizon_bars:] if len(ohlcv_bars) > horizon_bars else ohlcv_bars
    n = len(replay_bars)
    if n == 0:
        return BacktestResult(
            in_range_time_ratio=0.0,
            cumulative_fee_proxy=0.0,
            il_cost_proxy=0.0,
            first_breach_bar=None,
            breach_count=0,
            rebalance_count=0,
            realized_net_pnl_proxy=0.0,
        )

    lo = candidate.lower_price
    hi = candidate.upper_price
    entry_price = float(replay_bars[0].get("close", replay_bars[0].get("open", 0)))
    if entry_price <= 0:
        entry_price = (lo + hi) / 2.0

    cap_eff = _capital_efficiency(candidate.width_pct)

    in_range_count = 0
    cumulative_fee = 0.0
    first_breach_bar: Optional[int] = None
    breach_count = 0
    rebalance_count = 0

    was_in_range = (lo <= entry_price <= hi)
    oor_streak = 0  # consecutive OOR bars

    for i, bar in enumerate(replay_bars):
        close = float(bar.get("close", 0))
        volume = float(bar.get("volume", 0))

        if close <= 0:
            continue

        in_range = lo <= close <= hi

        if in_range:
            in_range_count += 1
            # Fee proxy: pool-specific volume × fee_rate × capital_efficiency (per $1 capital)
            # volume_scale corrects token-level OKX bar volumes to pool-specific fraction
            cumulative_fee += volume * volume_scale * fee_rate * cap_eff
            oor_streak = 0
        else:
            # Breach detection
            if was_in_range:
                breach_count += 1
                if first_breach_bar is None:
                    first_breach_bar = i
            oor_streak += 1
            if oor_streak == _REBALANCE_OOR_STREAK:
                rebalance_count += 1

        was_in_range = in_range

    in_range_time_ratio = in_range_count / n

    # IL cost: based on final price vs entry
    final_price = float(replay_bars[-1].get("close", entry_price))
    if final_price <= 0:
        final_price = entry_price

    # If price stayed in range most of the time, use standard CLMM IL
    price_ratio = final_price / entry_price if entry_price > 0 else 1.0
    base_il = _clmm_il_fraction(price_ratio)

    # Amplify IL if price spent significant time OOR (single-leg exposure)
    oor_ratio = 1.0 - in_range_time_ratio
    if oor_ratio > _SUSTAINED_OOR_THRESHOLD:
        # Amplify IL for the OOR portion: single-leg = full directional exposure
        single_leg_il = -(abs(price_ratio - 1.0))
        il_cost_proxy = (base_il * in_range_time_ratio + single_leg_il * oor_ratio)
    else:
        il_cost_proxy = base_il

    # Normalise fee_proxy to a per-$1-capital annual yield basis
    # fee proxy is currently in USD fee / $1 capital over horizon_bars hours
    # Convert to a fraction: divide by TVL to get per-unit
    # We keep it as-is (relative to TVL) since TVL normalization happens in scorer
    cumulative_fee_proxy = cumulative_fee / max(tvl_usd, 1.0)

    realized_net_pnl_proxy = cumulative_fee_proxy + il_cost_proxy  # il is negative

    return BacktestResult(
        in_range_time_ratio=round(in_range_time_ratio, 4),
        cumulative_fee_proxy=round(cumulative_fee_proxy, 8),
        il_cost_proxy=round(il_cost_proxy, 6),
        first_breach_bar=first_breach_bar,
        breach_count=breach_count,
        rebalance_count=rebalance_count,
        realized_net_pnl_proxy=round(realized_net_pnl_proxy, 8),
    )


def backtest_all_candidates(
    ohlcv_bars: list[dict],
    candidates: list[CandidateRange],
    horizon_bars: int,
    fee_rate: float,
    tvl_usd: float = 1.0,
    volume_scale: float = 1.0,
) -> list[BacktestResult]:
    """Run backtest_candidate for all candidates and return results in same order."""
    return [
        backtest_candidate(ohlcv_bars, c, horizon_bars, fee_rate, tvl_usd, volume_scale)
        for c in candidates
    ]
