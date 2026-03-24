"""Token detail endpoints — basic info, all pools, OHLCV bars, TX history."""
from __future__ import annotations
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from math import floor

import httpx
from fastapi import APIRouter, HTTPException, Query

from app.core.config import settings
from app.core.redis_client import get_redis
from app.services import mcp_client
from app.services.solana_dex_client import (
    get_meteora_damm_pools,
    get_meteora_fee_rate,
    get_meteora_pool_detail,
    get_meteora_pools,
    get_orca_fee_rate,
    get_raydium_pool_details,
    get_raydium_fee_rates,
    get_raydium_pools,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/token")

# chainIndex → GeckoTerminal network slug
_GT_NETWORK: dict[str, str] = {
    "1":     "eth",
    "56":    "bsc",
    "137":   "polygon_pos",
    "8453":  "base",
    "42161": "arbitrum",
    "10":    "optimism",
    "43114": "avax",
    "501":   "solana",
    "324":   "zksync",
    "59144": "linea",
    "130":   "unichain",
}

# chainIndex → DexScreener chainId（GT 限速时的二级 fallback，仅支持 4 条链）
_DS_CHAIN: dict[str, str] = {
    "1":    "ethereum",
    "56":   "bsc",
    "8453": "base",
    "501":  "solana",
}

# DexScreener dexId → human-readable name
_DS_DEX_NAME: dict[str, str] = {
    "uniswap-v3":     "Uniswap V3",
    "uniswap-v2":     "Uniswap V2",
    "pancakeswap-v3": "PancakeSwap V3",
    "pancakeswap-v2": "PancakeSwap V2",
    "raydium":        "Raydium V4",
    "raydium-clmm":   "Raydium CLMM",
    "meteora":        "Meteora",
    "meteora-dlmm":   "Meteora DLMM",
    "orca":           "Orca Whirlpool",
    "pumpswap":       "PumpSwap",
}

# 已知 V2 类 DEX 固定费率（DexScreener 不提供费率时用；V3 池子费率不固定，保持 0）
_DS_DEFAULT_FEE: dict[str, float] = {
    "uniswap-v2":     0.003,
    "pancakeswap-v2": 0.0025,
    "sushiswap":      0.003,
}

_GT_FRESH_TTL_SECONDS = 45
_GT_STALE_TTL_SECONDS = 600
_GT_BACKOFF_DEFAULT_SECONDS = 20
_SOL_POOL_FEE_TTL_SECONDS = 21600
_SOL_POOL_GT_ENRICH_LIMIT = 4
_SOL_POOL_GT_ENRICH_MIN_LIQUIDITY_USD = 5_000
_SOL_POOL_ENRICH_CONCURRENCY = 3
_gt_backoff_until = 0.0
_gt_inflight: dict[str, asyncio.Task] = {}
_PUMPSWAP_NON_CANONICAL_FEE_RATE = 0.003
_PUMPSWAP_CANONICAL_SUPPLY = 1_000_000_000
_PUMPSWAP_SOL_QUOTES = {"SOL", "WSOL"}
# From pump.fun PumpSwap fee schedule; thresholds are market cap in SOL.
_PUMPSWAP_CANONICAL_FEE_TIERS: list[tuple[float, float]] = [
    (420.0, 0.0125),
    (1470.0, 0.0120),
    (2460.0, 0.0115),
    (3440.0, 0.0110),
    (4420.0, 0.0105),
    (9820.0, 0.0100),
    (14740.0, 0.0095),
    (19650.0, 0.0090),
    (24560.0, 0.0085),
    (29470.0, 0.0080),
    (34380.0, 0.0075),
    (39300.0, 0.0070),
    (44210.0, 0.0065),
    (49120.0, 0.0060),
    (54030.0, 0.0055),
    (58940.0, 0.00525),
    (63860.0, 0.0050),
    (68770.0, 0.00475),
    (73681.0, 0.0045),
    (78590.0, 0.00425),
    (83500.0, 0.0040),
    (88400.0, 0.00375),
    (93330.0, 0.0035),
    (98240.0, 0.00325),
]


def _fee_from_name(name: str) -> float:
    """从 pair_name 正则解析费率，如 'WETH / USDC 0.05%' → 0.0005。"""
    m = re.search(r'(\d+\.?\d*)\s*%', name or "")
    if m:
        return float(m.group(1)) / 100.0
    return 0.0


def _percent_str_to_rate(raw) -> float:
    text = str(raw or "").strip()
    if not text:
        return 0.0
    try:
        return float(text.rstrip("%")) / 100.0
    except (TypeError, ValueError):
        return 0.0


def _gt_cache_keys(kind: str, chain_index: str, pool_address: str, interval: str, limit: int) -> tuple[str, str]:
    base = f"gtcache:{kind}:{chain_index}:{pool_address}:{interval}:{limit}"
    return f"{base}:fresh", f"{base}:stale"


def _pool_fee_cache_key(dex_name: str, pool_address: str) -> str:
    return f"pool_fee:{dex_name.lower()}:{pool_address}"


async def _load_cached_list(redis, key: str) -> list[dict] | None:
    raw = await redis.get(key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


async def _store_cached_list(redis, fresh_key: str, stale_key: str, payload: list[dict]) -> None:
    serialized = json.dumps(payload)
    pipe = redis.pipeline()
    pipe.set(fresh_key, serialized, ex=_GT_FRESH_TTL_SECONDS)
    pipe.set(stale_key, serialized, ex=_GT_STALE_TTL_SECONDS)
    await pipe.execute()


async def _load_cached_dict(redis, key: str) -> dict | None:
    raw = await redis.get(key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def _store_cached_dict(redis, fresh_key: str, stale_key: str, payload: dict) -> None:
    serialized = json.dumps(payload)
    pipe = redis.pipeline()
    pipe.set(fresh_key, serialized, ex=_GT_FRESH_TTL_SECONDS)
    pipe.set(stale_key, serialized, ex=_GT_STALE_TTL_SECONDS)
    await pipe.execute()


async def _load_cached_fee_rate(redis, dex_name: str, pool_address: str) -> float | None:
    raw = await redis.get(_pool_fee_cache_key(dex_name, pool_address))
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def _store_cached_fee_rate(redis, dex_name: str, pool_address: str, fee_rate: float) -> None:
    await redis.set(_pool_fee_cache_key(dex_name, pool_address), str(fee_rate), ex=_SOL_POOL_FEE_TTL_SECONDS)


def _gt_retry_after_seconds(resp: httpx.Response) -> int:
    retry_after = resp.headers.get("Retry-After", "").strip()
    if retry_after.isdigit():
        return max(int(retry_after), 1)
    return _GT_BACKOFF_DEFAULT_SECONDS


def _gt_backoff_active() -> bool:
    return time.time() < _gt_backoff_until


def _set_gt_backoff(seconds: int) -> None:
    global _gt_backoff_until
    _gt_backoff_until = max(_gt_backoff_until, time.time() + max(seconds, 1))


async def _serve_stale_or_empty(redis, stale_key: str, reason: str) -> list[dict]:
    stale = await _load_cached_list(redis, stale_key)
    if stale is not None:
        logger.info("GT fallback: serving stale cache for %s", reason)
        return stale
    return []


async def _run_gt_inflight(cache_key: str, producer) -> list[dict]:
    task = _gt_inflight.get(cache_key)
    if task is not None:
        return await task

    task = asyncio.create_task(producer())
    _gt_inflight[cache_key] = task
    try:
        return await task
    finally:
        if _gt_inflight.get(cache_key) is task:
            _gt_inflight.pop(cache_key, None)


# ─── Basic Info ───────────────────────────────────────────────────────────────

@router.get("/{chain_index}/{address}/basic-info")
async def get_basic_info(chain_index: str, address: str) -> dict:
    """Return token basic info via OKX MCP token search."""
    info: dict = {}
    try:
        result = await mcp_client.call_tool("dex-okx-market-token-search", {
            "chains": chain_index,
            "search": address,
        })
        okx: dict = {}
        if isinstance(result, dict):
            data = result.get("data", [])
            okx = data[0] if isinstance(data, list) and data else result
        elif isinstance(result, list) and result:
            okx = result[0]

        info = {
            "tokenSymbol": okx.get("tokenSymbol", ""),
            "tokenName":   okx.get("tokenName", ""),
            "logoUrl":     okx.get("tokenLogoUrl", ""),
            "priceUsd":    str(okx.get("price") or ""),
            "marketCap":   str(okx.get("marketCap") or ""),
            "volume24H":   str(okx.get("liquidity") or ""),
            "holders":     str(okx.get("holders") or ""),
            "change24H":   str(okx.get("change") or ""),
        }
        for field in ("totalSupply", "circulatingSupply", "website", "twitterUrl", "telegramUrl", "officialWebsite"):
            if okx.get(field):
                info[field] = okx[field]
    except Exception as e:
        logger.warning("OKX MCP token-search error: %s", e)

    logger.debug("basic-info result for %s/%s: fields=%s", chain_index, address, list(info.keys()))
    return info


# ─── Pools ────────────────────────────────────────────────────────────────────

def _normalize_okx_pool(pool: dict) -> dict:
    """Normalize raw OKX liquidity pool data to match GeckoTerminal Pool structure.

    OKX field names vary across API versions — try multiple variants.
    """
    def _float(v) -> float:
        try:
            return float(v or 0)
        except (ValueError, TypeError):
            return 0.0

    def _first(*keys: str) -> str:
        for k in keys:
            v = pool.get(k)
            if v:
                return str(v)
        return ""

    empty_txn = {"buys": 0, "sells": 0}
    pool_addr  = _first("poolAddress", "poolContractAddress", "address", "contractAddress")
    dex_name   = _first("protocolName", "dexName", "exchangeName", "dex", "protocol")
    pair_name  = _first("pool")

    # top-liquidity API returns token info inside liquidityAmount array
    liq_amounts: list[dict] = pool.get("liquidityAmount") or []
    base_sym  = str(liq_amounts[0].get("tokenSymbol", "") if len(liq_amounts) > 0 else "") or _first("token0Symbol", "baseTokenSymbol", "tokenASymbol")
    quote_sym = str(liq_amounts[1].get("tokenSymbol", "") if len(liq_amounts) > 1 else "") or _first("token1Symbol", "quoteTokenSymbol", "tokenBSymbol")
    base_addr  = _first("token0ContractAddress", "token0Address", "baseTokenAddress", "tokenAAddress")
    quote_addr = _first("token1ContractAddress", "token1Address", "quoteTokenAddress", "tokenBAddress")
    tvl        = _float(_first("liquidityUsd", "tvl", "liquidity", "totalLiquidityUsd"))
    vol24h     = _float(_first("volume24H", "vol24H", "volumeUsd24h", "volume24h"))
    price      = _float(_first("price", "priceUsd", "tokenPrice"))

    # top-liquidity API uses liquidityProviderFeePercent (e.g. "0.06%"), not feeRate
    fee_rate = _percent_str_to_rate(pool.get("liquidityProviderFeePercent"))
    if fee_rate <= 0:
        fee_raw = float(pool.get("feeRate", 0) or 0)
        fee_rate = fee_raw if fee_raw < 1 else fee_raw / 10_000

    if not pair_name:
        pair_name = f"{base_sym}/{quote_sym}" if base_sym or quote_sym else ""

    return {
        "pool_address":       pool_addr,
        "dex_name":           dex_name,
        "pair_name":          pair_name,
        "base_token_symbol":  base_sym,
        "base_token_name":    base_sym,
        "base_token_address": base_addr,
        "quote_token_symbol": quote_sym,
        "quote_token_address": quote_addr,
        "price_usd":          price,
        "liquidity_usd":      tvl,
        "fdv_usd":            0.0,
        "market_cap_usd":     0.0,
        "volume": {"m5": 0.0, "m15": 0.0, "h1": 0.0, "h6": 0.0, "h24": vol24h},
        "txns": {
            "m5":  empty_txn, "m15": empty_txn, "m30": empty_txn,
            "h1":  empty_txn, "h6":  empty_txn, "h24": empty_txn,
        },
        "price_change": {"m5": 0.0, "h1": 0.0, "h6": 0.0, "h24": 0.0},
        "pool_created_at": None,
        "fee_rate": fee_rate,
    }


def _parse_gt_pools(data: dict) -> list[dict]:
    """Parse GeckoTerminal token pools response into a flat list."""
    pools_raw: list[dict] = data.get("data", [])
    included: list[dict] = data.get("included", [])

    # Build lookup: id → attributes
    inc_map: dict[str, dict] = {item["id"]: item.get("attributes", {}) for item in included}

    result = []
    for pool in pools_raw:
        attrs = pool.get("attributes", {})
        rels = pool.get("relationships", {})

        dex_id = rels.get("dex", {}).get("data", {}).get("id", "")
        base_id = rels.get("base_token", {}).get("data", {}).get("id", "")
        quote_id = rels.get("quote_token", {}).get("data", {}).get("id", "")

        dex_attrs = inc_map.get(dex_id, {})
        base_attrs = inc_map.get(base_id, {})
        quote_attrs = inc_map.get(quote_id, {})

        def _float(v) -> float:
            try:
                return float(v or 0)
            except (ValueError, TypeError):
                return 0.0

        txns_raw = attrs.get("transactions", {})

        def _txn(window: str) -> dict:
            w = txns_raw.get(window, {})
            return {"buys": w.get("buys", 0), "sells": w.get("sells", 0)}

        vol_raw = attrs.get("volume_usd", {})
        pc_raw = attrs.get("price_change_percentage", {})

        pool_name = attrs.get("name", "")
        result.append({
            "pool_address": attrs.get("address", ""),
            "dex_name": dex_attrs.get("name", dex_id),
            "pair_name": pool_name,
            "base_token_symbol": base_attrs.get("symbol", ""),
            "base_token_name": base_attrs.get("name", ""),
            "base_token_address": base_attrs.get("address", base_id.split("_", 1)[-1]),
            "quote_token_symbol": quote_attrs.get("symbol", ""),
            "quote_token_address": quote_attrs.get("address", quote_id.split("_", 1)[-1]),
            "price_usd": _float(attrs.get("base_token_price_usd")),
            "liquidity_usd": _float(attrs.get("reserve_in_usd")),
            "fdv_usd": _float(attrs.get("fdv_usd")),
            "market_cap_usd": _float(attrs.get("market_cap_usd")),
            "volume": {
                "m5":  _float(vol_raw.get("m5")),
                "m15": _float(vol_raw.get("m15")),
                "h1":  _float(vol_raw.get("h1")),
                "h6":  _float(vol_raw.get("h6")),
                "h24": _float(vol_raw.get("h24")),
            },
            "txns": {
                "m5":  _txn("m5"),
                "m15": _txn("m15"),
                "m30": _txn("m30"),
                "h1":  _txn("h1"),
                "h6":  _txn("h6"),
                "h24": _txn("h24"),
            },
            "price_change": {
                "m5":  _float(pc_raw.get("m5")),
                "h1":  _float(pc_raw.get("h1")),
                "h6":  _float(pc_raw.get("h6")),
                "h24": _float(pc_raw.get("h24")),
            },
            "pool_created_at": attrs.get("pool_created_at"),
            # pool_fee 是 GT 的直接字段（如 0.003），缺失时从 pair_name 正则解析
            "fee_rate": _float(attrs.get("pool_fee")) or _fee_from_name(pool_name),
        })

    return result


def _parse_gt_pool_detail(data: dict) -> dict | None:
    """Parse GeckoTerminal single-pool response into a partial pool row."""
    pool = data.get("data") or {}
    attrs = pool.get("attributes") or {}
    if not attrs:
        return None

    rels = pool.get("relationships") or {}
    dex_id = rels.get("dex", {}).get("data", {}).get("id", "")
    base_id = rels.get("base_token", {}).get("data", {}).get("id", "")
    quote_id = rels.get("quote_token", {}).get("data", {}).get("id", "")

    def _float(v) -> float:
        try:
            return float(v or 0)
        except (ValueError, TypeError):
            return 0.0

    txns_raw = attrs.get("transactions", {})

    def _txn(window: str) -> dict:
        w = txns_raw.get(window, {})
        return {"buys": int(_float(w.get("buys"))), "sells": int(_float(w.get("sells")))}

    vol_raw = attrs.get("volume_usd", {})
    pc_raw = attrs.get("price_change_percentage", {})
    return {
        "pool_address": attrs.get("address", ""),
        "dex_id": dex_id,
        "dex_name": _DS_DEX_NAME.get(dex_id, dex_id),
        "base_token_address": base_id.split("_", 1)[-1] if base_id else "",
        "quote_token_address": quote_id.split("_", 1)[-1] if quote_id else "",
        "price_native": _float(attrs.get("base_token_price_native_currency")),
        "price_usd": _float(attrs.get("base_token_price_usd")),
        "liquidity_usd": _float(attrs.get("reserve_in_usd")),
        "fdv_usd": _float(attrs.get("fdv_usd")),
        "market_cap_usd": _float(attrs.get("market_cap_usd")),
        "volume": {
            "m5": _float(vol_raw.get("m5")),
            "m15": _float(vol_raw.get("m15")),
            "h1": _float(vol_raw.get("h1")),
            "h6": _float(vol_raw.get("h6")),
            "h24": _float(vol_raw.get("h24")),
        },
        "txns": {
            "m5": _txn("m5"),
            "m15": _txn("m15"),
            "m30": _txn("m30"),
            "h1": _txn("h1"),
            "h6": _txn("h6"),
            "h24": _txn("h24"),
        },
        "price_change": {
            "m5": _float(pc_raw.get("m5")),
            "h1": _float(pc_raw.get("h1")),
            "h6": _float(pc_raw.get("h6")),
            "h24": _float(pc_raw.get("h24")),
        },
        "pool_created_at": attrs.get("pool_created_at"),
        "fee_rate": _float(attrs.get("pool_fee")) or _percent_str_to_rate(attrs.get("pool_fee_percentage")),
    }


def _parse_ds_pools(pairs: list[dict], chain_id: str) -> list[dict]:
    """将原始 DexScreener pair 列表转换为 GT pool 格式（GT 限速时的二级 fallback）。"""
    result = []
    for pair in pairs:
        if pair.get("chainId") != chain_id:
            continue
        pool_addr = pair.get("pairAddress", "")
        if not pool_addr:
            continue

        def _f(v) -> float:
            try:
                return float(v or 0)
            except (ValueError, TypeError):
                return 0.0

        liq = _f((pair.get("liquidity") or {}).get("usd"))
        if liq < 1_000:
            continue

        base   = pair.get("baseToken")   or {}
        quote  = pair.get("quoteToken")  or {}
        vol    = pair.get("volume")      or {}
        txns   = pair.get("txns")        or {}
        pc     = pair.get("priceChange") or {}
        dex_id = pair.get("dexId", "")
        price_native = _f(pair.get("priceNative"))
        market_cap_usd = _f(pair.get("marketCap")) or _f(pair.get("fdv"))

        def _txn(w: str) -> dict:
            t = txns.get(w) or {}
            return {"buys": int(_f(t.get("buys"))), "sells": int(_f(t.get("sells")))}

        result.append({
            "pool_address":        pool_addr,
            "dex_id":              dex_id,
            "dex_name":            _DS_DEX_NAME.get(dex_id, dex_id),
            "pair_name":           f"{base.get('symbol', '')}/{quote.get('symbol', '')}",
            "base_token_symbol":   base.get("symbol", ""),
            "base_token_name":     base.get("name", ""),
            "base_token_address":  base.get("address", ""),
            "quote_token_symbol":  quote.get("symbol", ""),
            "quote_token_address": quote.get("address", ""),
            "price_native":        price_native,
            "price_usd":           _f(pair.get("priceUsd")),
            "liquidity_usd":       liq,
            "fdv_usd":             0.0,
            "market_cap_usd":      market_cap_usd,
            "volume": {
                "m5":  _f(vol.get("m5")),
                "m15": 0.0,
                "h1":  _f(vol.get("h1")),
                "h6":  _f(vol.get("h6")),
                "h24": _f(vol.get("h24")),
            },
            "txns": {
                "m5":  _txn("m5"), "m15": {"buys": 0, "sells": 0}, "m30": {"buys": 0, "sells": 0},
                "h1":  _txn("h1"), "h6":  _txn("h6"), "h24": _txn("h24"),
            },
            "price_change": {
                "m5":  _f(pc.get("m5")),
                "h1":  _f(pc.get("h1")),
                "h6":  _f(pc.get("h6")),
                "h24": _f(pc.get("h24")),
            },
            "pool_created_at": None,
            # V2 类 DEX 用已知固定费率；V3 池子费率不固定，DexScreener 不提供，保持 0
            "fee_rate": _DS_DEFAULT_FEE.get(dex_id, 0.0),
        })
    return result


def _pool_sort_key(pool: dict) -> tuple[float, float, float]:
    volume = pool.get("volume") or {}
    txns = pool.get("txns") or {}
    tx24 = txns.get("h24") or {}
    tx_count = float(tx24.get("buys", 0) or 0) + float(tx24.get("sells", 0) or 0)
    return (
        float(pool.get("liquidity_usd", 0) or 0),
        float(volume.get("h24", 0) or 0),
        tx_count,
    )


def _pool_row_from_okx_like(pool: dict, token_address: str, token_symbol: str = "") -> dict | None:
    pool_addr = (
        pool.get("poolContractAddress")
        or pool.get("poolAddress")
        or ""
    )
    if not pool_addr:
        return None

    def _float(v) -> float:
        try:
            return float(v or 0)
        except (ValueError, TypeError):
            return 0.0

    fee_raw = _float(pool.get("feeRate", 0))
    fee_rate = fee_raw if fee_raw < 1 else fee_raw / 10_000
    quote_sym = str(pool.get("quoteTokenSymbol", "") or "")
    pair_name = f"{token_symbol}/{quote_sym}" if token_symbol and quote_sym else (token_symbol or quote_sym or pool_addr)
    price_usd = _float(pool.get("priceUsd", 0))
    price_native = _float(pool.get("priceNative", 0))
    market_cap_usd = _float(pool.get("marketCapUsd", 0) or pool.get("marketCap", 0))

    create_ts = int(_float(pool.get("createTime", 0)))
    created_at = None
    if create_ts > 0:
        created_at = datetime.fromtimestamp(create_ts, tz=timezone.utc).isoformat()

    empty_txn = {"buys": 0, "sells": 0}
    return {
        "pool_address": pool_addr,
        "dex_name": str(pool.get("protocolName", "") or ""),
        "pair_name": pair_name,
        "base_token_symbol": token_symbol,
        "base_token_name": token_symbol,
        "base_token_address": token_address,
        "quote_token_symbol": quote_sym,
        "quote_token_address": str(pool.get("quoteTokenContractAddress", "") or ""),
        "price_native": price_native,
        "price_usd": price_usd,
        "liquidity_usd": _float(pool.get("liquidity", 0)),
        "fdv_usd": 0.0,
        "market_cap_usd": market_cap_usd,
        "volume": {
            "m5": 0.0,
            "m15": _float(pool.get("volumeUsd15M", 0)),
            "h1": _float(pool.get("volumeUsd1H", 0) or pool.get("volume1H", 0)),
            "h6": 0.0,
            "h24": _float(pool.get("volumeUsd24H", 0) or pool.get("volume24H", 0)),
        },
        "txns": {
            "m5": empty_txn,
            "m15": empty_txn,
            "m30": empty_txn,
            "h1": empty_txn,
            "h6": empty_txn,
            "h24": empty_txn,
        },
        "price_change": {"m5": 0.0, "h1": 0.0, "h6": 0.0, "h24": 0.0},
        "pool_created_at": created_at,
        "fee_rate": fee_rate,
    }


def _merge_pool_rows(existing: dict | None, incoming: dict) -> dict:
    if existing is None:
        return incoming

    merged = dict(existing)

    for key in (
        "dex_id",
        "dex_name",
        "pair_name",
        "base_token_symbol",
        "base_token_name",
        "base_token_address",
        "quote_token_symbol",
        "quote_token_address",
        "pool_created_at",
    ):
        if not merged.get(key) and incoming.get(key):
            merged[key] = incoming[key]

    for key in ("price_native", "price_usd", "fdv_usd", "market_cap_usd"):
        if float(merged.get(key, 0) or 0) <= 0 and float(incoming.get(key, 0) or 0) > 0:
            merged[key] = incoming[key]

    merged["liquidity_usd"] = max(float(merged.get("liquidity_usd", 0) or 0), float(incoming.get("liquidity_usd", 0) or 0))

    merged_volume = dict(merged.get("volume") or {})
    incoming_volume = incoming.get("volume") or {}
    for window in ("m5", "m15", "h1", "h6", "h24"):
        merged_volume[window] = max(float(merged_volume.get(window, 0) or 0), float(incoming_volume.get(window, 0) or 0))
    merged["volume"] = merged_volume

    merged_txns = dict(merged.get("txns") or {})
    incoming_txns = incoming.get("txns") or {}
    for window in ("m5", "m15", "m30", "h1", "h6", "h24"):
        mt = dict(merged_txns.get(window) or {"buys": 0, "sells": 0})
        it = incoming_txns.get(window) or {}
        mt["buys"] = max(int(mt.get("buys", 0) or 0), int(it.get("buys", 0) or 0))
        mt["sells"] = max(int(mt.get("sells", 0) or 0), int(it.get("sells", 0) or 0))
        merged_txns[window] = mt
    merged["txns"] = merged_txns

    merged_pc = dict(merged.get("price_change") or {})
    incoming_pc = incoming.get("price_change") or {}
    for window in ("m5", "h1", "h6", "h24"):
        if float(merged_pc.get(window, 0) or 0) == 0 and float(incoming_pc.get(window, 0) or 0) != 0:
            merged_pc[window] = incoming_pc[window]
    merged["price_change"] = merged_pc

    if float(incoming.get("fee_rate", 0) or 0) > 0:
        merged["fee_rate"] = incoming["fee_rate"]

    return merged


def _infer_token_symbol(pool: dict, token_address: str) -> str:
    token_lower = token_address.lower()
    if str(pool.get("base_token_address", "") or "").lower() == token_lower:
        return str(pool.get("base_token_symbol", "") or "")
    if str(pool.get("quote_token_address", "") or "").lower() == token_lower:
        return str(pool.get("quote_token_symbol", "") or "")
    return ""


async def _fetch_dexscreener_pool_rows(chain_index: str, address: str) -> list[dict]:
    chain_id = _DS_CHAIN.get(chain_index)
    if not chain_id:
        return []

    ds_url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(ds_url)
            if resp.status_code != 200:
                return []
            return _parse_ds_pools(resp.json().get("pairs") or [], chain_id)
    except Exception as e:
        logger.warning("DexScreener pool fetch error: %s", e)
        return []


async def _get_direct_pools(chain_index: str, address: str) -> list[dict]:
    ds_task = _fetch_dexscreener_pool_rows(chain_index, address)
    okx_task = mcp_client.get_token_liquidity(chain_index, address)

    if chain_index == "501":
        ds_rows, okx_pools, meteora_pools, meteora_damm_pools, raydium_pools = await asyncio.gather(
            ds_task,
            okx_task,
            get_meteora_pools(address),
            get_meteora_damm_pools(address),
            get_raydium_pools(address),
            return_exceptions=True,
        )
    else:
        ds_rows, okx_pools = await asyncio.gather(ds_task, okx_task, return_exceptions=True)
        meteora_pools = []
        meteora_damm_pools = []
        raydium_pools = []

    rows_by_addr: dict[str, dict] = {}
    token_symbol = ""

    if isinstance(ds_rows, list):
        for row in ds_rows:
            pool_addr = str(row.get("pool_address", "") or "").lower()
            if not pool_addr:
                continue
            rows_by_addr[pool_addr] = _merge_pool_rows(rows_by_addr.get(pool_addr), row)
            if not token_symbol:
                token_symbol = _infer_token_symbol(row, address)

    if isinstance(okx_pools, list):
        for pool in okx_pools:
            row = _normalize_okx_pool(pool)
            pool_addr = str(row.get("pool_address", "") or "").lower()
            if not pool_addr:
                continue
            rows_by_addr[pool_addr] = _merge_pool_rows(rows_by_addr.get(pool_addr), row)
            if not token_symbol:
                token_symbol = _infer_token_symbol(row, address)

    for pools in (meteora_pools, meteora_damm_pools, raydium_pools):
        if not isinstance(pools, list):
            continue
        for pool in pools:
            row = _pool_row_from_okx_like(pool, address, token_symbol)
            if not row:
                continue
            pool_addr = str(row.get("pool_address", "") or "").lower()
            rows_by_addr[pool_addr] = _merge_pool_rows(rows_by_addr.get(pool_addr), row)

    pools = sorted(rows_by_addr.values(), key=_pool_sort_key, reverse=True)
    if chain_index == "501":
        pools = await _apply_solana_pool_backfills(chain_index, address, pools)
    return pools


def _is_pumpswap_pool(pool: dict) -> bool:
    dex_id = str(pool.get("dex_id", "") or "").lower()
    dex_name = str(pool.get("dex_name", "") or "").lower()
    return "pumpswap" in dex_id or "pumpswap" in dex_name


def _pool_contains_pump_mint(pool: dict) -> bool:
    return any(str(pool.get(key, "") or "").lower().endswith("pump") for key in ("base_token_address", "quote_token_address"))


def _pool_market_cap_sol(pool: dict) -> float:
    price_native = float(pool.get("price_native", 0) or 0)
    market_cap_usd = float(pool.get("market_cap_usd", 0) or 0)
    price_usd = float(pool.get("price_usd", 0) or 0)

    if market_cap_usd > 0 and price_usd > 0 and price_native > 0:
        sol_price_usd = price_usd / price_native
        if sol_price_usd > 0:
            return market_cap_usd / sol_price_usd

    if price_native > 0:
        return price_native * _PUMPSWAP_CANONICAL_SUPPLY

    return 0.0


def _pumpswap_canonical_fee_rate(market_cap_sol: float) -> float:
    if market_cap_sol <= 0:
        return _PUMPSWAP_NON_CANONICAL_FEE_RATE

    for threshold, fee_rate in _PUMPSWAP_CANONICAL_FEE_TIERS:
        if market_cap_sol < threshold:
            return fee_rate
    return _PUMPSWAP_NON_CANONICAL_FEE_RATE


def _apply_pumpswap_fee_rates(pools: list[dict]) -> None:
    pumpswap_indexes = [idx for idx, pool in enumerate(pools) if _is_pumpswap_pool(pool)]
    if not pumpswap_indexes:
        return

    canonical_candidates = [idx for idx in pumpswap_indexes if _pool_contains_pump_mint(pools[idx])]
    if canonical_candidates:
        sol_quote_candidates = [
            idx for idx in canonical_candidates
            if str(pools[idx].get("quote_token_symbol", "") or "").upper() in _PUMPSWAP_SOL_QUOTES
        ]
        candidate_indexes = sol_quote_candidates or canonical_candidates
        canonical_index = max(
            candidate_indexes,
            key=lambda idx: (
                float(pools[idx].get("liquidity_usd", 0) or 0),
                float((pools[idx].get("volume") or {}).get("h24", 0) or 0),
            ),
        )
    else:
        canonical_index = None

    for idx in pumpswap_indexes:
        pool = pools[idx]
        if float(pool.get("fee_rate", 0) or 0) > 0:
            continue

        fee_rate = _PUMPSWAP_NON_CANONICAL_FEE_RATE
        if idx == canonical_index:
            fee_rate = _pumpswap_canonical_fee_rate(_pool_market_cap_sol(pool))
        pool["fee_rate"] = fee_rate


def _pool_tx_count(pool: dict, window: str = "h24") -> int:
    tx_window = (pool.get("txns") or {}).get(window) or {}
    return int(tx_window.get("buys", 0) or 0) + int(tx_window.get("sells", 0) or 0)


def _pool_token_symbol(pool: dict, token_address: str) -> str:
    return _infer_token_symbol(pool, token_address) or str(pool.get("base_token_symbol", "") or "")


async def _run_limited(items: list[str], limit: int, coro_factory):
    if not items:
        return []

    semaphore = asyncio.Semaphore(limit)

    async def _runner(item: str):
        async with semaphore:
            return await coro_factory(item)

    return await asyncio.gather(*[_runner(item) for item in items], return_exceptions=True)


async def _fetch_gt_pool_detail_row(chain_index: str, pool_address: str) -> dict | None:
    network = _GT_NETWORK.get(chain_index)
    if not network:
        return None

    redis = await get_redis()
    fresh_key, stale_key = _gt_cache_keys("pool-meta", chain_index, pool_address, "meta", 0)
    cached = await _load_cached_dict(redis, fresh_key)
    if cached is not None:
        return cached

    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool_address}"

    async def _fetch() -> dict:
        if _gt_backoff_active():
            stale = await _load_cached_dict(redis, stale_key)
            if stale is not None:
                logger.info("GT fallback: serving stale cache for pool-meta:%s", pool_address)
                return stale
            return {}

        async with httpx.AsyncClient(timeout=12.0) as client:
            try:
                resp = await client.get(url, headers={"Accept": "application/json"})
                if resp.status_code == 404:
                    return {}
                if resp.status_code == 429:
                    wait_seconds = _gt_retry_after_seconds(resp)
                    _set_gt_backoff(wait_seconds)
                    logger.warning("GT pool-meta 429 for %s, backing off %ss", pool_address, wait_seconds)
                    stale = await _load_cached_dict(redis, stale_key)
                    return stale or {}
                if resp.status_code != 200:
                    logger.warning("GT pool-meta HTTP %s for %s", resp.status_code, pool_address)
                    stale = await _load_cached_dict(redis, stale_key)
                    return stale or {}
                data = resp.json()
            except Exception as e:
                logger.warning("GT pool-meta request error for %s: %s", pool_address, e)
                stale = await _load_cached_dict(redis, stale_key)
                return stale or {}

        payload = _parse_gt_pool_detail(data)
        if not payload:
            stale = await _load_cached_dict(redis, stale_key)
            return stale or {}

        await _store_cached_dict(redis, fresh_key, stale_key, payload)
        return payload

    inflight_key = f"{fresh_key}:inflight"
    result = await _run_gt_inflight(inflight_key, _fetch)
    return result if isinstance(result, dict) and result else None


async def _apply_solana_protocol_enrichment(pools: list[dict], token_address: str) -> list[dict]:
    if not pools:
        return pools

    pool_index_by_addr = {
        str(pool.get("pool_address", "") or "").lower(): idx
        for idx, pool in enumerate(pools)
        if pool.get("pool_address")
    }

    meteora_targets = [
        str(pool.get("pool_address", "") or "")
        for pool in pools
        if "meteora" in str(pool.get("dex_name", "") or "").lower()
        and float(pool.get("liquidity_usd", 0) or 0) > 0
        and (
            float(pool.get("price_usd", 0) or 0) <= 0
            or float((pool.get("volume") or {}).get("h24", 0) or 0) <= 0
        )
    ]
    raydium_targets = [
        str(pool.get("pool_address", "") or "")
        for pool in pools
        if "raydium" in str(pool.get("dex_name", "") or "").lower()
        and float(pool.get("liquidity_usd", 0) or 0) > 0
        and (
            float(pool.get("price_usd", 0) or 0) <= 0
            or float((pool.get("volume") or {}).get("h24", 0) or 0) <= 0
        )
    ]

    if meteora_targets:
        meteora_results = await _run_limited(
            meteora_targets,
            _SOL_POOL_ENRICH_CONCURRENCY,
            lambda pool_addr: get_meteora_pool_detail(pool_addr, token_address),
        )
        for pool_addr, result in zip(meteora_targets, meteora_results):
            if not isinstance(result, dict) or not result:
                continue
            idx = pool_index_by_addr.get(pool_addr.lower())
            if idx is None:
                continue
            row = _pool_row_from_okx_like(result, token_address, _pool_token_symbol(pools[idx], token_address))
            if row:
                pools[idx] = _merge_pool_rows(pools[idx], row)

    if raydium_targets:
        raydium_rows = await get_raydium_pool_details(raydium_targets, token_address)
        for pool_addr, result in raydium_rows.items():
            idx = pool_index_by_addr.get(pool_addr.lower())
            if idx is None:
                continue
            row = _pool_row_from_okx_like(result, token_address, _pool_token_symbol(pools[idx], token_address))
            if row:
                pools[idx] = _merge_pool_rows(pools[idx], row)

    return pools


async def _apply_solana_gt_pool_enrichment(chain_index: str, pools: list[dict]) -> list[dict]:
    if not pools:
        return pools

    candidates = [
        pool for pool in pools
        if float(pool.get("liquidity_usd", 0) or 0) >= _SOL_POOL_GT_ENRICH_MIN_LIQUIDITY_USD
        and (
            float(pool.get("price_usd", 0) or 0) <= 0
            or float((pool.get("volume") or {}).get("h24", 0) or 0) <= 0
            or _pool_tx_count(pool, "h24") <= 0
            or float((pool.get("price_change") or {}).get("h24", 0) or 0) == 0
        )
    ]
    candidates = sorted(candidates, key=_pool_sort_key, reverse=True)[:_SOL_POOL_GT_ENRICH_LIMIT]
    if not candidates:
        return pools

    rows_by_addr = {
        str(pool.get("pool_address", "") or "").lower(): idx
        for idx, pool in enumerate(pools)
        if pool.get("pool_address")
    }
    gt_rows = await asyncio.gather(
        *[_fetch_gt_pool_detail_row(chain_index, str(pool.get("pool_address", "") or "")) for pool in candidates],
        return_exceptions=True,
    )
    for candidate, gt_row in zip(candidates, gt_rows):
        if not isinstance(gt_row, dict) or not gt_row:
            continue
        idx = rows_by_addr.get(str(candidate.get("pool_address", "") or "").lower())
        if idx is None:
            continue
        pools[idx] = _merge_pool_rows(pools[idx], gt_row)

    return pools


async def _apply_solana_fee_backfill(pools: list[dict]) -> list[dict]:
    if not pools:
        return pools

    _apply_pumpswap_fee_rates(pools)

    redis = await get_redis()
    raydium_missing: list[str] = []
    orca_missing: list[str] = []
    meteora_missing: list[str] = []

    for pool in pools:
        if float(pool.get("fee_rate", 0) or 0) > 0:
            continue

        pool_addr = str(pool.get("pool_address", "") or "")
        dex_name = str(pool.get("dex_name", "") or "")
        if not pool_addr or not dex_name:
            continue

        cached_fee = await _load_cached_fee_rate(redis, dex_name, pool_addr)
        if cached_fee and cached_fee > 0:
            pool["fee_rate"] = cached_fee
            continue

        dex_lower = dex_name.lower()
        if "raydium" in dex_lower:
            raydium_missing.append(pool_addr)
        elif "orca" in dex_lower:
            orca_missing.append(pool_addr)
        elif "meteora" in dex_lower:
            meteora_missing.append(pool_addr)

    fee_by_pool: dict[str, float] = {}

    if raydium_missing:
        for i in range(0, len(raydium_missing), 20):
            batch = raydium_missing[i:i + 20]
            fee_by_pool.update(await get_raydium_fee_rates(batch))

    if orca_missing:
        orca_results = await asyncio.gather(
            *[get_orca_fee_rate(pool_addr) for pool_addr in orca_missing],
            return_exceptions=True,
        )
        for pool_addr, fee in zip(orca_missing, orca_results):
            if isinstance(fee, (int, float)) and fee > 0:
                fee_by_pool[pool_addr] = float(fee)

    if meteora_missing:
        meteora_results = await asyncio.gather(
            *[get_meteora_fee_rate(pool_addr) for pool_addr in meteora_missing],
            return_exceptions=True,
        )
        for pool_addr, fee in zip(meteora_missing, meteora_results):
            if isinstance(fee, (int, float)) and fee > 0:
                fee_by_pool[pool_addr] = float(fee)

    for pool in pools:
        pool_addr = str(pool.get("pool_address", "") or "")
        dex_name = str(pool.get("dex_name", "") or "")
        fee = fee_by_pool.get(pool_addr)
        if fee and fee > 0:
            pool["fee_rate"] = fee
            await _store_cached_fee_rate(redis, dex_name, pool_addr, fee)

    return pools


async def _apply_solana_pool_backfills(chain_index: str, token_address: str, pools: list[dict]) -> list[dict]:
    if not pools:
        return pools

    pools = await _apply_solana_protocol_enrichment(pools, token_address)
    pools = await _apply_solana_gt_pool_enrichment(chain_index, pools)
    pools = await _apply_solana_fee_backfill(pools)
    return pools


@router.get("/{chain_index}/{address}/pools")
async def get_pools(chain_index: str, address: str) -> list[dict]:
    """Return all liquidity pools via direct sources first, GeckoTerminal only as fallback."""
    direct_pools = await _get_direct_pools(chain_index, address)
    if direct_pools:
        logger.info("Direct pools found: %d for %s/%s", len(direct_pools), chain_index, address)
        return direct_pools

    network = _GT_NETWORK.get(chain_index)
    if not network:
        raise HTTPException(status_code=400, detail=f"Unsupported chain: {chain_index}")

    base_url = (
        f"https://api.geckoterminal.com/api/v2/networks/{network}"
        f"/tokens/{address}/pools"
        f"?include=dex,base_token,quote_token"
    )

    async def _fetch_page(client: httpx.AsyncClient, page: int) -> list[dict]:
        try:
            resp = await client.get(f"{base_url}&page={page}", headers={"Accept": "application/json"})
            if resp.status_code == 200:
                return _parse_gt_pools(resp.json())
            if resp.status_code == 404:
                logger.info("GT: token not indexed on %s (%s)", network, address)
            else:
                logger.warning("GT pools HTTP %s (page %d) for %s", resp.status_code, page, address)
        except Exception as e:
            logger.warning("GT pools request error (page %d): %s", page, e)
        return []

    # Fetch page 1 first; only fetch page 2 if page 1 is full (20 results = likely more exist)
    gt_pools: list[dict] = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        page1 = await _fetch_page(client, 1)
        if len(page1) == 20:
            page2 = await _fetch_page(client, 2)
        else:
            page2 = []
        seen: set[str] = set()
        for pool in page1 + page2:
            addr = pool["pool_address"]
            if addr not in seen:
                seen.add(addr)
                gt_pools.append(pool)

    logger.info("GT pools found: %d for %s/%s", len(gt_pools), chain_index, address)

    if gt_pools:
        if chain_index == "501":
            gt_pools = await _apply_solana_pool_backfills(chain_index, address, gt_pools)
        return gt_pools

    return []


# ─── OHLCV ────────────────────────────────────────────────────────────────────

@router.get("/{chain_index}/{address}/ohlcv")
async def get_ohlcv(
    chain_index: str,
    address: str,
    pool_address: str = Query(None),  # kept for backwards compat; OKX candles are token-level
    interval: str = Query("5m", description="5m or 15m"),
    limit: int = Query(60, le=300),
    refresh: int = Query(0, ge=0, include_in_schema=False),
) -> list[dict]:
    """Historical OHLCV bars via OKX candles API (token-level). oldest → newest."""
    bar = "15m" if interval == "15m" else "5m"
    redis = await get_redis()
    fresh_key, stale_key = _gt_cache_keys("ohlcv", chain_index, address, interval, limit)

    if refresh == 0:
        cached = await _load_cached_list(redis, fresh_key)
        if cached is not None:
            return cached

    async def _fetch() -> list[dict]:
        if refresh == 0:
            cached = await _load_cached_list(redis, fresh_key)
            if cached is not None:
                return cached

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.get(
                    "https://web3.okx.com/api/v6/dex/market/candles",
                    headers={"OK-ACCESS-KEY": settings.okx_access_key},
                    params={
                        "chainIndex": chain_index,
                        "tokenContractAddress": address,
                        "bar": bar,
                        "limit": str(limit),
                    },
                )
                if resp.status_code != 200:
                    logger.warning("OKX candles HTTP %s for %s", resp.status_code, address)
                    return await _serve_stale_or_empty(redis, stale_key, f"ohlcv_http_{resp.status_code}:{address}")
                data = resp.json()
            except Exception as e:
                logger.error("OKX candles request error: %s", e)
                return await _serve_stale_or_empty(redis, stale_key, f"ohlcv_error:{address}")

        try:
            # Response: [[ts_ms, o, h, l, c, vol, volUsd, confirm], ...]  newest first
            raw: list = data.get("data", [])
            payload = [
                {
                    "time":   int(row[0]) // 1000,
                    "open":   float(row[1]),
                    "high":   float(row[2]),
                    "low":    float(row[3]),
                    "close":  float(row[4]),
                    "volume": float(row[6]),  # volUsd
                }
                for row in reversed(raw)
            ]
        except (KeyError, TypeError, IndexError) as e:
            logger.warning("OKX candles parse: %s", e)
            return await _serve_stale_or_empty(redis, stale_key, f"ohlcv_parse:{address}")

        await _store_cached_list(redis, fresh_key, stale_key, payload)
        return payload

    inflight_key = fresh_key if refresh == 0 else f"{fresh_key}:refresh"
    return await _run_gt_inflight(inflight_key, _fetch)


# ─── TX History ───────────────────────────────────────────────────────────────

@router.get("/{chain_index}/{address}/tx-history")
async def get_tx_history(
    chain_index: str,
    address: str,
    pool_address: str = Query(None),  # kept for backwards compat; OKX trades are token-level
    interval: str = Query("5m", description="5m or 15m"),
    refresh: int = Query(0, ge=0, include_in_schema=False),
) -> list[dict]:
    """Aggregate recent trades from OKX into 5m/15m TX count buckets.

    Returns list of {time, buys, sells, buy_volume, sell_volume} oldest → newest.
    """
    redis = await get_redis()
    fresh_key, stale_key = _gt_cache_keys("tx-history", chain_index, address, interval, 0)

    if refresh == 0:
        cached = await _load_cached_list(redis, fresh_key)
        if cached is not None:
            return cached

    async def _fetch() -> list[dict]:
        if refresh == 0:
            cached = await _load_cached_list(redis, fresh_key)
            if cached is not None:
                return cached

        try:
            result = await mcp_client.call_tool("dex-okx-market-trades", {
                "chainIndex": chain_index,
                "tokenContractAddress": address,
                "limit": "300",
            })
            if isinstance(result, dict):
                trades = result.get("data", result.get("trades", []))
            else:
                trades = result if isinstance(result, list) else []
        except Exception as e:
            logger.error("OKX trades request error: %s", e)
            return await _serve_stale_or_empty(redis, stale_key, f"tx_error:{address}")

        if not trades:
            return []

        bucket_seconds = 15 * 60 if interval == "15m" else 5 * 60

        # Aggregate into time buckets
        # OKX trade fields: time (ms unix string), type ("buy"/"sell"), volume (USD string)
        buckets: dict[int, dict] = {}
        for trade in trades:
            ts_raw = trade.get("time") or trade.get("txTime") or trade.get("timestamp", 0)
            try:
                ts = int(ts_raw) // 1000  # ms → seconds
            except (TypeError, ValueError):
                continue

            kind: str = trade.get("type", "")
            volume: float = float(trade.get("volume") or 0)

            bucket = floor(ts / bucket_seconds) * bucket_seconds
            if bucket not in buckets:
                buckets[bucket] = {"time": bucket, "buys": 0, "sells": 0,
                                   "buy_volume": 0.0, "sell_volume": 0.0}
            if kind == "buy":
                buckets[bucket]["buys"] += 1
                buckets[bucket]["buy_volume"] += volume
            elif kind == "sell":
                buckets[bucket]["sells"] += 1
                buckets[bucket]["sell_volume"] += volume

        payload = sorted(buckets.values(), key=lambda x: x["time"])
        await _store_cached_list(redis, fresh_key, stale_key, payload)
        return payload

    inflight_key = fresh_key if refresh == 0 else f"{fresh_key}:refresh"
    return await _run_gt_inflight(inflight_key, _fetch)
