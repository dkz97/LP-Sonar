"""
Holding Period Recommender: classifies LP opportunity into event / tactical / structural.

event:      2h–12h   High volatility, high Z-Score, short-lived opportunity.
tactical:   1d–3d    Active volume, not yet proven stable; harvest fees then exit.
structural: 7d+      Mature pool, stable volume, deep TVL; long-term market making.

Decision logic: score both "structural" and "event" signals, pick the dominant one,
default to "tactical".
"""
from __future__ import annotations

from app.models.schemas import HoldingPeriodResult, ILRiskResult, MarketQualityResult


def recommend_holding_period(
    z_score: float,
    price_change_24h: float,
    price_change_4h: float,
    tvl_usd: float,
    volume_24h: float,
    pool_age_days: float,
    fee_rate: float,
    il_risk: ILRiskResult,
    market_quality: MarketQualityResult,
    smart_money_buy_ratio: float,
) -> HoldingPeriodResult:
    """
    Recommend a holding period strategy.

    Parameters
    ----------
    z_score               Current volume Z-Score from hot_monitor.
    price_change_24h      24h price change percentage.
    price_change_4h       4h price change percentage.
    tvl_usd               Pool TVL in USD.
    volume_24h            Pool 24h volume in USD.
    pool_age_days         Pool age in days.
    fee_rate              Pool fee rate as decimal (e.g. 0.003 for 0.3%).
    il_risk               Result from il_risk.estimate_il_risk().
    market_quality        Result from market_quality.detect_market_quality().
    smart_money_buy_ratio Fraction of smart money trades that are buys (0.0–1.0).
    """
    reasons: list[str] = []

    # ── Derived metrics ────────────────────────────────────────────────
    # Annualised fee APR: vol24h × fee_rate × 365 / TVL
    fee_apr = (volume_24h * fee_rate * 365.0) / max(tvl_usd, 1.0)
    daily_fee_pct = volume_24h * fee_rate / max(tvl_usd, 1.0)

    # ── Structural signals ─────────────────────────────────────────────
    structural_signals = [
        pool_age_days >= 30,                             # mature pool
        pool_age_days >= 7,                              # at least a week old
        tvl_usd >= 500_000,                              # deep liquidity
        market_quality.vol_tvl_ratio < 3.0,              # stable daily turnover
        market_quality.wash_risk == "low",               # clean market
        il_risk.level == "low",                          # low IL drag
        fee_apr > 0.20,                                  # > 20% APR
    ]
    structural_score = sum(structural_signals)

    # ── Event signals ──────────────────────────────────────────────────
    event_signals = [
        z_score >= 3.0,                                  # extreme volume spike
        abs(price_change_4h) >= 10.0,                   # big 4h move
        pool_age_days < 3 and volume_24h > 500_000,      # brand-new pool with huge vol
        smart_money_buy_ratio > 0.6 and z_score >= 2.0, # smart money piling in
    ]
    event_score = sum(event_signals)

    # ── Strategy selection ────────────────────────────────────────────
    if event_score >= 2 or (z_score >= 4.0 and abs(price_change_4h) >= 5.0):
        strategy = "event"
        suggested = "2h-12h"
        confidence = min(0.60 + event_score * 0.08, 0.85)

        if z_score >= 3.0:
            reasons.append(f"Z-Score {z_score:.1f} → extreme volume spike, short window")
        if abs(price_change_4h) >= 10:
            reasons.append(f"4h price move {price_change_4h:+.1f}% → event-driven")
        if smart_money_buy_ratio > 0.6:
            reasons.append(f"Smart Money buy ratio {smart_money_buy_ratio:.0%} → momentum")
        if il_risk.level == "high":
            reasons.append("High IL risk: short holding limits downside exposure")

    elif structural_score >= 4:
        strategy = "structural"
        suggested = "7d+"
        confidence = min(0.50 + structural_score * 0.06, 0.90)

        if pool_age_days >= 30:
            reasons.append(f"Pool age {pool_age_days:.0f}d → established, low rug risk")
        if fee_apr > 0.20:
            reasons.append(f"Fee APR ~{fee_apr * 100:.0f}% → sustainable fee income")
        if tvl_usd >= 500_000:
            reasons.append(f"TVL ${tvl_usd / 1e6:.2f}M → deep depth, low slippage")
        if market_quality.wash_risk == "low":
            reasons.append("Clean market quality → fees come from genuine volume")
        if il_risk.level == "low":
            reasons.append("Low IL risk → fee income likely exceeds IL drag")

    else:
        strategy = "tactical"
        suggested = "1d-3d"
        confidence = 0.55

        reasons.append(f"Z-Score {z_score:.1f}, volume active but not extreme")
        if not (pool_age_days >= 7):
            reasons.append(f"Pool age {pool_age_days:.1f}d → needs more time to prove stability")
        if daily_fee_pct > 0.005:
            reasons.append(f"Daily fee rate ~{daily_fee_pct * 100:.2f}% of TVL → short-term harvest viable")
        if il_risk.level == "medium":
            reasons.append("Medium IL risk: 1–3 day window balances fee capture vs IL")

    return HoldingPeriodResult(
        strategy_type=strategy,
        suggested_range=suggested,
        reasons=reasons[:4],
        confidence=round(confidence, 3),
    )
