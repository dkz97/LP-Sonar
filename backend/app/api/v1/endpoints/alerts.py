import json
from fastapi import APIRouter, Query, Depends
from redis.asyncio import Redis

from app.core.redis_client import get_redis
from app.models.schemas import AlertRecord

router = APIRouter()


@router.get("/alerts", response_model=list[AlertRecord])
async def get_alerts(
    limit: int = Query(50, le=500),
    redis: Redis = Depends(get_redis),
):
    raw = await redis.lrange("alerts", 0, limit - 1)
    alerts: list[AlertRecord] = []
    for item in raw:
        try:
            alerts.append(AlertRecord(**json.loads(item)))
        except Exception:
            continue
    return alerts
