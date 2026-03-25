"""
Market Regime Detector: classifies current market state from OHLCV price series.

Four regimes:
  range_bound  — low drift, mean-reverting, positive autocorrelation
  trend_up     — sustained upward drift, positive autocorrelation
  trend_down   — sustained downward drift
  chaotic      — high jump ratio or extreme volatility

Inputs: list of OHLCV bar dicts  {"time", "open", "high", "low", "close", "volume"}
Output: RegimeResult
"""
from __future__ import annotations
import math
import logging

import numpy as np

from app.models.schemas import RegimeResult

logger = logging.getLogger(__name__)

# ── Thresholds ──────────────────────────────────────────────────────────

# Minimum bars required for reliable regime detection
_MIN_BARS = 24

# Fraction of bars with |log-return| > 3σ considered "jumpy"
_JUMP_RATIO_CHAOTIC = 0.12

# Annualised vol (fraction) above which we treat as chaotic
_REALIZED_VOL_CHAOTIC = 4.0  # 400% APY vol

# Drift slope (per bar) thresholds for trend classification
# Bars are 1-hour; slope in log-price units
# ~0.5% per hour ≈ 12% per day directional drift
_DRIFT_SLOPE_TREND = 0.005

# Lag-1 autocorrelation thresholds
_AUTOCORR_RANGE_BOUND = 0.10   # mild mean-reversion / sideways → positive autocorr
_AUTOCORR_TREND = 0.05         # trend → also positive autocorr (momentum)

# Dwell concentration Herfindahl index (HHI) threshold for range-bound confirmation
_HHI_RANGE_BOUND = 0.05        # price returns to same buckets often


def _safe_log_returns(prices: list[float]) -> np.ndarray:
    """Compute log-returns from a price series, skipping zero/negative prices."""
    arr = np.array(prices, dtype=float)
    valid = arr > 0
    if not np.all(valid):
        arr = arr[valid]
    if len(arr) < 2:
        return np.array([])
    return np.diff(np.log(arr))


def _realized_vol_annualised(log_returns: np.ndarray, bars_per_year: float = 8760.0) -> float:
    """
    Annualised realised volatility from log-returns.
    bars_per_year: 8760 for 1h bars (365 * 24).
    """
    if len(log_returns) < 2:
        return 0.0
    std = float(np.std(log_returns, ddof=1))
    return std * math.sqrt(bars_per_year)


def _drift_slope(prices: list[float]) -> float:
    """
    Linear regression slope of log-prices against bar index.
    Returns slope in log-price units per bar.
    """
    n = len(prices)
    if n < 2:
        return 0.0
    log_p = np.log(np.maximum(prices, 1e-30))
    x = np.arange(n, dtype=float)
    # slope via least-squares formula
    x_mean = x.mean()
    slope = float(np.dot(x - x_mean, log_p - log_p.mean()) / np.dot(x - x_mean, x - x_mean))
    return slope


def _lag1_autocorrelation(log_returns: np.ndarray) -> float:
    """Pearson lag-1 autocorrelation of the return series."""
    if len(log_returns) < 4:
        return 0.0
    r = log_returns
    mean = r.mean()
    demeaned = r - mean
    var = float(np.dot(demeaned, demeaned))
    if var < 1e-20:
        return 0.0
    cov = float(np.dot(demeaned[:-1], demeaned[1:]))
    return cov / var


def _jump_ratio(log_returns: np.ndarray) -> float:
    """Fraction of bars where |return| > 3σ (using rolling std)."""
    if len(log_returns) < 4:
        return 0.0
    sigma = float(np.std(log_returns, ddof=1))
    if sigma < 1e-20:
        return 0.0
    threshold = 3.0 * sigma
    return float(np.mean(np.abs(log_returns) > threshold))


def _dwell_hhi(prices: list[float], n_buckets: int = 20) -> float:
    """
    Herfindahl-Hirschman index of price dwell across equal-width buckets.
    High HHI → price dwells in a concentrated zone (range-bound signal).
    """
    if len(prices) < 4:
        return 0.0
    p = np.array(prices, dtype=float)
    lo, hi = p.min(), p.max()
    if hi <= lo:
        return 1.0  # all prices identical → maximally concentrated
    bucket_width = (hi - lo) / n_buckets
    bucket_ids = np.floor((p - lo) / bucket_width).clip(0, n_buckets - 1).astype(int)
    counts = np.bincount(bucket_ids, minlength=n_buckets).astype(float)
    shares = counts / counts.sum()
    return float(np.dot(shares, shares))  # HHI


