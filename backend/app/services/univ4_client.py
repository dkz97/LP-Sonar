"""
Uniswap V4 pool state fetcher.

V4 pools use a bytes32 poolId (66-char hex) instead of a contract address.
Data is sourced from two layers:

  Layer 1 — The Graph subgraph (TVL, volume, txCount, price, fee, age)
            Requires UNISWAP_V4_SUBGRAPH_BSC set in config / .env.

  Layer 2 — On-chain RPC (price, tick, lpFee, in-range liquidity)
            Available via BSC_RPC_URL; useful as a spot-data fallback/supplement.
            Historical recommendation flows still need subgraph metadata.

Chain support: BSC (chain_index "56").  Add more chains by extending
_V4_CHAIN_CONFIG below.
"""
from __future__ import annotations

import logging
import math
import time

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# ── V4 deployment config per chain ──────────────────────────────────────────
# pool_manager:  the singleton PoolManager contract address
# subgraph_url:  settings field name for the optional Graph subgraph URL
# rpc_url:       settings field name for the JSON-RPC endpoint
_V4_CHAIN_CONFIG: dict[str, dict] = {
    "56": {
        "pool_manager": "0x28e2Ea090877bF75740558f6BFB36A5ffeE9e9dF",
        "subgraph_url_attr": "uniswap_v4_subgraph_bsc",
        "rpc_url_attr": "bsc_rpc_url",
    },
}

# POOLS_SLOT in the V4 PoolManager (from StateLibrary.sol)
_POOLS_SLOT = (6).to_bytes(32, "big")


def is_v4_pool_id(pool_id: str) -> bool:
    """Return True if pool_id looks like a V4 bytes32 poolId (66 hex chars)."""
    return (
        isinstance(pool_id, str)
        and pool_id.startswith("0x")
        and len(pool_id) == 66
        and all(c in "0123456789abcdefABCDEF" for c in pool_id[2:])
    )


# ── The Graph layer ──────────────────────────────────────────────────────────

_GQL_POOL_QUERY = """
query PoolState($id: ID!) {
  pool(id: $id) {
    id
    token0 { id symbol }
    token1 { id symbol }
    feeTier
    tick
    sqrtPrice
    liquidity
    token0Price
    token1Price
    totalValueLockedUSD
    volumeUSD
    feesUSD
    txCount
    createdAtTimestamp
    poolDayData(first: 2, orderBy: date, orderDirection: desc) {
      volumeUSD
      txCount
    }
  }
}
"""


