"""
Range Generator (Layer C): generates a diverse set of candidate LP ranges.

Supports two pool types:
  "v3"     — Uniswap V3 style (tick = log(price) / log(1.0001), snap to tick_spacing)
  "dlmm"   — Meteora DLMM    (bin_id = log(price) / log(1 + bin_step/10000))

Four candidate families:
  A. Volatility-band   — center ± N*sigma (N = 0.5, 1.0, 1.5, 2.0)
  B. Volume-profile    — POC-centred range and value-area range
  C. Trend-biased      — asymmetric shift based on regime drift direction
  D. Defensive         — very wide fallback for chaotic / low-confidence markets

Each CandidateRange carries:
  lower_price, upper_price, lower_tick, upper_tick, width_pct, center_price, range_type
"""
from __future__ import annotations
import math
import logging
from typing import Literal

import numpy as np

from app.models.schemas import CandidateRange, RegimeResult

logger = logging.getLogger(__name__)

# ── Pool type constants ────────────────────────────────────────────────

PoolType = Literal["v3", "dlmm"]

# V3 log base
_V3_LOG_BASE = math.log(1.0001)

# Default tick spacings for V3 fee tiers (fee % → tick_spacing)
_V3_FEE_TICK_SPACING: dict[float, int] = {
    0.01:  1,    # 0.01% (Uniswap v3 on some chains)
    0.05: 10,    # 0.05%
    0.30: 60,    # 0.30%
    1.00: 200,   # 1.00%
}
_V3_DEFAULT_TICK_SPACING = 60


# ── Tick / Bin math ───────────────────────────────────────────────────

def _v3_price_to_tick(price: float) -> int:
    """Convert price to Uniswap V3 tick (un-snapped)."""
    if price <= 0:
        return 0
    return int(math.floor(math.log(price) / _V3_LOG_BASE))


def _v3_tick_to_price(tick: int) -> float:
    """Convert V3 tick to price."""
    return math.pow(1.0001, tick)


def _v3_snap_tick(tick: int, tick_spacing: int) -> int:
    """Snap tick to nearest valid multiple of tick_spacing (round toward zero)."""
    if tick_spacing <= 0:
        return tick
    return int(math.floor(tick / tick_spacing) * tick_spacing)


def _dlmm_price_to_bin(price: float, bin_step: int) -> int:
    """Convert price to Meteora DLMM bin_id."""
    if price <= 0 or bin_step <= 0:
        return 0
    base = 1.0 + bin_step / 10_000.0
    return int(math.floor(math.log(price) / math.log(base)))


def _dlmm_bin_to_price(bin_id: int, bin_step: int) -> float:
    """Convert Meteora DLMM bin_id to price."""
    base = 1.0 + bin_step / 10_000.0
    return math.pow(base, bin_id)


def price_to_position(price: float, pool_type: PoolType, step: int) -> int:
    """Convert price to tick (V3) or bin_id (DLMM)."""
    if pool_type == "v3":
        return _v3_price_to_tick(price)
    return _dlmm_price_to_bin(price, step)


def position_to_price(pos: int, pool_type: PoolType, step: int) -> float:
    """Convert tick / bin_id back to price."""
    if pool_type == "v3":
        return _v3_tick_to_price(pos)
    return _dlmm_bin_to_price(pos, step)


def snap_to_step(pos: int, step: int) -> int:
    """Snap a position to the nearest multiple of step (floor)."""
    if step <= 0:
        return pos
    return int(math.floor(pos / step) * step)


