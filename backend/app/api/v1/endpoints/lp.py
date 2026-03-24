"""LP Decision Layer endpoints."""
from __future__ import annotations
import json
from fastapi import APIRouter, Query, Depends
from redis.asyncio import Redis

from app.core.config import settings
from app.core.redis_client import get_redis

router = APIRouter()


@router.get("/lp-opportunities")
async def get_lp_opportunities(
    chain: str | None = Query(None, description="Filter by chainIndex, e.g. 501"),
    limit: int = Query(50, le=200),
    eligible_only: bool = Query(True, description="Only return eligible LP opportunities"),
    redis: Redis = Depends(get_redis),
) -> list[dict]:
    """
    Return top LP opportunities ranked by net_lp_score.
    Reads from lp_opportunities:{chain} sorted sets and lp_decision:{chain}:{pool} hashes.
    """
    chains = [chain] if chain else settings.chain_list
    results: list[dict] = []

    for chain_index in chains:
        opp_key = f"lp_opportunities:{chain_index}"
        # zrevrange: highest net_lp_score first
        entries = await redis.zrevrange(opp_key, 0, limit - 1, withscores=True)

        for pool_addr, score in entries:
            dec_key = f"lp_decision:{chain_index}:{pool_addr}"
            data = await redis.hgetall(dec_key)
            if not data:
                continue

            eligible = data.get("eligible", "0") == "1"
            if eligible_only and not eligible:
                continue

            try:
                token_addr = data.get("token_address", "")
                snap_logo = ""
                if token_addr:
                    snap_logo = await redis.hget(f"snapshot:{chain_index}:{token_addr}", "logo_url") or ""
                results.append({
                    "chain_index":          chain_index,
                    "pool_address":         pool_addr,
                    "token_address":        token_addr,
                    "token_symbol":         data.get("token_symbol", ""),
                    "logo_url":             snap_logo,
                    "pair_label":           data.get("pair_label", ""),
                    "protocol":             data.get("protocol", ""),
                    "fee_rate":             float(data.get("fee_rate", 0)),
                    "tvl_usd":              float(data.get("tvl_usd", 0)),
                    "eligible":             eligible,
                    "strategy_type":        data.get("strategy_type", ""),
                    "suggested_holding":    data.get("suggested_holding", ""),
                    "net_lp_score":         float(data.get("net_lp_score", score)),
                    "fee_income_score":     float(data.get("fee_income_score", 0)),
                    "market_quality_score": float(data.get("market_quality_score", 0)),
                    "il_risk_level":        data.get("il_risk_level", ""),
                    "wash_risk":            data.get("wash_risk", ""),
                    "main_reasons":         json.loads(data.get("main_reasons", "[]")),
                    "main_risks":           json.loads(data.get("main_risks", "[]")),
                    "confidence":           float(data.get("confidence", 0)),
                    "timestamp":            int(data.get("timestamp", 0)),
                })
            except Exception:
                continue

    # Sort globally by net_lp_score desc
    results.sort(key=lambda x: x["net_lp_score"], reverse=True)
    return results[:limit]


@router.get("/lp-decision/{chain_index}/{pool_address}")
async def get_lp_decision(
    chain_index: str,
    pool_address: str,
    redis: Redis = Depends(get_redis),
) -> dict:
    """Return the full LP decision for a specific pool."""
    key = f"lp_decision:{chain_index}:{pool_address}"
    data = await redis.hgetall(key)
    if not data:
        return {}
    try:
        return {
            "chain_index":          chain_index,
            "pool_address":         pool_address,
            "token_address":        data.get("token_address", ""),
            "token_symbol":         data.get("token_symbol", ""),
            "pair_label":           data.get("pair_label", ""),
            "protocol":             data.get("protocol", ""),
            "fee_rate":             float(data.get("fee_rate", 0)),
            "tvl_usd":              float(data.get("tvl_usd", 0)),
            "eligible":             data.get("eligible", "0") == "1",
            "failed_reasons":       json.loads(data.get("failed_reasons", "[]")),
            "warnings":             json.loads(data.get("warnings", "[]")),
            "strategy_type":        data.get("strategy_type", ""),
            "suggested_holding":    data.get("suggested_holding", ""),
            "net_lp_score":         float(data.get("net_lp_score", 0)),
            "fee_income_score":     float(data.get("fee_income_score", 0)),
            "market_quality_score": float(data.get("market_quality_score", 0)),
            "il_risk_level":        data.get("il_risk_level", ""),
            "wash_risk":            data.get("wash_risk", ""),
            "main_reasons":         json.loads(data.get("main_reasons", "[]")),
            "main_risks":           json.loads(data.get("main_risks", "[]")),
            "confidence":           float(data.get("confidence", 0)),
            "timestamp":            int(data.get("timestamp", 0)),
        }
    except Exception:
        return {}
