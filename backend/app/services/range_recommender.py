"""
Range Recommender: orchestrates the full LP range recommendation pipeline.

Flow:
  1. Fetch pool state (DexScreener by pair address)
  2. Fetch OHLCV history (OKX candles API, 1H bars, 30 days)
  3. Layer A: Risk gating (reuse existing modules)
  4. Layer B: Regime detection
  5. Layer C: Candidate range generation
  6. Replay + Scoring
  7. Profile selection (conservative / balanced / aggressive)
  8. Cache result in Redis (TTL 5 min)

Redis cache key: lp_range:{chain}:{pool_address}
"""
from __future__ import annotations
import json
import logging
import math
import time
from typing import Optional

import httpx

from app.core.config import settings
from app.core.redis_client import get_redis
from app.models.schemas import (
    ILRiskResult,
    RangeProfile,
    RangeRecommendation,
    ScoredRange,
)
from app.services.il_risk import estimate_il_risk
from app.services.holding_period import recommend_holding_period
from app.services.lp_decision_engine import check_lp_eligibility
from app.services.market_quality import detect_market_quality
from app.services.history_sufficiency import (
    SufficiencyResult,
    assess,
    age_based_width_floor,
    fee_persistence_factor,
)
from app.services.range_backtester import backtest_all_candidates
from app.services.range_scenario import (
    compute_all_scenario_pnl,
    compute_scenario_utility,
)
from app.services.range_generator import (
    generate_candidates,
    infer_pool_type,
    infer_v3_tick_spacing,
)
from app.services.range_scorer import DEFAULT_WEIGHTS, score_all_candidates, select_profiles
from app.services.regime_detector import detect_regime

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────

_CACHE_TTL_SECONDS = 300          # 5 minutes
_OHLCV_BARS_LIMIT = 300           # 300 bars of 1h = 12.5 days (OKX API max per call)
_DEFAULT_HORIZON_HOURS = 48.0     # default holding horizon

# Horizon mapping from holding strategy
_HORIZON_MAP: dict[str, float] = {
    "event":      12.0,
    "tactical":   48.0,
    "structural": 168.0,   # 7 days
}

# Chain index → DexScreener chainId
_DS_CHAIN: dict[str, str] = {
    "1":    "ethereum",
    "56":   "bsc",
    "8453": "base",
    "501":  "solana",
    "137":  "polygon_pos",
}


# ── Data fetching helpers ───────────────────────────────────────────────

async def _fetch_pool_state(chain_index: str, pool_address: str) -> dict | None:
    """
    Fetch pool state from DexScreener by pair address.
    Returns a normalised dict or None on failure.
    """
    chain_id = _DS_CHAIN.get(chain_index)
    if not chain_id:
        logger.warning("recommend: unsupported chain_index=%s", chain_index)
        return None

    url = f"{settings.dexscreener_api_url}/latest/dex/pairs/{chain_id}/{pool_address}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("DexScreener pool fetch failed pool=%.8s: %s", pool_address, e)
        return None

    pairs = data.get("pairs") or []
    if not pairs:
        return None

    pair = pairs[0]
    vol = pair.get("volume") or {}
    liq = pair.get("liquidity") or {}
    txns = pair.get("txns") or {}
    price_change = pair.get("priceChange") or {}
    base_token = pair.get("baseToken") or {}
    quote_token = pair.get("quoteToken") or {}

    txns_h1 = txns.get("h1") or {}
    trade_count_1h = int(_safe_float(txns_h1.get("buys")) + _safe_float(txns_h1.get("sells")))

    create_ms = pair.get("pairCreatedAt") or 0
    try:
        create_ts = int(create_ms) // 1000
    except (TypeError, ValueError):
        create_ts = 0

    pool_age_days = (time.time() - create_ts) / 86400.0 if create_ts > 0 else 0.0

    dex_id = (pair.get("dexId") or "").lower()
    protocol = _infer_protocol_name(dex_id)

    # Quote type classification
    quote_symbol = (quote_token.get("symbol") or "").upper()
    quote_type = _classify_quote_type(quote_symbol)

    # Fee rate: DexScreener doesn't expose fee; infer from protocol
    fee_rate = _infer_fee_rate(dex_id, pair)

    return {
        "pool_address":      pool_address,
        "chain_index":       chain_index,
        "protocol":          protocol,
        "dex_id":            dex_id,
        "fee_rate":          fee_rate,
        "tvl_usd":           _safe_float(liq.get("usd")),
        "volume_24h":        _safe_float(vol.get("h24")),
        "volume_1h":         _safe_float(vol.get("h1")),
        "trade_count_1h":    trade_count_1h,
        "pool_age_days":     pool_age_days,
        "current_price":     _safe_float(pair.get("priceUsd") or pair.get("priceNative")),
        "price_change_24h":  _safe_float(price_change.get("h24")),
        "price_change_4h":   _safe_float(price_change.get("h6")),   # best available proxy
        "price_change_1h":   _safe_float(price_change.get("h1")),
        "base_token_address": base_token.get("address", ""),
        "base_token_symbol":  base_token.get("symbol", ""),
        "quote_token_symbol": quote_symbol,
        "quote_type":         quote_type,
        # buy/sell split approximation (DexScreener doesn't provide volume split)
        "buy_volume":        _safe_float(vol.get("h1")) * 0.5,
        "sell_volume":       _safe_float(vol.get("h1")) * 0.5,
    }


