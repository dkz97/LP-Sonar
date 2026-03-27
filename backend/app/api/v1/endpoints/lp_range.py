"""LP Range Recommendation endpoint."""
from __future__ import annotations
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from app.models.schemas import RangeRecommendation
from app.services.range_recommender import recommend_range

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/lp-range")


class RangeRecommendRequest(BaseModel):
    pool_address: str
    chain: str
    # Optional: user's intended LP position size in USD.
    # When provided, execution_cost_fraction and expected_net_pnl reflect this position.
    # When absent, the engine uses min($10k, TVL×1%) as a representative default.
    position_usd: Optional[float] = None

    @field_validator("position_usd")
    @classmethod
    def _validate_position(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError("position_usd must be positive")
        return v


@router.post("/recommend", response_model=RangeRecommendation)
async def post_recommend_range(body: RangeRecommendRequest) -> RangeRecommendation:
    """
    Generate a concentrated-liquidity LP range recommendation for a single pool.

    Returns conservative / balanced / aggressive range profiles with expected
    fee APR, IL cost, breach probability, and utility scores.

    Optional body field `position_usd`: specify your LP capital in USD to get
    execution cost estimates (gas + slippage) tailored to your position size.

    Results are cached for 5 minutes per pool (cache bypassed when position_usd
    is provided, since the result is position-specific).
    """
    if not body.pool_address or not body.chain:
        raise HTTPException(status_code=422, detail="pool_address and chain are required")

    result = await recommend_range(
        pool_address=body.pool_address,
        chain_index=body.chain,
        user_position_usd=body.position_usd,
    )
    return result


@router.get("/{chain}/{pool_address}", response_model=RangeRecommendation)
async def get_recommend_range(
    chain: str,
    pool_address: str,
    position_usd: Optional[float] = Query(default=None, gt=0, description="LP position size in USD"),
) -> RangeRecommendation:
    """
    GET shorthand for LP range recommendation.
    Equivalent to POST /lp-range/recommend.
    Optional query param: ?position_usd=5000
    """
    result = await recommend_range(
        pool_address=pool_address,
        chain_index=chain,
        user_position_usd=position_usd,
    )
    return result