def _build_candidate(
    lower_price: float,
    upper_price: float,
    current_price: float,
    pool_type: PoolType,
    step: int,
    range_type: str,
) -> CandidateRange | None:
    """Build and validate a CandidateRange, snapping ticks to step multiples."""
    if lower_price <= 0 or upper_price <= lower_price:
        return None
    if current_price <= 0:
        return None

    lower_raw = price_to_position(lower_price, pool_type, step)
    upper_raw = price_to_position(upper_price, pool_type, step)

    lower_tick = snap_to_step(lower_raw, step)
    upper_tick = snap_to_step(upper_raw, step)

    if lower_tick >= upper_tick:
        # Ensure at least one step gap
        upper_tick = lower_tick + step

    # Recompute prices from snapped ticks for accuracy
    lower_price_snapped = position_to_price(lower_tick, pool_type, step)
    upper_price_snapped = position_to_price(upper_tick, pool_type, step)

    center = (lower_price_snapped + upper_price_snapped) / 2.0
    width_pct = (upper_price_snapped - lower_price_snapped) / max(center, 1e-30)

    return CandidateRange(
        lower_price=round(lower_price_snapped, 10),
        upper_price=round(upper_price_snapped, 10),
        lower_tick=lower_tick,
        upper_tick=upper_tick,
        width_pct=round(width_pct, 6),
        center_price=round(center, 10),
        range_type=range_type,
    )


# ── Volume profile helpers ────────────────────────────────────────────

def _compute_volume_profile(ohlcv_bars: list[dict], n_buckets: int = 50) -> tuple[float, float, float]:
    """
    Compute Point of Control (POC), Value Area High (VAH), Value Area Low (VAL)
    from OHLCV bars using price-bucket volume aggregation.

    Returns (poc_price, vah_price, val_price).
    Falls back to (median_close, p75_close, p25_close) if insufficient data.
    """
    if len(ohlcv_bars) < 4:
        closes = [b["close"] for b in ohlcv_bars if b.get("close", 0) > 0]
        if not closes:
            return 0.0, 0.0, 0.0
        med = float(np.median(closes))
        return med, med * 1.05, med * 0.95

    closes = np.array([float(b["close"]) for b in ohlcv_bars if b.get("close", 0) > 0])
    volumes = np.array([float(b.get("volume", 0)) for b in ohlcv_bars if b.get("close", 0) > 0])

    if len(closes) < 4 or volumes.sum() < 1e-10:
        med = float(np.median(closes))
        return med, med * 1.05, med * 0.95

    lo, hi = closes.min(), closes.max()
    if hi <= lo:
        med = float(np.median(closes))
        return med, med * 1.05, med * 0.95

    bucket_width = (hi - lo) / n_buckets
    bucket_ids = np.floor((closes - lo) / bucket_width).clip(0, n_buckets - 1).astype(int)
    bucket_vol = np.zeros(n_buckets)
    for i, vid in enumerate(bucket_ids):
        bucket_vol[vid] += volumes[i]

    poc_bucket = int(np.argmax(bucket_vol))
    poc_price = lo + (poc_bucket + 0.5) * bucket_width

    # Value area: accumulate volume from POC outward until 70% covered
    total_vol = bucket_vol.sum()
    target = total_vol * 0.70
    accumulated = bucket_vol[poc_bucket]
    lo_idx, hi_idx = poc_bucket, poc_bucket

    while accumulated < target and (lo_idx > 0 or hi_idx < n_buckets - 1):
        lo_can = bucket_vol[lo_idx - 1] if lo_idx > 0 else -1.0
        hi_can = bucket_vol[hi_idx + 1] if hi_idx < n_buckets - 1 else -1.0
        if lo_can >= hi_can and lo_idx > 0:
            lo_idx -= 1
            accumulated += bucket_vol[lo_idx]
        elif hi_idx < n_buckets - 1:
            hi_idx += 1
            accumulated += bucket_vol[hi_idx]
        else:
            break

    val_price = lo + lo_idx * bucket_width
    vah_price = lo + (hi_idx + 1) * bucket_width

    return float(poc_price), float(vah_price), float(val_price)


# ── Sigma scaling ──────────────────────────────────────────────────────

def _horizon_sigma(realized_vol_annual: float, horizon_hours: float) -> float:
    """
    Scale annualised realised vol to horizon sigma (as price fraction).
    sigma_horizon = rv_annual * sqrt(horizon_hours / 8760)
    """
    if realized_vol_annual <= 0 or horizon_hours <= 0:
        return 0.10  # fallback: 10% width
    return realized_vol_annual * math.sqrt(horizon_hours / 8760.0)


