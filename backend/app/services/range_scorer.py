"""
Range Scorer: computes utility score for each candidate range.

Score(r) = w_fee * FeeScore(r) * crowding_factor(width, tvl) * capture_ratio(vol_tvl)
         - w_il  * ILScore(r)
         - w_breach * BreachRisk(r)
         - w_rebalance * RebalanceCost(r)
         - w_quality * QualityPenalty(r)

All component scores are normalised to [0, 1].
Final utility_score is clipped to [0, 1].

Default weights (configurable via config.py):
  fee       0.30
  il        0.25
  breach    0.25
  rebalance 0.10
  quality   0.10

P2.2.2 — Crowding factor (structural density):
  Two independent structural dimensions reduce theoretical fee capture:
    Phase 1 (width):  narrower range → more crowded tick band
    Phase 2 (TVL):    larger pool → more LP capital competing
  crowding_factor ∈ [_CROWDING_FLOOR × _TVL_FLOOR, 1.0]

P2.3.3 — Competitive fee capture ratio (activity-driven competition):
  High-activity pools (vol/TVL >> 1) attract sophisticated automated LPs
  who concentrate in very narrow ranges, diluting individual LP's realized share.
  Proxy: vol_tvl_ratio (daily volume / TVL) from MarketQualityResult.
  capture_ratio ∈ [_CAPTURE_FLOOR, 1.0]; orthogonal to P2.2.2 crowding.

  Semantic boundary:
    crowding_factor  — structural: how many LPs (width + TVL)
    capture_ratio    — dynamic: how contested the fee flow is (vol/TVL)
  Same width + same TVL → same crowding; different vol/TVL → different capture.

P2.4.1 — Regime confidence → breach risk inflation:
  Low confidence means the regime classifier is uncertain about vol/drift, so
  the underlying breach probability estimate is less reliable.  An additive
  penalty inflates breach_risk when confidence < threshold (0.70).
  penalty ∈ [0, _BREACH_CONF_MAX]; applied after _breach_risk() in score_candidate().

  No double-counting with:
    uncertainty_penalty          — penalises evidence QUANTITY (data sufficiency)
    _apply_confidence_calibration — scales the DISPLAY recommendation_confidence post-selection
    jump_penalty in _breach_risk  — penalises chaotic RETURNS, not classifier uncertainty
"""
from __future__ import annotations
import logging
import math
from dataclasses import field

from app.models.schemas import (
    BacktestResult,
    CandidateRange,
    ILRiskResult,
    MarketQualityResult,
    RegimeResult,
    ScoredRange,
)

logger = logging.getLogger(__name__)

# ── Default scoring weights ────────────────────────────────────────────

DEFAULT_WEIGHTS: dict[str, float] = {
    "fee":       0.30,
    "il":        0.25,
    "breach":    0.25,
    "rebalance": 0.10,
    "quality":   0.10,
}

# Fee score: 300% APR proxy → full score (cap)
# fee_proxy is (vol_in_range × fee_rate × cap_eff) / TVL over horizon
# We scale by an annualisation factor to get APR-like number
_FEE_SCORE_FULL_APR = 3.0       # 300% APR → FeeScore = 1.0
_REBALANCE_COST_PER_EVENT = 0.001  # 0.1% of capital per rebalance (rough gas + slippage)
_BARS_PER_YEAR = 8760.0         # 1h bars per year

# ── P2.2.2: Crowding factor constants ────────────────────────────────────────
# Fee capture efficiency = width_factor × tvl_factor
#
# Combined captures two independent crowding dimensions:
#   width_factor — how congested this specific tick band is (Phase 1)
#   tvl_factor   — how many LPs are in the pool competing (Phase 2)
#
# Both are multiplicative because they model independent probability fractions:
# "P(you capture volume | price in range) = P(not out-crowded by width) × P(not out-crowded by TVL)"
#
# ── Phase 1: width-based sigmoid ─────────────────────────────────────────────
# capture_width = FLOOR + (1-FLOOR) × σ(STEEPNESS × (width_pct - INFLECTION))
#
#   width_pct 0.010 (±0.5%):  ≈ 0.68  [32% haircut — very narrow, maximum crowding]
#   width_pct 0.040 (±2%):    = 0.75  [25% haircut — narrow, crowded tick band]
#   width_pct 0.100 (±5%):    ≈ 0.88  [12% haircut — medium, moderate competition]
#   width_pct 0.200 (±10%):   ≈ 0.98  [ 2% haircut — wide, minimal crowding]
#   width_pct 0.400+:         ≈ 1.00  [  ~0% haircut — full-range-like, no crowding]
_CROWDING_FLOOR      = 0.50   # minimum capture ratio (ultra-narrow asymptote)
_CROWDING_INFLECTION = 0.04   # width_pct where capture = (FLOOR + 1) / 2 = 0.75
_CROWDING_STEEPNESS  = 20.0   # steepness of sigmoid transition

