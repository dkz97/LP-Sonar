"""
Offline calibration script — P2.X Confidence / Replay Calibration.

Usage (from backend/):
    python -m scripts.calibrate [--out data/calibration.json] [--dry-run]

Output: backend/data/calibration.json with structure:
    {
        "calibration_growing_standard_threshold": float,
        "calibration_mature_standard_threshold":  float,
        "replay_weight_lower_bound":              float,
        "replay_weight_upper_bound":              float,
        "confidence_regime_scales": {
            "range_bound": float,
            "trend_up":    float,
            "trend_down":  float,
            "chaotic":     float,
        },
        "meta": {
            "generated_at":      str (ISO-8601),
            "real_samples":      int,
            "synthetic_samples": int,
            "real_weight":       float,
            "synthetic_weight":  float,
        }
    }

Calibration strategy:
    1. Real walk-forward anchor: for each of N_REAL_POOLS pools, fetch 300 bars of
       1H OHLCV from OKX CEX (via public candle endpoint), run sliding-window
       walk-forward, record actual breach rate and utility accuracy.
    2. Synthetic supplement: generate GBM paths per regime (N_SYNTH_PATHS × 4),
       compute theoretical metrics. Weight=SYNTH_WEIGHT to keep real data as anchor.
    3. Blend real + synthetic; apply BOUNDS constraints on all outputs.
    4. Write calibration.json.

All outputs are constrained by BOUNDS to prevent synthetic data from pulling
thresholds outside safe operating ranges.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger("calibrate")

# ── Configuration ──────────────────────────────────────────────────────────────

# Reference pools for walk-forward (well-known high-liquidity pairs, each chain).
# Deliberately span a wide volatility range so regime breach rates are meaningfully
# differentiated — critical for calibrating confidence_regime_scales.
#
# Ultra-low vol  : USDC-USDT  (stable pair, near-zero ±10% breach rate → baseline)
# Low-medium vol : BTC-USDT, ETH-USDT, BNB-USDT, LINK-USDT
# Medium-high vol: SOL-USDC
# High vol       : DOGE-USDT
# Very high vol  : PEPE-USDT, WIF-USDT  (meme coins, frequent ±10% moves)
#
# All failures are gracefully skipped — the script continues with whatever succeeds.
REFERENCE_POOLS: list[dict] = [
    # ── Ultra-low vol (stable) ─────────────────────────────────────────────────
    {"symbol": "USDC/USDT",  "instId": "USDC-USDT",  "chain": "eth"},
    # ── Low-medium vol ─────────────────────────────────────────────────────────
    {"symbol": "BTC/USDT",   "instId": "BTC-USDT",   "chain": "btc"},
    {"symbol": "ETH/USDT",   "instId": "ETH-USDT",   "chain": "eth"},
    {"symbol": "BNB/USDT",   "instId": "BNB-USDT",   "chain": "bsc"},
    {"symbol": "LINK/USDT",  "instId": "LINK-USDT",  "chain": "eth"},
    # ── Medium-high vol ────────────────────────────────────────────────────────
    {"symbol": "SOL/USDC",   "instId": "SOL-USDC",   "chain": "solana"},
    # ── High vol ───────────────────────────────────────────────────────────────
    {"symbol": "DOGE/USDT",  "instId": "DOGE-USDT",  "chain": "bsc"},
    # ── Very high vol (meme) ───────────────────────────────────────────────────
    {"symbol": "PEPE/USDT",  "instId": "PEPE-USDT",  "chain": "eth"},
    {"symbol": "WIF/USDT",   "instId": "WIF-USDT",   "chain": "solana"},
]

# Walk-forward parameters
WF_BARS        = 300      # bars to fetch per pool
WF_WINDOW      = 48       # training window (bars)
WF_STEP        = 12       # slide step (bars)
WF_HORIZON     = 24       # forward horizon to evaluate (bars)

# Synthetic paths
N_SYNTH_PATHS  = 200      # per regime
SYNTH_WEIGHT   = 0.3      # how much synthetic contributes vs real
REAL_WEIGHT    = 1.0 - SYNTH_WEIGHT

# Output parameter bounds — hard limits, no calibration can exceed these.
BOUNDS: dict[str, tuple[float, float]] = {
    "calibration_growing_standard_threshold": (0.50, 0.80),
    "calibration_mature_standard_threshold":  (0.40, 0.70),
    "replay_weight_lower_bound":              (0.10, 0.40),
    "replay_weight_upper_bound":              (0.60, 0.90),
    "confidence_regime_scale_min":            (0.50, 0.70),   # chaotic / trend
    "confidence_regime_scale_max":            (0.90, 1.00),   # range_bound
}

# Defaults (current hand-crafted values — fallback when real data insufficient)
DEFAULTS: dict[str, Any] = {
    "calibration_growing_standard_threshold": 0.65,
    "calibration_mature_standard_threshold":  0.55,
    "replay_weight_lower_bound":              0.25,
    "replay_weight_upper_bound":              0.75,
    "confidence_regime_scales": {
        "range_bound": 1.0,
        "trend_up":    0.85,
        "trend_down":  0.85,
        "chaotic":     0.70,
    },
}

OKX_CANDLE_URL = "https://www.okx.com/api/v5/market/candles"
_HTTP_TIMEOUT  = 10.0


# ── OKX data fetch ─────────────────────────────────────────────────────────────

def _fetch_okx_candles(inst_id: str, limit: int = 300) -> list[list]:
    """
    Fetch 1H OHLCV candles from OKX public market API.

    Returns list of [ts, open, high, low, close, vol, volCcyQuote, ...] or [].
    Bars are newest-first from OKX; we reverse to oldest-first.
    """
    params = {"instId": inst_id, "bar": "1H", "limit": str(min(limit, 300))}
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.get(OKX_CANDLE_URL, params=params)
            if resp.status_code != 200:
                logger.warning("OKX candles HTTP %s for %s", resp.status_code, inst_id)
                return []
            data = resp.json()
            if data.get("code") != "0":
                logger.warning("OKX candles error: %s for %s", data.get("msg"), inst_id)
                return []
            bars = data.get("data", [])
            bars.reverse()   # oldest first
            return bars
    except Exception as exc:
        logger.warning("OKX candles request failed for %s: %s", inst_id, exc)
        return []


def _parse_closes(bars: list[list]) -> list[float]:
    """Extract close prices (index 4) as floats."""
    closes = []
    for bar in bars:
        try:
            closes.append(float(bar[4]))
        except (IndexError, ValueError):
            pass
    return closes


# ── Walk-forward analysis ──────────────────────────────────────────────────────

def _detect_regime(closes: list[float]) -> str:
    """
    Simple regime classifier for a price series.

    Returns one of: range_bound | trend_up | trend_down | chaotic
    """
    if len(closes) < 4:
        return "range_bound"

    total_return = (closes[-1] / closes[0]) - 1.0
    returns = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    std = (sum(r ** 2 for r in returns) / len(returns)) ** 0.5
    ann_vol = std * (24 ** 0.5)   # rough daily vol for 1H bars

    if ann_vol > 0.12:
        return "chaotic"
    elif total_return > 0.08:
        return "trend_up"
    elif total_return < -0.08:
        return "trend_down"
    else:
        return "range_bound"


def _compute_breach_rate(closes: list[float], lower: float, upper: float) -> float:
    """Fraction of forward bars that breach [lower, upper]."""
    if not closes:
        return 0.0
    breached = sum(1 for c in closes if c < lower or c > upper)
    return breached / len(closes)


def _walk_forward_pool(closes: list[float]) -> list[dict]:
    """
    Run sliding-window walk-forward on a close price series.

    Returns list of sample dicts with keys:
        regime, evidence_score, actual_breach_rate, window_std
    """
    samples = []
    n = len(closes)
    if n < WF_WINDOW + WF_HORIZON:
        return samples

    for start in range(0, n - WF_WINDOW - WF_HORIZON, WF_STEP):
        train  = closes[start : start + WF_WINDOW]
        fwd    = closes[start + WF_WINDOW : start + WF_WINDOW + WF_HORIZON]
        regime = _detect_regime(train)

        returns   = [train[i] / train[i - 1] - 1.0 for i in range(1, len(train))]
        std       = (sum(r ** 2 for r in returns) / max(len(returns), 1)) ** 0.5
        # Fixed ±10% range — regime-invariant window so that high-volatility
        # (chaotic) windows produce higher breach rates than range_bound windows.
        # An adaptive ±1σ√T range would normalise away regime differences.
        mid       = train[-1]
        lower_p   = mid * 0.90
        upper_p   = mid * 1.10

        breach    = _compute_breach_rate(fwd, lower_p, upper_p)
        # evidence_score proxy: more bars + lower vol → higher evidence
        coverage  = min(1.0, len(train) / 48)
        vol_pen   = min(1.0, std * 100)
        evidence  = round(0.6 * coverage + 0.4 * (1.0 - vol_pen), 3)

        samples.append({
            "regime":             regime,
            "evidence_score":     evidence,
            "actual_breach_rate": round(breach, 4),
            "window_std":         round(std, 6),
        })

    return samples


# ── Synthetic GBM paths ────────────────────────────────────────────────────────

def _gbm_path(n_bars: int, mu: float, sigma: float, seed: int) -> list[float]:
    """Generate a geometric Brownian motion price path."""
    rng = random.Random(seed)
    price = 100.0
    path  = [price]
    dt    = 1.0 / n_bars
    for _ in range(n_bars - 1):
        z     = rng.gauss(0, 1)
        price = price * math.exp((mu - 0.5 * sigma ** 2) * dt + sigma * math.sqrt(dt) * z)
        path.append(price)
    return path


_REGIME_PARAMS: dict[str, tuple[float, float]] = {
    "range_bound": (0.00,  0.30),
    "trend_up":    (0.40,  0.35),
    "trend_down":  (-0.40, 0.35),
    "chaotic":     (0.00,  0.90),
}


def _synthetic_samples() -> list[dict]:
    """Generate synthetic walk-forward samples across all regimes."""
    samples = []
    for regime, (mu, sigma) in _REGIME_PARAMS.items():
        for i in range(N_SYNTH_PATHS):
            total_bars = WF_BARS
            seed       = hash((regime, i)) & 0xFFFFFFFF
            closes     = _gbm_path(total_bars, mu, sigma, seed)
            pool_samps = _walk_forward_pool(closes)
            # Override regime label with intended regime for synthetic paths
            for s in pool_samps:
                s["regime"] = regime
                s["_synthetic"] = True
            samples.extend(pool_samps)
    return samples


# ── Calibration computation ────────────────────────────────────────────────────

def _clamp(value: float, key: str) -> float:
    lo, hi = BOUNDS[key]
    return max(lo, min(hi, value))


def _calibrate_thresholds(
    samples: list[dict], real_count: int, synth_count: int
) -> dict[str, Any]:
    """
    Derive calibration parameters from blended sample set.

    Strategy:
    - For actionability thresholds: find evidence_score percentile where
      actual_breach_rate transitions from high to acceptable (< 35% breach).
    - For replay weight bounds: map evidence_score range where breach prediction
      switches from mostly scenario to mostly replay accuracy.
    - For regime confidence scales: compare actual breach rates by regime, then
      map to a confidence multiplier (lower breach → higher scale).
    """
    if not samples:
        logger.warning("No samples available; returning defaults")
        return DEFAULTS.copy()

    # Sort samples by evidence_score for percentile analysis
    sorted_s = sorted(samples, key=lambda x: x["evidence_score"])

    # ── Actionability thresholds ──────────────────────────────────────────────
    # Find evidence level where breach_rate drops below 35% (manageable range)
    BREACH_TARGET = 0.35

    def _find_evidence_threshold(samps: list[dict]) -> float:
        if not samps:
            return 0.60
        # Bin samples into 10 buckets by evidence_score
        bucket_size = max(1, len(samps) // 10)
        for i in range(0, len(samps), bucket_size):
            bucket = samps[i : i + bucket_size]
            avg_ev = sum(s["evidence_score"] for s in bucket) / len(bucket)
            avg_br = sum(s["actual_breach_rate"] for s in bucket) / len(bucket)
            if avg_br < BREACH_TARGET:
                return round(avg_ev, 2)
        return round(samps[-1]["evidence_score"], 2)

    # Separate growing/mature by window coverage proxy
    # (mature: larger evidence generally; we use evidence > 0.55 as proxy)
    mature_samps  = [s for s in sorted_s if s["evidence_score"] >= 0.55]
    growing_samps = [s for s in sorted_s if s["evidence_score"] < 0.55]

    mature_thresh  = _find_evidence_threshold(mature_samps)  if mature_samps  else 0.55
    growing_thresh = _find_evidence_threshold(growing_samps) if growing_samps else 0.65

    # Growing always requires stricter evidence than mature
    growing_thresh = max(growing_thresh, mature_thresh + 0.05)

    growing_thresh = _clamp(growing_thresh, "calibration_growing_standard_threshold")
    mature_thresh  = _clamp(mature_thresh,  "calibration_mature_standard_threshold")

    # ── Replay weight bounds ───────────────────────────────────────────────────
    # Lower bound: evidence_score below which breach_rate is too high for replay
    # Upper bound: evidence_score above which breach_rate stabilises (replay useful)
    n = len(sorted_s)
    lower_idx = max(0, n // 5)    # 20th percentile
    upper_idx = min(n - 1, 3 * n // 4)   # 75th percentile
    rw_lo = _clamp(
        round(sorted_s[lower_idx]["evidence_score"], 2),
        "replay_weight_lower_bound",
    )
    rw_hi = _clamp(
        round(sorted_s[upper_idx]["evidence_score"], 2),
        "replay_weight_upper_bound",
    )
    # Ensure lo < hi with minimum gap of 0.30 for meaningful differentiation
    if rw_hi - rw_lo < 0.30:
        mid_rw = (rw_lo + rw_hi) / 2.0
        rw_lo  = _clamp(round(mid_rw - 0.15, 2), "replay_weight_lower_bound")
        rw_hi  = _clamp(round(mid_rw + 0.15, 2), "replay_weight_upper_bound")

    # ── Regime confidence scales ───────────────────────────────────────────────
    # Per-regime: average breach_rate → invert to scale (higher breach → lower scale).
    # Scale = 1.0 − (regime_breach − baseline_breach) / baseline_breach, clamped [0.5, 1.0].
    regime_breach: dict[str, list[float]] = {}
    for s in samples:
        r = s["regime"]
        regime_breach.setdefault(r, []).append(s["actual_breach_rate"])

    regime_avg: dict[str, float] = {
        r: sum(v) / len(v) for r, v in regime_breach.items() if v
    }

    baseline = regime_avg.get("range_bound", 0.30)
    scales: dict[str, float] = {}
    for regime in ("range_bound", "trend_up", "trend_down", "chaotic"):
        avg = regime_avg.get(regime, baseline)
        if baseline > 0:
            raw_scale = 1.0 - (avg - baseline) / (baseline + 1e-6)
        else:
            raw_scale = 1.0
        raw_scale = max(0.5, min(1.0, round(raw_scale, 3)))
        scales[regime] = raw_scale

    # range_bound always 1.0 (it's the reference)
    scales["range_bound"] = 1.0

    return {
        "calibration_growing_standard_threshold": growing_thresh,
        "calibration_mature_standard_threshold":  mature_thresh,
        "replay_weight_lower_bound":              rw_lo,
        "replay_weight_upper_bound":              rw_hi,
        "confidence_regime_scales":               scales,
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def run_calibration(out_path: str, dry_run: bool = False) -> dict:
    """Run full calibration pipeline and return (or write) result dict."""

    # ── 1. Real walk-forward ──────────────────────────────────────────────────
    real_samples: list[dict] = []
    for pool in REFERENCE_POOLS:
        logger.info("Fetching %s candles for %s …", WF_BARS, pool["symbol"])
        bars   = _fetch_okx_candles(pool["instId"], WF_BARS)
        closes = _parse_closes(bars)
        if len(closes) < WF_WINDOW + WF_HORIZON:
            logger.warning("  Insufficient bars (%d) for %s — skipped", len(closes), pool["symbol"])
            continue
        samps = _walk_forward_pool(closes)
        logger.info("  %d walk-forward samples from %s", len(samps), pool["symbol"])
        real_samples.extend(samps)

    logger.info("Real samples total: %d", len(real_samples))

    # ── 2. Synthetic supplement ────────────────────────────────────────────────
    logger.info("Generating synthetic GBM paths (%d × 4 regimes) …", N_SYNTH_PATHS)
    synth_samples = _synthetic_samples()
    logger.info("Synthetic samples total: %d", len(synth_samples))

    # ── 3. Blend (real as anchor) ──────────────────────────────────────────────
    # Replicate real samples by effective weight ratio so calibration is anchored.
    # Synthetic samples are regime-proportionally subsampled to guarantee each
    # regime (range_bound/trend_up/trend_down/chaotic) is represented — simple
    # head-slicing would silently cut off later regimes in the ordered list.
    if real_samples:
        real_rep = real_samples   # full weight
        synth_count_adj = int(len(synth_samples) * SYNTH_WEIGHT)
        # Group by regime then take equal share from each
        _by_regime: dict[str, list] = {}
        for s in synth_samples:
            _by_regime.setdefault(s["regime"], []).append(s)
        per_regime = max(1, synth_count_adj // max(len(_by_regime), 1))
        synth_rep = []
        for _regime_samps in _by_regime.values():
            synth_rep.extend(_regime_samps[:per_regime])
    else:
        logger.warning("No real samples — calibration will rely on synthetic paths only")
        real_rep   = []
        synth_rep  = synth_samples

    blended = real_rep + synth_rep
    logger.info(
        "Blended sample set: %d real + %d synthetic (effective)",
        len(real_rep), len(synth_rep),
    )

    # ── 4. Compute parameters ─────────────────────────────────────────────────
    params = _calibrate_thresholds(blended, len(real_rep), len(synth_rep))

    result: dict[str, Any] = {
        **params,
        "confidence_regime_scales": params["confidence_regime_scales"],
        "meta": {
            "generated_at":      datetime.now(timezone.utc).isoformat(),
            "real_samples":      len(real_samples),
            "synthetic_samples": len(synth_samples),
            "real_weight":       REAL_WEIGHT,
            "synthetic_weight":  SYNTH_WEIGHT,
        },
    }

    if dry_run:
        logger.info("DRY RUN — output:\n%s", json.dumps(result, indent=2))
        return result

    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    logger.info("Calibration written to %s", out_path)
    return result


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="LP-Sonar offline calibration")
    p.add_argument("--out",     default="data/calibration.json", help="Output JSON path")
    p.add_argument("--dry-run", action="store_true", help="Print output without writing")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    result = run_calibration(args.out, args.dry_run)
    # Summary
    print("\n── Calibration result ──────────────────────────────")
    for k, v in result.items():
        if k != "meta":
            print(f"  {k}: {v}")
    print(f"  meta: {result.get('meta', {})}")
