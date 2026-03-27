"""
Layer 0: History Sufficiency / Evidence Assessment

Evaluates the quality and quantity of available price history and determines:
  - history_tier:              mature / growing / fresh / infant
  - recommendation_mode:       full_replay / blended_replay / launch_mode / observe_only
  - effective_evidence_score:  0–1 composite quality signal
  - uncertainty_penalty:       flat deduction applied to final utility
  - replay_weight / scenario_weight: blending weights for final utility
  - preferred_resolution:      which bar interval to prefer for this pool

Design rules:
  - All outputs have Python defaults so callers that don't pass optional args still work.
  - No imports from other range_* modules (no circular deps).
  - Two standalone helpers exposed for use in range_generator and range_recommender:
      age_based_width_floor()   → minimum width_pct floor (fraction)
      fee_persistence_factor()  → shrinkage multiplier for fee APR estimates
"""
from __future__ import annotations
from dataclasses import dataclass

# ── Tier age thresholds ────────────────────────────────────────────────────────

_MATURE_AGE_HOURS  = 24.0
_GROWING_MIN_AGE   = 4.0
_FRESH_MIN_AGE     = 1.0

# Bar-count minimums per tier (for each available resolution)
_MATURE_1H_BARS    = 24        # 24h × 1 bar/h
_MATURE_5M_BARS    = 288       # 24h × 12 bars/h

_GROWING_MIN_5M    = 48        # 4h × 12
_GROWING_MIN_1H    = 4         # at least 4 × 1H bars

_FRESH_MIN_5M      = 12        # 1h × 12 bars/h
_FRESH_MIN_1M      = 60        # 1h × 60 bars/h

# Target bars for evidence-score coverage calculation
_TARGET_MINUTES    = 1440      # 24h
_TARGET_1H_BARS    = 24
_TARGET_5M_BARS    = 288
_TARGET_1M_BARS    = 1440

# Source quality map: token-level data is the weakest; pool-specific swap events are best
_SOURCE_QUALITY: dict[str, float] = {
    "pool_specific": 1.0,
    "pool_candle":   0.7,
    "token_level":   0.4,
}


# ── Result dataclass ───────────────────────────────────────────────────────────

@dataclass
class SufficiencyResult:
    history_tier: str               # "mature" | "growing" | "fresh" | "infant"
    recommendation_mode: str        # "full_replay" | "blended_replay" | "launch_mode" | "observe_only"
    actionability: str              # "standard" | "caution" | "watch_only"
    effective_evidence_score: float # 0–1 composite
    data_quality_score: float       # 0–1 (missing bar component)
    source_quality_score: float     # 0–1 (data source quality)
    uncertainty_penalty: float      # 0–0.40 flat deduction on final utility
    replay_weight: float            # 0–1
    scenario_weight: float          # 0–1  (always = 1 - replay_weight)
    preferred_resolution: str       # "1H" | "5m" | "1m"
    pool_age_hours: float


# ── Core assessment ────────────────────────────────────────────────────────────

