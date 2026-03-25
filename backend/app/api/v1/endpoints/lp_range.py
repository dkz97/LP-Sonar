"""LP Range Recommendation endpoint."""
from __future__ import annotations
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.models.schemas import RangeRecommendation
from app.services.range_recommender import recommend_range

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/lp-range")


class RangeRecommendRequest(BaseModel):
    pool_address: str
    chain: str


@router.post("/recommend", response_model=RangeRecommendation)
async def post_recommend_range(body: RangeRecommendRequest) -> RangeRecommendation:
    """
    Generate a concentrated-liquidity LP range recommendation for a single pool.

    Returns conservative / balanced / aggressive range profiles with expected
    fee APR, IL cost, breach probability, and utility scores.

    Results are cached for 5 minutes per pool.
    """
    if not body.pool_address or not body.chain:
        raise HTTPException(status_code=422, detail="pool_address and chain are required")

    result = await recommend_range(
        pool_address=body.pool_address,
        chain_index=body.chain,
    )
    return result


@router.get("/{chain}/{pool_address}", response_model=RangeRecommendation)
async def get_recommend_range(chain: str, pool_address: str) -> RangeRecommendation:
    """
    GET shorthand for LP range recommendation.
    Equivalent to POST /lp-range/recommend with {pool_address, chain} body.
    """
    result = await recommend_range(
        pool_address=pool_address,
        chain_index=chain,
    )
    return result