# ── Phase 2: TVL-based log-linear discount ────────────────────────────────────
# Larger TVL pools attract more LPs competing at the same tick bands.
# LP density ~ log(TVL), so fee capture discount scales with log10(tvl / TVL_REF).
#
# tvl_factor = clip(1.0 - TVL_DECAY × max(0, log10(tvl_usd / TVL_REF)), TVL_FLOOR, 1.0)
#
# Design choices:
#   - Upper-capped at 1.0: small pools (TVL < REF) do NOT get a boost above Phase 1 baseline.
#     Phase 1 already assumed "average" competition; small pools don't deserve extra credit.
#   - Floor at 0.85: even a $1B+ pool gets at most a 15% additional discount from TVL alone.
#     Prevents extreme penalisation of flagship pools.
#   - TVL_REF = $1M: the neutral anchor. Most mid-size pools sit here; effect kicks in above.
#   - TVL_DECAY = 0.05: 5% additional discount per order of magnitude above $1M.
#     $10M → 0.95, $100M → 0.90, $1B → 0.85 (floor)
#
# Combined floor: 0.50 (width) × 0.85 (TVL) = 0.425 at the theoretical extreme
# (ultra-narrow range in a $1B+ pool). In practice narrowest seen range ≈ 0.68 × 0.85 = 0.58.
_TVL_REF   = 1_000_000.0   # $1M — neutral TVL, factor = 1.0
_TVL_DECAY = 0.05           # per-decade discount above TVL_REF
_TVL_FLOOR = 0.85           # minimum TVL factor (even for $1B+ pools)

# ── P2.3.3: Competitive fee capture constants ─────────────────────────────────
# High-activity pools attract automated LPs / market-makers with very tight ranges
# who capture a disproportionate share of fees.  vol/TVL is a proxy for how
# "contested" the fee flow is.  Result: individual LP realized share < theoretical.
#
# Formula:
#   capture_ratio = FLOOR + (1-FLOOR) × (1 - σ(STEEPNESS × (vol_tvl - NEUTRAL)))
#
# vol/TVL anchors (daily_volume / tvl_usd):
#   0.0  → 1.00 (fail-safe; treat missing as quiet pool)
#   0.1  → 0.94 ( 6% haircut — quiet pool, passive LP only)
#   0.5  → 0.90 (10% haircut — lightly active)
#   1.0  → 0.85 (15% haircut — neutral midpoint)
#   2.0  → 0.76 (24% haircut — hot pool, active competition)
#   3.0  → 0.71 (29% haircut — very hot, near floor)
#   5.0+ → 0.70 (floor enforced)
#
# Orthogonality: same width + same TVL → crowding_factor identical for two pools;
# different vol/TVL → capture_ratio differs → separation is provable by construction.
#
# Combined floor: 0.50 (width) × 0.85 (TVL) × 0.70 (capture) = 0.30 at extreme.
_CAPTURE_FLOOR     = 0.70   # minimum individual capture ratio (hottest pools)
_CAPTURE_NEUTRAL   = 1.0    # vol/TVL inflection point — midpoint of sigmoid
_CAPTURE_STEEPNESS = 1.5    # steepness of sigmoid transition

# ── P2.4.1: Regime confidence → breach risk inflation ────────────────────────
# When the regime classifier has low confidence, its vol/drift outputs are less
# reliable, meaning breach probability estimates should be treated conservatively.
#
# Formula:
#   shortfall = clip((THRESHOLD - confidence) / THRESHOLD, 0, 1)
#   penalty   = shortfall × MAX
#
# Anchors (confidence → additive penalty to breach_risk):
#   0.85 → 0.000  (high-confidence signal, no inflation)
#   0.70 → 0.000  (threshold boundary, still no penalty)
#   0.50 → 0.023  (moderate uncertainty)
#   0.35 → 0.040  (weak regime signal — default fallback from detect_regime)
#   0.20 → 0.057  (minimum confidence: insufficient bars fallback)
#
# Profile-ordering preservation: all profiles in the same pool see the SAME
# regime confidence → SAME penalty → relative utility differences are preserved.
#
# max utility impact: w_breach × MAX = 0.25 × 0.08 = 0.02  (small but meaningful)
_BREACH_CONF_THRESHOLD = 0.70   # confidence at or above this → no inflation
_BREACH_CONF_MAX       = 0.08   # maximum additive penalty (confidence approaching 0)