def assess(
    pool_age_hours: float,
    bars_1h: int,
    bars_5m: int = 0,
    bars_1m: int = 0,
    missing_bar_ratio: float = 0.0,
    source_quality: str = "token_level",
    liquidity_instability: float = 0.0,
) -> SufficiencyResult:
    """
    Evaluate historical evidence quality for a pool.

    Parameters
    ----------
    pool_age_hours        Hours since pool creation (from DexScreener pairCreatedAt).
    bars_1h               Count of 1H OHLCV bars successfully fetched.
    bars_5m               Count of 5m bars fetched (0 if not attempted).
    bars_1m               Count of 1m bars fetched (0 if not attempted).
    missing_bar_ratio     Fraction of expected bars that are gaps/missing (0–1).
    source_quality        "pool_specific" | "pool_candle" | "token_level"
    liquidity_instability Volatility of liquidity_usd over recent snapshots (0–1).
                          Pass 0.0 if unknown.

    Returns
    -------
    SufficiencyResult
    """
    # ── 1. Classify tier ──────────────────────────────────────────────────────
    if pool_age_hours >= _MATURE_AGE_HOURS and (
        bars_1h >= _MATURE_1H_BARS or bars_5m >= _MATURE_5M_BARS
    ):
        tier = "mature"
    elif pool_age_hours >= _GROWING_MIN_AGE and (
        bars_5m >= _GROWING_MIN_5M or bars_1h >= _GROWING_MIN_1H
    ):
        tier = "growing"
    elif pool_age_hours >= _FRESH_MIN_AGE and (
        bars_5m >= _FRESH_MIN_5M or bars_1m >= _FRESH_MIN_1M
    ):
        tier = "fresh"
    else:
        tier = "infant"

    # ── 2. Mode and preferred resolution ─────────────────────────────────────
    _TIER_MODE = {
        "mature":  "full_replay",
        "growing": "blended_replay",
        "fresh":   "launch_mode",
        "infant":  "observe_only",
    }
    mode = _TIER_MODE[tier]

    if tier in ("mature", "growing"):
        preferred_resolution = "1H"
    elif bars_5m >= _FRESH_MIN_5M:
        preferred_resolution = "5m"
    elif bars_1m >= _FRESH_MIN_1M:
        preferred_resolution = "1m"
    else:
        preferred_resolution = "1m"   # best-effort for infant

    # ── 3. Evidence score ─────────────────────────────────────────────────────
    available_minutes = bars_1h * 60 + bars_5m * 5 + bars_1m * 1
    history_coverage = min(1.0, available_minutes / _TARGET_MINUTES)

    # Bar coverage: use the most granular resolution available
    if bars_1h > 0:
        bar_coverage = min(1.0, bars_1h / _TARGET_1H_BARS)
    elif bars_5m > 0:
        bar_coverage = min(1.0, bars_5m / _TARGET_5M_BARS)
    else:
        bar_coverage = min(1.0, bars_1m / _TARGET_1M_BARS) if bars_1m > 0 else 0.0

    sq_factor    = _SOURCE_QUALITY.get(source_quality, 0.4)
    data_quality = max(0.0, 1.0 - missing_bar_ratio)

    evidence = (
        0.35 * history_coverage
        + 0.30 * bar_coverage
        + 0.20 * sq_factor
        + 0.15 * data_quality
    )
    evidence = round(min(1.0, max(0.0, evidence)), 4)

    # ── 4. Uncertainty penalty ────────────────────────────────────────────────
    source_penalty = 1.0 - sq_factor   # 0.0 for pool_specific, 0.6 for token_level
    penalty = (
        0.20 * (1.0 - evidence)
        + 0.10 * min(liquidity_instability, 1.0)
        + 0.10 * source_penalty
    )
    penalty = round(min(0.40, max(0.0, penalty)), 4)

    # ── 5. Replay / Scenario weights ──────────────────────────────────────────
    # Evidence mapped linearly to replay_weight using calibrated bounds.
    # Below lower_bound → pure scenario; above upper_bound → pure replay.
    # Bounds are read from settings so they can be updated after calibration.
    from app.core.config import settings as _settings
    _rw_lo = _settings.replay_weight_lower_bound   # default 0.25
    _rw_hi = _settings.replay_weight_upper_bound   # default 0.75
    _rw_span = max(_rw_hi - _rw_lo, 1e-6)          # guard division by zero
    raw_replay    = (evidence - _rw_lo) / _rw_span
    replay_weight = round(min(1.0, max(0.0, raw_replay)), 4)
    scenario_weight = round(1.0 - replay_weight, 4)

    # ── 6. Actionability ──────────────────────────────────────────────────────
    # Thresholds are read from settings so they can be updated after calibration
    # without changing code.  Defaults preserve original hand-crafted values.
    _grow_std  = _settings.calibration_growing_standard_threshold  # default 0.65
    _mat_std   = _settings.calibration_mature_standard_threshold   # default 0.55
    if tier == "infant":
        actionability = "watch_only"
    elif tier == "fresh":
        actionability = "caution"
    elif tier == "growing":
        actionability = "standard" if evidence >= _grow_std else "caution"
    else:   # mature
        actionability = "standard" if evidence >= _mat_std else "caution"

    return SufficiencyResult(
        history_tier=tier,
        recommendation_mode=mode,
        actionability=actionability,
        effective_evidence_score=evidence,
        data_quality_score=round(data_quality, 4),
        source_quality_score=round(sq_factor, 4),
        uncertainty_penalty=penalty,
        replay_weight=replay_weight,
        scenario_weight=scenario_weight,
        preferred_resolution=preferred_resolution,
        pool_age_hours=round(pool_age_hours, 2),
    )


# ── Age-based minimum width floor ─────────────────────────────────────────────

def age_based_width_floor(pool_age_hours: float) -> float:
    """
    Return the minimum width_pct floor (as a fraction) based on pool age.

    Prevents overly narrow ranges on young pools where price discovery is unstable.
    For mature pools (>= 24h) returns 0.0 (no forced floor beyond normal logic).

    Examples
    --------
    age = 1h  → 0.18  (18%)
    age = 3h  → 0.14  (14%)
    age = 12h → 0.10  (10%)
    age = 24h → 0.00  (no extra floor)
    """
    if pool_age_hours < 2.0:
        return 0.18
    elif pool_age_hours < 6.0:
        return 0.14
    elif pool_age_hours < 24.0:
        return 0.10
    else:
        return 0.0


# ── Fee persistence factor ─────────────────────────────────────────────────────

def fee_persistence_factor(
    pool_age_hours: float,
    jump_ratio: float = 0.0,
    liquidity_instability: float = 0.0,
) -> float:
    """
    Shrinkage multiplier for fee APR estimates on young pools.

    Prevents inflated annualised fee estimates caused by the initial high-volume
    launch spike being naively annualised.

    Returns a factor in [0.15, 1.0]:
      - mature pool (24h+): factor ≈ 1.0
      - brand-new pool (0h): factor = 0.15 (floor)
      - high jump ratio or unstable liquidity reduce factor further

    Usage:
        shrunk_fee_apr = raw_fee_apr * fee_persistence_factor(...)
    """
    age_factor = min(pool_age_hours / 24.0, 1.0)
    age_factor  = max(age_factor, 0.15)

    jump_factor = max(1.0 - jump_ratio, 0.4)
    liq_factor  = max(1.0 - liquidity_instability, 0.5)

    factor = age_factor * jump_factor * liq_factor
    return round(min(1.0, max(0.15, factor)), 4)
