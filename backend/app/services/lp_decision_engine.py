"""
LP Decision Engine: integrates all sub-modules to produce a final LP decision per pair.

Pipeline per pool:
  1. market_quality  → detect wash trading / thin depth
  2. lp_eligibility  → hard gate (must pass all checks)
  3. il_risk         → estimate impermanent loss risk
  4. holding_period  → recommend strategy type + duration
  5. net_lp_score    → composite 0.0–1.0 (only if eligible)

Net LP Score weights:
  fee_income_score    30%   (fee APR proxy)
  vol_stability_score 20%   (stable daily turnover)
  market_quality_score 20%  (1 - wash_score)
  il_penalty          20%   (il_risk.score / 100, subtracted)
  tail_risk_penalty   10%   (security + pool age, subtracted)

Redis outputs:
  lp_decision:{chain}:{pool_addr}     Hash   TTL 3600s
  lp_opportunities:{chain}            Sorted Set (score = net_lp_score)  TTL 3600s
"""
from __future__ import annotations
import json
import logging
import time
import uuid

from app.core.redis_client import get_redis
from app.models.schemas import (
    AlertType,
    EligibilityResult,
    HoldingPeriodResult,
    ILRiskResult,
    LPDecision,
    MarketQualityResult,
)
from app.services.il_risk import estimate_il_risk
from app.services.holding_period import recommend_holding_period
from app.services.market_quality import detect_market_quality

logger = logging.getLogger(__name__)

# Alert threshold: only emit LP_OPPORTUNITY if net_lp_score >= this
_LP_OPPORTUNITY_THRESHOLD = 0.55


# ── Eligibility hard gate ──────────────────────────────────────────────

def check_lp_eligibility(
    tvl_usd: float,
    volume_24h: float,
    pool_age_days: float,
    risk_level: int,
    is_mint: bool,
    is_freeze: bool,
    wash_score: float,
    wash_risk: str,
    is_primary: bool,
    quote_type: str,
) -> EligibilityResult:
    """
    Hard gate: a pool must pass ALL checks to be eligible for LP.
    Returns EligibilityResult with lists of failed_reasons and warnings.
    """
    failed: list[str] = []
    warnings: list[str] = []

    # Basic liquidity
    if tvl_usd < 50_000:
        failed.append(f"TVL too low: ${tvl_usd:,.0f} (min $50k)")
    if volume_24h < 10_000:
        failed.append(f"24h volume too low: ${volume_24h:,.0f} (min $10k)")

    # Security
    if risk_level >= 4:
        failed.append(f"Security risk level {risk_level}/5 (max 3)")
    elif risk_level >= 3:
        warnings.append(f"Elevated security risk: level {risk_level}/5")
    if is_mint:
        failed.append("isMint=True: unlimited token minting possible")
    if is_freeze:
        failed.append("isFreeze=True: account freezing possible")

    # Market quality
    if wash_score >= 0.60:
        failed.append(f"Wash trading risk too high (score={wash_score:.2f})")
    elif wash_score >= 0.35:
        warnings.append(f"Elevated wash trading suspicion (score={wash_score:.2f})")

    # Pool maturity
    if pool_age_days < 0.5:
        failed.append(f"Pool too new: {pool_age_days * 24:.1f}h old (min 12h)")
    elif pool_age_days < 1.0:
        warnings.append(f"Pool very young: {pool_age_days * 24:.1f}h old")

    # Structure
    if not is_primary:
        warnings.append("Not the primary pool for this token (lower liquidity/volume)")
    if quote_type == "alt":
        warnings.append("Alt-token quote: unknown IL correlation, higher risk")

    return EligibilityResult(
        eligible=len(failed) == 0,
        failed_reasons=failed,
        warnings=warnings,
    )


# ── Net LP Score ───────────────────────────────────────────────────────

