"""
Protocol-native fee rate fetcher (P2.1 Protocol-native fee tier).

Priority chain used by _fetch_pool_state():
  1. fetch_protocol_fee_rate() — native protocol API / subgraph
  2. _infer_fee_rate()          — DexScreener feeTier + static lookup (fallback)

Supported protocols:
  - Raydium CLMM   (chain 501)  : GET raydium_api_url/pools/info/ids?ids=...
  - Meteora DLMM   (chain 501)  : GET meteora_api_url/pair/...
  - Uniswap V3     (1, 8453)    : GraphQL subgraph (configurable URL)
  - Uniswap V3     (137)        : GraphQL subgraph (optional; config uniswap_v3_subgraph_polygon)

All functions return None on failure; callers must fall back to _infer_fee_rate().
"""
from __future__ import annotations
import logging
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Fee rate validity bounds (0.0001% – 5%)
_FEE_MIN = 0.000001
_FEE_MAX = 0.05

# Per-call HTTP timeouts (seconds)
_TIMEOUT = 8.0


def _validate_fee(fee_rate: float) -> Optional[float]:
    """Return fee_rate if in valid bounds [_FEE_MIN, _FEE_MAX], else None."""
    if _FEE_MIN <= fee_rate <= _FEE_MAX:
        return fee_rate
    logger.warning("fee_fetcher: fee_rate %.6f out of bounds [%.6f, %.2f]", fee_rate, _FEE_MIN, _FEE_MAX)
    return None


async def _raydium_clmm_fee(pool_address: str) -> Optional[float]:
    """
    Fetch fee rate from Raydium CLMM pool info API.

    Endpoint: GET {raydium_api_url}/pools/info/ids?ids={pool_address}
    Response path: data[0].config.tradeFeeRate  (integer units: 2500 → 0.25%)
    tradeFeeRate / 1_000_000 = fee_rate fraction
    """
    url = f"{settings.raydium_api_url}/pools/info/ids"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params={"ids": pool_address})
            if resp.status_code != 200:
                logger.debug("raydium_clmm_fee: HTTP %s pool=%.8s", resp.status_code, pool_address)
                return None
            data = resp.json()
    except Exception as e:
        logger.debug("raydium_clmm_fee: request error pool=%.8s: %s", pool_address, e)
        return None

    try:
        pool_data = (data.get("data") or [])[0]
        trade_fee_rate = pool_data["config"]["tradeFeeRate"]
        fee = float(trade_fee_rate) / 1_000_000.0
    except (IndexError, KeyError, TypeError, ValueError) as e:
        logger.debug("raydium_clmm_fee: parse error pool=%.8s: %s", pool_address, e)
        return None

    return _validate_fee(fee)


async def _meteora_fee(pool_address: str) -> Optional[float]:
    """
    Fetch fee rate from Meteora DLMM pair API.

    Endpoint: GET {meteora_api_url}/pair/{pool_address}
    Response path: base_fee_percentage  (string/float: "0.3" → 0.3%)
    base_fee_percentage / 100 = fee_rate fraction
    """
    url = f"{settings.meteora_api_url}/pair/{pool_address}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.debug("meteora_fee: HTTP %s pool=%.8s", resp.status_code, pool_address)
                return None
            data = resp.json()
    except Exception as e:
        logger.debug("meteora_fee: request error pool=%.8s: %s", pool_address, e)
        return None

    try:
        raw = data["base_fee_percentage"]
        fee = float(raw) / 100.0
    except (KeyError, TypeError, ValueError) as e:
        logger.debug("meteora_fee: parse error pool=%.8s: %s", pool_address, e)
        return None

    return _validate_fee(fee)


async def _uniswap_v3_subgraph_fee(pool_address: str, chain_index: str) -> Optional[float]:
    """
    Fetch fee rate from Uniswap V3 subgraph (configurable URL).

    Subgraph URLs configured in settings:
      chain 1    → uniswap_v3_subgraph_ethereum
      chain 8453 → uniswap_v3_subgraph_base
      chain 137  → uniswap_v3_subgraph_polygon (optional)

    Query: { pool(id: "<address>") { feeTier } }
    feeTier is integer units: 500 → 0.05%, 3000 → 0.3%, 10000 → 1%
    feeTier / 1_000_000 = fee_rate fraction
    """
    subgraph_url_map: dict[str, str] = {
        "1":    settings.uniswap_v3_subgraph_ethereum,
        "8453": settings.uniswap_v3_subgraph_base,
        "137":  settings.uniswap_v3_subgraph_polygon,
    }
    subgraph_url = subgraph_url_map.get(chain_index, "")
    if not subgraph_url:
        logger.debug(
            "uniswap_v3_subgraph_fee: no subgraph URL configured chain=%s pool=%.8s",
            chain_index, pool_address,
        )
        return None

    query = '{ pool(id: "%s") { feeTier } }' % pool_address.lower()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(subgraph_url, json={"query": query})
            if resp.status_code != 200:
                logger.debug(
                    "uniswap_v3_subgraph_fee: HTTP %s chain=%s pool=%.8s",
                    resp.status_code, chain_index, pool_address,
                )
                return None
            data = resp.json()
    except Exception as e:
        logger.debug(
            "uniswap_v3_subgraph_fee: request error chain=%s pool=%.8s: %s",
            chain_index, pool_address, e,
        )
        return None

    try:
        fee_tier = data["data"]["pool"]["feeTier"]
        fee = float(fee_tier) / 1_000_000.0
    except (KeyError, TypeError, ValueError) as e:
        logger.debug(
            "uniswap_v3_subgraph_fee: parse error chain=%s pool=%.8s: %s",
            chain_index, pool_address, e,
        )
        return None

    return _validate_fee(fee)


async def fetch_protocol_fee_rate(
    dex_id: str,
    pool_address: str,
    chain_index: str,
    pair: dict,
) -> Optional[float]:
    """
    Attempt to fetch the fee rate from the protocol's native API or subgraph.

    Returns the fee rate as a fraction (e.g. 0.003 for 0.3%) or None if:
      - Protocol not supported by native fetch
      - API call failed
      - Parsed fee is outside validity bounds

    Callers must fall back to _infer_fee_rate(dex_id, pair) when this returns None.

    Protocol routing:
      raydium-clmm  (chain 501)       → Raydium CLMM pool info API
      meteora-dlmm  (chain 501)       → Meteora DLMM pair API
      uniswap-v3    (chain 1, 8453, 137) → Uniswap V3 subgraph (URL must be configured)
    """
    dex = dex_id.lower()

    if dex == "raydium-clmm" and chain_index == "501":
        return await _raydium_clmm_fee(pool_address)

    if dex == "meteora-dlmm" and chain_index == "501":
        return await _meteora_fee(pool_address)

    if dex == "uniswap-v3" and chain_index in ("1", "8453", "137"):
        return await _uniswap_v3_subgraph_fee(pool_address, chain_index)

    # Protocol not covered by native fetch
    return None
