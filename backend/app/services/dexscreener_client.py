"""
DEX Screener Client: cross-chain pool discovery and real-time price/volume data.

Rate limit: 300 requests/minute (free, no API key required).
Used as primary pool enrichment source for all chains, supplementing OKX's top-5 limit.

Supported chains:
  501  → solana
  8453 → base
  56   → bsc
  1    → ethereum
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Chain index → DexScreener chainId
CHAIN_ID_MAP: dict[str, str] = {
    "501":  "solana",
    "8453": "base",
    "56":   "bsc",
    "1":    "ethereum",
}

# DexScreener dexId → human-readable protocol name
DEX_NAME_MAP: dict[str, str] = {
    "uniswap-v3":     "Uniswap V3",
    "uniswap-v2":     "Uniswap V2",
    "pancakeswap-v3": "PancakeSwap V3",
    "pancakeswap-v2": "PancakeSwap V2",
    "raydium":        "Raydium V4",
    "raydium-clmm":   "Raydium CLMM",
    "meteora":        "Meteora",
    "meteora-dlmm":   "Meteora DLMM",
    "orca":           "Orca Whirlpool",
    "sushiswap":      "SushiSwap",
    "curve":          "Curve",
    "balancer":       "Balancer",
    "lifinity-v2":    "Lifinity V2",
    "phoenix":        "Phoenix",
    "openbook":       "OpenBook",
}

# Minimum liquidity USD to include a pool (filter dust pools)
_MIN_LIQUIDITY_USD = 1_000.0


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val or default)
    except (TypeError, ValueError):
        return default


def _normalize_pair(pair: dict, chain_id: str) -> dict | None:
    """Convert a DexScreener pair object to OKX-compatible pool format."""
    if pair.get("chainId") != chain_id:
        return None

    pool_addr = pair.get("pairAddress", "")
    if not pool_addr:
        return None

    # Liquidity filter
    liq = _safe_float((pair.get("liquidity") or {}).get("usd"))
    if liq < _MIN_LIQUIDITY_USD:
        return None

    quote = pair.get("quoteToken") or {}
    vol = pair.get("volume") or {}
    txns = pair.get("txns") or {}

    # pairCreatedAt is in milliseconds
    create_ms = pair.get("pairCreatedAt") or 0
    try:
        create_ts = int(create_ms) // 1000
    except (TypeError, ValueError):
        create_ts = 0

    dex_id = pair.get("dexId", "")
    protocol_name = DEX_NAME_MAP.get(dex_id, dex_id)

    # Rough tx count for 24h (buy + sell)
    txns_24h = 0
    for period in ("h24",):
        t = txns.get(period) or {}
        txns_24h = int(_safe_float(t.get("buys")) + _safe_float(t.get("sells")))

    return {
        "poolContractAddress":       pool_addr,
        "quoteTokenContractAddress": quote.get("address", ""),
        "quoteTokenSymbol":          quote.get("symbol", ""),
        "protocolName":              protocol_name,
        "feeRate":                   0.0,   # DexScreener does not expose fee tier
        "liquidity":                 liq,
        "volumeUsd24H":              _safe_float(vol.get("h24")),
        "volumeUsd1H":               _safe_float(vol.get("h1")),
        "createTime":                create_ts,
        "txns24H":                   txns_24h,
        "source":                    "dexscreener",
    }


async def get_pools_by_token(chain_index: str, token_address: str) -> list[dict]:
    """
    Return all pools for *token_address* on *chain_index*, normalized to OKX format.

    Parameters
    ----------
    chain_index   Internal chain ID (e.g. "501", "8453", "56").
    token_address Token contract address (case-insensitive; Solana base58 or EVM hex).

    Returns
    -------
    list of pool dicts, empty list on error or unsupported chain.
    """
    chain_id = CHAIN_ID_MAP.get(chain_index)
    if not chain_id:
        logger.debug("DexScreener: unsupported chain_index=%s", chain_index)
        return []

    url = f"{settings.dexscreener_api_url}/latest/dex/tokens/{token_address}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.debug("DexScreener fetch failed chain=%s token=%.8s: %s", chain_index, token_address, e)
        return []

    pairs = data.get("pairs") or []
    results: list[dict] = []
    for pair in pairs:
        normalized = _normalize_pair(pair, chain_id)
        if normalized:
            results.append(normalized)

    logger.debug(
        "DexScreener chain=%s token=%.8s: %d pairs found, %d passed filter",
        chain_index, token_address, len(pairs), len(results),
    )
    return results
