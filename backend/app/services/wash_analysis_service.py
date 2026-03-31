"""
Sniper-tagged wallet activity service.

Uses OKX MCP's Sniper-tagged trade data to surface early-entry wallet addresses
for a token.  Aggregates per wallet: trade count, buy/sell volume, first/last seen.

NOTE: "Sniper" is OKX's label for wallets that buy early after token launch.
This is NOT a definitive wash-trading detection — it is one signal among several.
The pool-level wash_score comes from a separate heuristic in the LP decision cache.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import asdict

from app.models.schemas import SuspiciousTrader, WashAnalysis
from app.services import mcp_client

logger = logging.getLogger(__name__)

# Candidate field names OKX might use for the trader wallet address
_ADDR_FIELDS = (
    "userAddress",      # confirmed field name in OKX dex-okx-market-trades response
    "traderAddress",
    "walletAddress",
    "address",
    "maker",
    "from",
    "account",
    "trader",
    "wallet",
)


def _extract_address(trade: dict) -> str:
    for field in _ADDR_FIELDS:
        val = trade.get(field)
        if val and isinstance(val, str) and len(val) > 10:
            return val
    return ""


def _extract_timestamp(trade: dict) -> int:
    """Return unix seconds from millisecond or second timestamp."""
    raw = trade.get("time") or trade.get("txTime") or trade.get("timestamp") or 0
    try:
        t = int(raw)
        return t // 1000 if t > 1_000_000_000_000 else t
    except (ValueError, TypeError):
        return 0


async def get_wash_analysis(
    chain_index: str,
    token_address: str,
    pool_wash_score: float = 0.0,
    pool_wash_risk: str = "low",
    pool_volume_24h: float = 0.0,
) -> WashAnalysis:
    """
    Fetch Sniper-tagged trades and aggregate per wallet.

    pool_wash_score / pool_wash_risk come from the existing LP decision cache
    (callers should look these up before calling).
    pool_volume_24h is used to compute sniper_volume_pct.
    """
    now = int(time.time())

    try:
        trades = await mcp_client.get_sniper_traders(chain_index, token_address, limit=100)
    except Exception as exc:
        logger.warning(
            "wash_analysis: sniper fetch failed chain=%s token=%.8s: %s",
            chain_index, token_address, exc,
        )
        return WashAnalysis(
            pool_wash_score=pool_wash_score,
            pool_wash_risk=pool_wash_risk,
            sniper_count=0,
            sniper_volume_pct=0.0,
            suspicious_traders=[],
            cached_at=now,
        )

    # Aggregate by address — also aggregate unknown-address trades under ""
    agg: dict[str, dict] = defaultdict(lambda: {
        "buy_vol": 0.0, "sell_vol": 0.0, "count": 0,
        "first": int(time.time()), "last": 0,
    })

    total_sniper_vol = 0.0

    for trade in trades:
        addr = _extract_address(trade)
        kind = (trade.get("type") or "").lower()
        try:
            vol = float(trade.get("volume") or 0)
        except (TypeError, ValueError):
            vol = 0.0

        ts = _extract_timestamp(trade)

        bucket = agg[addr]
        if kind == "buy":
            bucket["buy_vol"] += vol
        else:
            bucket["sell_vol"] += vol
        bucket["count"] += 1
        if ts > 0:
            bucket["first"] = min(bucket["first"], ts)
            bucket["last"]  = max(bucket["last"],  ts)
        total_sniper_vol += vol

    sniper_volume_pct = (total_sniper_vol / pool_volume_24h) if pool_volume_24h > 0 else 0.0

    # Build SuspiciousTrader list (skip empty-address aggregation)
    traders: list[SuspiciousTrader] = []
    for addr, b in agg.items():
        if not addr:
            continue
        traders.append(SuspiciousTrader(
            address=addr,
            tag="Sniper",
            trade_count=b["count"],
            buy_volume_usd=round(b["buy_vol"], 2),
            sell_volume_usd=round(b["sell_vol"], 2),
            total_volume_usd=round(b["buy_vol"] + b["sell_vol"], 2),
            first_seen=b["first"],
            last_seen=b["last"],
        ))

    # Sort descending by total volume, keep top 20
    traders.sort(key=lambda t: t.total_volume_usd, reverse=True)
    traders = traders[:20]

    return WashAnalysis(
        pool_wash_score=pool_wash_score,
        pool_wash_risk=pool_wash_risk,
        sniper_count=len(traders),
        sniper_volume_pct=round(min(sniper_volume_pct, 1.0), 4),
        suspicious_traders=[asdict(t) for t in traders],
        cached_at=now,
    )
