# Release Notes — Phase 1.5

**Released:** 2026-03-25
**Type:** Feature increment on Phase 1 — no breaking changes, fully backward-compatible

---

## Summary

Phase 1.5 solves the "new pool problem": Phase 1 rejected any pool with fewer than 24 hours of OHLCV history. Phase 1.5 replaces that hard gate with an **evidence-adaptive pipeline** that gracefully degrades recommendation confidence as a function of data quantity and quality — rather than refusing to operate.

---

## What Was Completed

### Backend

**New module: `history_sufficiency.py`**
- Classifies pool history into 4 tiers: `mature / growing / fresh / infant`
- Computes `effective_evidence_score` (0–1), `replay_weight`, `scenario_weight`, `uncertainty_penalty`
- Provides `age_based_width_floor()` and `fee_persistence_factor()` helpers
- No dependencies on other range_* modules (safe import order)

**Updated: `range_generator.py`**
- `_apply_width_floor()`: enforces minimum width post-tick-snap via 2% overshoot buffer + one-step expansion fallback (0 violations)
- `generate_candidates()`: `fresh_mode` skips trend-biased candidates, applies conservative sigma multipliers, generates 3 defensive candidates; `min_width_floor_pct` enforced on all outputs

**Updated: `range_scenario.py`**
- 5 new launch-mode scenarios: `discovery_sideways`, `grind_up`, `fade_down`, `spike_then_mean_revert`, `pump_and_dump`
- `compute_scenario_utility()`: `0.60×median + 0.25×min + 0.15×mean`, normalised to [0, 1]
- `compute_all_scenario_pnl()`: new `use_launch_scenarios` parameter

**Updated: `range_recommender.py`**
- Removed hard `len(ohlcv_bars) < 24` rejection gate
- Multi-resolution OHLCV fetch: 1H → 5m (≤24h pools) → 1m (≤4h pools)
- Full blended utility formula: `FinalUtility = w_replay × Replay + w_scenario × Scenario − penalty`
- Fee persistence shrinkage applied **only** to young pools (fresh/infant/growing)
- `range_type` now propagated from `CandidateRange` to `RangeProfile` (schema consistency fix)
- All new `RangeRecommendation` and `RangeProfile` fields populated

**Updated: `schemas.py`**
- `RangeProfile`: +1 existing fix (`range_type: str = ""`), +5 new optional fields
- `RangeRecommendation`: +9 new optional fields, all with safe backward-compatible defaults

**Validation:** 161 PASS / 0 FAIL / 163 total — see `docs/backend_validation_report.md`

---

### Frontend

**Updated: `frontend/lib/api.ts`**
- New types: `HistoryTier`, `RecommendationMode`, `Actionability`, `LaunchScenarioName`
- `ALL_SCENARIO_LABELS`: combined label map for both mature and launch scenario names
- `RangeProfile`: +5 optional Phase 1.5 fields
- `RangeRecommendation`: +9 optional Phase 1.5 fields (all optional, backward-compatible)

**Updated: `frontend/components/LPAnalysis.tsx`**
- `HistoryTierBadge`: colored inline badge in summary header for non-mature pools (hidden for mature)
- `EvidenceScore`: evidence % shown next to confidence bar when pool age < 24h
- `ActionabilityBanner`: full-width warning panel for `caution` (yellow) and `watch_only` (red) pools; includes applied-adjustment list and explicit "绝对数值不应过度解读" reminder
- `ScenarioTable`: now handles both 5 mature and 5 launch scenarios dynamically; shows "新池模式" tag when launch scenarios are present
- `ProfileCard` (expanded): shows `final_utility` (blended score), `scenario_utility`, fee shrinkage note when `shrunk_fee_apr` is set, and `young_pool_adjustments` list
- Not-recommended block: shows tier badge and evidence score for infant / watch-only rejections
- Footer: appends young-pool data caveat for non-mature pools

---

## What You Can Trust

| Signal | Reliability | Notes |
|---|---|---|
| **Relative range ranking** (wider vs narrower) within one pool | ✓ Reliable | IL/breach ordering is structurally correct |
| **Tier classification** (mature/growing/fresh/infant) | ✓ Reliable | Based on objective age + bar counts |
| **Evidence-adaptive mode switching** | ✓ Reliable | replay_weight + scenario_weight = 1.0 invariant confirmed |
| **Width floor enforcement** | ✓ Reliable | Tick-snap violation fixed, 0 violations in test |
| **Fee shrinkage gating** (young pools only) | ✓ Reliable | Mature pools unaffected |
| Regime detection direction (trend up/down, range_bound) | ✓ Directionally correct | Confidence varies with bar count |

## What You Should NOT Over-Interpret

| Signal | Issue | Root Cause |
|---|---|---|
| **Absolute fee APR values** | Systematically inflated | OKX volume API is token-level (all DEXes), not pool-specific |
| **Absolute utility_score / final_utility values** | Depressed for all pools using token-level data | `source_quality="token_level"` adds uncertainty penalty |
| **Cross-pool utility comparisons** | Not meaningful | Score normalisation is pool-internal |
| Scenario PnL absolute values for young pools | High variance | Based on synthetic simulation, not historical replay |

---

## Bugs Fixed

| ID | Description | File |
|---|---|---|
| B1 | Shared FakeRedis caused test cache contamination (test-only) | `validate_backend.py` |
| B2 | Width floor violated after tick-snap | `range_generator.py:_apply_width_floor()` |
| B3 | Fee shrinkage applied to mature pools with non-zero jump_ratio | `range_recommender.py` |
| B4 | `range_type` never propagated to `RangeProfile` (schema gap) | `schemas.py` + `range_recommender.py` |

---

## API Contract

All changes are **backward-compatible**:
- New fields on `RangeProfile` are optional (`None` / `[]` by default)
- New fields on `RangeRecommendation` are optional with safe defaults (`"mature"`, `"full_replay"`, `"standard"`, `1.0`)
- Existing Phase 1 fields (`reasons`, `risk_flags`, `scenario_pnl`, `utility_score`, etc.) are unchanged

---

## What Was NOT Done (Scope Boundary)

Phase 1.5 explicitly does NOT include:
- Pool-specific volume feed (still token-level)
- Real fee tier from on-chain data
- Tick liquidity distribution model
- Reward/incentive APR
- RL/ML components
- Any Phase 2 data quality improvements

These are in the Phase 2 backlog.
