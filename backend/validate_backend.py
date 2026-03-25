#!/usr/bin/env python3
"""
Backend Validation Script — LP Range Recommendation Engine Phase 1.5

Sections:
  A. Pure-logic unit tests (no network)
  B. Integration tests via real API calls (DexScreener + OKX)
  C. Young-pool simulation (patched pool_state)
  D. Rejection cases

Usage:
  cd /Users/zhangjiajun/LP-Sonar/backend
  python3 validate_backend.py
"""
from __future__ import annotations
import asyncio
import json
import sys
import time
import traceback
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

PASS = f"{GREEN}PASS{RESET}"
FAIL = f"{RED}FAIL{RESET}"
WARN = f"{YELLOW}WARN{RESET}"
INFO = f"{CYAN}INFO{RESET}"

results: list[dict] = []

def _ok(label: str, detail: str = "") -> None:
    print(f"  {PASS}  {label}" + (f"  [{detail}]" if detail else ""))
    results.append({"label": label, "status": "pass", "detail": detail})

def _fail(label: str, detail: str = "") -> None:
    print(f"  {FAIL}  {label}" + (f"  [{detail}]" if detail else ""))
    results.append({"label": label, "status": "fail", "detail": detail})

def _warn(label: str, detail: str = "") -> None:
    print(f"  {WARN}  {label}" + (f"  [{detail}]" if detail else ""))
    results.append({"label": label, "status": "warn", "detail": detail})

def section(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'='*60}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'='*60}{RESET}")

def check(cond: bool, label: str, detail: str = "") -> bool:
    if cond:
        _ok(label, detail)
    else:
        _fail(label, detail)
    return cond


# ══════════════════════════════════════════════════════════════════════════════
# A. UNIT TESTS — pure logic, no network
# ══════════════════════════════════════════════════════════════════════════════

def test_history_sufficiency_tiers() -> None:
    section("A1. History Sufficiency — Tier Classification")
    from app.services.history_sufficiency import assess

    # ── MATURE: 48h, enough 1H bars ──────────────────────────────────────────
    r = assess(pool_age_hours=48, bars_1h=48)
    check(r.history_tier == "mature",        "MATURE: correct tier",        r.history_tier)
    check(r.recommendation_mode == "full_replay", "MATURE: full_replay",    r.recommendation_mode)
    check(r.actionability == "standard",     "MATURE: standard actionability", r.actionability)
    check(r.replay_weight >= 0.9,            "MATURE: replay_weight≥0.9",   f"{r.replay_weight:.3f}")
    check(r.effective_evidence_score > 0.70, "MATURE: evidence>0.70",       f"{r.effective_evidence_score:.3f}")
    check(r.uncertainty_penalty < 0.10,      "MATURE: low penalty",         f"{r.uncertainty_penalty:.3f}")

    # ── GROWING: 8h pool, 96 × 5m bars ───────────────────────────────────────
    r = assess(pool_age_hours=8, bars_1h=8, bars_5m=96)
    check(r.history_tier == "growing",       "GROWING: correct tier",       r.history_tier)
    check(r.recommendation_mode == "blended_replay", "GROWING: blended_replay", r.recommendation_mode)
    check(0.0 < r.replay_weight < 1.0,       "GROWING: mixed replay_weight", f"{r.replay_weight:.3f}")
    check(r.preferred_resolution == "1H",    "GROWING: prefers 1H",         r.preferred_resolution)

    # ── FRESH: 2h pool, 24 × 5m bars ─────────────────────────────────────────
    r = assess(pool_age_hours=2, bars_1h=2, bars_5m=24, bars_1m=120)
    check(r.history_tier == "fresh",         "FRESH: correct tier",         r.history_tier)
    check(r.recommendation_mode == "launch_mode", "FRESH: launch_mode",     r.recommendation_mode)
    check(r.actionability == "caution",      "FRESH: caution actionability",r.actionability)
    check(r.replay_weight < 0.4,             "FRESH: low replay_weight",    f"{r.replay_weight:.3f}")
    check(r.scenario_weight > 0.6,           "FRESH: high scenario_weight", f"{r.scenario_weight:.3f}")
    check(r.preferred_resolution == "5m",    "FRESH: prefers 5m",           r.preferred_resolution)

    # ── INFANT: 20min pool, almost no data ───────────────────────────────────
    r = assess(pool_age_hours=0.33, bars_1h=0, bars_5m=4, bars_1m=20)
    check(r.history_tier == "infant",        "INFANT: correct tier",        r.history_tier)
    check(r.recommendation_mode == "observe_only", "INFANT: observe_only",  r.recommendation_mode)
    check(r.actionability == "watch_only",   "INFANT: watch_only",          r.actionability)
    check(r.replay_weight < 0.10,            "INFANT: near-zero replay_wt", f"{r.replay_weight:.4f}")

    # Edge: exactly on FRESH boundary (1h age, minimal bars)
    r = assess(pool_age_hours=1.0, bars_1h=1, bars_5m=12)
    check(r.history_tier == "fresh",         "BOUNDARY: 1h+12×5m = fresh",  r.history_tier)

    # Edge: 24h age but only 5 bars → should be GROWING (not mature)
    r = assess(pool_age_hours=24, bars_1h=5, bars_5m=60)
    check(r.history_tier == "growing",       "EDGE: 24h but few bars→growing", r.history_tier)


