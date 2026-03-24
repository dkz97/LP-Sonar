"""
Universe Layer: Discover all tokens across monitored chains.
Runs every 15 minutes. Results written to Redis sorted sets.

Strategy per chain:
  - Solana (501): token_ranking (top 100 by volume) + hot_tokens (trending) + meme_token_list
  - Base (8453): token_ranking + hot_tokens
  - BSC (56):    token_ranking + hot_tokens

Admission filter before adding to universe:
  - volume24H > 0 (any traded today)
  - Has tokenContractAddress (not native-only)
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Any

from app.core.config import settings
from app.core.redis_client import get_redis
from app.services import mcp_client

logger = logging.getLogger(__name__)

# Chain-specific quote assets (lowercase contract addresses)
# Token must have at least one pool with these as the quote token
MAINNET_QUOTE_TOKENS: dict[str, set[str]] = {
    "501": {
        "so11111111111111111111111111111111111111112",   # SOL (native wrapped)
        "epjfwdd5aufqssqem2qn1xzybapc8g4weggkzwytdt1v", # USDC
        "es9vmfrzacermjfrf4h2fyd4kconky11mcce8benwnyb",  # USDT
    },
    "8453": {
        "0x4200000000000000000000000000000000000006",  # WETH
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    },
    "56": {
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
        "0x55d398326f99059ff775485246999027b3197955",  # USDT
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
    },
}


def _extract_address(token: dict) -> str | None:
    addr = token.get("tokenContractAddress", "")
    return addr if addr else None


def _passes_basic_filter(token: dict) -> bool:
    addr = _extract_address(token)
    if not addr:
        return False
    # Must have some volume
    try:
        vol = float(token.get("volume", 0) or 0)
        if vol <= 0:
            vol = float(token.get("volume24H", 0) or 0)
    except (ValueError, TypeError):
        vol = 0
    return vol > 0


async def _fetch_chain_tokens(chain_index: str) -> list[dict]:
    """Fetch tokens from multiple sources for one chain, deduplicated."""
    tasks = [
        mcp_client.get_token_ranking(chain_index, sort_by="5", time_frame="4"),
        mcp_client.get_hot_tokens(chain_index, ranking_type="4"),
    ]

    # Solana gets extra meme token discovery
    if chain_index == "501":
        tasks.append(mcp_client.get_meme_token_list(chain_index))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    seen: dict[str, dict] = {}
    for result in results:
        if isinstance(result, Exception):
            logger.warning("Universe fetch error for chain %s: %s", chain_index, result)
            continue
        if not isinstance(result, list):
            continue
        for token in result:
            addr = _extract_address(token)
            if addr and _passes_basic_filter(token):
                seen[addr] = token

    return list(seen.values())


async def run_universe_scan() -> None:
    """Discover tokens on all monitored chains and populate universe sorted sets."""
    redis = await get_redis()
    total_new = 0
    # meta_by_chain[chain_index][addr] = {tokenSymbol, tokenName, tokenLogoUrl}
    meta_by_chain: dict[str, dict[str, dict]] = {}

    for chain_index in settings.chain_list:
        try:
            tokens = await _fetch_chain_tokens(chain_index)
            if not tokens:
                logger.warning("Universe scan: no tokens found for chain %s", chain_index)
                continue

            key = f"universe:{chain_index}"
            pipe = redis.pipeline()
            meta_by_chain[chain_index] = {}

            for token in tokens:
                addr = _extract_address(token)
                if not addr:
                    continue
                try:
                    score = float(token.get("volume", 0) or token.get("volume24H", 0) or 0)
                except (ValueError, TypeError):
                    score = 0.0
                pipe.zadd(key, {addr: score})
                # Preserve metadata from ranking/hot APIs (have symbol+logo)
                meta_by_chain[chain_index][addr] = {
                    "tokenSymbol": token.get("tokenSymbol", ""),
                    "tokenName":   token.get("tokenName", ""),
                    "tokenLogoUrl": token.get("tokenLogoUrl", ""),
                }

            pipe.expire(key, 7200)  # 2h TTL
            await pipe.execute()

            count = await redis.zcard(key)
            logger.info("Universe scan chain=%s: %d tokens in universe set", chain_index, count)
            total_new += len(tokens)

        except Exception as e:
            logger.error("Universe scan failed for chain %s: %s", chain_index, e)

    logger.info("Universe scan complete: processed %d tokens across %d chains",
                total_new, len(settings.chain_list))

    # After universe scan, ensure hot layer has something to monitor
    await _promote_universe_to_hot(meta_by_chain)


async def _promote_universe_to_hot(meta_by_chain: dict[str, dict[str, dict]] | None = None) -> None:
    """
    Batch-fetch volume data for universe tokens and promote those passing
    admission thresholds into the hot layer.

    meta_by_chain: optional {chain_index: {addr: {tokenSymbol, tokenName, tokenLogoUrl}}}
                   populated by run_universe_scan() from ranking APIs that carry metadata.
    """
    redis = await get_redis()
    if meta_by_chain is None:
        meta_by_chain = {}

    for chain_index in settings.chain_list:
        universe_key = f"universe:{chain_index}"
        # Get top 200 by score (volume24H)
        token_addrs = await redis.zrevrange(universe_key, 0, settings.universe_top_n - 1)

        if not token_addrs:
            continue

        # Batch-fetch price info in chunks of 100
        all_price_data: list[dict] = []
        for i in range(0, len(token_addrs), 100):
            batch = list(token_addrs[i:i + 100])
            try:
                price_data = await mcp_client.get_token_price_info_batch(chain_index, batch)
                all_price_data.extend(price_data)
            except Exception as e:
                logger.error("Price info batch failed chain=%s batch=%d: %s", chain_index, i, e)

        if not all_price_data:
            continue

        chain_meta = meta_by_chain.get(chain_index, {})
        hot_key = f"hot:{chain_index}"
        now = int(time.time())
        pipe = redis.pipeline()
        admitted = 0

        for item in all_price_data:
            addr = item.get("tokenContractAddress", "")
            if not addr:
                continue

            try:
                vol5m = float(item.get("volume5M", 0) or 0)
                liquidity = float(item.get("liquidity", 0) or 0)
            except (ValueError, TypeError):
                continue

            # Admission criteria
            if liquidity < settings.min_tvl_usd:
                continue
            if vol5m < settings.min_volume_5m_usd:
                continue

            # Add to hot layer with z_score=0 initially (hot_monitor will compute)
            existing = await redis.zscore(hot_key, addr)
            if existing is None:
                pipe.zadd(hot_key, {addr: 0.0})
                admitted += 1

            # Merge metadata from ranking API (has symbol/logo) with price data
            token_meta = chain_meta.get(addr, {})
            snap_key = f"snapshot:{chain_index}:{addr}"
            snap_mapping: dict[str, Any] = {
                "chain_index": chain_index,
                "token_address": addr,
                "price_usd": str(item.get("price", 0)),
                "volume_5m": str(vol5m),
                "volume_1h": str(item.get("volume1H", 0)),
                "volume_4h": str(item.get("volume4H", 0)),
                "volume_24h": str(item.get("volume24H", 0)),
                "price_change_5m": str(item.get("priceChange5M", 0)),
                "price_change_1h": str(item.get("priceChange1H", 0)),
                "price_change_4h": str(item.get("priceChange4H", 0)),
                "price_change_24h": str(item.get("priceChange24H", 0)),
                "txs_5m": str(item.get("txs5M", 0)),
                "z_score": "0.0",
                "layer": "hot",
                "updated_at": str(now),
            }
            # Write symbol/name/logo only if we have them (don't overwrite enriched values with empty)
            if token_meta.get("tokenSymbol"):
                snap_mapping["token_symbol"] = token_meta["tokenSymbol"]
            if token_meta.get("tokenName"):
                snap_mapping["token_name"] = token_meta["tokenName"]
            if token_meta.get("tokenLogoUrl"):
                snap_mapping["logo_url"] = token_meta["tokenLogoUrl"]

            pipe.hset(snap_key, mapping=snap_mapping)
            pipe.expire(snap_key, 600)

        pipe.expire(hot_key, 3600)
        await pipe.execute()
        logger.info("Universe→Hot promotion chain=%s: %d newly admitted", chain_index, admitted)