async def _fetch_ohlcv(
    chain_index: str,
    token_address: str,
    limit: int = _OHLCV_BARS_LIMIT,
    bar: str = "1H",
) -> list[dict]:
    """
    Fetch OHLCV bars from OKX candles API.

    Parameters
    ----------
    bar     Bar interval string accepted by OKX: "1H", "5m", "1m", etc.

    Returns list of {"time", "open", "high", "low", "close", "volume"} oldest → newest.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://web3.okx.com/api/v6/dex/market/candles",
                headers={"OK-ACCESS-KEY": settings.okx_access_key},
                params={
                    "chainIndex":           chain_index,
                    "tokenContractAddress": token_address,
                    "bar":                  bar,
                    "limit":                str(limit),
                },
            )
            if resp.status_code != 200:
                logger.warning("OKX candles %s HTTP %s token=%.8s", bar, resp.status_code, token_address)
                return []
            data = resp.json()
    except Exception as e:
        logger.warning("OKX candles %s error token=%.8s: %s", bar, token_address, e)
        return []

    raw: list = data.get("data", [])
    try:
        bars = [
            {
                "time":   int(row[0]) // 1000,
                "open":   float(row[1]),
                "high":   float(row[2]),
                "low":    float(row[3]),
                "close":  float(row[4]),
                "volume": float(row[6]),  # volUsd
            }
            for row in reversed(raw)  # OKX returns newest first
        ]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        logger.warning("OKX candles %s parse error: %s", bar, e)
        return []

    return bars


# ── Profile conversion ──────────────────────────────────────────────────

def _scored_to_profile(
    scored: ScoredRange,
    horizon_bars: int,
    scenario_pnl: dict[str, float] | None = None,
    bars_per_year: float = 8760.0,
    # Phase 1.5 additions
    shrunk_fee_apr: Optional[float] = None,
    scenario_utility: Optional[float] = None,
    final_utility: Optional[float] = None,
    young_pool_adjustments: list[str] | None = None,
) -> RangeProfile:
    """Convert a ScoredRange to a RangeProfile for the API response."""
    c = scored.candidate
    b = scored.backtest

    # Fee APR proxy: annualise the fee_score
    # fee_score = min(apr / FEE_SCORE_FULL_APR, 1.0) → apr = fee_score * FULL_APR
    expected_fee_apr = scored.fee_score * 3.0   # 3.0 = _FEE_SCORE_FULL_APR

    # IL cost: make positive fraction for display
    expected_il_cost = abs(b.il_cost_proxy)

    # Breach probability ≈ OOR ratio
    breach_probability = 1.0 - b.in_range_time_ratio

    # Rebalance frequency per 7 days
    if horizon_bars > 0:
        rebalance_per_7d = b.rebalance_count * (7 * 24) / horizon_bars
    else:
        rebalance_per_7d = 0.0

    # Use shrunk fee APR for display if provided (young pool)
    display_fee_apr = shrunk_fee_apr if shrunk_fee_apr is not None else expected_fee_apr

    return RangeProfile(
        lower_price=c.lower_price,
        upper_price=c.upper_price,
        lower_tick=c.lower_tick,
        upper_tick=c.upper_tick,
        width_pct=round(c.width_pct * 100, 2),
        range_type=c.range_type,
        expected_fee_apr=round(display_fee_apr, 4),
        expected_il_cost=round(expected_il_cost, 4),
        breach_probability=round(breach_probability, 4),
        expected_rebalance_frequency=round(rebalance_per_7d, 2),
        expected_net_pnl=round(b.realized_net_pnl_proxy, 6),
        utility_score=scored.utility_score,
        reasons=scored.reasons,
        risk_flags=scored.risk_flags,
        scenario_pnl=scenario_pnl or {},
        # Phase 1.5 fields
        shrunk_fee_apr=round(shrunk_fee_apr, 4) if shrunk_fee_apr is not None else None,
        replay_utility=scored.utility_score,
        scenario_utility=scenario_utility,
        final_utility=final_utility,
        young_pool_adjustments=young_pool_adjustments or [],
    )


# ── Cache helpers ───────────────────────────────────────────────────────

def _cache_key(chain_index: str, pool_address: str) -> str:
    return f"lp_range:{chain_index}:{pool_address}"


async def _load_cached(chain_index: str, pool_address: str) -> RangeRecommendation | None:
    try:
        redis = await get_redis()
        raw = await redis.get(_cache_key(chain_index, pool_address))
        if raw:
            return RangeRecommendation.model_validate(json.loads(raw))
    except Exception as e:
        logger.debug("cache load failed: %s", e)
    return None


async def _store_cached(chain_index: str, pool_address: str, result: RangeRecommendation) -> None:
    try:
        redis = await get_redis()
        await redis.setex(
            _cache_key(chain_index, pool_address),
            _CACHE_TTL_SECONDS,
            result.model_dump_json(),
        )
    except Exception as e:
        logger.debug("cache store failed: %s", e)


# ── No-recommendation helpers ───────────────────────────────────────────

def _no_recommendation(
    reason: str,
    regime: str = "unknown",
    sufficiency: Optional["SufficiencyResult"] = None,
) -> RangeRecommendation:
    return RangeRecommendation(
        is_recommended=False,
        recommendation_confidence=0.0,
        regime=regime,
        holding_horizon="",
        profiles={"conservative": None, "balanced": None, "aggressive": None},
        pool_quality_summary="Not recommended",
        no_recommendation_reason=reason,
        timestamp=time.time(),
        data_freshness="live",
        # Phase 1.5 fields: carry sufficiency info even for rejections
        history_tier=sufficiency.history_tier if sufficiency else "unknown",
        recommendation_mode=sufficiency.recommendation_mode if sufficiency else "observe_only",
        actionability=sufficiency.actionability if sufficiency else "watch_only",
        pool_age_hours=sufficiency.pool_age_hours if sufficiency else 0.0,
        effective_evidence_score=sufficiency.effective_evidence_score if sufficiency else 0.0,
        data_quality_score=sufficiency.data_quality_score if sufficiency else 0.0,
        uncertainty_penalty=sufficiency.uncertainty_penalty if sufficiency else 0.0,
        replay_weight=sufficiency.replay_weight if sufficiency else 0.0,
        scenario_weight=sufficiency.scenario_weight if sufficiency else 1.0,
    )


def _select_profiles_by_final_utility(
    scored: list[ScoredRange],
    final_utilities: dict[int, float],
) -> dict[str, ScoredRange | None]:
    """
    Select conservative / balanced / aggressive profiles using pre-computed
    final_utility scores (blended replay + scenario - uncertainty_penalty).

    Mirrors the logic of range_scorer.select_profiles() but sorts by
    final_utility instead of utility_score.
    """
    if not scored:
        return {"balanced": None, "aggressive": None, "conservative": None}

    non_defensive = [s for s in scored if s.candidate.range_type != "defensive"]
    pool_for_roles = non_defensive if len(non_defensive) >= 2 else scored

    aggressive  = min(pool_for_roles, key=lambda s: s.candidate.width_pct)
    conservative = max(scored, key=lambda s: s.candidate.width_pct)

    by_utility = sorted(
        scored,
        key=lambda s: final_utilities.get(id(s), s.utility_score),
        reverse=True,
    )
    balanced = by_utility[0]
    if (balanced is aggressive or balanced is conservative) and len(by_utility) > 1:
        for candidate in by_utility:
            if candidate is not aggressive and candidate is not conservative:
                balanced = candidate
                break
        else:
            balanced = by_utility[0]

    return {"balanced": balanced, "aggressive": aggressive, "conservative": conservative}


# ── Main entry point ────────────────────────────────────────────────────

async def recommend_range(
    pool_address: str,
    chain_index: str,
    scoring_weights: dict[str, float] | None = None,
) -> RangeRecommendation:
    """
    Run the full LP range recommendation pipeline for a single pool.

    Parameters
    ----------
    pool_address     Pool contract address.
    chain_index      Internal chain ID (e.g. "501", "8453", "56").
    scoring_weights  Optional override for utility scoring weights.

    Returns
    -------
    RangeRecommendation with profiles and metadata.
    """
    # ── Cache check ──────────────────────────────────────────────────────
    cached = await _load_cached(chain_index, pool_address)
    if cached is not None:
        logger.debug("range_recommender: cache hit pool=%.8s", pool_address)
        return cached

    # ── 1. Fetch pool state ──────────────────────────────────────────────
    pool = await _fetch_pool_state(chain_index, pool_address)
    if pool is None:
        result = _no_recommendation("Pool data unavailable: not found on DexScreener")
        await _store_cached(chain_index, pool_address, result)
        return result

    current_price = pool["current_price"]
    if current_price <= 0:
        result = _no_recommendation("Pool price unavailable or zero")
        await _store_cached(chain_index, pool_address, result)
        return result

    token_address  = pool["base_token_address"]
    pool_age_hours = pool["pool_age_days"] * 24.0

    # ── 2. Fetch OHLCV — primary 1H pass ────────────────────────────────
    ohlcv_1h = await _fetch_ohlcv(chain_index, token_address, bar="1H")

    # Quick first-pass sufficiency assessment to decide whether to fetch finer bars
    sufficiency_1h = assess(
        pool_age_hours=pool_age_hours,
        bars_1h=len(ohlcv_1h),
        source_quality="token_level",
    )

    # Fetch finer-resolution bars for growing/fresh/infant pools
    ohlcv_5m: list[dict] = []
    ohlcv_1m: list[dict] = []
    if sufficiency_1h.history_tier in ("growing", "fresh", "infant"):
        ohlcv_5m = await _fetch_ohlcv(chain_index, token_address, bar="5m", limit=288)
        if sufficiency_1h.history_tier in ("fresh", "infant"):
            ohlcv_1m = await _fetch_ohlcv(chain_index, token_address, bar="1m", limit=120)

    # Full sufficiency assessment with all available data
    sufficiency = assess(
        pool_age_hours=pool_age_hours,
        bars_1h=len(ohlcv_1h),
        bars_5m=len(ohlcv_5m),
        bars_1m=len(ohlcv_1m),
        source_quality="token_level",
    )

    # Select active bars and matching annualisation factor
    if sufficiency.preferred_resolution == "5m" and ohlcv_5m:
        active_bars  = ohlcv_5m
        bars_per_year = 105_120.0   # 5m bars per year (12 × 24 × 365)
    elif sufficiency.preferred_resolution == "1m" and ohlcv_1m:
        active_bars  = ohlcv_1m
        bars_per_year = 525_960.0   # 1m bars per year
    else:
        active_bars  = ohlcv_1h
        bars_per_year = 8_760.0

    # Hard reject only if no price data at all
    if not active_bars:
        result = _no_recommendation(
            f"No price history available for this pool "
            f"(age {pool_age_hours:.1f}h, 1H bars: {len(ohlcv_1h)})"
        )
        await _store_cached(chain_index, pool_address, result)
        return result

    logger.info(
        "range_recommender: pool=%.8s tier=%s mode=%s age=%.1fh bars=%d(%s) "
        "evidence=%.2f replay_w=%.2f",
        pool_address, sufficiency.history_tier, sufficiency.recommendation_mode,
        pool_age_hours, len(active_bars), sufficiency.preferred_resolution,
        sufficiency.effective_evidence_score, sufficiency.replay_weight,
    )

    # ── 3. Layer A: Risk gating ──────────────────────────────────────────
    price_1h = pool["price_change_1h"]
    if price_1h > 2.0:
        buy_vol  = pool["volume_1h"] * 0.65
        sell_vol = pool["volume_1h"] * 0.35
    elif price_1h < -2.0:
        buy_vol  = pool["volume_1h"] * 0.35
        sell_vol = pool["volume_1h"] * 0.65
    else:
        buy_vol  = pool["volume_1h"] * 0.50
        sell_vol = pool["volume_1h"] * 0.50

    quality = detect_market_quality(
        pool_address=pool_address,
        tvl_usd=pool["tvl_usd"],
        volume_24h=pool["volume_24h"],
        volume_1h=pool["volume_1h"],
        buy_volume=buy_vol,
        sell_volume=sell_vol,
        trade_count_1h=pool["trade_count_1h"],
    )

    elig = check_lp_eligibility(
        tvl_usd=pool["tvl_usd"],
        volume_24h=pool["volume_24h"],
        pool_age_days=pool["pool_age_days"],
        risk_level=2,
        is_mint=False,
        is_freeze=False,
        wash_score=quality.wash_score,
        wash_risk=quality.wash_risk,
        is_primary=True,
        quote_type=pool["quote_type"],
    )

    il_result = estimate_il_risk(
        quote_type=pool["quote_type"],
        price_change_24h=pool["price_change_24h"],
        price_change_4h=pool["price_change_4h"],
        price_change_1h=pool["price_change_1h"],
        z_score=0.0,
        pool_age_days=pool["pool_age_days"],
        protocol=pool["protocol"],
    )

    holding = recommend_holding_period(
        z_score=0.0,
        price_change_24h=pool["price_change_24h"],
        price_change_4h=pool["price_change_4h"],
        tvl_usd=pool["tvl_usd"],
        volume_24h=pool["volume_24h"],
        pool_age_days=pool["pool_age_days"],
        fee_rate=pool["fee_rate"],
        il_risk=il_result,
        market_quality=quality,
        smart_money_buy_ratio=0.5,
    )

    if not elig.eligible:
        reason = "; ".join(elig.failed_reasons[:3])
        result = _no_recommendation(f"Pool not eligible: {reason}", sufficiency=sufficiency)
        await _store_cached(chain_index, pool_address, result)
        return result

    # ── 4. Layer B: Regime detection ─────────────────────────────────────
    regime_result = detect_regime(active_bars, bars_per_year=bars_per_year)

    if regime_result.regime == "chaotic" and regime_result.confidence < 0.50:
        logger.info("range_recommender: chaotic market (low-confidence), using defensive ranges")

    # ── 5. Layer C: Candidate range generation ───────────────────────────
    pool_type = infer_pool_type(pool["protocol"])
    fee_pct   = pool["fee_rate"] * 100.0

    step = infer_v3_tick_spacing(fee_pct) if pool_type == "v3" else 20

    horizon_hours = _HORIZON_MAP.get(holding.strategy_type, _DEFAULT_HORIZON_HOURS)
    # For young pools, cap horizon to available history; backtester handles slicing
    horizon_bars = min(
        max(int(horizon_hours), 12),   # at least 12 bars
        max(len(active_bars), 1),       # but never more than available
    ) if sufficiency.history_tier in ("fresh", "infant") else max(int(horizon_hours), 24)

    fresh_mode      = sufficiency.history_tier in ("fresh", "infant")
    width_floor_pct = age_based_width_floor(pool_age_hours)

    candidates = generate_candidates(
        current_price=current_price,
        pool_type=pool_type,
        step=step,
        regime_result=regime_result,
        ohlcv_bars=active_bars,
        horizon_hours=horizon_hours,
        fee_pct=fee_pct,
        min_width_floor_pct=width_floor_pct,
        fresh_mode=fresh_mode,
    )

    if not candidates:
        result = _no_recommendation(
            "Could not generate candidate ranges",
            sufficiency=sufficiency,
        )
        await _store_cached(chain_index, pool_address, result)
        return result

    # ── 6. Replay ────────────────────────────────────────────────────────
    backtests = backtest_all_candidates(
        ohlcv_bars=active_bars,
        candidates=candidates,
        horizon_bars=horizon_bars,
        fee_rate=pool["fee_rate"],
        tvl_usd=pool["tvl_usd"],
    )

    weights = scoring_weights or DEFAULT_WEIGHTS
    scored  = score_all_candidates(
        candidates=candidates,
        backtests=backtests,
        il_result=il_result,
        quality_result=quality,
        regime_result=regime_result,
        tvl_usd=pool["tvl_usd"],
        horizon_bars=horizon_bars,
        weights=weights,
    )

    # ── 7. Scenario PnL ──────────────────────────────────────────────────
    use_launch = sufficiency.history_tier in ("fresh", "infant")
    scenario_pnl_list = compute_all_scenario_pnl(
        candidates=candidates,
        current_price=current_price,
        realized_vol_annual=regime_result.realized_vol,
        ohlcv_bars=active_bars,
        horizon_bars=horizon_bars,
        fee_rate=pool["fee_rate"],
        tvl_usd=pool["tvl_usd"],
        use_launch_scenarios=use_launch,
    )

    tick_to_scenario: dict[tuple[int, int], dict[str, float]] = {
        (candidates[i].lower_tick, candidates[i].upper_tick): scenario_pnl_list[i]
        for i in range(len(candidates))
    }

    def _get_scenario(s: ScoredRange) -> dict[str, float]:
        return tick_to_scenario.get((s.candidate.lower_tick, s.candidate.upper_tick), {})

    # ── 8. Blended utility + profile selection ───────────────────────────
    # FinalUtility = w_replay * replay_util + w_scenario * scenario_util - penalty
    rw = sufficiency.replay_weight
    sw = sufficiency.scenario_weight
    penalty = sufficiency.uncertainty_penalty

    final_utilities: dict[int, float] = {}
    scenario_utilities: dict[int, float] = {}
    for s in scored:
        sc_pnl = _get_scenario(s)
        sc_util = compute_scenario_utility(sc_pnl)
        scenario_utilities[id(s)] = sc_util
        fu = rw * s.utility_score + sw * sc_util - penalty
        final_utilities[id(s)] = round(max(0.0, min(1.0, fu)), 4)

    # Profile selection by final_utility (re-implementing select_profiles logic
    # to use final_utility instead of utility_score for sorting)
    profiles_raw = _select_profiles_by_final_utility(scored, final_utilities)

    # Fee persistence shrinkage — only applied for young pools (fresh/infant/growing).
    # Mature pools may have non-zero jump_ratio but their fee estimates are reliable.
    is_young_pool = sufficiency.history_tier in ("fresh", "infant", "growing")
    persist_factor = fee_persistence_factor(
        pool_age_hours=pool_age_hours,
        jump_ratio=regime_result.jump_ratio,
    ) if is_young_pool else 1.0

    # Young-pool adjustment notes
    yp_adjustments: list[str] = []
    if fresh_mode:
        yp_adjustments.append("Trend-biased candidates skipped (short history)")
    if width_floor_pct > 0:
        yp_adjustments.append(f"Min range width enforced: {width_floor_pct*100:.0f}%")
    if persist_factor < 1.0:
        yp_adjustments.append(f"Fee APR shrunk by persistence factor {persist_factor:.2f}")
    if use_launch:
        yp_adjustments.append("Launch-mode scenarios used")

    # Build profiles
    profiles: dict[str, RangeProfile | None] = {}
    for pname, s in profiles_raw.items():
        if s is None:
            profiles[pname] = None
            continue
        sc_pnl  = _get_scenario(s)
        sc_util = scenario_utilities.get(id(s))
        fu      = final_utilities.get(id(s), s.utility_score)
        raw_fee_apr = s.fee_score * 3.0
        shrunk  = round(raw_fee_apr * persist_factor, 4) if persist_factor < 1.0 else None
        profiles[pname] = _scored_to_profile(
            s, horizon_bars, sc_pnl,
            shrunk_fee_apr=shrunk,
            scenario_utility=sc_util,
            final_utility=fu,
            young_pool_adjustments=yp_adjustments if yp_adjustments else None,
        )

    best       = profiles_raw.get("balanced")
    confidence = final_utilities.get(id(best), best.utility_score if best else 0.0) if best else 0.0

    # ── 9. Alternative ranges ─────────────────────────────────────────────
    selected_ticks = {
        (s.candidate.lower_tick, s.candidate.upper_tick)
        for s in profiles_raw.values()
        if s is not None
    }
    alt_ranges: list[RangeProfile] = []
    for s in sorted(scored, key=lambda x: final_utilities.get(id(x), 0), reverse=True):
        key = (s.candidate.lower_tick, s.candidate.upper_tick)
        if key not in selected_ticks and len(alt_ranges) < 5:
            sc_pnl = _get_scenario(s)
            sc_util = scenario_utilities.get(id(s))
            fu = final_utilities.get(id(s), s.utility_score)
            raw_fee_apr = s.fee_score * 3.0
            shrunk = round(raw_fee_apr * persist_factor, 4) if persist_factor < 1.0 else None
            alt_ranges.append(_scored_to_profile(
                s, horizon_bars, sc_pnl,
                shrunk_fee_apr=shrunk,
                scenario_utility=sc_util,
                final_utility=fu,
                young_pool_adjustments=yp_adjustments if yp_adjustments else None,
            ))
            selected_ticks.add(key)

    # Quality summary
    quality_parts = []
    if quality.wash_risk != "low":
        quality_parts.append(f"wash risk: {quality.wash_risk}")
    if quality.flags:
        quality_parts.append(", ".join(quality.flags[:2]))
    quality_summary = f"TVL ${pool['tvl_usd']/1e3:.0f}k | 24h vol ${pool['volume_24h']/1e3:.0f}k"
    if quality_parts:
        quality_summary += f" | {'; '.join(quality_parts)}"

    result = RangeRecommendation(
        is_recommended=True,
        recommendation_confidence=round(confidence, 4),
        regime=regime_result.regime,
        holding_horizon=holding.suggested_range,
        profiles=profiles,
        pool_quality_summary=quality_summary,
        no_recommendation_reason=None,
        alternative_ranges=alt_ranges,
        timestamp=time.time(),
        data_freshness="live",
        # Phase 1.5 fields
        history_tier=sufficiency.history_tier,
        recommendation_mode=sufficiency.recommendation_mode,
        actionability=sufficiency.actionability,
        pool_age_hours=sufficiency.pool_age_hours,
        effective_evidence_score=sufficiency.effective_evidence_score,
        data_quality_score=sufficiency.data_quality_score,
        uncertainty_penalty=sufficiency.uncertainty_penalty,
        replay_weight=sufficiency.replay_weight,
        scenario_weight=sufficiency.scenario_weight,
    )

    await _store_cached(chain_index, pool_address, result)

    logger.info(
        "range_recommender: pool=%.8s chain=%s tier=%s regime=%s confidence=%.3f "
        "actionability=%s evidence=%.2f",
        pool_address, chain_index, sufficiency.history_tier,
        regime_result.regime, confidence, sufficiency.actionability,
        sufficiency.effective_evidence_score,
    )
    return result


# ── Utility helpers ─────────────────────────────────────────────────────

def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val or default)
    except (TypeError, ValueError):
        return default


def _infer_protocol_name(dex_id: str) -> str:
    _NAMES = {
        "uniswap-v3":     "Uniswap V3",
        "pancakeswap-v3": "PancakeSwap V3",
        "meteora-dlmm":   "Meteora DLMM",
        "meteora":        "Meteora",
        "raydium-clmm":   "Raydium CLMM",
        "orca":           "Orca Whirlpool",
        "sushiswap":      "SushiSwap",
        "baseswap":       "BaseSwap V3",
        "aerodrome":      "Aerodrome V3",
    }
    return _NAMES.get(dex_id, dex_id or "Unknown")


def _classify_quote_type(quote_symbol: str) -> str:
    _STABLES = {"USDC", "USDT", "DAI", "BUSD", "USDE", "USDBC", "USDS", "TUSD", "FDUSD"}
    _WRAPPED = {"WETH", "WBTC", "WBNB", "WSOL", "SOL", "ETH", "BNB", "WMATIC", "WAVAX"}
    s = quote_symbol.upper()
    if s in _STABLES:
        return "stable"
    if s in _WRAPPED:
        return "wrapped_native"
    return "alt"


def _infer_fee_rate(dex_id: str, pair: dict) -> float:
    """Infer fee rate from dex_id or fall back to 0.3%."""
    _DEFAULT_FEES: dict[str, float] = {
        "uniswap-v3":     0.003,    # most common tier; actual varies
        "pancakeswap-v3": 0.0025,
        "meteora-dlmm":   0.003,
        "meteora":        0.0025,
        "raydium-clmm":   0.0025,
        "orca":           0.003,
        "aerodrome":      0.0005,
        "uniswap-v2":     0.003,
        "pancakeswap-v2": 0.0025,
    }
    return _DEFAULT_FEES.get(dex_id, 0.003)