def test_width_floor() -> None:
    section("A2. Age-Based Width Floor")
    from app.services.history_sufficiency import age_based_width_floor

    cases = [
        (0.5,  0.18, "0.5h → 18%"),
        (1.5,  0.18, "1.5h → 18%"),
        (2.0,  0.14, "2.0h → 14%"),  # boundary: >=2 → 14%
        (3.0,  0.14, "3.0h → 14%"),
        (6.0,  0.10, "6.0h → 10%"),
        (12.0, 0.10, "12h  → 10%"),
        (24.0, 0.00, "24h  → 0%"),
        (48.0, 0.00, "48h  → 0%"),
    ]
    for age, expected, label in cases:
        got = age_based_width_floor(age)
        check(got == expected, label, f"got={got} expected={expected}")


def test_fee_persistence() -> None:
    section("A3. Fee Persistence Factor")
    from app.services.history_sufficiency import fee_persistence_factor

    r0   = fee_persistence_factor(0.0)
    r2   = fee_persistence_factor(2.0)
    r12  = fee_persistence_factor(12.0)
    r24  = fee_persistence_factor(24.0)
    r48  = fee_persistence_factor(48.0)

    check(r0  == 0.15, "0h pool → floor 0.15",  f"{r0}")
    check(r2  == 0.15, "2h pool → still 0.15",  f"{r2}")  # 2/24=0.083 < floor 0.15
    check(r12 >  0.40, "12h pool → >0.40",       f"{r12:.3f}")
    check(r24 == 1.0,  "24h pool → 1.0",         f"{r24}")
    check(r48 == 1.0,  "48h pool → 1.0",         f"{r48}")

    # High jump ratio should reduce factor
    r_jump = fee_persistence_factor(12.0, jump_ratio=0.3)
    check(r_jump < r12, "Jump ratio reduces factor", f"with_jump={r_jump:.3f} base={r12:.3f}")

    # High liq instability should reduce factor
    r_liq = fee_persistence_factor(12.0, liquidity_instability=0.5)
    check(r_liq < r12, "Liq instability reduces factor", f"with_liq={r_liq:.3f} base={r12:.3f}")

    # Floor always ≥ 0.15
    r_worst = fee_persistence_factor(0.0, jump_ratio=1.0, liquidity_instability=1.0)
    check(r_worst >= 0.15, "Worst case ≥ floor 0.15", f"{r_worst:.3f}")


def test_scenario_utility() -> None:
    section("A4. Scenario Utility Computation")
    from app.services.range_scenario import compute_scenario_utility

    # Positive PnL → > 0.5
    pnl_good = {"a": 0.03, "b": 0.02, "c": 0.01, "d": -0.005, "e": 0.015}
    u_good = compute_scenario_utility(pnl_good)
    check(u_good > 0.5,  "Good PnL → utility > 0.5",  f"{u_good:.4f}")
    check(u_good <= 1.0, "Good PnL → utility ≤ 1.0",  f"{u_good:.4f}")

    # All negative PnL → < 0.5
    pnl_bad = {"a": -0.05, "b": -0.04, "c": -0.03, "d": -0.02, "e": -0.01}
    u_bad = compute_scenario_utility(pnl_bad)
    check(u_bad < 0.5,  "Bad PnL → utility < 0.5",   f"{u_bad:.4f}")
    check(u_bad >= 0.0, "Bad PnL → utility ≥ 0.0",   f"{u_bad:.4f}")

    # Zero PnL → ~0.5
    pnl_zero = {"a": 0.0, "b": 0.0, "c": 0.0}
    u_zero = compute_scenario_utility(pnl_zero)
    check(abs(u_zero - 0.5) < 0.01, "Zero PnL → utility ≈ 0.5", f"{u_zero:.4f}")

    # Min dominance: one catastrophic scenario should pull utility down
    pnl_mixed = {"a": 0.05, "b": 0.04, "c": 0.03, "d": 0.02, "e": -0.10}
    u_mixed = compute_scenario_utility(pnl_mixed)
    pnl_all_pos = {"a": 0.05, "b": 0.04, "c": 0.03, "d": 0.02, "e": 0.01}
    u_all_pos = compute_scenario_utility(pnl_all_pos)
    check(u_mixed < u_all_pos, "Bad min pulls utility below all-positive", f"mixed={u_mixed:.4f} all_pos={u_all_pos:.4f}")

    # Empty → 0.0
    check(compute_scenario_utility({}) == 0.0, "Empty dict → 0.0", "")


