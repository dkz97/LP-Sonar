"""
CEX spot price fetcher — P2.3.2 CEX/DEX price divergence signal.

Primary use case: Solana (chain=501) pools on Raydium / Meteora / Orca.
All major Solana liquid tokens (SOL, JUP, JTO, BONK, WIF, PYTH, RAY)
map directly to OKX USDT spot pairs and are supported out of the box.

Also covers EVM Uniswap V3 pools (chain 1/8453/137) as a secondary case.

Implementation is chain-agnostic: it reads `base_token_symbol` from the
DexScreener pool state dict and looks up the OKX instId by symbol only.
No chain_index check needed.

Price comparison:
  dex_price_usd (DexScreener priceUsd) ≈ token price in USD
  OKX {TOKEN}-USDT last            ≈ token price in USDT ≈ USD
  Both quote against USD/USDT — directly comparable.
  USDC-quoted pools (SOL/USDC) vs USDT quote: USDC peg ≈ $1 → valid.

When divergence >= threshold (default 1%) the caller receives a modified
RegimeResult with regime overridden to "chaotic".  All failures are logged
at DEBUG level; the original regime_result is returned unchanged.

Symbol mapping:
  map_dex_to_okx_symbol(base, quote) → OKX instId (e.g. "SOL-USDT")
  Unmappable / DEX-only tokens return None → CEX check skipped silently.

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
# Solana tokens (SOL, JUP, JTO, BONK, WIF, PYTH, RAY) pass through as-is.
# Only entries that need renaming are listed here.
_UNWRAP: dict[str, str] = {
    # EVM wrapped tokens
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
    # Solana wrapped / bridged
    "WSOL":   "SOL",   # wrapped SOL used as quote in some Raydium pairs
}

# Stablecoin symbols — skip pairs where the base is a stablecoin.
_STABLES: frozenset[str] = frozenset({
    "USDC", "USDT", "DAI", "FRAX", "TUSD", "BUSD",
    "USDE", "PYUSD", "LUSD", "CRVUSD", "SFRAX", "GHO",
    "FDUSD", "USDP",
})

# Tokens confirmed to not have a spot listing on OKX.
# Returning None here avoids a guaranteed-51001 API round-trip.
# Add only tokens verified absent from OKX; otherwise let fail-open handle it.
_NO_CEX: frozenset[str] = frozenset({
    "ORCA",   # Orca DEX governance token — delisted / never listed on OKX
})

# Divergence threshold: override regime to "chaotic" when |dex - cex| / cex >= this.
DEFAULT_THRESHOLD = 0.01   # 1%


def map_dex_to_okx_symbol(base_symbol: str, quote_symbol: str) -> Optional[str]:
    """
    Return the OKX spot instId (e.g. 'SOL-USDT') for a DEX base/quote pair.

    Returns None when:
    - Base token is a stablecoin (stable-swap pool, no meaningful cross price)
    - Base token is in _NO_CEX (confirmed not listed on OKX — skip API call)
    - Token is exotic / LP / receipt token that slips through as None from OKX

    Solana tokens: SOL, JUP, JTO, BONK, WIF, PYTH, RAY → pass through as-is.
    EVM wrapped: WETH→ETH, WBTC→BTC, WMATIC→POL, WSOL→SOL, etc.

    Always returns USDT-quoted instId; OKX USDT pairs are most liquid.
    USDC-quoted DEX pools compare fine: USDC peg ≈ $1.00 ≈ USDT.
    """
    base = base_symbol.upper().strip()

    if base in _STABLES:
        return None

    if base in _NO_CEX:
        logger.debug("cex_price: %s is in _NO_CEX skip list", base)
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
