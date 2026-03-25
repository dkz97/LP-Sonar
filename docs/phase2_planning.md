# Phase 2 Planning Note

**Status:** Not started — pending decision
**Prerequisite:** Phase 1.5 backend validated and shipped ✓

---

## Decision context

After Phase 1.5, the main limiting factor is no longer **logic correctness** but **data quality**. The engine's ranking logic and evidence-adaptive pipeline work correctly. What distorts outputs is the mismatch between what the engine assumes (pool-specific volume, real fee tiers, tick-level liquidity) and what the data actually provides (token-level aggregates, inferred fees, uniform TVL).

Phase 2 should be tackled in priority order below. Each P2.x block is independently deployable — they do not need to be developed together.

---

## P2.1 — Data Quality Fixes (Highest priority)

> Fixes the root cause of inflated fee APR and depressed utility scores.
> These changes affect every pool, not just new ones.

### P2.1.1 Pool-specific volume

**Current state:** `volume_24h` from OKX token-level API — aggregates all DEXes for the token.

**Problem:** A SOL/USDC pool with $7M TVL appears to earn 282% APR because the volume covers all SOL/USDC pairs across Raydium, Orca, Meteora, etc.

**Fix:** Use DexScreener `pairs/{chain}/{address}` response `volume.h24` — this is pool-specific.

**Impact:** Fee APR drops to realistic range (~50–150% for major pools). Utility scores increase in absolute terms and become more comparable across pools.