def test_blended_utility_formula() -> None:
    section("A5. Blended Utility Formula")
    # Verify the formula: FinalUtility = rw * replay + sw * scenario - penalty

    def blended(replay_w, scenario_w, replay_util, scenario_util, penalty):
        fu = replay_w * replay_util + scenario_w * scenario_util - penalty
        return max(0.0, min(1.0, round(fu, 4)))

    # Mature pool: 100% replay → final = replay
    fu = blended(1.0, 0.0, 0.7, 0.4, 0.05)
    check(abs(fu - 0.65) < 0.01, "Mature: final ≈ replay - penalty", f"{fu:.4f}")

    # Fresh pool: 20% replay, 80% scenario, penalty 0.12
    fu = blended(0.2, 0.8, 0.7, 0.6, 0.12)
    expected = max(0.0, min(1.0, 0.2*0.7 + 0.8*0.6 - 0.12))
    check(abs(fu - expected) < 0.001, "Fresh: blended = 0.2*r + 0.8*s - p", f"got={fu:.4f} expected={expected:.4f}")

    # High penalty should push to 0 floor
    fu = blended(0.5, 0.5, 0.1, 0.05, 0.40)
    check(fu == 0.0, "High penalty → floor 0.0", f"{fu:.4f}")

    # Uncertainty penalty never causes negative
    from app.services.history_sufficiency import assess
    r = assess(pool_age_hours=0.5, bars_1h=1, source_quality="token_level")
    check(r.uncertainty_penalty >= 0.0,  "Penalty always ≥ 0", f"{r.uncertainty_penalty:.4f}")
    check(r.uncertainty_penalty <= 0.40, "Penalty always ≤ 0.40", f"{r.uncertainty_penalty:.4f}")

    # replay + scenario always = 1.0
    for age in [0.5, 2, 8, 24, 72]:
        r = assess(pool_age_hours=age, bars_1h=int(age), bars_5m=int(age*12))
        total = round(r.replay_weight + r.scenario_weight, 6)
        check(total == 1.0, f"age={age}h: replay+scenario=1.0", f"{total}")


def test_width_floor_in_generator() -> None:
    section("A6. Width Floor Enforcement in Generator")
    try:
        import numpy as np
        from app.services.regime_detector import detect_regime
        from app.services.range_generator import generate_candidates
        from app.services.history_sufficiency import age_based_width_floor

        # Build 48 fake 1H bars (flat price, low vol)
        base_price = 100.0
        bars = [{"time": i*3600, "open": base_price, "high": base_price*1.002,
                 "low": base_price*0.998, "close": base_price, "volume": 50000}
                for i in range(48)]
        regime = detect_regime(bars)

        # Mature pool — no floor
        mature_candidates = generate_candidates(
            current_price=base_price, pool_type="v3", step=60,
            regime_result=regime, ohlcv_bars=bars,
            horizon_hours=48, min_width_floor_pct=0.0, fresh_mode=False,
        )
        # Fresh pool — 14% floor, fresh_mode
        floor_pct = age_based_width_floor(3.0)  # = 0.14
        fresh_candidates = generate_candidates(
            current_price=base_price, pool_type="v3", step=60,
            regime_result=regime, ohlcv_bars=bars,
            horizon_hours=24, min_width_floor_pct=floor_pct, fresh_mode=True,
        )

        check(len(mature_candidates) > 0, "Mature: generates candidates", str(len(mature_candidates)))
        check(len(fresh_candidates) > 0,  "Fresh: generates candidates",  str(len(fresh_candidates)))

        # All fresh candidates should be ≥ floor
        violations = [c for c in fresh_candidates if c.width_pct < floor_pct - 0.001]
        check(len(violations) == 0, "Fresh: all candidates ≥ width floor",
              f"violations={len(violations)} floor={floor_pct:.2%}")

        # Fresh candidates should be wider on average than mature
        avg_fresh  = sum(c.width_pct for c in fresh_candidates) / len(fresh_candidates)
        avg_mature = sum(c.width_pct for c in mature_candidates) / len(mature_candidates)
        check(avg_fresh >= avg_mature * 0.9,
              "Fresh avg width ≥ 90% of mature avg",
              f"fresh={avg_fresh:.2%} mature={avg_mature:.2%}")

        # Fresh mode: no trend_biased candidates
        trend_in_fresh = [c for c in fresh_candidates if c.range_type == "trend_biased"]
        check(len(trend_in_fresh) == 0, "Fresh: no trend_biased candidates",
              f"found={len(trend_in_fresh)}")

        # Fresh mode: has defensive candidates
        defensive_in_fresh = [c for c in fresh_candidates if c.range_type == "defensive"]
        check(len(defensive_in_fresh) >= 2, "Fresh: ≥2 defensive candidates",
              str(len(defensive_in_fresh)))

    except ImportError as e:
        _warn("Width floor test skipped (numpy not importable)", str(e))


