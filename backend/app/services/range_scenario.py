"""
Scenario PnL Calculator (spec §7.3)

Generates 5 synthetic future price scenarios and estimates net PnL for each
candidate LP range under each scenario.

Scenarios:
  sideways       — price wanders ±(σ/4) around entry, no drift
  slow_up        — gradual +1σ upward drift over horizon
  slow_down      — gradual -1σ downward drift over horizon
  breakout_up    — quick +2σ jump in first 25% of bars, then consolidation
  breakdown_down — quick -2σ drop in first 25% of bars, then consolidation

Each scenario produces a list of synthetic OHLCV bars fed through the existing
backtest_candidate kernel. The result is a dict mapping scenario name → net PnL
(fee_proxy − il_cost_proxy, as a fraction of capital).
"""
from __future__ import annotations
import math
import random
from typing import TYPE_CHECKING

from app.services.range_backtester import backtest_candidate

if TYPE_CHECKING:
    from app.models.schemas import CandidateRange

# Scenario names in display order
SCENARIO_NAMES: list[str] = [
    "sideways",
    "slow_up",
    "slow_down",
    "breakout_up",
    "breakdown_down",
]

# Launch-mode scenarios for young pools (price discovery phase)
LAUNCH_SCENARIO_NAMES: list[str] = [
    "discovery_sideways",       # tight consolidation after listing
    "grind_up",                 # slow organic accumulation
    "fade_down",                # listing enthusiasm fades
    "spike_then_mean_revert",   # pump followed by partial reversal
    "pump_and_dump",            # aggressive pump then crash below entry
]

# Volume multiplier per scenario (relative to avg_volume)
_VOL_MULTIPLIER: dict[str, float] = {
    "sideways":       1.0,
    "slow_up":        1.2,
    "slow_down":      0.9,
    "breakout_up":    2.0,   # breakouts have higher volume
    "breakdown_down": 1.8,
}

# Noise scale (fraction of sigma per bar, controls synthetic bar-level noise)
_NOISE_SCALE = 0.30   # 30% of per-bar sigma as Gaussian noise

# Seed for reproducibility within each call
_RNG_SEED = 42


def _per_bar_sigma(realized_vol_annual: float, bars_per_year: float = 8760.0) -> float:
    """Convert annualised vol to per-bar sigma."""
    return realized_vol_annual / math.sqrt(bars_per_year)


