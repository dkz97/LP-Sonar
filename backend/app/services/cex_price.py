"""
CEX spot price fetcher — P2.3.2 CEX/DEX price divergence signal.

Fetches the base-token spot price from OKX public API (no auth required)
and computes relative divergence vs DexScreener priceUsd.

When divergence >= threshold (default 1%) the caller receives a modified
RegimeResult with regime overridden to "chaotic".  All failures are logged
at DEBUG level; the original regime_result is returned unchanged.

Symbol mapping:
  map_dex_to_okx_symbol(base, quote) → OKX instId (e.g. "ETH-USDT")
  Unmappable tokens return None → CEX check is skipped silently.

Main entry point:
  apply_cex_regime_override(regime_result, dex_price_usd, base_symbol, quote_symbol)
"""
from __future__ import annotations
import dataclasses
import logging
from typing import Optional

import httpx

from app.core.config import settings
from app.models.schemas import RegimeResult

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0   # keep short — CEX check must not block the recommendation

# ── Symbol normalisation ──────────────────────────────────────────────────────
# Wrapped / synthetic → canonical OKX base symbol.
# Only list tokens that need renaming; everything else is tried as-is.
_UNWRAP: dict[str, str] = {
    "WETH":   "ETH",
    "WBTC":   "BTC",
    "CBBTC":  "BTC",
    "TBTC":   "BTC",
    "WSTETH": "ETH",   # liquid-staking approximation; close enough for 1% threshold
    "RETH":   "ETH",
    "STETH":  "ETH",
    "WBNB":   "BNB",
    "WAVAX":  "AVAX",
    "WMATIC": "POL",   # MATIC → POL rename effective on OKX 2024
    "MATIC":  "POL",
    # "POL": "POL" — identity, no entry needed
}

# Stablecoin symbols — skip pairs where the base is a stablecoin.
_STABLES: frozenset[str] = frozenset({
    "USDC", "USDT", "DAI", "FRAX", "TUSD", "BUSD",
    "USDE", "PYUSD", "LUSD", "CRVUSD", "SFRAX", "GHO",
    "FDUSD", "USDP",
})

# Divergence threshold: override regime to "chaotic" when |dex - cex| / cex >= this.
DEFAULT_THRESHOLD = 0.01   # 1%


def map_dex_to_okx_symbol(base_symbol: str, quote_symbol: str) -> Optional[str]:
    """
    Return the OKX spot instId (e.g. 'ETH-USDT') for a DEX base/quote pair.

    Returns None when:
    - Base token is a stablecoin (no meaningful cross-vs-USD price)
    - Both tokens are stablecoins
    - Token cannot be mapped (exotic / LP / receipt token)

    The returned symbol always uses USDT as quote because OKX USDT pairs
    are most liquid and closest to USD spot.
    """
    base = base_symbol.upper().strip()

    if base in _STABLES:
        # e.g. USDC/USDT stable-swap pool — skip
        return None

    okx_base = _UNWRAP.get(base, base)
    return f"{okx_base}-USDT"


async def fetch_cex_spot_price(inst_id: str) -> Optional[float]:
    """
    Return the last traded spot price for an OKX instId, or None on any error.

    Failure modes handled silently (logged at DEBUG):
    - Network error / timeout
    - HTTP non-200
    - OKX error code (e.g. 51001 = instrument not found)
    - Missing / invalid price in response body
    """
    url = f"{settings.okx_cex_base_url}/api/v5/market/ticker"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params={"instId": inst_id})
            if resp.status_code != 200:
                logger.debug("cex_price: HTTP %s inst=%s", resp.status_code, inst_id)
                return None
            data = resp.json()
    except Exception as e:
        logger.debug("cex_price: request error inst=%s: %s", inst_id, e)
        return None

    try:
        if data.get("code") != "0":
            logger.debug("cex_price: API error code=%s inst=%s", data.get("code"), inst_id)
            return None
        price = float(data["data"][0]["last"])
        if price <= 0:
            return None
        return price
    except (KeyError, IndexError, TypeError, ValueError) as e:
        logger.debug("cex_price: parse error inst=%s: %s", inst_id, e)
        return None


async def apply_cex_regime_override(
    regime_result: RegimeResult,
    dex_price_usd: float,
    base_symbol: str,
    quote_symbol: str,
    threshold: float = DEFAULT_THRESHOLD,
) -> RegimeResult:
    """
    Override regime to 'chaotic' when CEX/DEX price divergence exceeds threshold.

    Returns regime_result unchanged (fail-open) when:
    - Symbol mapping not found (exotic token)
    - OKX request fails or times out
    - CEX price is zero or missing
    - Divergence is below threshold

    Only modifies regime_result.regime when ALL of:
    - Valid OKX price is available
    - abs(dex_price_usd - cex_price) / cex_price >= threshold

    Args:
        regime_result:  result of detect_regime(); may be any regime value
        dex_price_usd:  pool's current price in USD (DexScreener priceUsd)
        base_symbol:    base token symbol, e.g. 'WETH'
        quote_symbol:   quote token symbol, e.g. 'USDC'  (unused in mapping, kept for future)
        threshold:      divergence fraction above which regime becomes chaotic (default 1%)
    """
    inst_id = map_dex_to_okx_symbol(base_symbol, quote_symbol)
    if inst_id is None:
        logger.debug(
            "cex_price: no OKX mapping base=%s → skipping divergence check",
            base_symbol,
        )
        return regime_result

    cex_price = await fetch_cex_spot_price(inst_id)
    if cex_price is None:
        # fetch failed — fail-open, do not degrade recommendation
        return regime_result

    divergence = abs(dex_price_usd - cex_price) / cex_price
    logger.debug(
        "cex_price: inst=%s dex=%.4f cex=%.4f divergence=%.3f%%",
        inst_id, dex_price_usd, cex_price, divergence * 100,
    )

    if divergence >= threshold:
        logger.info(
            "cex_price: divergence %.2f%% > %.0f%% threshold inst=%s → override regime → chaotic",
            divergence * 100, threshold * 100, inst_id,
        )
        return dataclasses.replace(regime_result, regime="chaotic")

    return regime_result