def _price_bounds_from_sigma(center: float, sigma_pct: float, multiplier: float) -> tuple[float, float]:
    """lower = center * (1 - mul*sigma), upper = center * (1 + mul*sigma)."""
    lower = center * (1.0 - multiplier * sigma_pct)
    upper = center * (1.0 + multiplier * sigma_pct)
    return max(lower, center * 1e-6), upper


# ── Main entry point ──────────────────────────────────────────────────

def _apply_width_floor(
    candidate: CandidateRange | None,
    min_width_pct: float,
    current_price: float,
    pool_type: PoolType,
    step: int,
) -> CandidateRange | None:
    """
    Widen a candidate if its width_pct is below min_width_pct.
    The center is preserved; both sides are expanded symmetrically.

    Uses a 2% target buffer to compensate for tick-snap rounding that can
    reduce width below the floor. Falls back to a one-step expansion if the
    buffered candidate still lands below the floor after snapping.

    Returns None if the widened candidate cannot be built.
    """
    if candidate is None or min_width_pct <= 0:
        return candidate
    if candidate.width_pct >= min_width_pct:
        return candidate

    center = candidate.center_price
    # Add 2% buffer to overshoot the floor slightly, compensating for snap rounding
    target_width = min_width_pct * 1.02
    half   = center * target_width / 2.0
    lo     = max(center - half, center * 1e-6)
    hi     = center + half
    widened = _build_candidate(lo, hi, current_price, pool_type, step, candidate.range_type)
    if widened is None:
        return candidate

    # Post-check: if tick-snap still left us below the floor, expand by one step each side
    if widened.width_pct < min_width_pct:
        lo2 = position_to_price(widened.lower_tick - step, pool_type, step)
        hi2 = position_to_price(widened.upper_tick + step, pool_type, step)
        expanded = _build_candidate(lo2, hi2, current_price, pool_type, step, candidate.range_type)
        if expanded and expanded.width_pct >= min_width_pct:
            return expanded

    return widened