**Implementation:**
- In `range_recommender.py`: replace `pool["volume_24h"]` with DexScreener `volume.h24` from the already-fetched pool state (it's already in `_fetch_pool_state()` output but assigned to `volume_24h` as is — just needs to use the right field)
- Update `_scored_to_profile()` fee APR calculation to use pool-specific volume / TVL ratio
- Update `range_backtester.py` `_fee_proxy()` to use pool volume, not token volume

**Risk:** Low — DexScreener already returns this. No new API dependencies.

---

### P2.1.2 Real fee tier from on-chain or DexScreener

**Current state:** Fee tier inferred from `dex_id` via a static lookup table (`_infer_fee_rate()`). Uniswap V3 is always mapped to 0.3% even though 0.05% and 1% pools exist.

**Problem:** Fee tier errors of 6× (0.05% vs 0.3%) or 3× (0.3% vs 1%) are common and directly multiply into fee APR error.

**Fix options:**
1. Parse fee tier from DexScreener `pair.feeTier` (available for some protocols) — zero new API calls
2. For Solana: parse from Raydium/Meteora on-chain pool metadata

**Implementation:**
- Update `_infer_fee_rate()` to check `pair.feeTier` from DexScreener response first
- Fallback to existing lookup for protocols that don't expose it

**Risk:** Low — additive change to existing inference logic.

---

### P2.1.3 Source quality promotion for pool-specific feeds

**Current state:** `source_quality="token_level"` for all pools, adding a `source_penalty=0.05` to uncertainty.

**Fix:** When using pool-specific volume (P2.1.1), upgrade source quality:
- Pool-specific DexScreener volume → `source_quality="pool_candle"` (quality=0.7)
- Pool-specific candle API (future) → `source_quality="pool_specific"` (quality=1.0)

**Impact:** Evidence scores increase from 0.88 to ~0.95 for typical mature pools.

---

## P2.2 — CLMM Precision Enhancements (Medium priority)

> Improves the accuracy of backtesting and scoring for concentrated liquidity positions.
> These are independent model improvements that don't require new data sources.

### P2.2.1 Tick liquidity distribution model

**Current state:** `range_backtester.py` treats the full TVL as uniformly distributed across the range. This underestimates IL near the boundaries and overestimates fee capture at the extremes.

**Fix:** Model the TVL distribution as a simplified triangular or Gaussian concentration around the center price. Use this in `_il_cost_proxy()` to apply higher IL weight to positions at range edges.

**Impact:** Wider ranges get slightly penalised relative to tighter ones for the same in-range time. More realistic IL cost estimates.

**Implementation:** Pure math change within `range_backtester.py` — no new data dependencies.

---

### P2.2.2 Crowding / active liquidity adjustment

**Current state:** Assumes the LP position captures fees proportional to `position_liquidity / total_tvl`. In practice, active tick concentration means narrower positions often outperform even with lower in-range time.

**Fix:** Add a concentration factor: if position width ≤ 2× the current tick's active liquidity density, apply a multiplier > 1 to fee_proxy. This can be approximated from DexScreener `liquidity.base` and `liquidity.quote` ratio without requiring on-chain tick data.

**Risk:** Medium — empirical calibration needed. Gate behind a feature flag.

---

### P2.2.3 Improved breach probability model

**Current state:** `breach_probability = 1 - in_range_time_ratio` from historical replay. Simple but ignores the asymmetry of trending markets.

**Fix:** Supplement with a GBM-based analytical breach probability using `realized_vol`, `drift_slope`, and `horizon_hours`. Use the analytical estimate when replay history is < 24 bars, blend with replay for growing pools, pure replay for mature.

**Dependency:** Only depends on `RegimeResult` fields already computed — no new data.

---

## P2.3 — Execution and External Signals (Lower priority)

> These improve decision quality for active traders but are not needed for basic LP guidance.

### P2.3.1 Execution cost model

**Current state:** Rebalance cost is counted as a discrete event (breach → full rebalance) with no cost model.

**Fix:** Estimate gas + slippage cost per rebalance as a function of TVL position size and pool liquidity depth. Subtract from expected net PnL.

**Data needed:** Gas price feed (on-chain or DexScreener), pool depth estimate.

---

### P2.3.2 CEX/DEX price divergence signal

**Current state:** OKX token prices are from the DEX candles API. These may diverge from CEX spot during high-volatility events, introducing noise in regime detection.

**Fix:** Fetch CEX spot price from OKX market API as a reference. When CEX/DEX spread > 1%, flag as `regime=chaotic` regardless of candlestick regime. Also use CEX volume as a sanity check on token-level DEX volume.

**Data needed:** OKX market API (already have API key, just new endpoint).

---

### P2.3.3 Competitive fee capture

**Current state:** Fee APR assumes position captures fees proportional to position_TVL / pool_TVL, ignoring competitive effects from other LPs who may be providing tighter ranges.

**Fix:** Use pool TVL composition (from DexScreener `liquidity.base/quote` over time) to estimate effective competitive concentration. Downward-adjust fee APR when a large fraction of TVL is in a tighter range than the candidate.

**Data needed:** Time-series TVL data — not currently fetched.
**Complexity:** High — requires additional API polling and state storage.

---

## Recommended Phase 2 entry order

```
Phase 2.0 (quick wins, ~2 days each):
  P2.1.1  Pool-specific volume (DexScreener volume.h24)
  P2.1.2  Real fee tier from pair.feeTier
  P2.1.3  Source quality promotion

Phase 2.1 (model improvements, ~3–5 days each):
  P2.2.1  Tick liquidity distribution
  P2.2.3  Analytical breach probability for young pools

Phase 2.2 (execution signals, when needed):
  P2.3.1  Execution cost model
  P2.3.2  CEX/DEX divergence signal
  P2.2.2  Crowding factor (gate behind flag)
  P2.3.3  Competitive fee capture (after data infra)
```

---

## What does NOT require Phase 2 data improvements

- The blended utility formula and evidence-adaptive pipeline are correct and production-ready
- Tier classification, width floors, fee shrinkage, scenario simulation — all working
- Relative ranking within a pool — already reliable
- Young pool caution/watch-only signals — already correct

Phase 2 is about making the **absolute values** trustworthy, not fixing fundamental logic.
