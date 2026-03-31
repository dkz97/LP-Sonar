"""
Solana DEX Client: direct integration with Meteora, Raydium, and Orca APIs.

Supplements DEX Screener data with protocol-accurate fee rates and pool metadata.
Only used for Solana (chain_index == "501").

APIs:
  Meteora DLMM: https://dlmm-api.meteora.ag / https://dlmm.datapi.meteora.ag
  Meteora DAMM: https://damm-v2.datapi.meteora.ag
  Raydium v3:   https://api-v3.raydium.io
  Orca:         https://api.orca.so / https://api.mainnet.orca.so
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)
_STABLE_QUOTES = {"USDC", "USDT", "PYUSD", "USDS", "USDY"}
_SOL_QUOTES = {"SOL", "WSOL", "JSOL", "JITOSOL", "MSOL", "BSOL", "SOLP"}


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val or default)
    except (TypeError, ValueError):
        return default


def _parse_iso_to_ts(s: str | None) -> int:
    """Parse ISO 8601 string to Unix timestamp (seconds). Returns 0 on failure."""
    if not s:
        return 0
    try:
        # Handle both 'Z' suffix and '+00:00'
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp())
    except Exception:
        return 0


def _to_unix_seconds(ts) -> int:
    """Convert millisecond or second Unix timestamp to seconds."""
    try:
        t = int(ts or 0)
        return t // 1000 if t > 1_000_000_000_000 else t
    except (TypeError, ValueError):
        return 0


def _dedupe_pools_by_address(pools: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for pool in pools:
        addr = str(pool.get("poolContractAddress", "") or pool.get("poolAddress", "") or "")
        if not addr or addr in seen:
            continue
        seen.add(addr)
        out.append(pool)
    return out


def _price_native_from_token_prices(token_price_usd: float, quote_price_usd: float) -> float:
    if token_price_usd > 0 and quote_price_usd > 0:
        return token_price_usd / quote_price_usd
    return 0.0


def _pick_token_side(token_x: dict, token_y: dict, token_address: str) -> tuple[dict, dict] | tuple[None, None]:
    token_lower = token_address.lower()
    addr_x = (token_x.get("address") or "").lower()
    addr_y = (token_y.get("address") or "").lower()
    if addr_x == token_lower:
        return token_x, token_y
    if addr_y == token_lower:
        return token_y, token_x
    return None, None


# ─── Meteora DLMM ─────────────────────────────────────────────────────────────

def _normalize_meteora_pool(pool: dict, token_address: str) -> dict | None:
    """Convert a Meteora DLMM pair dict to OKX-compatible pool format."""
    pool_addr = pool.get("address", "")
    if not pool_addr:
        return None

    mint_x = (pool.get("mint_x") or "").lower()
    mint_y = (pool.get("mint_y") or "").lower()
    token_lower = token_address.lower()

    # Determine quote token (the one that is NOT our target token)
    if mint_x == token_lower:
        quote_addr = mint_y
    elif mint_y == token_lower:
        quote_addr = mint_x
    else:
        # Pool doesn't contain our token — skip
        return None

    # Parse quote symbol from pool name (format: "TOKEN-QUOTE" or "QUOTE-TOKEN")
    name = pool.get("name", "")
    parts = name.split("-")
    if len(parts) == 2:
        # Infer which part is the quote symbol
        quote_sym = parts[1] if parts[0].upper() in name.upper() else parts[0]
    else:
        quote_sym = ""

    # fee_rate: base_fee_percentage is in percent (e.g. 0.1 means 0.1%)
    fee_pct = _safe_float(pool.get("base_fee_percentage"))
    fee_rate = fee_pct / 100.0  # convert percent → decimal (0.001)

    tvl = _safe_float(pool.get("liquidity"))
    vol24h = _safe_float(pool.get("trade_volume_24h"))
    vol1h = vol24h / 24.0 if vol24h > 0 else 0.0
    create_ts = _parse_iso_to_ts(pool.get("created_at"))

    return {
        "poolContractAddress":       pool_addr,
        "quoteTokenContractAddress": quote_addr,
        "quoteTokenSymbol":          quote_sym,
        "protocolName":              "Meteora DLMM",
        "feeRate":                   fee_rate,
        "liquidity":                 tvl,
        "volumeUsd24H":              vol24h,
        "volumeUsd1H":               vol1h,
        "createTime":                create_ts,
        "bin_step":                  pool.get("bin_step", 0),   # DLMM-specific
        "source":                    "meteora",
    }


async def get_meteora_pools(token_address: str) -> list[dict]:
    """
    Fetch all Meteora DLMM pools containing *token_address*.

    Uses: GET {meteora_api_url}/pair/all_by_groups?groups={token_address}
    """
    url = f"{settings.meteora_api_url}/pair/all_by_groups"
    params = {"groups": token_address}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.debug("Meteora fetch failed token=%.8s: %s", token_address, e)
        return []

    # Response may be a list directly or wrapped in a dict
    pairs: list[dict] = data if isinstance(data, list) else (data.get("data") or [])

    results: list[dict] = []
    for pool in pairs:
        normalized = _normalize_meteora_pool(pool, token_address)
        if normalized:
            results.append(normalized)

    logger.debug("Meteora token=%.8s: %d pools found", token_address, len(results))
    return results


# ─── Meteora DAMM v2 ──────────────────────────────────────────────────────────

def _normalize_meteora_damm_pool(pool: dict, token_address: str) -> dict | None:
    """Convert a Meteora DAMM v2 pool dict to OKX-compatible pool format."""
    pool_addr = pool.get("address", "")
    if not pool_addr:
        return None

    token_x = pool.get("token_x") or {}
    token_y = pool.get("token_y") or {}
    target_token, quote_token = _pick_token_side(token_x, token_y, token_address)
    if target_token is None or quote_token is None:
        return None

    quote_addr = (quote_token.get("address") or "").lower()
    quote_sym = quote_token.get("symbol", "")
    target_price_usd = _safe_float(target_token.get("price"))
    target_market_cap_usd = _safe_float(target_token.get("market_cap"))
    quote_price_usd = _safe_float(quote_token.get("price"))

    cfg = pool.get("pool_config") or {}
    base_fee_pct = _safe_float(cfg.get("base_fee_pct"))
    dynamic_fee_pct = _safe_float(pool.get("dynamic_fee_pct"))
    fee_rate = (base_fee_pct + dynamic_fee_pct) / 100.0

    volume = pool.get("volume") or {}
    return {
        "poolContractAddress":       pool_addr,
        "quoteTokenContractAddress": quote_addr,
        "quoteTokenSymbol":          quote_sym,
        "protocolName":              "Meteora",
        "feeRate":                   fee_rate,
        "liquidity":                 _safe_float(pool.get("tvl")),
        "priceUsd":                  target_price_usd,
        "priceNative":               _price_native_from_token_prices(target_price_usd, quote_price_usd),
        "marketCapUsd":              target_market_cap_usd,
        "volumeUsd30M":              _safe_float(volume.get("30m")),
        "volumeUsd24H":              _safe_float(volume.get("24h")),
        "volumeUsd1H":               _safe_float(volume.get("1h")),
        "createTime":                _to_unix_seconds(pool.get("created_at")),
        "source":                    "meteora_damm",
    }


async def _fetch_meteora_damm_pools(token_address: str, side: str) -> list[dict]:
    url = f"{settings.meteora_damm_api_url}/pools"
    params = {
        "filter_by": f"{side}={token_address}",
        "page": 1,
        "page_size": 50,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.debug("Meteora DAMM fetch failed token=%.8s side=%s: %s", token_address, side, e)
        return []

    rows = data.get("data") or []
    results: list[dict] = []
    for pool in rows:
        normalized = _normalize_meteora_damm_pool(pool, token_address)
        if normalized:
            results.append(normalized)
    return results


async def get_meteora_damm_pools(token_address: str) -> list[dict]:
    """Fetch Meteora DAMM v2 pools containing *token_address* on either side."""
    token_x_pools, token_y_pools = await asyncio.gather(
        _fetch_meteora_damm_pools(token_address, "token_x"),
        _fetch_meteora_damm_pools(token_address, "token_y"),
        return_exceptions=True,
    )

    pools: list[dict] = []
    for result in (token_x_pools, token_y_pools):
        if isinstance(result, list):
            pools.extend(result)

    deduped = _dedupe_pools_by_address(pools)
    logger.debug("Meteora DAMM token=%.8s: %d pools found", token_address, len(deduped))
    return deduped


def _normalize_meteora_pool_detail(pool: dict, token_address: str) -> dict | None:
    """Convert a Meteora single-pool detail payload to OKX-compatible format."""
    pool_addr = pool.get("address", "")
    if not pool_addr:
        return None

    token_x = pool.get("token_x") or {}
    token_y = pool.get("token_y") or {}
    target_token, quote_token = _pick_token_side(token_x, token_y, token_address)
    if target_token is None or quote_token is None:
        return None

    quote_addr = (quote_token.get("address") or "").lower()
    quote_sym = quote_token.get("symbol", "")
    target_price_usd = _safe_float(target_token.get("price"))
    target_market_cap_usd = _safe_float(target_token.get("market_cap"))
    quote_price_usd = _safe_float(quote_token.get("price"))

    cfg = pool.get("pool_config") or {}
    base_fee_pct = _safe_float(cfg.get("base_fee_pct"))
    dynamic_fee_pct = _safe_float(pool.get("dynamic_fee_pct"))
    fee_rate = (base_fee_pct + dynamic_fee_pct) / 100.0

    volume = pool.get("volume") or {}
    protocol_name = "Meteora DLMM" if pool.get("bin_step") or cfg.get("bin_step") else "Meteora"
    return {
        "poolContractAddress":       pool_addr,
        "quoteTokenContractAddress": quote_addr,
        "quoteTokenSymbol":          quote_sym,
        "protocolName":              protocol_name,
        "feeRate":                   fee_rate,
        "liquidity":                 _safe_float(pool.get("tvl") or pool.get("liquidity")),
        "priceUsd":                  target_price_usd,
        "priceNative":               _price_native_from_token_prices(target_price_usd, quote_price_usd),
        "marketCapUsd":              target_market_cap_usd,
        "volumeUsd30M":              _safe_float(volume.get("30m")),
        "volumeUsd24H":              _safe_float(volume.get("24h") or pool.get("trade_volume_24h")),
        "volumeUsd1H":               _safe_float(volume.get("1h")),
        "createTime":                _to_unix_seconds(pool.get("created_at")),
        "source":                    "meteora_detail",
    }


async def get_meteora_pool_detail(pool_address: str, token_address: str) -> dict | None:
    """Fetch a single Meteora pool detail (DLMM first, then DAMM)."""
    urls = [
        f"https://dlmm.datapi.meteora.ag/pools/{pool_address}",
        f"{settings.meteora_damm_api_url}/pools/{pool_address}",
    ]
    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue
                data = resp.json()
        except Exception as e:
            logger.debug("Meteora detail fetch failed pool=%.8s url=%s: %s", pool_address, url, e)
            continue

        normalized = _normalize_meteora_pool_detail(data, token_address)
        if normalized:
            return normalized

    return None


# ─── Raydium v3 ───────────────────────────────────────────────────────────────

def _normalize_raydium_pool(pool: dict, token_address: str) -> dict | None:
    """Convert a Raydium API v3 pool dict to OKX-compatible format."""
    pool_id = pool.get("id", "")
    if not pool_id:
        return None

    mint_a = pool.get("mintA") or {}
    mint_b = pool.get("mintB") or {}
    addr_a = (mint_a.get("address") or "").lower()
    addr_b = (mint_b.get("address") or "").lower()
    token_lower = token_address.lower()

    # Identify quote token
    if addr_a == token_lower:
        quote_addr = addr_b
        quote_sym = mint_b.get("symbol", "")
    elif addr_b == token_lower:
        quote_addr = addr_a
        quote_sym = mint_a.get("symbol", "")
    else:
        return None

    # Pool type → protocol name
    pool_type = pool.get("type", "")
    if pool_type == "Concentrated":
        protocol_name = "Raydium CLMM"
    elif pool_type == "Standard":
        protocol_name = "Raydium V4"
    else:
        protocol_name = f"Raydium {pool_type}".strip()

    fee_rate = _safe_float(pool.get("feeRate"))
    tvl = _safe_float(pool.get("tvl"))
    raw_price = _safe_float(pool.get("price"))
    quote_sym_upper = str(quote_sym or "").upper()
    price_usd = raw_price if quote_sym_upper in _STABLE_QUOTES else 0.0
    price_native = raw_price if quote_sym_upper in _SOL_QUOTES else 0.0

    day = pool.get("day") or {}
    vol24h = _safe_float(day.get("volume"))
    vol1h = vol24h / 24.0 if vol24h > 0 else 0.0

    create_ts = _to_unix_seconds(pool.get("openTime", 0))

    return {
        "poolContractAddress":       pool_id,
        "quoteTokenContractAddress": quote_addr,
        "quoteTokenSymbol":          quote_sym,
        "protocolName":              protocol_name,
        "feeRate":                   fee_rate,
        "liquidity":                 tvl,
        "priceUsd":                  price_usd,
        "priceNative":               price_native,
        "volumeUsd24H":              vol24h,
        "volumeUsd1H":               vol1h,
        "createTime":                create_ts,
        "source":                    "raydium",
    }


async def get_raydium_pools(token_address: str) -> list[dict]:
    """
    Fetch all Raydium pools (AMM + CLMM) containing *token_address*.

    Uses: GET {raydium_api_url}/pools/info/mint?mint1={address}&...
    """
    url = f"{settings.raydium_api_url}/pools/info/mint"
    params = {
        "mint1": token_address,
        "poolType": "all",
        "poolSortField": "default",
        "sortType": "desc",
        "pageSize": 10,
        "page": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.debug("Raydium fetch failed token=%.8s: %s", token_address, e)
        return []

    # Response: { success: bool, data: { count: int, data: [...] } }
    if not data.get("success"):
        logger.debug("Raydium API returned success=false for token=%.8s", token_address)
        return []

    pools: list[dict] = (data.get("data") or {}).get("data") or []

    results: list[dict] = []
    for pool in pools:
        normalized = _normalize_raydium_pool(pool, token_address)
        if normalized:
            results.append(normalized)

    logger.debug("Raydium token=%.8s: %d pools found", token_address, len(results))
    return results


async def get_raydium_pool_details(pool_addresses: list[str], token_address: str) -> dict[str, dict]:
    """Batch fetch Raydium pools by ids and normalize them by pool address."""
    addrs = [a for a in pool_addresses if a]
    if not addrs:
        return {}

    url = f"{settings.raydium_api_url}/pools/info/ids"
    params = {"ids": ",".join(addrs)}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.debug("Raydium detail backfill failed for %d pools: %s", len(addrs), e)
        return {}

    if not data.get("success"):
        return {}

    out: dict[str, dict] = {}
    for pool in (data.get("data") or []):
        normalized = _normalize_raydium_pool(pool, token_address)
        if normalized:
            pool_id = str(normalized.get("poolContractAddress", "") or "")
            if pool_id:
                out[pool_id] = normalized
    return out


# ─── Pool-specific fee backfills ──────────────────────────────────────────────

async def get_raydium_fee_rates(pool_addresses: list[str]) -> dict[str, float]:
    """Batch fetch Raydium pool fee rates keyed by pool address."""
    addrs = [a for a in pool_addresses if a]
    if not addrs:
        return {}

    url = f"{settings.raydium_api_url}/pools/info/ids"
    params = {"ids": ",".join(addrs)}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.debug("Raydium fee backfill failed for %d pools: %s", len(addrs), e)
        return {}

    if not data.get("success"):
        return {}

    results: dict[str, float] = {}
    for pool in (data.get("data") or []):
        pool_id = str(pool.get("id", "") or "")
        if not pool_id:
            continue
        fee = _safe_float(pool.get("feeRate"))
        if fee > 0:
            results[pool_id] = fee
    return results


async def get_meteora_fee_rate(pool_address: str) -> float:
    """Fetch Meteora DLMM pool fee rate as decimal, or 0 on failure."""
    url = f"https://dlmm.datapi.meteora.ag/pools/{pool_address}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.debug("Meteora fee backfill failed pool=%.8s: %s", pool_address, e)
        return 0.0

    cfg = data.get("pool_config") or {}
    base_fee_pct = _safe_float(cfg.get("base_fee_pct"))
    dynamic_fee_pct = _safe_float(data.get("dynamic_fee_pct"))
    total_fee_pct = base_fee_pct + dynamic_fee_pct
    return total_fee_pct / 100.0 if total_fee_pct > 0 else 0.0


async def get_meteora_pool_positions(pool_address: str) -> list[dict]:
    """
    Fetch all active LP positions for a Meteora DLMM pool.

    Endpoint: GET {meteora_api_url}/pair/{pool_address}/positions
    Returns list of position dicts with keys:
      position_address, owner, lower_bin_id, upper_bin_id, liquidity_shares,
      fee_x, fee_y, total_fee_usd (if available)
    Returns empty list on failure or if endpoint not available.
    """
    url = f"{settings.meteora_api_url}/pair/{pool_address}/positions"
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                logger.debug("Meteora positions: pool not found pool=%.8s", pool_address)
                return []
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.debug("Meteora positions fetch failed pool=%.8s: %s", pool_address, e)
        return []

    rows: list = data if isinstance(data, list) else (data.get("positions") or data.get("data") or [])

    results: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        owner = (
            row.get("owner")
            or row.get("wallet")
            or row.get("user")
            or (row.get("publicKey") or "")
        )
        if not owner:
            continue
        results.append({
            "owner":          str(owner),
            "tick_lower":     int(row.get("lower_bin_id") or row.get("lowerBinId") or 0),
            "tick_upper":     int(row.get("upper_bin_id") or row.get("upperBinId") or 0),
            "liquidity":      str(row.get("liquidity_shares") or row.get("liquidity") or 0),
            "deposited_x":    _safe_float(row.get("total_x_amount") or row.get("depositedX")),
            "deposited_y":    _safe_float(row.get("total_y_amount") or row.get("depositedY")),
            "fee_x":          _safe_float(row.get("fee_x") or row.get("feeX")),
            "fee_y":          _safe_float(row.get("fee_y") or row.get("feeY")),
            "total_fee_usd":  _safe_float(row.get("total_fee_usd") or row.get("totalFeeUsd")),
        })

    logger.debug("Meteora positions pool=%.8s: %d found", pool_address, len(results))
    return results


async def get_orca_fee_rate(pool_address: str) -> float:
    """Fetch Orca whirlpool fee rate as decimal, or 0 on failure."""
    url = f"https://api.orca.so/v2/solana/pools/{pool_address}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.debug("Orca fee backfill failed pool=%.8s: %s", pool_address, e)
        return 0.0

    pool = data.get("data") or {}
    fee_rate_raw = _safe_float(pool.get("feeRate"))
    return fee_rate_raw / 1_000_000.0 if fee_rate_raw > 0 else 0.0
