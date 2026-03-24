"""
Market Quality Detector: identifies wash trading, thin depth, and imbalanced flow.

Rule-based heuristics, no external API calls required.
All inputs come from pair_snapshot + focus_analyzer trade data.

wash_score 0.0~1.0:
  >= 0.5  → high
  >= 0.25 → medium
  <  0.25 → low
"""
from __future__ import annotations

from app.models.schemas import MarketQualityResult

# ── Thresholds ─────────────────────────────────────────────────────────

# vol24h / TVL
_VOL_TVL_EXTREME = 20.0
_VOL_TVL_WARN    = 10.0

# vol1h / TVL (1-hour turnover)
_VOL_TVL_1H_EXTREME = 2.0

# Directional imbalance: |buy_ratio - 0.5| × 2  (0=balanced, 1=totally one-sided)
_IMBALANCE_SEVERE = 0.60   # buy_ratio > 0.80 or < 0.20
_IMBALANCE_WARN   = 0.40   # buy_ratio > 0.70 or < 0.30

# Average trade size
_AVG_TRADE_MICRO  = 10.0   # < $10    → bot-like micro trades
_AVG_TRADE_SMALL  = 50.0   # < $50    → small trades warning

# TVL floor for adequate depth
_MIN_TVL_DEPTH = 100_000


def detect_market_quality(
    pool_address: str,
    tvl_usd: float,
    volume_24h: float,
    volume_1h: float,
    buy_volume: float,
    sell_volume: float,
    trade_count_1h: int,
    avg_trade_size_usd: float | None = None,
) -> MarketQualityResult:
    """
    Evaluate market quality for a single pool.

    Parameters
    ----------
    pool_address       Pool contract address (for reference only).
    tvl_usd            Current pool TVL in USD.
    volume_24h         Pool 24-hour trading volume in USD.
    volume_1h          Pool 1-hour trading volume in USD.
    buy_volume         Estimated buy-side volume over recent window (USD).
    sell_volume        Estimated sell-side volume over recent window (USD).
    trade_count_1h     Number of trades in the past hour.
    avg_trade_size_usd Pre-computed average trade size; computed from volume_1h /
                       trade_count_1h if None.
    """
    flags: list[str] = []
    wash_score = 0.0

    # ── 1. Volume / TVL ratio ──────────────────────────────────────────
    vol_tvl = volume_24h / max(tvl_usd, 1.0)
    if vol_tvl > _VOL_TVL_EXTREME:
        flags.append("EXTREME_VOL_TVL")
        wash_score += 0.40
    elif vol_tvl > _VOL_TVL_WARN:
        flags.append("HIGH_VOL_TVL")
        wash_score += 0.20

    # ── 2. Buy / sell imbalance ────────────────────────────────────────
    total_flow = buy_volume + sell_volume
    if total_flow > 0:
        buy_ratio = buy_volume / total_flow
    else:
        buy_ratio = 0.5

    imbalance = abs(buy_ratio - 0.5) * 2  # 0 = balanced, 1 = fully one-sided
    if imbalance >= _IMBALANCE_SEVERE:
        flags.append("SEVERE_IMBALANCE")
        wash_score += 0.25
    elif imbalance >= _IMBALANCE_WARN:
        flags.append("IMBALANCED_FLOW")
        wash_score += 0.10

    # ── 3. Average trade size ─────────────────────────────────────────
    if avg_trade_size_usd is None:
        avg_trade_size_usd = (
            volume_1h / trade_count_1h if trade_count_1h > 0 else 0.0
        )

    if avg_trade_size_usd > 0:
        if avg_trade_size_usd < _AVG_TRADE_MICRO:
            flags.append("MICRO_TRADES")
            wash_score += 0.20
        elif avg_trade_size_usd < _AVG_TRADE_SMALL:
            flags.append("SMALL_TRADES")
            wash_score += 0.05

    # ── 4. Depth adequacy ─────────────────────────────────────────────
    if tvl_usd < _MIN_TVL_DEPTH:
        flags.append("THIN_DEPTH")
        wash_score += 0.10

    # ── 5. 1-hour turnover ────────────────────────────────────────────
    vol_tvl_1h = volume_1h / max(tvl_usd, 1.0)
    if vol_tvl_1h > _VOL_TVL_1H_EXTREME:
        flags.append("EXTREME_1H_TURNOVER")
        wash_score += 0.15

    wash_score = round(min(wash_score, 1.0), 3)

    if wash_score >= 0.50:
        wash_risk = "high"
    elif wash_score >= 0.25:
        wash_risk = "medium"
    else:
        wash_risk = "low"

    return MarketQualityResult(
        pool_address=pool_address,
        wash_risk=wash_risk,
        wash_score=wash_score,
        vol_tvl_ratio=round(vol_tvl, 3),
        imbalance_ratio=round(buy_ratio, 3),
        avg_trade_size_usd=round(avg_trade_size_usd, 2),
        flags=flags,
    )