def test_launch_scenarios() -> None:
    section("A7. Launch Scenario Names and Simulation")
    try:
        import numpy as np
        from app.services.range_scenario import (
            LAUNCH_SCENARIO_NAMES, SCENARIO_NAMES, _simulate_scenario
        )

        expected_launch = {"discovery_sideways", "grind_up", "fade_down",
                          "spike_then_mean_revert", "pump_and_dump"}
        check(set(LAUNCH_SCENARIO_NAMES) == expected_launch,
              "LAUNCH_SCENARIO_NAMES correct", str(LAUNCH_SCENARIO_NAMES))
        check(len(SCENARIO_NAMES) == 5, "SCENARIO_NAMES has 5 entries", str(len(SCENARIO_NAMES)))

        # Each launch scenario should generate horizon_bars bars
        for name in LAUNCH_SCENARIO_NAMES:
            bars = _simulate_scenario(
                current_price=100.0, realized_vol_annual=1.5,
                avg_volume=50000, horizon_bars=24, scenario=name,
            )
            check(len(bars) == 24, f"'{name}' generates 24 bars", str(len(bars)))
            prices = [b["close"] for b in bars]
            check(all(p > 0 for p in prices), f"'{name}' all prices > 0", "")

        # pump_and_dump: final price should be near or below start price (dumps)
        bars_pnd = _simulate_scenario(100.0, 1.5, 50000, 48, "pump_and_dump")
        final_price = bars_pnd[-1]["close"]
        peak_price  = max(b["close"] for b in bars_pnd)
        check(peak_price > 100.0, "pump_and_dump: peak > start", f"peak={peak_price:.2f}")
        check(final_price < peak_price, "pump_and_dump: dump from peak", f"final={final_price:.2f}")

        # spike_then_mean_revert: price spikes then partly reverts
        bars_smr = _simulate_scenario(100.0, 1.5, 50000, 48, "spike_then_mean_revert")
        early_prices = [b["close"] for b in bars_smr[:10]]
        late_prices  = [b["close"] for b in bars_smr[35:]]
        check(max(early_prices) > 100.0, "spike_then_mean_revert: spike up", "")

    except ImportError as e:
        _warn("Launch scenario test skipped (numpy not importable)", str(e))


def test_schema_fields() -> None:
    section("A8. Schema Field Completeness")
    from app.models.schemas import RangeRecommendation, RangeProfile

    # Check all Phase 1.5 fields exist in RangeRecommendation
    rr_fields = set(RangeRecommendation.model_fields.keys())
    required_rr = {
        "history_tier", "recommendation_mode", "actionability",
        "pool_age_hours", "effective_evidence_score", "data_quality_score",
        "uncertainty_penalty", "replay_weight", "scenario_weight",
    }
    for f in required_rr:
        check(f in rr_fields, f"RangeRecommendation.{f} exists", "")

    # Check defaults are backward-compatible
    rr_defaults = {k: v.default for k, v in RangeRecommendation.model_fields.items()}
    check(rr_defaults.get("history_tier") == "mature",
          "history_tier default='mature' (backward compat)", "")
    check(rr_defaults.get("actionability") == "standard",
          "actionability default='standard' (backward compat)", "")
    check(rr_defaults.get("replay_weight") == 1.0,
          "replay_weight default=1.0 (backward compat)", "")

    # Check RangeProfile new fields
    rp_fields = set(RangeProfile.model_fields.keys())
    required_rp = {"shrunk_fee_apr", "replay_utility", "scenario_utility",
                   "final_utility", "young_pool_adjustments"}
    for f in required_rp:
        check(f in rp_fields, f"RangeProfile.{f} exists", "")

    # Confirm old fields still present (backward compat)
    old_fields = {"lower_price", "upper_price", "expected_fee_apr", "utility_score",
                  "reasons", "risk_flags", "scenario_pnl"}
    for f in old_fields:
        check(f in rp_fields, f"RangeProfile.{f} still present (backward compat)", "")


# ══════════════════════════════════════════════════════════════════════════════
# B. INTEGRATION TESTS — real API calls
# ══════════════════════════════════════════════════════════════════════════════

# ── Redis mock (no-op in-memory) ──────────────────────────────────────────────

class _FakeRedis:
    """In-memory replacement for redis.asyncio.Redis."""
    def __init__(self):
        self._data: dict = {}

    async def get(self, key):
        return self._data.get(key)

    async def setex(self, key, ttl, value):
        self._data[key] = value

    async def aclose(self):
        pass


_fake_redis = _FakeRedis()


async def _patched_get_redis():
    return _fake_redis


# ── Pool discovery via DexScreener ────────────────────────────────────────────

