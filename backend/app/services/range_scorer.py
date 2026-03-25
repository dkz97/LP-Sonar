"""
Range Scorer: computes utility score for each candidate range.

Score(r) = w_fee * FeeScore(r)
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
"""
from __future__ import annotations
import logging
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


def _breach_risk(backtest: BacktestResult, regime: RegimeResult) -> float:
    """
    BreachRisk = (1 - in_range_time_ratio) weighted by breach count and jump ratio.
    """
    oor_ratio = 1.0 - backtest.in_range_time_ratio            # 0–1
    breach_penalty = min(backtest.breach_count / 10.0, 1.0)   # normalise to 0–1
    jump_penalty = min(regime.jump_ratio * 5.0, 1.0)           # 0–1

    # Blend: 60% OOR ratio + 25% breach count + 15% jump ratio
    return round(0.60 * oor_ratio + 0.25 * breach_penalty + 0.15 * jump_penalty, 4)


def _rebalance_cost(backtest: BacktestResult, tvl_usd: float) -> float:
    """
    RebalanceCost = estimated_rebalance_cost / TVL, normalised to [0, 1].
    Each rebalance costs ~0.1% of capital (gas + slippage proxy).
    """
    n_rebalances = backtest.rebalance_count
    cost_fraction = n_rebalances * _REBALANCE_COST_PER_EVENT
    # A cost_fraction of 5% or more → full penalty
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

    fee_s = _fee_score(backtest, horizon_bars)
    il_s = _il_score(backtest, il_result)
    breach_r = _breach_risk(backtest, regime_result)
    rebalance_c = _rebalance_cost(backtest, tvl_usd)
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
) -> list[ScoredRange]:
    """
    Score all candidates and return sorted by utility_score descending.
    """
    scored = [
        score_candidate(c, b, il_result, quality_result, regime_result, tvl_usd, horizon_bars, weights)
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