def generate_candidates(
    current_price: float,
    pool_type: PoolType,
    step: int,
    regime_result: RegimeResult,
    ohlcv_bars: list[dict],
    horizon_hours: float = 48.0,
    fee_pct: float = 0.30,
    # Phase 1.5 params (optional, backward-compatible defaults)
    min_width_floor_pct: float = 0.0,
    fresh_mode: bool = False,
) -> list[CandidateRange]:
    """
    Generate 8–12 candidate LP ranges across 4 families.

    Parameters
    ----------
    current_price       Current spot price of the base token.
    pool_type           "v3" or "dlmm".
    step                tick_spacing (V3) or bin_step (DLMM).
    regime_result       Output of regime_detector.detect_regime().
    ohlcv_bars          OHLCV history (oldest → newest), used for volume profile.
    horizon_hours       Target LP holding horizon in hours (used to scale sigma).
    fee_pct             Pool fee percentage (0.30 for 0.3%); used only for labels.
    min_width_floor_pct Minimum width_pct for all candidates (fraction, e.g. 0.14).
                        Candidates narrower than this floor are widened symmetrically.
                        Pass 0.0 (default) to disable.
    fresh_mode          When True, applies young-pool candidate logic:
                          - Skips trend-biased candidates (unreliable on short history)
                          - Uses conservative sigma multipliers for vol-band
                          - Generates more defensive candidates

    Returns
    -------
    List of CandidateRange objects (8–12 items, deduplicated by tick pair).
    """
    if current_price <= 0:
        logger.warning("generate_candidates: invalid current_price=%s", current_price)
        return []

    rv = regime_result.realized_vol if regime_result.realized_vol > 0 else 0.80
    regime = regime_result.regime
    sigma = _horizon_sigma(rv, horizon_hours)

    # For fresh pools with very short history, sigma estimate is unreliable;
    # floor it at a reasonable minimum to avoid excessively narrow candidates.
    if fresh_mode:
        sigma = max(sigma, 0.08)   # at least 8% horizon sigma for young pools

    candidates: list[CandidateRange] = []

    def _add(c: CandidateRange | None) -> None:
        if c:
            c = _apply_width_floor(c, min_width_floor_pct, current_price, pool_type, step)
            if c:
                candidates.append(c)

    # ── A. Volatility-band candidates ─────────────────────────────────
    # fresh_mode: only 3 wider multipliers (skip 0.5× which is too tight)
    vol_center   = current_price
    multipliers  = (1.0, 1.5, 2.0) if fresh_mode else (0.5, 1.0, 1.5, 2.0)
    for multiplier in multipliers:
        lo, hi = _price_bounds_from_sigma(vol_center, sigma, multiplier)
        _add(_build_candidate(lo, hi, current_price, pool_type, step, "volatility_band"))

    # ── B. Volume-profile candidates ──────────────────────────────────
    poc, vah, val = _compute_volume_profile(ohlcv_bars)
    if poc > 0 and val > 0 and vah > val:
        c_va = _build_candidate(val, vah, current_price, pool_type, step, "volume_profile")
        _add(c_va)

        lo_poc, hi_poc = _price_bounds_from_sigma(poc, sigma, 1.0)
        _add(_build_candidate(lo_poc, hi_poc, current_price, pool_type, step, "volume_profile"))

    # ── C. Trend-biased asymmetric candidates ─────────────────────────
    # Skipped in fresh_mode: short history makes trend signals unreliable.
    if not fresh_mode and regime in ("trend_up", "trend_down"):
        if regime == "trend_up":
            lo_trend = current_price * (1.0 - 0.5 * sigma)
            hi_trend = current_price * (1.0 + 1.5 * sigma)
        else:
            lo_trend = current_price * (1.0 - 1.5 * sigma)
            hi_trend = current_price * (1.0 + 0.5 * sigma)

        lo_trend = max(lo_trend, current_price * 1e-6)
        _add(_build_candidate(lo_trend, hi_trend, current_price, pool_type, step, "trend_biased"))

        if regime == "trend_up":
            lo2 = current_price * (1.0 - 0.3 * sigma)
            hi2 = current_price * (1.0 + 2.0 * sigma)
        else:
            lo2 = current_price * (1.0 - 2.0 * sigma)
            hi2 = current_price * (1.0 + 0.3 * sigma)
        lo2 = max(lo2, current_price * 1e-6)
        _add(_build_candidate(lo2, hi2, current_price, pool_type, step, "trend_biased"))

    # ── D. Defensive fallback candidates ──────────────────────────────
    # fresh_mode: generate 3 defensive candidates (wider coverage of unknowns)
    defensive_muls = (3.0, 4.0, 5.0) if fresh_mode else (3.0, 4.0)
    for multiplier in defensive_muls:
        lo, hi = _price_bounds_from_sigma(current_price, sigma, multiplier)
        _add(_build_candidate(lo, hi, current_price, pool_type, step, "defensive"))

    # Deduplicate by (lower_tick, upper_tick)
    seen: set[tuple[int, int]] = set()
    unique: list[CandidateRange] = []
    for c in candidates:
        key = (c.lower_tick, c.upper_tick)
        if key not in seen:
            seen.add(key)
            unique.append(c)

    logger.debug(
        "generate_candidates: regime=%s sigma=%.3f pool_type=%s step=%d "
        "fresh_mode=%s floor=%.2f%% → %d candidates",
        regime, sigma, pool_type, step, fresh_mode,
        min_width_floor_pct * 100, len(unique),
    )
    return unique


def infer_v3_tick_spacing(fee_pct: float) -> int:
    """Look up V3 tick_spacing from fee percentage. Falls back to 60."""
    # Normalise: accept 0.3 or 0.003
    fee = fee_pct if fee_pct >= 0.01 else fee_pct * 100.0
    # Round to nearest known tier
    for known_fee, ts in sorted(_V3_FEE_TICK_SPACING.items()):
        if abs(fee - known_fee) < 0.02:
            return ts
    return _V3_DEFAULT_TICK_SPACING


def infer_pool_type(protocol: str) -> PoolType:
    """
    Infer pool type from protocol name string.
    Defaults to "v3" for unknown protocols.
    """
    proto_lower = protocol.lower()
    if "meteora dlmm" in proto_lower or "dlmm" in proto_lower:
        return "dlmm"
    return "v3"