def _tvl_crowding_factor(tvl_usd: float) -> float:
    """
    TVL-based crowding discount for fee capture efficiency (P2.2.2 Phase 2).

    Larger TVL pools attract more LP competition at the same tick bands,
    reducing actual fee capture below the width-only Phase 1 estimate.

    Formula:
        tvl_factor = clip(1.0 - TVL_DECAY × max(0, log10(tvl_usd / TVL_REF)),
                          TVL_FLOOR, 1.0)

    Properties:
        tvl_usd <= TVL_REF  → factor = 1.0  (no boost for small/medium pools)
        tvl_usd = $10M      → factor = 0.95  (5% additional discount)
        tvl_usd = $100M     → factor = 0.90  (10% additional discount)
        tvl_usd = $1B+      → factor = 0.85  (floor, 15% maximum discount)

    Returns 1.0 on invalid input (fail-safe: TVL unavailable → no TVL adjustment).
    Does NOT affect il_score / breach_risk / rebalance_cost (no double-counting).
    """
    if tvl_usd <= 0:
        return 1.0   # fail-safe: treat missing TVL as neutral
    # Only apply discount above TVL_REF; cap at 1.0 (no boost for small pools)
    decades_above_ref = max(0.0, math.log10(tvl_usd / _TVL_REF))
    factor = 1.0 - _TVL_DECAY * decades_above_ref
    return max(_TVL_FLOOR, min(1.0, factor))


def _competitive_capture_ratio(vol_tvl_ratio: float) -> float:
    """
    Competitive fee capture ratio (P2.3.3).

    High-activity pools attract automated market-makers and bots that concentrate
    liquidity in extremely tight ranges, capturing a disproportionate share of fees.
    This reduces the realistic fee capture of a normal LP below the width+TVL baseline.

    Proxy: vol_tvl_ratio = 24h_volume / tvl_usd (from MarketQualityResult).

    Formula:
        capture = FLOOR + (1-FLOOR) × (1 - σ(STEEPNESS × (vol_tvl - NEUTRAL)))

    Anchors:
        vol_tvl ≤ 0  → 1.0  (fail-safe: treat as quiet pool)
        vol_tvl = 0.1 → 0.94 (quiet pool, passive LPs)
        vol_tvl = 1.0 → 0.85 (neutral midpoint, moderate competition)
        vol_tvl = 3.0 → 0.71 (very hot pool, near floor)
        vol_tvl = 5.0+ → 0.70 (floor enforced)

    Orthogonal to P2.2.2:
        crowding_factor = f(width, TVL)  — structural LP density
        capture_ratio   = f(vol/TVL)     — activity-driven LP competition
        Same width + same TVL → identical crowding; different vol/TVL → different capture.

    Does NOT affect il_score / breach_risk / rebalance_cost.
    """
    if vol_tvl_ratio <= 0:
        return 1.0   # fail-safe: missing or zero vol/TVL → no competitive adjustment
    x = _CAPTURE_STEEPNESS * (vol_tvl_ratio - _CAPTURE_NEUTRAL)
    x = max(-500.0, min(500.0, x))   # clamp against fp overflow
    sigmoid = 1.0 / (1.0 + math.exp(-x))
    # Inverted sigmoid: high vol_tvl → high sigmoid → low capture
    return _CAPTURE_FLOOR + (1.0 - _CAPTURE_FLOOR) * (1.0 - sigmoid)