def _simulate_scenario(
    current_price: float,
    realized_vol_annual: float,
    avg_volume: float,
    horizon_bars: int,
    scenario: str,
    seed: int = _RNG_SEED,
) -> list[dict]:
    """
    Generate synthetic OHLCV bars for a given scenario.

    Parameters
    ----------
    current_price         Entry price.
    realized_vol_annual   Annualised realised vol (fraction, e.g. 0.80 = 80%).
    avg_volume            Average per-bar volume in USD.
    horizon_bars          Number of bars to simulate.
    scenario              One of SCENARIO_NAMES.
    seed                  RNG seed for reproducibility.

    Returns
    -------
    List of synthetic OHLCV bar dicts (oldest → newest).
    """
    rng = random.Random(seed)
    sigma_bar = _per_bar_sigma(realized_vol_annual)
    vol_mul = _VOL_MULTIPLIER.get(scenario, 1.0)
    noise = sigma_bar * _NOISE_SCALE

    bars: list[dict] = []
    price = current_price

    for i in range(horizon_bars):
        t = i / max(horizon_bars - 1, 1)  # 0→1 over horizon

        if scenario == "sideways":
            # Random walk with small noise, no drift
            drift = 0.0
            shock = rng.gauss(0.0, noise)

        elif scenario == "slow_up":
            # Linear drift: +1σ total distributed over horizon
            drift = sigma_bar / horizon_bars
            shock = rng.gauss(0.0, noise)

        elif scenario == "slow_down":
            # Linear drift: -1σ total distributed over horizon
            drift = -sigma_bar / horizon_bars
            shock = rng.gauss(0.0, noise)

        elif scenario == "breakout_up":
            # Fast +2σ in first 25% of bars, then small positive drift + noise
            if t <= 0.25:
                drift = (2.0 * sigma_bar) / max(int(horizon_bars * 0.25), 1)
                shock = rng.gauss(0.0, noise * 1.5)   # higher noise during breakout
            else:
                drift = 0.0
                shock = rng.gauss(0.0, noise)

        elif scenario == "breakdown_down":
            # Fast -2σ in first 25% of bars, then small drift + noise
            if t <= 0.25:
                drift = -(2.0 * sigma_bar) / max(int(horizon_bars * 0.25), 1)
                shock = rng.gauss(0.0, noise * 1.5)
            else:
                drift = 0.0
                shock = rng.gauss(0.0, noise)

        # ── Launch-mode scenarios (price discovery phase) ─────────────
        elif scenario == "discovery_sideways":
            # Tighter noise: price finding equilibrium near listing price
            drift = 0.0
            shock = rng.gauss(0.0, noise * 0.5)

        elif scenario == "grind_up":
            # Gentle organic accumulation: +0.5σ total over horizon
            drift = (0.5 * sigma_bar) / horizon_bars
            shock = rng.gauss(0.0, noise * 0.8)

        elif scenario == "fade_down":
            # Launch enthusiasm fading: -0.5σ total over horizon
            drift = -(0.5 * sigma_bar) / horizon_bars
            shock = rng.gauss(0.0, noise * 0.8)

        elif scenario == "spike_then_mean_revert":
            # +2σ spike in first 20%, then drift back toward start
            phase_bars = max(int(horizon_bars * 0.20), 1)
            if t <= 0.20:
                drift = (2.0 * sigma_bar) / phase_bars
                shock = rng.gauss(0.0, noise * 1.5)
            else:
                remaining = max(horizon_bars - phase_bars, 1)
                drift = -(1.0 * sigma_bar) / remaining   # partial revert, ends ~+1σ above start
                shock = rng.gauss(0.0, noise)

        elif scenario == "pump_and_dump":
            # Aggressive +3σ pump in first 25%, then crash below entry price
            phase_bars = max(int(horizon_bars * 0.25), 1)
            if t <= 0.25:
                drift = (3.0 * sigma_bar) / phase_bars
                shock = rng.gauss(0.0, noise * 2.0)
            else:
                remaining = max(horizon_bars - phase_bars, 1)
                drift = -(3.5 * sigma_bar) / remaining   # dump exceeds pump: ends below start
                shock = rng.gauss(0.0, noise * 1.5)

        else:
            drift = 0.0
            shock = rng.gauss(0.0, noise)

        # Multiplicative price update
        price *= math.exp(drift + shock)
        price = max(price, current_price * 1e-4)  # floor at 0.01% of entry

        # Synthetic OHLC (simplified)
        bar_range = price * sigma_bar * 0.5
        volume = avg_volume * vol_mul * rng.uniform(0.7, 1.3)

        bars.append({
            "time":   i * 3600,
            "open":   price * math.exp(-drift * 0.5),
            "high":   price + bar_range,
            "low":    max(price - bar_range, price * 0.01),
            "close":  price,
            "volume": volume,
        })

    return bars


