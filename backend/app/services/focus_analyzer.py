"""
Focus Layer Analyzer: Runs every 60 seconds.

For each token in focus:{chainIndex}:
1. Fetch LP pool info (TVL, fee, pool addresses)
2. Fetch safety info (LP burn, mint authority, freeze authority, risk level)
3. Fetch recent Smart Money trades
4. Update snapshot with enriched data
5. Emit SAFETY_RISK alert if high-risk indicators found
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
import uuid

from app.core.config import settings
from app.core.redis_client import get_redis
from app.models.schemas import AlertType, Layer
from app.services import mcp_client
from app.services.dexscreener_client import get_pools_by_token as dexscreener_get_pools
from app.services.solana_dex_client import get_meteora_pools, get_raydium_pools
from app.services.pair_ranker import rank_pairs_for_token
from app.services.lp_decision_engine import run_lp_decision_for_pair

logger = logging.getLogger(__name__)

# Risk level thresholds from OKX API (0=undefined,1=low,2=medium,3=medium-high,4=high,5=very high)
SAFETY_RISK_THRESHOLD = 3

# Quote asset type classification per chain (lowercase addresses)
_QUOTE_TYPE_MAP: dict[str, str] = {
    # Solana
    "so11111111111111111111111111111111111111112":   "wrapped_native",  # SOL
    "epjfwdd5aufqssqem2qn1xzybapc8g4weggkzwytdt1v": "stable",          # USDC
    "es9vmfrzacermjfrf4h2fyd4kconky11mcce8benwnyb":  "stable",          # USDT
    # Base
    "0x4200000000000000000000000000000000000006":    "wrapped_native",  # WETH
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913":   "stable",          # USDC
    # BSC
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c":   "wrapped_native",  # WBNB
    "0x55d398326f99059ff775485246999027b3197955":   "stable",          # USDT
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d":   "stable",          # USDC
}


async def _decompose_pools_to_pairs(
    chain_index: str,
    token_address: str,
    token_symbol: str,
    pools: list[dict],
    now: int,
) -> None:
    """
    Decompose a token's pool list into per-pool pair_snapshot hashes in Redis,
    then run pair_ranker to identify the primary pool.
    Triggered from _analyze_token after fetching liquidity data.
    """
    redis = await get_redis()
    pipe = redis.pipeline()
    snapshots: list[dict] = []

    for pool in pools:
        pool_addr = (
            pool.get("poolContractAddress")
            or pool.get("poolAddress")
            or ""
        )
        if not pool_addr:
            continue

        quote_addr = (pool.get("quoteTokenContractAddress") or "").lower()
        quote_sym = pool.get("quoteTokenSymbol", "")

        # fee_rate: OKX may return as 0.003 (decimal) or 30 (bps) depending on pool type
        try:
            fee_raw = float(pool.get("feeRate", 0) or 0)
            fee_rate = fee_raw if fee_raw < 1 else fee_raw / 10_000
        except (ValueError, TypeError):
            fee_rate = 0.0

        try:
            tvl = float(pool.get("liquidity", 0) or 0)
        except (ValueError, TypeError):
            tvl = 0.0

        # Pool-level volume (OKX field names vary by endpoint)
        try:
            vol24h = float(
                pool.get("volumeUsd24H")
                or pool.get("volume24H")
                or 0
            )
            vol1h = float(
                pool.get("volumeUsd1H")
                or pool.get("volume1H")
                or 0
            )
        except (ValueError, TypeError):
            vol24h = vol1h = 0.0

        # Pool age
        try:
            create_ts = int(pool.get("createTime", 0) or 0)
            age_days = (now - create_ts) / 86_400 if create_ts > 0 else 0.0
        except (ValueError, TypeError):
            age_days = 0.0

        quote_type = _QUOTE_TYPE_MAP.get(quote_addr, "alt")

        snap = {
            "chain_index":    chain_index,
            "pool_address":   pool_addr,
            "token_address":  token_address,
            "token_symbol":   token_symbol,
            "quote_address":  quote_addr,
            "quote_symbol":   quote_sym,
            "quote_type":     quote_type,
            "protocol":       pool.get("protocolName", ""),
            "fee_rate":       str(fee_rate),
            "tvl_usd":        str(tvl),
            "volume_24h":     str(vol24h),
            "volume_1h":      str(vol1h),
            "pool_age_days":  str(age_days),
            "is_primary":     "0",
            "updated_at":     str(now),
        }

        snap_key = f"pair_snapshot:{chain_index}:{pool_addr}"
        pipe.hset(snap_key, mapping=snap)
        pipe.expire(snap_key, 600)

        # Rolling 1h-volume history for volume stability scoring (48 points = 48h)
        if vol1h > 0:
            hist_key = f"pool_vol_history:{chain_index}:{pool_addr}"
            pipe.lpush(hist_key, str(vol1h))
            pipe.ltrim(hist_key, 0, 47)

        snapshots.append(snap)

    await pipe.execute()

    if snapshots:
        await rank_pairs_for_token(chain_index, token_address, snapshots)


async def _analyze_token(chain_index: str, addr: str) -> None:
    redis = await get_redis()
    snap_key = f"snapshot:{chain_index}:{addr}"

    # Confirm still in focus
    layer_raw = await redis.hget(snap_key, "layer")
    if layer_raw != Layer.focus.value:
        return

    now = int(time.time())
    enriched: dict[str, str] = {}
    alerts_to_push: list[str] = []
    symbol = await redis.hget(snap_key, "token_symbol") or ""

    # 1. Liquidity pools — merge from multiple sources
    pools: list[dict] = []
    try:
        # 1a. OKX (primary: has fee rates, security metadata, createTime)
        pools = await mcp_client.get_token_liquidity(chain_index, addr) or []

        # Track addresses already covered by OKX to avoid duplicates
        existing_addrs: set[str] = {
            (p.get("poolContractAddress") or p.get("poolAddress") or "").lower()
            for p in pools
        }

        # 1b. DEX Screener (all chains: broader pool discovery, real volume data)
        dex_pools = await dexscreener_get_pools(chain_index, addr)

        # 1c. Solana-specific: Meteora DLMM + Raydium (accurate fee rates)
        if chain_index == "501":
            meteora_pools, raydium_pools = await asyncio.gather(
                get_meteora_pools(addr),
                get_raydium_pools(addr),
                return_exceptions=True,
            )
            for extra in (meteora_pools, raydium_pools):
                if isinstance(extra, list):
                    dex_pools.extend(extra)

        # Merge: skip pools already returned by OKX (OKX data takes priority)
        for p in dex_pools:
            pa = (p.get("poolContractAddress") or "").lower()
            if pa and pa not in existing_addrs:
                pools.append(p)
                existing_addrs.add(pa)

        if pools:
            total_tvl = sum(float(p.get("liquidity", 0) or 0) for p in pools)
            # Sort by TVL descending to pick top_pool for quick summary
            pools_sorted = sorted(pools, key=lambda p: float(p.get("liquidity", 0) or 0), reverse=True)
            top_pool = pools_sorted[0]
            enriched["tvl_usd"] = str(total_tvl)
            enriched["top_pool_protocol"] = str(top_pool.get("protocolName", ""))
            enriched["top_pool_fee"] = str(top_pool.get("feeRate", ""))
            enriched["pool_count"] = str(len(pools))
            # Decompose pools → pair_snapshot + pair_ranker
            await _decompose_pools_to_pairs(chain_index, addr, symbol, pools, now)
    except Exception as e:
        logger.debug("Focus: pool fetch failed %s/%s: %s", chain_index, addr[:8], e)

    # 2. Safety info
    try:
        safety = await mcp_client.get_token_advanced_info(chain_index, addr)
        if safety:
            risk_level = int(safety.get("riskLevelControl", 0) or 0)
            is_lp_burnt = safety.get("isLpBurnt", False)
            is_mint = safety.get("isMint", False)
            is_freeze = safety.get("isFreeze", False)

            enriched["risk_level"] = str(risk_level)
            enriched["is_lp_burnt"] = "1" if is_lp_burnt else "0"
            enriched["is_mint"] = "1" if is_mint else "0"
            enriched["is_freeze"] = "1" if is_freeze else "0"

            if risk_level >= SAFETY_RISK_THRESHOLD:
                z_score = float(await redis.hget(snap_key, "z_score") or 0)
                alert = {
                    "id": str(uuid.uuid4()),
                    "chain_index": chain_index,
                    "token_address": addr,
                    "token_symbol": symbol,
                    "alert_type": AlertType.safety_risk.value,
                    "z_score": round(z_score, 4),
                    "price_change_5m": float(await redis.hget(snap_key, "price_change_5m") or 0),
                    "volume_5m": float(await redis.hget(snap_key, "volume_5m") or 0),
                    "layer": Layer.focus.value,
                    "timestamp": now,
                    "detail": {
                        "risk_level": risk_level,
                        "is_mint": is_mint,
                        "is_freeze": is_freeze,
                        "is_lp_burnt": is_lp_burnt,
                    },
                }
                alerts_to_push.append(json.dumps(alert))
    except Exception as e:
        logger.debug("Focus: safety fetch failed %s/%s: %s", chain_index, addr[:8], e)

    # 3. Smart Money trades (last 10, filter by SmartMoney tag)
    try:
        trades = await mcp_client.get_recent_trades(
            chain_index, addr, limit=10, tag_filter="smart_money"
        )
        if trades:
            buy_count = sum(1 for t in trades if t.get("type") == "buy")
            sell_count = len(trades) - buy_count
            enriched["smart_money_buys_10"] = str(buy_count)
            enriched["smart_money_sells_10"] = str(sell_count)
    except Exception as e:
        logger.debug("Focus: trades fetch failed %s/%s: %s", chain_index, addr[:8], e)

    # Persist enriched data
    if enriched:
        enriched["focus_analyzed_at"] = str(now)
        await redis.hset(snap_key, mapping=enriched)

    if alerts_to_push:
        await redis.lpush("alerts", *alerts_to_push)
        await redis.ltrim("alerts", 0, 499)

    # 4. LP Decision — run against the primary pool identified by pair_ranker
    try:
        primary_pool_addr = await redis.get(f"primary_pool:{chain_index}:{addr}")
        if primary_pool_addr:
            ps = await redis.hgetall(f"pair_snapshot:{chain_index}:{primary_pool_addr}")
            if ps:
                snap_fields = await redis.hmget(
                    snap_key,
                    "price_change_24h", "price_change_4h", "price_change_1h",
                    "z_score", "txs_5m",
                )
                await run_lp_decision_for_pair(
                    chain_index=chain_index,
                    pool_address=primary_pool_addr,
                    token_address=addr,
                    token_symbol=symbol,
                    quote_type=ps.get("quote_type", "alt"),
                    quote_symbol=ps.get("quote_symbol", ""),
                    protocol=ps.get("protocol", ""),
                    fee_rate=float(ps.get("fee_rate", 0) or 0),
                    tvl_usd=float(ps.get("tvl_usd", 0) or 0),
                    volume_24h=float(ps.get("volume_24h", 0) or 0),
                    volume_1h=float(ps.get("volume_1h", 0) or 0),
                    pool_age_days=float(ps.get("pool_age_days", 0) or 0),
                    is_primary=True,
                    price_change_24h=float(snap_fields[0] or 0),
                    price_change_4h=float(snap_fields[1] or 0),
                    price_change_1h=float(snap_fields[2] or 0),
                    z_score=float(snap_fields[3] or 0),
                    risk_level=int(enriched.get("risk_level", 0) or 0),
                    is_mint=enriched.get("is_mint", "0") == "1",
                    is_freeze=enriched.get("is_freeze", "0") == "1",
                    smart_money_buys=int(enriched.get("smart_money_buys_10", 0) or 0),
                    smart_money_sells=int(enriched.get("smart_money_sells_10", 0) or 0),
                    # txs_5m × 12 ≈ trades per hour (rough proxy for Phase 1)
                    trade_count_1h=int(snap_fields[4] or 0) * 12,
                )
    except Exception as e:
        logger.debug("Focus: LP decision failed %s/%s: %s", chain_index, addr[:8], e)


async def run_focus_analysis() -> None:
    redis = await get_redis()

    tasks = []
    for chain_index in settings.chain_list:
        focus_key = f"focus:{chain_index}"
        token_addrs = await redis.zrevrange(focus_key, 0, 49)  # top 50
        for addr in token_addrs:
            tasks.append(_analyze_token(chain_index, addr))

    if not tasks:
        return

    # Run up to 10 analyses concurrently to avoid API rate limits
    semaphore = asyncio.Semaphore(10)

    async def bounded(coro):
        async with semaphore:
            return await coro

    await asyncio.gather(*[bounded(t) for t in tasks], return_exceptions=True)
    logger.info("Focus analysis complete: analyzed %d tokens", len(tasks))