def _compute_net_lp_score(
    tvl_usd: float,
    volume_24h: float,
    fee_rate: float,
    il_risk: ILRiskResult,
    market_quality: MarketQualityResult,
    pool_age_days: float,
    risk_level: int,
    smart_money_buy_ratio: float,
) -> tuple[float, float, float]:
    """
    Returns (net_lp_score, fee_income_score, data_confidence).

    fee_income_score: how good is the fee APR (0–1).
    data_confidence:  how much we trust the inputs (0–1).
    """
    # Fee income: APR = vol24h × fee_rate × 365 / TVL; 50% APR = full score
    fee_apr = (volume_24h * fee_rate * 365.0) / max(tvl_usd, 1.0)
    fee_income_score = min(fee_apr / 0.50, 1.0)

    # Volume stability: stable daily turnover 0.3×–5× TVL scores well
    vol_tvl = volume_24h / max(tvl_usd, 1.0)
    if 0.3 <= vol_tvl <= 5.0:
        vol_stability = 1.0
    elif vol_tvl < 0.3:
        vol_stability = vol_tvl / 0.3
    else:
        vol_stability = max(0.0, 1.0 - (vol_tvl - 5.0) / 15.0)

    # Market quality
    mq_score = 1.0 - market_quality.wash_score

    # IL penalty (0–1)
    il_penalty = il_risk.score / 100.0

    # Tail risk: security level + pool age
    tail_risk = 0.0
    if risk_level >= 3:
        tail_risk += 0.40
    elif risk_level >= 2:
        tail_risk += 0.10
    if pool_age_days < 3:
        tail_risk += 0.30
    elif pool_age_days < 7:
        tail_risk += 0.10
    tail_risk = min(tail_risk, 1.0)

    # Smart money micro-bonus
    sm_bonus = 0.05 if smart_money_buy_ratio > 0.6 else 0.0

    net_score = (
        fee_income_score * 0.30
        + vol_stability  * 0.20
        + mq_score       * 0.20
        - il_penalty     * 0.20
        - tail_risk      * 0.10
        + sm_bonus
    )
    net_score = round(max(0.0, min(net_score, 1.0)), 4)

    # Data confidence: improves with pool age and volume scale
    confidence = 0.50
    if pool_age_days >= 7:
        confidence += 0.15
    if pool_age_days >= 30:
        confidence += 0.10
    if volume_24h > 100_000:
        confidence += 0.10
    if tvl_usd > 500_000:
        confidence += 0.10
    confidence = round(min(confidence, 0.95), 3)

    return net_score, round(fee_income_score, 3), confidence


# ── Main entry point ───────────────────────────────────────────────────

async def run_lp_decision_for_pair(
    chain_index: str,
    pool_address: str,
    token_address: str,
    token_symbol: str,
    quote_type: str,
    quote_symbol: str,
    protocol: str,
    fee_rate: float,
    tvl_usd: float,
    volume_24h: float,
    volume_1h: float,
    pool_age_days: float,
    is_primary: bool,
    # From token snapshot
    price_change_24h: float,
    price_change_4h: float,
    price_change_1h: float,
    z_score: float,
    # From safety
    risk_level: int,
    is_mint: bool,
    is_freeze: bool,
    # From trades
    smart_money_buys: int,
    smart_money_sells: int,
    trade_count_1h: int,
) -> LPDecision:
    """
    Run the full LP decision pipeline for a single pair/pool.
    Persists results to Redis and emits an alert if warranted.
    """
    now = int(time.time())
    pair_label = f"{token_symbol}/{quote_symbol}" if quote_symbol else token_symbol

    # Smart money buy ratio
    sm_total = smart_money_buys + smart_money_sells
    sm_buy_ratio = smart_money_buys / sm_total if sm_total > 0 else 0.5

    # Approximate buy/sell split from price direction (Phase 1 proxy)
    if price_change_1h > 2.0:
        buy_vol = volume_1h * 0.65
        sell_vol = volume_1h * 0.35
    elif price_change_1h < -2.0:
        buy_vol = volume_1h * 0.35
        sell_vol = volume_1h * 0.65
    else:
        buy_vol = volume_1h * 0.50
        sell_vol = volume_1h * 0.50

    # 1. Market quality
    mq = detect_market_quality(
        pool_address=pool_address,
        tvl_usd=tvl_usd,
        volume_24h=volume_24h,
        volume_1h=volume_1h,
        buy_volume=buy_vol,
        sell_volume=sell_vol,
        trade_count_1h=trade_count_1h,
    )

    # 2. Eligibility
    elig = check_lp_eligibility(
        tvl_usd=tvl_usd,
        volume_24h=volume_24h,
        pool_age_days=pool_age_days,
        risk_level=risk_level,
        is_mint=is_mint,
        is_freeze=is_freeze,
        wash_score=mq.wash_score,
        wash_risk=mq.wash_risk,
        is_primary=is_primary,
        quote_type=quote_type,
    )

    # 3. IL risk (compute regardless of eligibility, useful for context)
    il = estimate_il_risk(
        quote_type=quote_type,
        price_change_24h=price_change_24h,
        price_change_4h=price_change_4h,
        price_change_1h=price_change_1h,
        z_score=z_score,
        pool_age_days=pool_age_days,
        protocol=protocol,
    )

    # 4. Holding period recommendation
    holding = recommend_holding_period(
        z_score=z_score,
        price_change_24h=price_change_24h,
        price_change_4h=price_change_4h,
        tvl_usd=tvl_usd,
        volume_24h=volume_24h,
        pool_age_days=pool_age_days,
        fee_rate=fee_rate,
        il_risk=il,
        market_quality=mq,
        smart_money_buy_ratio=sm_buy_ratio,
    )

    # 5. Net LP score (only meaningful when eligible)
    if elig.eligible:
        net_score, fee_income_score, confidence = _compute_net_lp_score(
            tvl_usd=tvl_usd,
            volume_24h=volume_24h,
            fee_rate=fee_rate,
            il_risk=il,
            market_quality=mq,
            pool_age_days=pool_age_days,
            risk_level=risk_level,
            smart_money_buy_ratio=sm_buy_ratio,
        )
        confidence = round((confidence + holding.confidence) / 2.0, 3)
    else:
        net_score = 0.0
        fee_income_score = 0.0
        confidence = 0.0

    # Compile reasons and risks
    main_reasons = holding.reasons[:3] if elig.eligible else []
    main_risks: list[str] = []
    if il.level == "high":
        main_risks.append(f"High IL risk: {il.main_driver}")
    if mq.wash_risk == "high":
        main_risks.append(f"Wash trading risk: {', '.join(mq.flags)}")
    main_risks.extend(elig.failed_reasons)
    main_risks.extend(elig.warnings)
    main_risks = main_risks[:5]

    decision = LPDecision(
        chain_index=chain_index,
        pool_address=pool_address,
        token_address=token_address,
        token_symbol=token_symbol,
        pair_label=pair_label,
        protocol=protocol,
        fee_rate=fee_rate,
        tvl_usd=tvl_usd,
        eligible=elig.eligible,
        failed_reasons=elig.failed_reasons,
        warnings=elig.warnings,
        strategy_type=holding.strategy_type if elig.eligible else "",
        suggested_holding=holding.suggested_range if elig.eligible else "",
        net_lp_score=net_score,
        main_reasons=main_reasons,
        main_risks=main_risks,
        confidence=confidence,
        il_risk_level=il.level,
        wash_risk=mq.wash_risk,
        market_quality_score=round(1.0 - mq.wash_score, 3),
        fee_income_score=fee_income_score,
        timestamp=now,
    )

    await _persist(decision)

    if elig.eligible and net_score >= _LP_OPPORTUNITY_THRESHOLD:
        await _emit_alert(decision)

    logger.info(
        "LP decision chain=%s pool=%.8s eligible=%s score=%.3f strategy=%s",
        chain_index, pool_address, elig.eligible, net_score, holding.strategy_type,
    )
    return decision