def detect_regime(
    ohlcv_bars: list[dict],
    bars_per_year: float = 8760.0,
) -> RegimeResult:
    """
    Classify market regime from a list of OHLCV bar dicts.

    Parameters
    ----------
    ohlcv_bars    List of {"time", "open", "high", "low", "close", "volume"} dicts,
                  ordered oldest → newest.
    bars_per_year 8760 for 1h bars; adjust for other resolutions.

    Returns
    -------
    RegimeResult with regime, confidence, realized_vol, drift_slope, jump_ratio.
    """
    if len(ohlcv_bars) < _MIN_BARS:
        logger.warning("regime_detector: only %d bars, need %d; returning chaotic", len(ohlcv_bars), _MIN_BARS)
        return RegimeResult(
            regime="chaotic",
            confidence=0.20,
            realized_vol=0.0,
            drift_slope=0.0,
            jump_ratio=0.0,
        )

    closes = [float(b["close"]) for b in ohlcv_bars if b.get("close", 0) > 0]
    if len(closes) < _MIN_BARS:
        return RegimeResult(regime="chaotic", confidence=0.20, realized_vol=0.0, drift_slope=0.0, jump_ratio=0.0)

    log_returns = _safe_log_returns(closes)
    rv = _realized_vol_annualised(log_returns, bars_per_year)
    slope = _drift_slope(closes)
    autocorr = _lag1_autocorrelation(log_returns)
    jratio = _jump_ratio(log_returns)
    hhi = _dwell_hhi(closes)

    # ── Classification rules ─────────────────────────────────────────────

    # 1. Chaotic: extreme vol or too many jumps
    if jratio >= _JUMP_RATIO_CHAOTIC or rv >= _REALIZED_VOL_CHAOTIC:
        confidence = min(0.50 + jratio * 2.0, 0.85)
        return RegimeResult(
            regime="chaotic",
            confidence=round(confidence, 3),
            realized_vol=round(rv, 4),
            drift_slope=round(slope, 6),
            jump_ratio=round(jratio, 4),
        )

    abs_slope = abs(slope)

    # 2. Trending (up or down)
    # Primary signal: drift slope. Autocorr is a bonus signal, not a gate.
    # (Log-return autocorr is near-zero for i.i.d. shocks even with trend;
    #  drift_slope from log-price regression is the reliable trend signal.)
    if abs_slope >= _DRIFT_SLOPE_TREND:
        regime = "trend_up" if slope > 0 else "trend_down"
        # Confidence: slope strength + bonus for positive autocorr (momentum confirmation)
        slope_conf = min(abs_slope / (_DRIFT_SLOPE_TREND * 3), 1.0)
        autocorr_bonus = max(autocorr, 0.0) * 0.20  # 0–0.20 bonus
        confidence = 0.45 + 0.30 * slope_conf + autocorr_bonus
        return RegimeResult(
            regime=regime,
            confidence=round(min(confidence, 0.85), 3),
            realized_vol=round(rv, 4),
            drift_slope=round(slope, 6),
            jump_ratio=round(jratio, 4),
        )

    # 3. Range-bound: low drift, high dwell concentration, positive autocorr
    if abs_slope < _DRIFT_SLOPE_TREND:
        hhi_conf = min(hhi / _HHI_RANGE_BOUND, 1.0) if hhi < _HHI_RANGE_BOUND else 1.0
        autocorr_conf = min(max(autocorr - _AUTOCORR_RANGE_BOUND, 0.0) / 0.2, 1.0)
        confidence = 0.45 + 0.25 * hhi_conf + 0.15 * autocorr_conf
        return RegimeResult(
            regime="range_bound",
            confidence=round(min(confidence, 0.85), 3),
            realized_vol=round(rv, 4),
            drift_slope=round(slope, 6),
            jump_ratio=round(jratio, 4),
        )

    # 4. Default: tactical / uncertain → range_bound with low confidence
    return RegimeResult(
        regime="range_bound",
        confidence=0.35,
        realized_vol=round(rv, 4),
        drift_slope=round(slope, 6),
        jump_ratio=round(jratio, 4),
    )
