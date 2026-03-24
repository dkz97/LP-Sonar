import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.redis_client import get_redis, close_redis
from app.api.v1.router import router
from app.tasks.scheduler import scheduler, setup_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting LP-Sonar backend...")
    await get_redis()  # warm up connection
    setup_scheduler()
    scheduler.start()
    # Trigger first scans immediately
    scheduler.get_job("universe_scan").modify(next_run_time=__import__("datetime").datetime.now())
    logger.info("Scheduler started, initial universe scan queued")
    yield
    # Shutdown
    logger.info("Shutting down...")
    scheduler.shutdown(wait=False)
    await close_redis()


app = FastAPI(
    title="LP-Sonar",
    description="DeFi LP monitoring system",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health")
async def health():
    redis = await get_redis()
    await redis.ping()
    return {"status": "ok", "redis": "connected"}