def _regime_uncertainty_breach_penalty(confidence: float) -> float:
    """
    Additive breach risk inflation when regime confidence is low (P2.4.1).

    Low confidence means the regime classifier is unsure about vol/drift, making
    the breach probability estimate less reliable.  The penalty shifts breach_risk
    conservatively upward without changing the relative ordering between profiles
    (all candidates in the same pool receive the same penalty).

    Formula:
        shortfall = clip((THRESHOLD - confidence) / THRESHOLD, 0, 1)
        penalty   = shortfall × _BREACH_CONF_MAX

    Returns 0.0 for confidence >= _BREACH_CONF_THRESHOLD (no penalty for reliable regimes).
    Returns 0.0 for confidence <= 0 (degenerate input guard — treated as maximally uncertain
    but formula self-clamps to MAX anyway).

    No overlap with:
      - uncertainty_penalty in final_utility (that uses evidence QUANTITY)
      - jump_penalty in _breach_risk (that uses chaotic RETURNS)
      - _apply_confidence_calibration (that scales the DISPLAY output post-selection)
    """
    if confidence >= _BREACH_CONF_THRESHOLD:
        return 0.0
    shortfall = (_BREACH_CONF_THRESHOLD - max(confidence, 0.0)) / _BREACH_CONF_THRESHOLD
    return round(min(_BREACH_CONF_MAX, shortfall * _BREACH_CONF_MAX), 4)


# ── Analytical terminal OOR helpers (P2.2.3) ───────────────────────────────