async def _discover_pool(chain_id: str, query: str) -> tuple[str, dict] | None:
    """Find a pool address by querying DexScreener search."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.dexscreener.com/latest/dex/search",
                params={"q": query}
            )
            if resp.status_code != 200:
                return None
            pairs = resp.json().get("pairs") or []
            # Filter by chain
            chain_map = {"56": "bsc", "8453": "base", "501": "solana", "1": "ethereum", "137": "polygon_pos"}
            target_chain = chain_map.get(chain_id, chain_id)
            filtered = [p for p in pairs if p.get("chainId") == target_chain
                       and float((p.get("liquidity") or {}).get("usd") or 0) >= 50_000
                       and float((p.get("volume") or {}).get("h24") or 0) >= 10_000]
            if not filtered:
                return None
            # Sort by TVL descending, pick top
            filtered.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
            pair = filtered[0]
            addr = pair.get("pairAddress") or ""
            return addr, pair
    except Exception as e:
        print(f"    DexScreener discovery failed: {e}")
        return None


async def _run_recommendation(chain_index: str, pool_address: str) -> dict:
    """Run recommend_range with mocked Redis, return result dict."""
    from app.services.range_recommender import recommend_range
    with patch("app.services.range_recommender.get_redis", new=AsyncMock(return_value=_fake_redis)):
        # Also patch redis in lp_decision_engine (it doesn't cache in recommend_range path but safe)
        try:
            result = await recommend_range(pool_address=pool_address, chain_index=chain_index)
            return result.model_dump()
        except Exception as e:
            return {"__error__": str(e), "__traceback__": traceback.format_exc()}


def _validate_recommendation(r: dict, label: str, expected_tier: str | None = None,
                               should_recommend: bool = True) -> None:
    """Run standard sanity checks on a RangeRecommendation dict."""
    if "__error__" in r:
        _fail(f"{label}: pipeline crashed", r["__error__"][:120])
        return

    is_rec = r.get("is_recommended", False)

    if should_recommend:
        check(is_rec, f"{label}: is_recommended=True", r.get("no_recommendation_reason", ""))
    else:
        check(not is_rec, f"{label}: correctly NOT recommended", r.get("no_recommendation_reason", ""))
        return  # nothing more to check for rejections

    if not is_rec:
        _warn(f"{label}: unexpectedly not recommended", r.get("no_recommendation_reason", ""))
        return

    # Check schema fields present
    check("history_tier" in r, f"{label}: has history_tier", r.get("history_tier"))
    check("recommendation_mode" in r, f"{label}: has recommendation_mode", "")
    check("actionability" in r, f"{label}: has actionability", "")
    check("effective_evidence_score" in r, f"{label}: has evidence_score", "")
    check("uncertainty_penalty" in r, f"{label}: has uncertainty_penalty", "")
    check("replay_weight" in r, f"{label}: has replay_weight", "")

    # Evidence scores in range
    ev = r.get("effective_evidence_score", -1)
    check(0.0 <= ev <= 1.0, f"{label}: evidence_score ∈ [0,1]", f"{ev:.4f}")

    penalty = r.get("uncertainty_penalty", -1)
    check(0.0 <= penalty <= 0.40, f"{label}: uncertainty_penalty ∈ [0,0.4]", f"{penalty:.4f}")

    rw = r.get("replay_weight", -1)
    sw = r.get("scenario_weight", -1)
    check(abs(rw + sw - 1.0) < 0.001, f"{label}: replay+scenario=1.0", f"rw={rw:.3f} sw={sw:.3f}")

    # Profiles
    profiles = r.get("profiles", {})
    profile_count = sum(1 for v in profiles.values() if v is not None)
    check(profile_count >= 1, f"{label}: at least 1 profile returned", str(profile_count))

    for pname, prof in profiles.items():
        if prof is None:
            continue
        # Prices sane
        check(prof.get("lower_price", 0) > 0, f"{label}/{pname}: lower_price > 0",
              str(prof.get("lower_price")))
        check(prof.get("upper_price", 0) > prof.get("lower_price", 0),
              f"{label}/{pname}: upper > lower", "")
        # Width in range
        width = prof.get("width_pct", 0)
        check(0 < width < 1000, f"{label}/{pname}: width_pct sane", f"{width:.1f}%")
        # Utility in [0,1]
        util = prof.get("utility_score", -1)
        check(0.0 <= util <= 1.0, f"{label}/{pname}: utility_score ∈ [0,1]", f"{util:.4f}")
        # Fee APR: shouldn't be astronomically high
        fee_apr = prof.get("expected_fee_apr", -1)
        check(0 <= fee_apr <= 30.0, f"{label}/{pname}: fee_apr ≤ 3000%", f"{fee_apr:.2f}")
        # Breach probability in [0,1]
        breach = prof.get("breach_probability", -1)
        check(0.0 <= breach <= 1.0, f"{label}/{pname}: breach_prob ∈ [0,1]", f"{breach:.4f}")
        # Scenario PnL: should have entries
        spnl = prof.get("scenario_pnl", {})
        check(len(spnl) >= 5, f"{label}/{pname}: ≥5 scenario_pnl entries", str(len(spnl)))
        # New Phase 1.5 fields
        check("replay_utility" in prof, f"{label}/{pname}: has replay_utility", "")
        check("final_utility" in prof,  f"{label}/{pname}: has final_utility", "")

    if expected_tier:
        got_tier = r.get("history_tier", "")
        check(got_tier == expected_tier, f"{label}: history_tier={expected_tier}", got_tier)

    # Print summary
    age_h = r.get("pool_age_hours", 0)
    mode  = r.get("recommendation_mode", "?")
    action= r.get("actionability", "?")
    ev    = r.get("effective_evidence_score", 0)
    rw    = r.get("replay_weight", 0)
    print(f"\n    {CYAN}Summary:{RESET} age={age_h:.1f}h mode={mode} action={action} "
          f"evidence={ev:.3f} replay_w={rw:.3f}")
    if profiles.get("balanced"):
        p = profiles["balanced"]
        print(f"    {CYAN}Balanced:{RESET} "
              f"width={p.get('width_pct',0):.1f}% "
              f"fee_apr={p.get('expected_fee_apr',0):.1%} "
              f"utility={p.get('utility_score',0):.4f} "
              f"final_util={p.get('final_utility') or 'N/A'}")
        adj = p.get("young_pool_adjustments", [])
        if adj:
            print(f"    {YELLOW}YP adjustments:{RESET} {'; '.join(adj)}")


async def test_integration_mature_bsc() -> None:
    section("B1. Integration — BSC Mature Pool (PancakeSwap V3)")
    print(f"  {INFO} Discovering BSC WBNB/USDT pool via DexScreener…")
    found = await _discover_pool("56", "WBNB USDT")
    if not found:
        _warn("BSC pool discovery failed, skipping", "")
        return
    addr, pair = found
    tvl = float((pair.get("liquidity") or {}).get("usd") or 0)
    age_h = (time.time() - int(pair.get("pairCreatedAt") or 0) / 1000) / 3600
    print(f"  {INFO} Found: {addr[:20]}… TVL=${tvl/1e6:.2f}M age={age_h:.0f}h protocol={pair.get('dexId')}")

    r = await _run_recommendation("56", addr)
    _validate_recommendation(r, "BSC-mature", should_recommend=True)


async def test_integration_base_pool() -> None:
    section("B2. Integration — Base Mature Pool (Aerodrome / UniV3)")
    print(f"  {INFO} Discovering Base WETH/USDC pool via DexScreener…")
    found = await _discover_pool("8453", "WETH USDC")
    if not found:
        _warn("Base pool discovery failed, skipping", "")
        return
    addr, pair = found
    tvl = float((pair.get("liquidity") or {}).get("usd") or 0)
    age_h = (time.time() - int(pair.get("pairCreatedAt") or 0) / 1000) / 3600
    print(f"  {INFO} Found: {addr[:20]}… TVL=${tvl/1e6:.2f}M age={age_h:.0f}h protocol={pair.get('dexId')}")

    r = await _run_recommendation("8453", addr)
    _validate_recommendation(r, "Base-mature", should_recommend=True)


async def test_integration_solana_pool() -> None:
    section("B3. Integration — Solana Pool (Raydium / Meteora)")
    print(f"  {INFO} Discovering Solana SOL/USDC pool via DexScreener…")
    found = await _discover_pool("501", "SOL USDC")
    if not found:
        _warn("Solana pool discovery failed, skipping", "")
        return
    addr, pair = found
    tvl = float((pair.get("liquidity") or {}).get("usd") or 0)
    age_h = (time.time() - int(pair.get("pairCreatedAt") or 0) / 1000) / 3600
    dex = pair.get("dexId") or "?"
    print(f"  {INFO} Found: {addr[:20]}… TVL=${tvl/1e6:.2f}M age={age_h:.0f}h protocol={dex}")

    r = await _run_recommendation("501", addr)
    _validate_recommendation(r, "Solana-pool", should_recommend=True)


# ══════════════════════════════════════════════════════════════════════════════
# C. YOUNG POOL SIMULATION — patch pool_state to simulate fresh/infant
# ══════════════════════════════════════════════════════════════════════════════

def _make_pool_state(age_hours: float, tvl: float = 500_000, vol_24h: float = 200_000,
                     protocol: str = "uniswap-v3", chain: str = "8453") -> dict:
    return {
        "pool_address":      "0xSIMULATED",
        "chain_index":       chain,
        "protocol":          protocol,
        "dex_id":            "uniswap-v3",
        "fee_rate":          0.003,
        "tvl_usd":           tvl,
        "volume_24h":        vol_24h,
        "volume_1h":         vol_24h / 24,
        "trade_count_1h":    50,
        "pool_age_days":     age_hours / 24,
        "current_price":     1800.0,
        "price_change_24h":  2.0,
        "price_change_4h":   0.5,
        "price_change_1h":   0.1,
        "base_token_address": "0xFAKETOKEN",
        "base_token_symbol":  "SIMT",
        "quote_token_symbol": "USDC",
        "quote_type":         "stable",
        "buy_volume":         vol_24h / 48,
        "sell_volume":        vol_24h / 48,
    }


def _make_ohlcv_bars(n: int, base_price: float = 1800.0, vol: float = 50000.0) -> list[dict]:
    import math
    import random
    rng = random.Random(12345)
    bars = []
    price = base_price
    for i in range(n):
        r = rng.gauss(0, 0.02)
        price *= math.exp(r)
        price = max(price, 1.0)
        bars.append({
            "time":   i * 3600,
            "open":   price * 0.999,
            "high":   price * 1.002,
            "low":    price * 0.997,
            "close":  price,
            "volume": vol * rng.uniform(0.8, 1.2),
        })
    return bars


async def _run_simulated(pool_state: dict, bars_1h: list, bars_5m: list, bars_1m: list,
                          label: str) -> dict:
    """Run recommend_range with fully patched data sources.
    Uses a FRESH Redis instance per call to avoid cross-test cache contamination."""
    from app.services.range_recommender import recommend_range

    # Fresh Redis per call — prevents result from test A leaking into test B
    # when both use the same simulated pool address
    fresh_redis = _FakeRedis()

    async def fake_fetch_pool(chain, addr):
        return pool_state

    async def fake_fetch_ohlcv(chain, token, limit=300, bar="1H"):
        if bar == "1H":
            return bars_1h
        elif bar == "5m":
            return bars_5m
        elif bar == "1m":
            return bars_1m
        return []

    with patch("app.services.range_recommender._fetch_pool_state", new=fake_fetch_pool), \
         patch("app.services.range_recommender._fetch_ohlcv", new=fake_fetch_ohlcv), \
         patch("app.services.range_recommender.get_redis", new=AsyncMock(return_value=fresh_redis)):
        try:
            result = await recommend_range("0xSIMULATED", "8453")
            return result.model_dump()
        except Exception as e:
            return {"__error__": str(e), "__traceback__": traceback.format_exc()}


async def test_fresh_pool_2h() -> None:
    section("C1. Young Pool Simulation — FRESH (2h)")
    pool  = _make_pool_state(age_hours=2.0, tvl=150_000, vol_24h=80_000)
    b1h   = _make_ohlcv_bars(2)       # 2 bars (only 2h of 1H history)
    b5m   = _make_ohlcv_bars(24)      # 24 × 5m bars
    b1m   = _make_ohlcv_bars(120)     # 120 × 1m bars

    r = await _run_simulated(pool, b1h, b5m, b1m, "FRESH-2h")
    if "__error__" in r:
        _fail("FRESH-2h: pipeline crashed", r["__error__"][:120])
        return

    check(r.get("history_tier") == "fresh",        "FRESH-2h: tier=fresh",        r.get("history_tier"))
    check(r.get("recommendation_mode") == "launch_mode", "FRESH-2h: launch_mode", r.get("recommendation_mode"))
    check(r.get("actionability") == "caution",     "FRESH-2h: caution",           r.get("actionability"))
    check(r.get("replay_weight", 1) < 0.5,         "FRESH-2h: replay_w<0.5",      f"{r.get('replay_weight'):.3f}")
    check(r.get("scenario_weight", 0) > 0.5,       "FRESH-2h: scenario_w>0.5",    f"{r.get('scenario_weight'):.3f}")

    # Check width floor was applied
    profiles = r.get("profiles", {})
    if profiles.get("aggressive"):
        width = profiles["aggressive"].get("width_pct", 0)
        check(width >= 14.0, "FRESH-2h: aggressive width≥14% (floor)", f"{width:.1f}%")

    # Check fee shrinkage
    if profiles.get("balanced"):
        shrunk = profiles["balanced"].get("shrunk_fee_apr")
        adj = profiles["balanced"].get("young_pool_adjustments", [])
        check(shrunk is not None, "FRESH-2h: shrunk_fee_apr present", str(shrunk))
        check(len(adj) > 0, "FRESH-2h: young_pool_adjustments non-empty", str(adj))

    # Check no inflated fee APR (shrinkage should have kicked in)
    if profiles.get("balanced"):
        fee = profiles["balanced"].get("expected_fee_apr", 999)
        check(fee < 5.0, "FRESH-2h: fee_apr<500% (shrinkage working)", f"{fee:.2f}")

    # Scenario PnL should use launch scenarios
    if profiles.get("balanced"):
        spnl = profiles["balanced"].get("scenario_pnl", {})
        has_launch = "discovery_sideways" in spnl or "grind_up" in spnl
        check(has_launch, "FRESH-2h: launch scenarios in pnl", str(list(spnl.keys())))


async def test_infant_pool_30min() -> None:
    section("C2. Young Pool Simulation — INFANT (<1h)")
    pool = _make_pool_state(age_hours=0.5, tvl=120_000, vol_24h=60_000)
    b1h  = []
    b5m  = _make_ohlcv_bars(6)   # 30min of 5m data
    b1m  = _make_ohlcv_bars(30)  # 30min of 1m data

    r = await _run_simulated(pool, b1h, b5m, b1m, "INFANT-30min")
    if "__error__" in r:
        _fail("INFANT-30min: pipeline crashed", r["__error__"][:120])
        return

    check(r.get("history_tier") == "infant",       "INFANT-30min: tier=infant",      r.get("history_tier"))
    check(r.get("recommendation_mode") == "observe_only", "INFANT-30min: observe_only", "")
    check(r.get("actionability") == "watch_only",  "INFANT-30min: watch_only",       "")
    # Should still return a recommendation (or a graceful no-rec), not crash
    is_rec = r.get("is_recommended", None)
    check(is_rec is not None, "INFANT-30min: returns valid response", str(is_rec))


async def test_mature_pool_sim() -> None:
    section("C3. Mature Pool Simulation (control group)")
    pool = _make_pool_state(age_hours=72.0, tvl=2_000_000, vol_24h=800_000)
    b1h  = _make_ohlcv_bars(72)   # 72 × 1H bars
    b5m  = []
    b1m  = []

    r = await _run_simulated(pool, b1h, b5m, b1m, "MATURE-72h")
    if "__error__" in r:
        _fail("MATURE-72h: pipeline crashed", r["__error__"][:120])
        return

    check(r.get("history_tier") == "mature",           "MATURE-72h: tier=mature",       r.get("history_tier"))
    check(r.get("recommendation_mode") == "full_replay","MATURE-72h: full_replay",       "")
    check(r.get("actionability") == "standard",        "MATURE-72h: standard",          "")
    check(r.get("replay_weight", 0) > 0.9,             "MATURE-72h: replay_w>0.9",      f"{r.get('replay_weight'):.3f}")
    check(r.get("is_recommended") == True,             "MATURE-72h: is_recommended",    "")

    profiles = r.get("profiles", {})
    check(len([v for v in profiles.values() if v]) == 3, "MATURE-72h: all 3 profiles",  "")

    if profiles.get("balanced"):
        adj = profiles["balanced"].get("young_pool_adjustments", [])
        check(len(adj) == 0, "MATURE-72h: no young_pool_adjustments", str(adj))
        shrunk = profiles["balanced"].get("shrunk_fee_apr")
        check(shrunk is None, "MATURE-72h: no fee shrinkage", str(shrunk))

        # Scenario PnL should use mature scenarios
        spnl = profiles["balanced"].get("scenario_pnl", {})
        has_mature = "sideways" in spnl and "slow_up" in spnl
        check(has_mature, "MATURE-72h: mature scenario_pnl keys", str(list(spnl.keys())))


# ══════════════════════════════════════════════════════════════════════════════
# D. REJECTION TESTS
# ══════════════════════════════════════════════════════════════════════════════

async def test_rejection_low_tvl() -> None:
    section("D1. Rejection — Low TVL Pool")
    pool = _make_pool_state(age_hours=48, tvl=10_000, vol_24h=5_000)  # below $50k TVL
    b1h  = _make_ohlcv_bars(48)
    r = await _run_simulated(pool, b1h, [], [], "low-tvl")

    if "__error__" in r:
        _fail("low-tvl: crashed unexpectedly", r["__error__"][:120])
        return

    check(r.get("is_recommended") == False, "low-tvl: correctly rejected", "")
    reason = r.get("no_recommendation_reason") or ""
    check("TVL" in reason or "eligible" in reason.lower() or len(reason) > 0,
          "low-tvl: meaningful rejection reason", reason[:80])
    check(r.get("history_tier") != "unknown", "low-tvl: tier still populated",
          r.get("history_tier", ""))


async def test_rejection_no_price_data() -> None:
    section("D2. Rejection — No OHLCV Data")
    pool = _make_pool_state(age_hours=0.1, tvl=200_000, vol_24h=100_000)
    r = await _run_simulated(pool, [], [], [], "no-data")  # zero bars

    if "__error__" in r:
        _fail("no-data: crashed unexpectedly", r["__error__"][:120])
        return

    check(r.get("is_recommended") == False, "no-data: correctly no recommendation", "")
    check(len(r.get("no_recommendation_reason") or "") > 0,
          "no-data: has rejection reason", r.get("no_recommendation_reason", "")[:80])


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  LP Range Engine — Phase 1.5 Backend Validation{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")

    t0 = time.time()

    # ── A. Unit tests ─────────────────────────────────────────────────────────
    test_history_sufficiency_tiers()
    test_width_floor()
    test_fee_persistence()
    test_scenario_utility()
    test_blended_utility_formula()
    test_width_floor_in_generator()
    test_launch_scenarios()
    test_schema_fields()

    # ── B. Integration tests ──────────────────────────────────────────────────
    await test_integration_mature_bsc()
    await test_integration_base_pool()
    await test_integration_solana_pool()

    # ── C. Simulation tests ───────────────────────────────────────────────────
    await test_fresh_pool_2h()
    await test_infant_pool_30min()
    await test_mature_pool_sim()

    # ── D. Rejection tests ────────────────────────────────────────────────────
    await test_rejection_low_tvl()
    await test_rejection_no_price_data()

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed = time.time() - t0
    passed  = sum(1 for r in results if r["status"] == "pass")
    failed  = sum(1 for r in results if r["status"] == "fail")
    warned  = sum(1 for r in results if r["status"] == "warn")
    total   = len(results)

    section("VALIDATION SUMMARY")
    print(f"  Total:  {total}")
    print(f"  {GREEN}Passed:{RESET} {passed}")
    print(f"  {RED}Failed:{RESET} {failed}")
    print(f"  {YELLOW}Warned:{RESET} {warned}")
    print(f"  Time:   {elapsed:.1f}s")

    if failed > 0:
        print(f"\n{RED}FAILED CHECKS:{RESET}")
        for r in results:
            if r["status"] == "fail":
                print(f"  ✗ {r['label']}" + (f": {r['detail']}" if r['detail'] else ""))

    print()
    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    asyncio.run(main())