def compute_scenario_pnl(
    candidate: "CandidateRange",
    current_price: float,
    realized_vol_annual: float,
    avg_volume: float,
    horizon_bars: int,
    fee_rate: float,
    tvl_usd: float,
    scenario_names: list[str] | None = None,
) -> dict[str, float]:
    """
    Compute expected net PnL under each scenario for a single candidate range.

    Parameters
    ----------
    candidate             CandidateRange to evaluate.
    current_price         Current spot price.
    realized_vol_annual   Annualised realised vol from regime detector.
    avg_volume            Average per-bar volume (USD) from recent history.
    horizon_bars          Number of bars in holding horizon.
    fee_rate              Pool fee rate as decimal (e.g. 0.003).
    tvl_usd               Pool TVL in USD.
    scenario_names        Which scenarios to run. Defaults to SCENARIO_NAMES (mature).
                          Pass LAUNCH_SCENARIO_NAMES for fresh/infant pools.

    Returns
    -------
    Dict mapping scenario_name → net_pnl (fraction of capital, can be negative).
    """
    names = scenario_names if scenario_names is not None else SCENARIO_NAMES
    result: dict[str, float] = {}

    for scenario in names:
        synthetic_bars = _simulate_scenario(
            current_price=current_price,
            realized_vol_annual=realized_vol_annual,
            avg_volume=avg_volume,
            horizon_bars=horizon_bars,
            scenario=scenario,
        )
        bt = backtest_candidate(
            ohlcv_bars=synthetic_bars,
            candidate=candidate,
            horizon_bars=horizon_bars,
            fee_rate=fee_rate,
            tvl_usd=tvl_usd,
        )
        result[scenario] = round(bt.realized_net_pnl_proxy, 6)

    return result


def compute_scenario_utility(scenario_pnl: dict[str, float]) -> float:
    """
    Compute a single [0, 1] utility from a scenario PnL dict.

    Formula (per spec §8.5):
        raw = 0.60 * median(pnl) + 0.25 * min(pnl) + 0.15 * mean(pnl)

    Normalisation: maps ±10% capital return to [0, 1] with centre at 0.
    Values outside ±10% are clamped. Returns 0.0 for empty input.
    """
    values = list(scenario_pnl.values())
    if not values:
        return 0.0

    sorted_vals = sorted(values)
    n = len(sorted_vals)
    # Median (without scipy dependency)
    mid = n // 2
    median_pnl = sorted_vals[mid] if n % 2 == 1 else (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0
    min_pnl  = sorted_vals[0]
    mean_pnl = sum(values) / n

    raw = 0.60 * median_pnl + 0.25 * min_pnl + 0.15 * mean_pnl

    # Normalize: raw=0 → 0.5, raw=+0.10 → 1.0, raw=-0.10 → 0.0
    normalized = raw / 0.10 * 0.5 + 0.5
    return round(max(0.0, min(1.0, normalized)), 4)


def compute_all_scenario_pnl(
    candidates: list["CandidateRange"],
    current_price: float,
    realized_vol_annual: float,
    ohlcv_bars: list[dict],
    horizon_bars: int,
    fee_rate: float,
    tvl_usd: float,
    use_launch_scenarios: bool = False,
    volume_scale: float = 1.0,
) -> list[dict[str, float]]:
    """
    Compute scenario PnL for each candidate in the list.

    avg_volume is inferred from the recent OHLCV history.

    Parameters
    ----------
    use_launch_scenarios  When True, uses LAUNCH_SCENARIO_NAMES instead of
                          SCENARIO_NAMES. Set True for fresh/infant pools.
    volume_scale          Multiply inferred avg_volume by this factor to convert
                          token-level OKX volumes to pool-specific levels.
                          Only applied when avg_volume is derived from bars (not fallback).
                          Default 1.0 (no scaling).

    Returns a list of dicts (same order as candidates).
    """
    scenario_names = LAUNCH_SCENARIO_NAMES if use_launch_scenarios else SCENARIO_NAMES

    recent = ohlcv_bars[-min(horizon_bars, len(ohlcv_bars)):]
    if recent:
        avg_volume = (sum(float(b.get("volume", 0)) for b in recent) / len(recent)) * volume_scale
    else:
        # Young-pool fallback: use TVL proxy; don't apply volume_scale (no OKX data available)
        avg_volume = tvl_usd * 0.05

    return [
        compute_scenario_pnl(
            candidate=c,
            current_price=current_price,
            realized_vol_annual=max(realized_vol_annual, 0.10),
            avg_volume=avg_volume,
            horizon_bars=horizon_bars,
            fee_rate=fee_rate,
            tvl_usd=tvl_usd,
            scenario_names=scenario_names,
        )
        for c in candidates
    ]