def _normal_cdf(x: float) -> float:
    """Standard normal CDF via math.erf — no scipy dependency."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _analytical_terminal_oor(
    realized_vol_annual: float,
    drift_slope: float,
    lower_price: float,
    upper_price: float,
    entry_price: float,
    horizon_bars: int,
    bars_per_year: float = _BARS_PER_YEAR,
) -> float | None:
    """
    GBM terminal OOR probability: P(price_T outside [lower, upper]) starting
    from entry_price after horizon_bars steps.

    IMPORTANT — this is a *terminal OOR proxy*, NOT a first-exit / barrier-hit
    probability.  True breach probability (first-passage) >= this value.
    Use only as a conservative lower-bound correction for young pools where
    replay history is too short to estimate OOR directly.

    Returns None for degenerate inputs.  Callers MUST NOT substitute 0.0 for
    None — that would silently understate breach risk on fresh/infant pools.
    """
    if entry_price <= 0 or lower_price <= 0 or upper_price <= 0:
        return None
    if lower_price >= upper_price or horizon_bars < 1:
        return None

    sigma_bar = realized_vol_annual / math.sqrt(bars_per_year)
    sigma_T   = sigma_bar * math.sqrt(horizon_bars)
    mu_T      = drift_slope * horizon_bars

    if sigma_T < 1e-9:
        # Deterministic: check if terminal drift stays inside range
        log_lo = math.log(lower_price / entry_price)
        log_hi = math.log(upper_price / entry_price)
        return 0.0 if log_lo <= mu_T <= log_hi else 1.0

    z_upper = (math.log(upper_price / entry_price) - mu_T) / sigma_T
    z_lower = (math.log(lower_price / entry_price) - mu_T) / sigma_T
    p_in_range = _normal_cdf(z_upper) - _normal_cdf(z_lower)
    return max(0.0, min(1.0, 1.0 - p_in_range))


def compute_blended_oor(
    backtest: BacktestResult,
    regime: RegimeResult,
    candidate: CandidateRange,
    horizon_bars: int,
    replay_weight: float = 1.0,
    entry_price: float | None = None,
) -> float:
    """
    Blend replay OOR history with analytical terminal OOR proxy (P2.2.3).

    replay_weight = 1.0  →  pure replay (mature pool, default — backward-compat)
    replay_weight = 0.0  →  pure analytical (infant pool)

    When analytical inputs are invalid (returns None), falls back to replay_oor
    rather than 0.0 — invalid inputs must never deflate breach risk.

    entry_price: current spot price (preferred).  Falls back to candidate.center_price.
    """
    replay_oor = 1.0 - backtest.in_range_time_ratio

    # Reference price: spot first, center_price as fallback
    ref_price = entry_price if (entry_price and entry_price > 0) else candidate.center_price

    analytical = _analytical_terminal_oor(
        realized_vol_annual=regime.realized_vol,
        drift_slope=regime.drift_slope,
        lower_price=candidate.lower_price,
        upper_price=candidate.upper_price,
        entry_price=ref_price,
        horizon_bars=horizon_bars,
    )

    if analytical is None:
        # Invalid inputs: return replay_oor — do not pull breach risk toward 0
        return replay_oor

    blended = replay_weight * replay_oor + (1.0 - replay_weight) * analytical
    return round(max(0.0, min(1.0, blended)), 4)


def _fee_capture_efficiency(width_pct: float) -> float:
    """
    Crowding discount for fee capture efficiency (P2.2.2 Phase 1).

    Accounts for competing LPs concentrating at the same narrow tick band,
    which reduces actual fee capture vs the theoretical maximum.

    Uses a sigmoid curve parameterised by module constants:
        capture = FLOOR + (1 - FLOOR) × σ(STEEPNESS × (width_pct - INFLECTION))

    Returns a multiplier in [_CROWDING_FLOOR, 1.0].
    Applied only to fee_score — does NOT affect IL, breach, or rebalance terms.

    Phase 2 upgrade path: incorporate pool TVL and vol/TVL as secondary modifiers
    (larger TVL → more LP competition → lower capture for same width).
    """
    if width_pct <= 0:
        return _CROWDING_FLOOR
    x = _CROWDING_STEEPNESS * (max(width_pct, 0.0) - _CROWDING_INFLECTION)
    # Clamp x to avoid fp overflow on extreme inputs (|x| > 500 → sigmoid ≈ 0 or 1)
    x = max(-500.0, min(500.0, x))
    sigmoid = 1.0 / (1.0 + math.exp(-x))
    return _CROWDING_FLOOR + (1.0 - _CROWDING_FLOOR) * sigmoid


def _fee_score(backtest: BacktestResult, horizon_bars: int, bars_per_year: float = 8760.0) -> float:
    """
    Normalise cumulative_fee_proxy to an annualised yield (APR) and map to [0, 1].
    """
    if horizon_bars <= 0:
        return 0.0
    # Annualise
    annualisation = bars_per_year / horizon_bars
    apr_proxy = backtest.cumulative_fee_proxy * annualisation
    return min(apr_proxy / _FEE_SCORE_FULL_APR, 1.0)


def _il_score(backtest: BacktestResult, il_result: ILRiskResult) -> float:
    """
    ILScore = blend of backtest IL cost and heuristic IL risk score.
    Both are normalised to [0, 1]; higher means worse IL.
    """
    # Backtest IL: il_cost_proxy is negative (loss fraction), cap at -1.0
    backtest_il = min(abs(backtest.il_cost_proxy), 1.0)    # 0–1
    heuristic_il = il_result.score / 100.0                  # 0–1

    # 60% weight on backtest (more empirical), 40% on heuristic
    return round(0.60 * backtest_il + 0.40 * heuristic_il, 4)


def _breach_risk(
    backtest: BacktestResult,
    regime: RegimeResult,
    candidate: CandidateRange,
    horizon_bars: int,
    replay_weight: float = 1.0,
    entry_price: float | None = None,
) -> float:
    """
    BreachRisk = blended OOR (replay + analytical terminal proxy) weighted by
    breach count and jump ratio.

    The oor_ratio component uses compute_blended_oor() so that scoring and
    the breach_probability display field share the same underlying signal (P2.2.3).
    jump_penalty is kept separate to avoid double-counting with the analytical
    formula (which deliberately excludes jump adjustment).
    """
    oor_ratio = compute_blended_oor(
        backtest, regime, candidate, horizon_bars,
        replay_weight=replay_weight, entry_price=entry_price,
    )
    breach_penalty = min(backtest.breach_count / 10.0, 1.0)   # normalise to 0–1
    jump_penalty   = min(regime.jump_ratio * 5.0, 1.0)         # 0–1

    # Blend: 60% blended OOR + 25% breach count + 15% jump ratio
    return round(0.60 * oor_ratio + 0.25 * breach_penalty + 0.15 * jump_penalty, 4)


def _rebalance_cost(
    backtest: BacktestResult,
    tvl_usd: float,
    chain_index: str = "",
    position_usd: float = 0.0,
) -> float:
    """
    RebalanceCost normalised to [0, 1] for the utility penalty term.

    When chain_index and position_usd are provided, uses the execution_cost module
    (gas + slippage components, chain-aware).  A total cost of 5% or more of
    capital → full penalty (1.0).

    Fallback (position_usd ≤ 0): legacy flat-rate formula
    (rebalance_count × 0.1%), backward-compatible.
    """
    if position_usd > 0:
        from app.services.execution_cost import total_execution_cost_fraction
        cost_fraction = total_execution_cost_fraction(
            backtest.rebalance_count, chain_index, position_usd, tvl_usd,
        )
    else:
        # Legacy: flat 0.1% per rebalance
        cost_fraction = backtest.rebalance_count * _REBALANCE_COST_PER_EVENT
    # Normalise: 5% total cost → full penalty
    return min(cost_fraction / 0.05, 1.0)


def _quality_penalty(quality: MarketQualityResult, regime: RegimeResult) -> float:
    """
    QualityPenalty = blend of wash score and jump ratio.
    """
    wash_pen = quality.wash_score                       # already 0–1
    jump_pen = min(regime.jump_ratio * 5.0, 1.0)       # 0–1
    return round(0.70 * wash_pen + 0.30 * jump_pen, 4)


def _build_reasons(
    candidate: CandidateRange,
    backtest: BacktestResult,
    fee_s: float,
    il_s: float,
    breach_r: float,
) -> list[str]:
    reasons = []
    if fee_s >= 0.6:
        reasons.append(f"Strong fee capture: {fee_s * 100:.0f}% APR proxy")
    elif fee_s >= 0.3:
        reasons.append(f"Moderate fee capture: {fee_s * 100:.0f}% APR proxy")
    if backtest.in_range_time_ratio >= 0.75:
        reasons.append(f"High in-range time: {backtest.in_range_time_ratio * 100:.0f}%")
    if candidate.range_type == "volume_profile":
        reasons.append("Range centred on historical volume POC")
    if candidate.range_type == "trend_biased":
        reasons.append("Asymmetric range adjusted for market trend")
    if il_s <= 0.25:
        reasons.append("Low estimated IL at this width")
    return reasons[:4]


def _build_risk_flags(
    candidate: CandidateRange,
    backtest: BacktestResult,
    il_result: ILRiskResult,
    quality: MarketQualityResult,
    breach_r: float,
) -> list[str]:
    flags = []
    if backtest.breach_count >= 3:
        flags.append(f"High breach count: {backtest.breach_count} exits in backtest")
    if backtest.in_range_time_ratio < 0.50:
        flags.append(f"Low in-range time: {backtest.in_range_time_ratio * 100:.0f}%")
    if il_result.level == "high":
        flags.append(f"High IL risk: {il_result.main_driver}")
    if quality.wash_risk != "low":
        flags.append(f"Market quality {quality.wash_risk}: {', '.join(quality.flags[:2])}")
    if candidate.width_pct < 0.02:
        flags.append("Very narrow range: high breach sensitivity")
    return flags[:4]


def score_candidate(
    candidate: CandidateRange,
    backtest: BacktestResult,
    il_result: ILRiskResult,
    quality_result: MarketQualityResult,
    regime_result: RegimeResult,
    tvl_usd: float = 1.0,
    horizon_bars: int = 48,
    weights: dict[str, float] | None = None,
    replay_weight: float = 1.0,
    entry_price: float | None = None,
    chain_index: str = "",
    position_usd: float = 0.0,
) -> ScoredRange:
    """
    Compute utility score for a single candidate range.

    Parameters
    ----------
    candidate      CandidateRange to score.
    backtest       BacktestResult from range_backtester.
    il_result      ILRiskResult from il_risk module.
    quality_result MarketQualityResult from market_quality module.
    regime_result  RegimeResult from regime_detector.
    tvl_usd        Pool TVL in USD.
    horizon_bars   Number of bars replayed.
    weights        Scoring weights dict. Uses DEFAULT_WEIGHTS if None.

    Returns
    -------
    ScoredRange with all component scores and final utility_score.
    """
    w = weights or DEFAULT_WEIGHTS

    fee_raw = _fee_score(backtest, horizon_bars)
    # P2.2.2: structural crowding — width (Phase 1) × TVL (Phase 2)
    width_factor = _fee_capture_efficiency(candidate.width_pct)
    tvl_factor   = _tvl_crowding_factor(tvl_usd)
    crowding     = width_factor * tvl_factor
    # P2.3.3: competitive capture — vol/TVL activity-driven LP competition
    capture      = _competitive_capture_ratio(quality_result.vol_tvl_ratio)
    fee_s        = round(fee_raw * crowding * capture, 4)
    logger.debug(
        "scorer: fee_raw=%.4f width=%.3f tvl=%.3f crowding=%.3f capture=%.3f fee_s=%.4f",
        fee_raw, width_factor, tvl_factor, crowding, capture, fee_s,
    )
    il_s = _il_score(backtest, il_result)
    breach_r = _breach_risk(
        backtest, regime_result, candidate, horizon_bars,
        replay_weight=replay_weight, entry_price=entry_price,
    )
    # P2.4.1: inflate breach risk when regime classifier confidence is low
    conf_penalty = _regime_uncertainty_breach_penalty(regime_result.confidence)
    if conf_penalty > 0:
        breach_r = round(min(1.0, breach_r + conf_penalty), 4)
        logger.debug(
            "scorer: regime conf=%.3f → breach_penalty=%.4f breach_r=%.4f",
            regime_result.confidence, conf_penalty, breach_r,
        )
    rebalance_c = _rebalance_cost(backtest, tvl_usd, chain_index, position_usd)
    quality_p = _quality_penalty(quality_result, regime_result)

    utility = (
        w.get("fee", 0.30) * fee_s
        - w.get("il", 0.25) * il_s
        - w.get("breach", 0.25) * breach_r
        - w.get("rebalance", 0.10) * rebalance_c
        - w.get("quality", 0.10) * quality_p
    )
    utility = round(max(0.0, min(utility, 1.0)), 4)

    reasons = _build_reasons(candidate, backtest, fee_s, il_s, breach_r)
    risk_flags = _build_risk_flags(candidate, backtest, il_result, quality_result, breach_r)

    return ScoredRange(
        candidate=candidate,
        backtest=backtest,
        fee_score=round(fee_s, 4),
        il_score=round(il_s, 4),
        breach_risk=round(breach_r, 4),
        rebalance_cost=round(rebalance_c, 4),
        quality_penalty=round(quality_p, 4),
        utility_score=utility,
        reasons=reasons,
        risk_flags=risk_flags,
        capture_ratio=round(capture, 4),
    )


def score_all_candidates(
    candidates: list[CandidateRange],
    backtests: list[BacktestResult],
    il_result: ILRiskResult,
    quality_result: MarketQualityResult,
    regime_result: RegimeResult,
    tvl_usd: float = 1.0,
    horizon_bars: int = 48,
    weights: dict[str, float] | None = None,
    replay_weight: float = 1.0,
    entry_price: float | None = None,
    chain_index: str = "",
    position_usd: float = 0.0,
) -> list[ScoredRange]:
    """
    Score all candidates and return sorted by utility_score descending.
    """
    scored = [
        score_candidate(
            c, b, il_result, quality_result, regime_result,
            tvl_usd, horizon_bars, weights,
            replay_weight=replay_weight, entry_price=entry_price,
            chain_index=chain_index, position_usd=position_usd,
        )
        for c, b in zip(candidates, backtests)
    ]
    scored.sort(key=lambda s: s.utility_score, reverse=True)
    return scored


def select_profiles(scored: list[ScoredRange]) -> dict[str, ScoredRange | None]:
    """
    Select three profiles from the scored candidate list by role:
      aggressive   → narrowest width_pct (highest capital efficiency)
      conservative → widest width_pct (lowest breach risk)
      balanced     → highest utility_score that is not the most extreme on either end

    Returns dict with keys "balanced", "aggressive", "conservative".
    All may be None if the scored list is empty.
    """
    if not scored:
        return {"balanced": None, "aggressive": None, "conservative": None}

    # Aggressive: narrowest range (excluding defensive family when possible)
    non_defensive = [s for s in scored if s.candidate.range_type != "defensive"]
    pool_for_roles = non_defensive if len(non_defensive) >= 2 else scored
    aggressive = min(pool_for_roles, key=lambda s: s.candidate.width_pct)

    # Conservative: widest range
    conservative = max(scored, key=lambda s: s.candidate.width_pct)

    # Balanced: highest utility_score; prefer not to duplicate aggressive or conservative
    by_utility = sorted(scored, key=lambda s: s.utility_score, reverse=True)
    balanced = by_utility[0]
    if (balanced is aggressive or balanced is conservative) and len(by_utility) > 1:
        # Pick highest utility that's not the extreme choices
        for candidate in by_utility:
            if candidate is not aggressive and candidate is not conservative:
                balanced = candidate
                break
        else:
            # All candidates are the same as aggressive/conservative; keep top utility
            balanced = by_utility[0]

    return {"balanced": balanced, "aggressive": aggressive, "conservative": conservative}
