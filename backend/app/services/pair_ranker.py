"""
Primary Pair Selector: ranks all pools of a token and identifies the primary pool.

Scoring weights:
  TVL          30%  — depth / stability proxy
  Volume 24h   25%  — fee income proxy
  Quote type   20%  — stable > wrapped_native > alt
  Pool age     15%  — maturity
  Protocol     10%  — protocol trust

Called from focus_analyzer after pool data is decomposed into pair_snapshots.
"""
from __future__ import annotations
import logging
import math

from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)

# Quote asset quality weights
QUOTE_TYPE_WEIGHT: dict[str, float] = {
    "stable": 1.0,
    "wrapped_native": 0.85,
    "alt": 0.4,
}

# Known protocol trust scores (longer keys matched first for precision)
PROTOCOL_TRUST: dict[str, float] = {
    "uniswap v3":     1.0,
    "uniswap v2":     1.0,
    "uniswap":        1.0,
    "raydium clmm":   1.0,    # Raydium concentrated liquidity
    "raydium v4":     0.95,   # Raydium standard AMM
    "raydium amm":    0.90,   # Raydium legacy AMM
    "raydium":        1.0,
    "orca whirlpool": 0.95,   # Orca concentrated liquidity
    "whirlpool":      0.95,
    "orca":           0.95,
    "meteora dlmm":   0.92,   # Meteora DLMM (more mature than standard)
    "meteora":        0.80,
    "pancakeswap v3": 1.0,
    "pancakeswap":    1.0,
    "sushiswap":      0.85,
    "curve":          0.95,
    "balancer":       0.85,
    "lifinity":       0.80,
    "phoenix":        0.75,
}


def _score_pool(snap: dict) -> float:
    """Compute 0.0~1.0 composite score for a pool snapshot dict."""
    try:
        tvl = float(snap.get("tvl_usd", 0) or 0)
        vol24h = float(snap.get("volume_24h", 0) or 0)
        age_days = float(snap.get("pool_age_days", 0) or 0)
        quote_type = snap.get("quote_type", "alt")
        protocol = (snap.get("protocol", "") or "").lower()
    except (ValueError, TypeError):
        return 0.0

    # TVL: log10-normalized, 1M USD = score 1.0
    tvl_score = min(math.log10(max(tvl, 1)) / 6.0, 1.0)

    # Volume: log10-normalized, 5M USD/day = score 1.0
    vol_score = min(
        math.log10(max(vol24h, 1)) / math.log10(5_000_000),
        1.0,
    )

    # Quote quality
    quote_score = QUOTE_TYPE_WEIGHT.get(quote_type, 0.3)

    # Age: 30 days = full score
    age_score = min(age_days / 30.0, 1.0)

    # Protocol trust: match longest key first (most specific wins)
    proto_score = 0.5
    for key in sorted(PROTOCOL_TRUST, key=len, reverse=True):
        if key in protocol:
            proto_score = PROTOCOL_TRUST[key]
            break

    return round(
        tvl_score   * 0.30
        + vol_score   * 0.25
        + quote_score * 0.20
        + age_score   * 0.15
        + proto_score * 0.10,
        4,
    )


async def rank_pairs_for_token(
    chain_index: str,
    token_address: str,
    pair_snapshots: list[dict],
) -> str | None:
    """
    Score and rank all pool snapshots for one token.
    Marks the highest-scoring pool as primary in Redis.
    Returns the primary pool address, or None if no snapshots.
    """
    if not pair_snapshots:
        return None

    redis = await get_redis()

    scored = sorted(
        [(snap, _score_pool(snap)) for snap in pair_snapshots],
        key=lambda x: x[1],
        reverse=True,
    )

    primary_snap, primary_score = scored[0]
    primary_addr = primary_snap.get("pool_address", "")
    if not primary_addr:
        return None

    pipe = redis.pipeline()
    for snap, score in scored:
        pool_addr = snap.get("pool_address", "")
        if not pool_addr:
            continue
        snap_key = f"pair_snapshot:{chain_index}:{pool_addr}"
        pipe.hset(snap_key, mapping={
            "is_primary": "1" if pool_addr == primary_addr else "0",
            "pair_rank_score": str(score),
        })

    # token → primary pool mapping (TTL matches pair_snapshot)
    pipe.set(f"primary_pool:{chain_index}:{token_address}", primary_addr, ex=600)

    await pipe.execute()

    logger.debug(
        "Pair ranking chain=%s token=%.8s primary=%.8s score=%.3f pools=%d",
        chain_index, token_address, primary_addr, primary_score, len(scored),
    )
    return primary_addr