async def _fetch_v4_state_graph(chain_index: str, pool_id: str) -> dict | None:
    """
    Query The Graph subgraph for V4 pool state.
    Returns normalised dict or None if subgraph isn't configured / pool not found.
    """
    cfg = _V4_CHAIN_CONFIG.get(chain_index)
    if not cfg:
        return None

    subgraph_url = getattr(settings, cfg["subgraph_url_attr"], "")
    if not subgraph_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                subgraph_url,
                json={"query": _GQL_POOL_QUERY, "variables": {"id": pool_id.lower()}},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("V4 subgraph fetch failed pool=%.10s: %s", pool_id, exc)
        return None

    pool = (data.get("data") or {}).get("pool")
    if not pool:
        logger.debug("V4 subgraph: pool not found id=%s", pool_id)
        return None

    def _f(v, default: float = 0.0) -> float:
        try:
            return float(v or default)
        except (TypeError, ValueError):
            return default

    def _i(v, default: int = 0) -> int:
        try:
            return int(v or default)
        except (TypeError, ValueError):
            return default

    # Prefer yesterday's volumeUSD over cumulative for a "24h" proxy
    day_data = pool.get("poolDayData") or []
    volume_24h = _f(day_data[0]["volumeUSD"]) if day_data else 0.0
    trade_count_1h = _i(day_data[0]["txCount"]) // 24 if day_data else 0

    fee_tier = _i(pool.get("feeTier"))       # e.g. 2093 → 0.2093%
    fee_rate = fee_tier / 1_000_000.0 if fee_tier else 0.003

    token0 = pool.get("token0") or {}
    token1 = pool.get("token1") or {}
    token0_symbol = token0.get("symbol", "")
    token1_symbol = token1.get("symbol", "")

    # price: token0 per token1 means 1 token1 = token0Price token0
    # We expose price as "USD price of base token" — use token1Price (token1 per token0 → USD if T1=stable)
    # token0Price = token0 per token1 (price of token1 in token0 units)
    # token1Price = token1 per token0 (price of token0 in token1 units)
    token0_price = _f(pool.get("token0Price"))
    token1_price = _f(pool.get("token1Price"))

    create_ts = _i(pool.get("createdAtTimestamp"))
    pool_age_days = (time.time() - create_ts) / 86400.0 if create_ts > 0 else 0.0

    tvl_usd = _f(pool.get("totalValueLockedUSD"))
    tick = _i(pool.get("tick"))

    # Orient base/quote so the non-stable is "base" and stable/major is "quote".
    # V4 sorts tokens by address, so token0 may be the stable (e.g. USDT < SIREN).
    t0_is_stable = _classify_quote(token0_symbol) in ("stable", "major")
    if t0_is_stable:
        base_symbol   = token1_symbol
        base_address  = token1.get("id", "")
        quote_symbol  = token0_symbol
        current_price = token0_price   # token0 per token1 = stable per base → USD price of base
    else:
        base_symbol   = token0_symbol
        base_address  = token0.get("id", "")
        quote_symbol  = token1_symbol
        current_price = token1_price   # token1 per token0 = quote per base

    return {
        "pool_address":       pool_id,
        "chain_index":        chain_index,
        "protocol":           "Uniswap V4",
        "dex_id":             "uniswap-v4",
        "fee_rate":           fee_rate,
        "tvl_usd":            tvl_usd,
        "volume_24h":         volume_24h,
        "volume_1h":          volume_24h / 24.0,
        "trade_count_1h":     trade_count_1h,
        "pool_age_days":      pool_age_days,
        "current_price":      current_price,
        "price_change_24h":   0.0,   # not in subgraph schema
        "price_change_4h":    0.0,
        "price_change_1h":    0.0,
        "base_token_address": base_address,
        "base_token_symbol":  base_symbol,
        "quote_token_symbol": quote_symbol,
        "quote_type":         _classify_quote(quote_symbol),
        "buy_volume":         volume_24h / 24.0 * 0.5,
        "sell_volume":        volume_24h / 24.0 * 0.5,
        "_tick":              tick,
        "_source":            "graph",
    }


# ── On-chain RPC layer ───────────────────────────────────────────────────────

def _keccak256(data: bytes) -> bytes:
    from eth_hash.auto import keccak
    return keccak(data)


def _pool_state_slot(pool_id: str) -> bytes:
    """
    Compute storage slot of Pool.State in PoolManager for a given poolId.
    slot = keccak256(poolId_bytes32 || POOLS_SLOT)
    """
    pool_id_bytes = bytes.fromhex(pool_id[2:])
    return _keccak256(pool_id_bytes + _POOLS_SLOT)


async def _eth_get_storage(rpc_url: str, contract: str, slot_hex: str) -> str:
    """eth_getStorageAt JSON-RPC call, returns 32-byte hex string."""
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getStorageAt",
        "params": [contract, slot_hex, "latest"],
        "id": 1,
    }
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(rpc_url, json=payload)
        resp.raise_for_status()
    return resp.json()["result"]


