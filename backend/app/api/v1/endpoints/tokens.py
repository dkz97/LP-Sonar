import json
from fastapi import APIRouter, Query, Depends
from redis.asyncio import Redis

from app.core.redis_client import get_redis
from app.models.schemas import TokenSnapshot, Layer

router = APIRouter()


@router.get("/tokens", response_model=list[TokenSnapshot])
async def get_tokens(
    layer: Layer = Query(Layer.hot, description="Filter by layer: hot or focus"),
    chain: str | None = Query(None, description="Filter by chainIndex, e.g. 501"),
    limit: int = Query(200, le=500),
    redis: Redis = Depends(get_redis),
):
    from app.core.config import settings

    chains = [chain] if chain else settings.chain_list
    results: list[TokenSnapshot] = []

    for chain_index in chains:
        key = f"{layer.value}:{chain_index}"
        # Sorted set: member=tokenAddr, score=z_score (desc = highest first)
        token_addrs = await redis.zrevrange(key, 0, limit - 1)

        for addr in token_addrs:
            snap_key = f"snapshot:{chain_index}:{addr}"
            data = await redis.hgetall(snap_key)
            if not data:
                continue
            try:
                snap = TokenSnapshot(
                    chain_index=chain_index,
                    token_address=addr,
                    token_symbol=data.get("token_symbol", ""),
                    token_name=data.get("token_name", ""),
                    logo_url=data.get("logo_url", ""),
                    price_usd=float(data.get("price_usd", 0)),
                    price_change_5m=float(data.get("price_change_5m", 0)),
                    price_change_1h=float(data.get("price_change_1h", 0)),
                    price_change_4h=float(data.get("price_change_4h", 0)),
                    price_change_24h=float(data.get("price_change_24h", 0)),
                    volume_5m=float(data.get("volume_5m", 0)),
                    volume_1h=float(data.get("volume_1h", 0)),
                    volume_4h=float(data.get("volume_4h", 0)),
                    volume_24h=float(data.get("volume_24h", 0)),
                    txs_5m=int(data.get("txs_5m", 0)),
                    z_score=float(data.get("z_score", 0)),
                    layer=Layer(data.get("layer", layer.value)),
                    updated_at=int(data.get("updated_at", 0)),
                    # Focus enrichment
                    tvl_usd=float(data.get("tvl_usd", 0)) or None,
                    top_pool_protocol=data.get("top_pool_protocol") or None,
                    risk_level=int(data.get("risk_level", 0)) if data.get("risk_level") else None,
                    is_lp_burnt=data.get("is_lp_burnt") or None,
                    is_mint=data.get("is_mint") or None,
                    smart_money_buys_10=int(data.get("smart_money_buys_10", 0)) if data.get("smart_money_buys_10") else None,
                    smart_money_sells_10=int(data.get("smart_money_sells_10", 0)) if data.get("smart_money_sells_10") else None,
                )

                # Attach LP decision if it exists (focus layer only)
                if layer == Layer.focus:
                    primary_pool = await redis.get(f"primary_pool:{chain_index}:{addr}")
                    if primary_pool:
                        lp_key = f"lp_decision:{chain_index}:{primary_pool}"
                        lp_data = await redis.hgetall(lp_key)
                        if lp_data and lp_data.get("eligible") == "1":
                            snap.lp_eligible = True
                            snap.lp_pool_address = primary_pool
                            snap.lp_pair_label = lp_data.get("pair_label", "")
                            snap.lp_net_score = float(lp_data.get("net_lp_score", 0))
                            snap.lp_strategy = lp_data.get("strategy_type", "")
                            snap.lp_holding = lp_data.get("suggested_holding", "")
                            snap.lp_il_risk = lp_data.get("il_risk_level", "")
                            snap.lp_wash_risk = lp_data.get("wash_risk", "")

                results.append(snap)
            except Exception:
                continue

    # Sort by z_score desc across chains
    results.sort(key=lambda x: x.z_score, reverse=True)
    return results[:limit]
