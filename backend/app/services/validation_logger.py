"""
Minimal validation logger.

Appends one JSON record per recommendation to:
  backend/data/lp_validation_log.jsonl

Fail-safe: all errors are swallowed — never affects the main recommendation flow.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.schemas import RangeProfile

logger = logging.getLogger(__name__)

# backend/data/lp_validation_log.jsonl
_DATA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data")
)
_LOG_PATH = os.path.join(_DATA_DIR, "lp_validation_log.jsonl")


def log_recommendation(
    *,
    pool_address: str,
    chain_index: str,
    protocol: str,
    current_price: float,
    tvl_usd: float,
    volume_24h: float,
    fee_rate: float,
    history_tier: str,
    regime: str,
    recommendation_confidence: float,
    balanced_profile: "RangeProfile | None",
    fee_haircut_factor: float = 1.0,
) -> None:
    """Append one validation record. Silently no-ops if balanced_profile is None."""
    try:
        if balanced_profile is None:
            return

        record: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pool_address": pool_address,
            "chain_index": chain_index,
            "protocol": protocol,
            "current_price": current_price,
            "lower_price": balanced_profile.lower_price,
            "upper_price": balanced_profile.upper_price,
            "width_pct": balanced_profile.width_pct,
            "history_tier": history_tier,
            "regime": regime,
            "recommendation_confidence": recommendation_confidence,
            "expected_fee_apr": balanced_profile.expected_fee_apr,
            "breach_probability": balanced_profile.breach_probability,
            "fee_haircut_factor": round(fee_haircut_factor, 4),
            "final_utility": balanced_profile.final_utility,
            "tvl_usd": tvl_usd,
            "vol_tvl_ratio": round(volume_24h / max(tvl_usd, 1.0), 4),
            "fee_rate": fee_rate,
        }

        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    except Exception as exc:  # noqa: BLE001
        logger.warning("validation_logger: write failed: %s", exc)