# ── Persistence helpers ────────────────────────────────────────────────

async def _persist(d: LPDecision) -> None:
    redis = await get_redis()
    pipe = redis.pipeline()

    key = f"lp_decision:{d.chain_index}:{d.pool_address}"
    pipe.hset(key, mapping={
        "token_address":        d.token_address,
        "token_symbol":         d.token_symbol,
        "pair_label":           d.pair_label,
        "protocol":             d.protocol,
        "fee_rate":             str(d.fee_rate),
        "tvl_usd":              str(d.tvl_usd),
        "eligible":             "1" if d.eligible else "0",
        "failed_reasons":       json.dumps(d.failed_reasons),
        "warnings":             json.dumps(d.warnings),
        "strategy_type":        d.strategy_type,
        "suggested_holding":    d.suggested_holding,
        "net_lp_score":         str(d.net_lp_score),
        "main_reasons":         json.dumps(d.main_reasons),
        "main_risks":           json.dumps(d.main_risks),
        "confidence":           str(d.confidence),
        "il_risk_level":        d.il_risk_level,
        "wash_risk":            d.wash_risk,
        "market_quality_score": str(d.market_quality_score),
        "fee_income_score":     str(d.fee_income_score),
        "timestamp":            str(d.timestamp),
    })
    pipe.expire(key, 3600)

    opp_key = f"lp_opportunities:{d.chain_index}"
    if d.eligible:
        pipe.zadd(opp_key, {d.pool_address: d.net_lp_score})
        pipe.expire(opp_key, 3600)
    else:
        pipe.zrem(opp_key, d.pool_address)

    await pipe.execute()


async def _emit_alert(d: LPDecision) -> None:
    redis = await get_redis()
    alert_type = (
        AlertType.lp_opportunity.value
        if d.net_lp_score >= 0.65
        else AlertType.lp_risk_warn.value
    )
    alert = {
        "id":            str(uuid.uuid4()),
        "chain_index":   d.chain_index,
        "token_address": d.token_address,
        "token_symbol":  d.token_symbol,
        "alert_type":    alert_type,
        "pool_address":  d.pool_address,
        "pair_label":    d.pair_label,
        "protocol":      d.protocol,
        "strategy_type": d.strategy_type,
        "suggested_holding": d.suggested_holding,
        "net_lp_score":  d.net_lp_score,
        "il_risk_level": d.il_risk_level,
        "wash_risk":     d.wash_risk,
        "main_reasons":  d.main_reasons,
        "main_risks":    d.main_risks,
        "timestamp":     d.timestamp,
    }
    await redis.lpush("alerts", json.dumps(alert))
    await redis.ltrim("alerts", 0, 499)
