from fastapi import APIRouter
from app.api.v1.endpoints import tokens, alerts, token_detail, lp, lp_range

router = APIRouter(prefix="/api/v1")
router.include_router(tokens.router, tags=["tokens"])
router.include_router(alerts.router, tags=["alerts"])
router.include_router(token_detail.router, tags=["token_detail"])
router.include_router(lp.router, tags=["lp"])
router.include_router(lp_range.router, tags=["lp_range"])
