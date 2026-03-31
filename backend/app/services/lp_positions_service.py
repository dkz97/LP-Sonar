"""
LP Position Holders service.

Fetches LP position data for a given pool across chains:
  - EVM V4 (BSC):         Uniswap V4 subgraph (univ4_client)
  - EVM V3 (ETH/Base/Polygon): Uniswap V3 subgraph (direct GraphQL)
  - Solana (Meteora DLMM): Meteora API (solana_dex_client)

Returns a PoolPositionSummary with top-50 positions and aggregate stats.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Optional

import httpx

from app.core.config import settings
from app.models.schemas import LPPosition, PoolPositionSummary
from app.services.univ4_client import get_v4_pool_positions, is_v4_pool_id
from app.services.solana_dex_client import get_meteora_pool_positions

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

# V3 subgraph URLs keyed by chain_index (from Settings)
_V3_SUBGRAPH_ATTR: dict[str, str] = {
    "1":   "uniswap_v3_subgraph_ethereum",
    "8453": "uniswap_v3_subgraph_base",
    "137": "uniswap_v3_subgraph_polygon",
}

_STABLES = {"USDT", "USDC", "DAI", "BUSD", "FDUSD", "TUSD", "USDS", "PYUSD"}

# ── V3 Math helpers ────────────────────────────────────────────────────────────

def _tick_to_sqrt_price(tick: int) -> float:
    """Tick → sqrt(price), where price = token1/token0."""
    return 1.0001 ** (tick / 2.0)


def _sqrt_price_x96_to_float(val: str) -> float:
    try:
        return float(val) / (2 ** 96)
    except (ValueError, TypeError):
        return 0.0


def _get_amounts_from_liquidity(
    liquidity: int,
    sqrt_price: float,
    tick_lower: int,
    tick_upper: int,
) -> tuple[float, float]:
    """
    Standard Uniswap V3 liquidity math.
    Returns (amount0_raw, amount1_raw) — divide by 10^decimals for human units.
    """
    if liquidity <= 0 or sqrt_price <= 0:
        return 0.0, 0.0

    sqrt_lower = _tick_to_sqrt_price(tick_lower)
    sqrt_upper = _tick_to_sqrt_price(tick_upper)

    if sqrt_price <= sqrt_lower:
        amount0 = liquidity * (1.0 / sqrt_lower - 1.0 / sqrt_upper)
        amount1 = 0.0
    elif sqrt_price >= sqrt_upper:
        amount0 = 0.0
        amount1 = liquidity * (sqrt_upper - sqrt_lower)
    else:
        amount0 = liquidity * (1.0 / sqrt_price - 1.0 / sqrt_upper)
        amount1 = liquidity * (sqrt_price - sqrt_lower)

    return amount0, amount1


def _infer_token_prices(
    token0_sym: str,
    token1_sym: str,
    token0_price_field: float,  # subgraph token0Price = token0 per token1
    token1_price_field: float,  # subgraph token1Price = token1 per token0
    tvl_usd: float,
    tvl_token0: float,
    tvl_token1: float,
) -> tuple[float, float]:
    """
    Return (price0_usd, price1_usd).

    Uniswap subgraph definitions:
      token0Price = how many token0 per 1 token1  (= 1/price_of_token0_in_t1)
      token1Price = how many token1 per 1 token0  (= price_of_token0_in_t1)

    So if token1 is USDC: price0_usd = token1Price (USDC per token0).
    If token0 is USDC: price1_usd = token0Price (USDC per token1... no).
    """
    t0 = token0_sym.upper()
    t1 = token1_sym.upper()

    if t1 in _STABLES:
        # token1 = $1; token1Price = token1 per token0 = USD per token0
        return token1_price_field, 1.0
    if t0 in _STABLES:
        # token0 = $1; token0Price = token0 per token1 = USD per token1
        return 1.0, token0_price_field
    # No stable — estimate from TVL split
    if tvl_usd > 0 and tvl_token0 > 0 and tvl_token1 > 0:
        return (tvl_usd / 2.0) / tvl_token0, (tvl_usd / 2.0) / tvl_token1
    return 0.0, 0.0


def _tick_to_display_price(
    tick: int,
    token0_decimals: int,
    token1_decimals: int,
    t0_is_base: bool,
) -> float:
    """
    Convert a tick to a human-readable price (base token in quote token units).
    """
    raw_ratio = 1.0001 ** tick * (10 ** (token0_decimals - token1_decimals))
    if t0_is_base:
        # price = token1 per token0 (but token1Price from subgraph is also this)
        return raw_ratio
    else:
        return 1.0 / raw_ratio if raw_ratio != 0 else 0.0


# ── GraphQL for V3 subgraph ────────────────────────────────────────────────────

_GQL_V3_POSITIONS = """
query V3PoolPositions($poolId: String!, $skip: Int!) {
  pool(id: $poolId) {
    sqrtPrice
    tick
    token0Price
    token1Price
    token0 { id symbol decimals }
    token1 { id symbol decimals }
    totalValueLockedToken0
    totalValueLockedToken1
    totalValueLockedUSD
  }
  positions(
    where: { pool: $poolId, liquidity_gt: "0" }
    first: 100
    skip: $skip
    orderBy: liquidity
    orderDirection: desc
  ) {
    id
    owner
    tickLower { tickIdx }
    tickUpper { tickIdx }
    liquidity
    depositedToken0
    depositedToken1
    withdrawnToken0
    withdrawnToken1
    collectedFeesToken0
    collectedFeesToken1
  }
}
"""


async def _fetch_v3_positions(subgraph_url: str, pool_id: str, skip: int = 0) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                subgraph_url,
                json={
                    "query": _GQL_V3_POSITIONS,
                    "variables": {"poolId": pool_id.lower(), "skip": skip},
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("V3 positions query failed pool=%.10s: %s", pool_id, exc)
        return None

    gql_data = data.get("data") or {}
    return {
        "pool":      gql_data.get("pool"),
        "positions": gql_data.get("positions") or [],
    }


# ── Position parsing ───────────────────────────────────────────────────────────

def _parse_evm_positions(
    pool_meta: dict,
    raw_positions: list[dict],
    current_pool_tick: Optional[int] = None,
) -> list[LPPosition]:
    """
    Convert raw subgraph positions + pool metadata into LPPosition objects.
    """
    def _f(v, d: float = 0.0) -> float:
        try:
            return float(v or d)
        except (TypeError, ValueError):
            return d

    def _i(v, d: int = 0) -> int:
        try:
            return int(v or d)
        except (TypeError, ValueError):
            return d

    token0 = pool_meta.get("token0") or {}
    token1 = pool_meta.get("token1") or {}
    t0_sym = token0.get("symbol", "")
    t1_sym = token1.get("symbol", "")
    t0_dec = _i(token0.get("decimals"), 18)
    t1_dec = _i(token1.get("decimals"), 18)

    sqrt_price_raw = pool_meta.get("sqrtPrice", "0")
    sqrt_price = _sqrt_price_x96_to_float(sqrt_price_raw)
    pool_tick = _i(pool_meta.get("tick"))
    if current_pool_tick is not None:
        pool_tick = current_pool_tick

    token0_price = _f(pool_meta.get("token0Price"))  # token0 per token1
    token1_price = _f(pool_meta.get("token1Price"))  # token1 per token0
    tvl_usd = _f(pool_meta.get("totalValueLockedUSD"))
    tvl_t0 = _f(pool_meta.get("totalValueLockedToken0"))
    tvl_t1 = _f(pool_meta.get("totalValueLockedToken1"))

    price0_usd, price1_usd = _infer_token_prices(
        t0_sym, t1_sym,
        token0_price, token1_price,
        tvl_usd, tvl_t0, tvl_t1,
    )

    # Determine base token for price display
    t0_is_base = t1_sym.upper() in _STABLES or t0_sym.upper() not in _STABLES

    results: list[LPPosition] = []

    for raw in raw_positions:
        owner = raw.get("owner", "")
        tick_lower_obj = raw.get("tickLower") or {}
        tick_upper_obj = raw.get("tickUpper") or {}
        tick_lower = _i(tick_lower_obj.get("tickIdx") if isinstance(tick_lower_obj, dict) else tick_lower_obj)
        tick_upper = _i(tick_upper_obj.get("tickIdx") if isinstance(tick_upper_obj, dict) else tick_upper_obj)
        liquidity_str = str(raw.get("liquidity") or "0")

        dep0 = _f(raw.get("depositedToken0"))
        dep1 = _f(raw.get("depositedToken1"))
        wit0 = _f(raw.get("withdrawnToken0"))
        wit1 = _f(raw.get("withdrawnToken1"))
        fee0 = _f(raw.get("collectedFeesToken0"))
        fee1 = _f(raw.get("collectedFeesToken1"))

        fees_usd = fee0 * price0_usd + fee1 * price1_usd

        # Current position value via V3 liquidity math
        current_value_usd: Optional[float] = None
        pnl_usd: Optional[float] = None

        try:
            liq_int = int(liquidity_str)
            amt0_raw, amt1_raw = _get_amounts_from_liquidity(
                liq_int, sqrt_price, tick_lower, tick_upper,
            )
            # Convert raw units to human units
            amt0 = amt0_raw / (10 ** t0_dec) if t0_dec > 0 else amt0_raw
            amt1 = amt1_raw / (10 ** t1_dec) if t1_dec > 0 else amt1_raw

            if price0_usd > 0 or price1_usd > 0:
                current_value_usd = amt0 * price0_usd + amt1 * price1_usd
                # Mark-to-market PnL: value now + withdrawn + fees - deposited (at current prices)
                deposited_usd = dep0 * price0_usd + dep1 * price1_usd
                withdrawn_usd = wit0 * price0_usd + wit1 * price1_usd
                pnl_usd = (current_value_usd or 0.0) + withdrawn_usd + fees_usd - deposited_usd
        except Exception:
            pass

        # Price range from ticks
        price_lower = _tick_to_display_price(tick_lower, t0_dec, t1_dec, t0_is_base)
        price_upper = _tick_to_display_price(tick_upper, t0_dec, t1_dec, t0_is_base)

        in_range = tick_lower <= pool_tick <= tick_upper

        results.append(LPPosition(
            owner=owner,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            price_lower=min(price_lower, price_upper),
            price_upper=max(price_lower, price_upper),
            liquidity=liquidity_str,
            deposited_token0=dep0,
            deposited_token1=dep1,
            fees_token0=fee0,
            fees_token1=fee1,
            fees_usd=fees_usd,
            current_value_usd=current_value_usd,
            pnl_usd=pnl_usd,
            in_range=in_range,
        ))

    return results


# ── Chain-specific fetchers ────────────────────────────────────────────────────

async def _get_evm_v3_positions(
    chain_index: str, pool_address: str,
) -> PoolPositionSummary:
    attr = _V3_SUBGRAPH_ATTR.get(chain_index, "")
    subgraph_url = getattr(settings, attr, "") if attr else ""

    if not subgraph_url:
        return PoolPositionSummary(
            pool_address=pool_address, chain_index=chain_index,
            total_positions=0, active_positions=0,
            top10_liquidity_pct=0.0, positions_fetched=0, positions=[],
            cached_at=int(time.time()),
            unavailable_reason="v3_subgraph_not_configured",
        )

    result = await _fetch_v3_positions(subgraph_url, pool_address)
    if not result or not result.get("pool"):
        return PoolPositionSummary(
            pool_address=pool_address, chain_index=chain_index,
            total_positions=0, active_positions=0,
            top10_liquidity_pct=0.0, positions_fetched=0, positions=[],
            cached_at=int(time.time()),
            unavailable_reason="pool_not_found_in_subgraph",
        )

    positions = _parse_evm_positions(result["pool"], result["positions"])
    return _build_summary(pool_address, chain_index, positions)


async def _get_evm_v4_positions(
    chain_index: str, pool_address: str,
) -> PoolPositionSummary:
    # BSC V4 subgraph Position type lacks liquidity/tickLower/tickUpper/depositedToken*
    # fields — the V3-style query returns 9 GraphQL errors. Do not attempt the call.
    return PoolPositionSummary(
        pool_address=pool_address, chain_index=chain_index,
        total_positions=0, active_positions=0,
        top10_liquidity_pct=0.0, positions_fetched=0, positions=[],
        cached_at=int(time.time()),
        unavailable_reason="v4_positions_not_supported_with_current_subgraph",
    )


async def _get_solana_positions(pool_address: str) -> PoolPositionSummary:
    raw = await get_meteora_pool_positions(pool_address)

    if not raw:
        return PoolPositionSummary(
            pool_address=pool_address, chain_index="501",
            total_positions=0, active_positions=0,
            top10_liquidity_pct=0.0, positions_fetched=0, positions=[],
            cached_at=int(time.time()),
            unavailable_reason="meteora_positions_api_unavailable",
        )

    positions: list[LPPosition] = []
    for row in raw:
        tick_l = row.get("tick_lower", 0)
        tick_u = row.get("tick_upper", 0)
        fee_x = row.get("fee_x", 0.0)
        fee_y = row.get("fee_y", 0.0)
        positions.append(LPPosition(
            owner=row["owner"],
            tick_lower=tick_l,
            tick_upper=tick_u,
            price_lower=float(tick_l),   # bin_id, not a price ratio — show as-is
            price_upper=float(tick_u),
            liquidity=str(row.get("liquidity", 0)),
            deposited_token0=row.get("deposited_x", 0.0),
            deposited_token1=row.get("deposited_y", 0.0),
            fees_token0=fee_x,
            fees_token1=fee_y,
            fees_usd=row.get("total_fee_usd", 0.0),
            current_value_usd=None,
            pnl_usd=None,
            in_range=False,  # unknown without current bin
        ))

    return _build_summary(pool_address, "501", positions)


def _build_summary(
    pool_address: str,
    chain_index: str,
    positions: list[LPPosition],
) -> PoolPositionSummary:
    total = len(positions)
    active = sum(1 for p in positions if p.in_range)

    # Liquidity concentration: top 10 positions share of total fetched liquidity
    liq_values = []
    for p in positions:
        try:
            liq_values.append(int(p.liquidity))
        except (ValueError, TypeError):
            liq_values.append(0)

    total_liq = sum(liq_values)
    top10_liq = sum(sorted(liq_values, reverse=True)[:10])
    top10_pct = top10_liq / total_liq if total_liq > 0 else 0.0

    position_dicts = [asdict(p) for p in positions[:50]]

    return PoolPositionSummary(
        pool_address=pool_address,
        chain_index=chain_index,
        total_positions=total,
        active_positions=active,
        top10_liquidity_pct=top10_pct,
        positions_fetched=total,
        positions=position_dicts,
        cached_at=int(time.time()),
    )


# ── Public entry point ─────────────────────────────────────────────────────────

async def get_pool_positions(
    chain_index: str,
    pool_address: str,
) -> PoolPositionSummary:
    """
    Fetch LP position holders for a pool.

    Routing:
      - chain_index "501"         → Solana (Meteora DLMM)
      - chain_index "56" + V4 id  → Uniswap V4 BSC subgraph
      - chain_index "1","8453","137" → Uniswap V3 subgraph
      - others                    → unsupported, returns empty summary
    """
    if chain_index == "501":
        result = await _get_solana_positions(pool_address)
    elif chain_index == "56":
        result = await _get_evm_v4_positions(chain_index, pool_address)
    elif chain_index in _V3_SUBGRAPH_ATTR:
        result = await _get_evm_v3_positions(chain_index, pool_address)
    else:
        result = PoolPositionSummary(
            pool_address=pool_address,
            chain_index=chain_index,
            total_positions=0,
            active_positions=0,
            top10_liquidity_pct=0.0,
            positions_fetched=0,
            positions=[],
            cached_at=int(time.time()),
            unavailable_reason=f"chain_{chain_index}_position_data_not_supported",
        )

    if result.unavailable_reason:
        logger.info(
            "positions unavailable chain=%s pool=%.10s reason=%s",
            chain_index, pool_address, result.unavailable_reason,
        )
    else:
        logger.info(
            "positions ok chain=%s pool=%.10s total=%d active=%d",
            chain_index, pool_address, result.total_positions, result.active_positions,
        )
    return result
