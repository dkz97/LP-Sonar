"""
IL Risk Estimator: rule-based heuristic for Impermanent Loss risk.

Score 0~100 (higher = riskier):
  >= 65 → high
  >= 35 → medium
  <  35 → low

Component breakdown:
  Quote asset type   up to 40 pts  (stable = most exposed to single-leg IL)
  24h price change   up to 35 pts
  Z-Score trend      up to 15 pts  (strong trend = single-leg risk)
  1h price spike     up to 10 pts  (current momentum)

Upgrade path (Phase 3):
  - Replace price_change proxy with realized volatility from OKX CEX kline data.
  - For Uniswap V3 concentrated liquidity: accept [lower_tick, upper_tick] range
    and compute exact in-range IL formula.
"""
from __future__ import annotations
import math

from app.models.schemas import ILRiskResult


def _uniswap_v2_il(price_ratio: float) -> float:
    """
    Exact IL formula for constant-product AMM (Uniswap V2 / Raydium).

    price_ratio = new_price / initial_price
    Returns IL as a negative fraction, e.g. -0.057 for 50% price change.
    """
    if price_ratio <= 0:
        return -1.0
    return (2.0 * math.sqrt(price_ratio) / (1.0 + price_ratio)) - 1.0


def estimate_il_risk(
    quote_type: str,
    price_change_24h: float,
    price_change_4h: float,
    price_change_1h: float,
    z_score: float,
    pool_age_days: float,
    protocol: str = "",
) -> ILRiskResult:
    """
    Estimate IL risk for a token/pool using heuristics.

    Parameters
    ----------
    quote_type        "stable" | "wrapped_native" | "alt"
    price_change_24h  Percentage change over 24h, e.g. 15.0 for +15%.
    price_change_4h   Percentage change over 4h.
    price_change_1h   Percentage change over 1h.
    z_score           Volume Z-Score from hot_monitor (trend strength proxy).
    pool_age_days     Pool age in days (new pools have less data confidence).
    """
    score = 0
    factors: dict[str, str] = {}
    drivers: list[str] = []

    # ── 1. Quote asset type ───────────────────────────────────────────
    # stable: token single-leg fully exposed.
    # wrapped_native: token and quote tend to correlate → lower net IL.
    # alt: correlation unknown → treated as mostly uncorrelated.
    if quote_type == "stable":
        score += 40
        factors["quote_type"] = "stable_full_il_exposure"
        drivers.append("Stable quote → single-leg IL fully exposed")
    elif quote_type == "wrapped_native":
        score += 20
        factors["quote_type"] = "wrapped_native_partial_hedge"
        drivers.append("Wrapped-native quote → partial IL hedge via correlation")
    else:
        score += 35
        factors["quote_type"] = "alt_unknown_correlation"
        drivers.append("Alt quote → unknown correlation, high IL exposure")

    # ── 2. 24h price volatility ───────────────────────────────────────
    abs_pc24h = abs(price_change_24h)
    if abs_pc24h > 50:
        score += 35
        factors["volatility_24h"] = f"{abs_pc24h:.1f}%_extreme"
        drivers.append(f"24h move {abs_pc24h:.0f}% → severe IL region")
    elif abs_pc24h > 20:
        score += 20
        factors["volatility_24h"] = f"{abs_pc24h:.1f}%_high"
        drivers.append(f"24h move {abs_pc24h:.0f}% → material IL risk")
    elif abs_pc24h > 10:
        score += 10
        factors["volatility_24h"] = f"{abs_pc24h:.1f}%_medium"
        drivers.append(f"24h move {abs_pc24h:.0f}% → moderate IL")
    else:
        score += 3
        factors["volatility_24h"] = f"{abs_pc24h:.1f}%_low"

    # ── 3. Z-Score (trend / momentum) ────────────────────────────────
    if z_score >= 3.0:
        score += 15
        factors["z_score"] = f"{z_score:.1f}_strong_trend"
        drivers.append(f"Z={z_score:.1f}: strong single-leg momentum")
    elif z_score >= 2.0:
        score += 8
        factors["z_score"] = f"{z_score:.1f}_moderate_trend"
    else:
        factors["z_score"] = f"{z_score:.1f}_normal"

    # ── 4. 1h spike (in-progress single-leg move) ────────────────────
    abs_pc1h = abs(price_change_1h)
    if abs_pc1h > 10:
        score += 10
        factors["volatility_1h"] = f"{abs_pc1h:.1f}%_spike"
        drivers.append(f"1h spike {abs_pc1h:.0f}% → active directional move")
    elif abs_pc1h > 5:
        score += 5
        factors["volatility_1h"] = f"{abs_pc1h:.1f}%_elevated"
    else:
        factors["volatility_1h"] = f"{abs_pc1h:.1f}%_normal"

    # ── 5. Concentrated liquidity protocol modifier ───────────────────
    # DLMM / CLMM / Whirlpool: when price exits the active range the position
    # becomes fully single-sided, making IL risk materially higher than standard AMM.
    _CONCENTRATED = ("meteora dlmm", "raydium clmm", "orca whirlpool", "whirlpool")
    proto_lower = protocol.lower()
    if any(p in proto_lower for p in _CONCENTRATED):
        score = int(score * 1.25)
        factors["protocol_il_multiplier"] = f"1.25x ({proto_lower})"
        drivers.append(f"Concentrated LP ({protocol}): out-of-range → full single-leg exposure")

    score = min(score, 100)

    # Annotate estimated IL at current 24h volatility level
    price_ratio = 1.0 + abs_pc24h / 100.0
    il_est = _uniswap_v2_il(price_ratio)
    factors["il_estimate_at_24h_vol"] = f"{il_est * 100:.1f}%"

    if score >= 65:
        level = "high"
    elif score >= 35:
        level = "medium"
    else:
        level = "low"

    main_driver = drivers[0] if drivers else "Moderate volatility across all factors"

    return ILRiskResult(
        level=level,
        score=score,
        main_driver=main_driver,
        factors=factors,
    )