async def _fetch_v4_state_rpc(chain_index: str, pool_id: str) -> dict | None:
    """
    Fetch V4 pool spot state directly from on-chain storage.
    Returns price, tick, lpFee, in-range liquidity.
    Does NOT return token metadata, TVL (USD), or volume — these require an indexer.
    """
    cfg = _V4_CHAIN_CONFIG.get(chain_index)
    if not cfg:
        return None

    rpc_url = getattr(settings, cfg["rpc_url_attr"], "")
    if not rpc_url:
        return None

    pool_manager = cfg["pool_manager"]

    try:
        state_slot = _pool_state_slot(pool_id)
        state_slot_int = int.from_bytes(state_slot, "big")
        slot0_hex = "0x" + format(state_slot_int, "064x")
        liq_hex = "0x" + format(state_slot_int + 3, "064x")

        slot0_raw, liq_raw = (
            await _eth_get_storage(rpc_url, pool_manager, slot0_hex),
            await _eth_get_storage(rpc_url, pool_manager, liq_hex),
        )
    except Exception as exc:
        logger.warning("V4 RPC fetch failed pool=%.10s: %s", pool_id, exc)
        return None

    slot0_val = int(slot0_raw, 16)
    liq_val   = int(liq_raw, 16)

    # Slot0 packing (LSB first):
    #   bits   0-159  sqrtPriceX96 (uint160)
    #   bits 160-183  tick (int24)
    #   bits 184-207  protocolFee (uint24)
    #   bits 208-231  lpFee (uint24)
    sqrt_price = slot0_val & ((1 << 160) - 1)
    if sqrt_price == 0:
        logger.debug("V4 RPC: pool not initialized pool=%.10s", pool_id)
        return None

    tick_raw = (slot0_val >> 160) & ((1 << 24) - 1)
    tick = tick_raw if tick_raw < (1 << 23) else tick_raw - (1 << 24)
    lp_fee = (slot0_val >> 208) & ((1 << 24) - 1)

    price_ratio = (sqrt_price / (1 << 96)) ** 2   # token1 per token0

    return {
        "pool_address":       pool_id,
        "chain_index":        chain_index,
        "protocol":           "Uniswap V4",
        "dex_id":             "uniswap-v4",
        "fee_rate":           lp_fee / 1_000_000.0,
        "tvl_usd":            0.0,    # not available from RPC alone
        "volume_24h":         0.0,
        "volume_1h":          0.0,
        "trade_count_1h":     0,
        "pool_age_days":      0.0,    # not available from RPC alone
        "current_price":      price_ratio,
        "price_change_24h":   0.0,
        "price_change_4h":    0.0,
        "price_change_1h":    0.0,
        "base_token_address": "",
        "base_token_symbol":  "",
        "quote_token_symbol": "",
        "quote_type":         "unknown",
        "buy_volume":         0.0,
        "sell_volume":        0.0,
        "_tick":              tick,
        "_source":            "rpc",
        "_raw_liquidity":     liq_val,
    }


# ── Public entry point ───────────────────────────────────────────────────────

async def fetch_v4_pool_state(chain_index: str, pool_id: str) -> dict | None:
    """
    Fetch Uniswap V4 pool state.

    Tries The Graph subgraph first (rich data), falls back to on-chain RPC
    (spot price/fee/liquidity only, with no token metadata, TVL, or volume).

    Returns None if chain is unsupported or pool is not found on either source.
    """
    if chain_index not in _V4_CHAIN_CONFIG:
        logger.warning("V4: unsupported chain_index=%s for pool %.10s", chain_index, pool_id)
        return None

    # Layer 1: subgraph
    state = await _fetch_v4_state_graph(chain_index, pool_id)
    if state:
        logger.debug("V4 pool state from subgraph pool=%.10s tvl=%.0f", pool_id, state["tvl_usd"])
        return state

    # Layer 2: on-chain RPC
    state = await _fetch_v4_state_rpc(chain_index, pool_id)
    if state:
        logger.info(
            "V4 pool state from RPC only (no subgraph configured) pool=%.10s fee=%.4f%%",
            pool_id, state["fee_rate"] * 100,
        )
    return state


# ── Helper ───────────────────────────────────────────────────────────────────

def _classify_quote(symbol: str) -> str:
    s = symbol.upper()
    if s in {"USDT", "USDC", "DAI", "BUSD", "FDUSD", "TUSD"}:
        return "stable"
    if s in {"WBNB", "BNB", "ETH", "WETH", "BTC", "WBTC"}:
        return "major"
    return "alt"
