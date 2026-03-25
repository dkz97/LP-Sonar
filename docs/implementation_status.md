# LP-Sonar Implementation Status

**Last updated:** 2026-03-25
**Current milestone:** Phase 1.5 complete ✓ (backend validated + frontend shipped)

---

## Phase 1 — LP Range Recommendation Engine (Complete)

| Module | File | Status |
|---|---|---|
| History sufficiency baseline | _(implicit: hard 24-bar gate)_ | Superseded by Phase 1.5 |
| Regime detector | `backend/app/services/regime_detector.py` | ✓ |
| Candidate generator | `backend/app/services/range_generator.py` | ✓ (updated Phase 1.5) |
| Backtester | `backend/app/services/range_backtester.py` | ✓ |
| Scorer | `backend/app/services/range_scorer.py` | ✓ |
| Scenario PnL | `backend/app/services/range_scenario.py` | ✓ (updated Phase 1.5) |
| Range recommender (orchestrator) | `backend/app/services/range_recommender.py` | ✓ (updated Phase 1.5) |
| API endpoint | `backend/app/api/v1/endpoints/lp_range.py` | ✓ |
| Schemas | `backend/app/models/schemas.py` | ✓ (updated Phase 1.5) |
| Frontend types | `frontend/lib/api.ts` | ✓ (updated Phase 1.5) |
| Frontend UI | `frontend/components/LPAnalysis.tsx` | ✓ (updated Phase 1.5) |

---

## Phase 1.5 — Young Pool / Short History Support (Backend Validated ✓)

### Problem statement

Phase 1 rejected any pool with fewer than 24 OHLCV bars (`len(ohlcv_bars) < 24`).
This blocked all newly-launched pools that might still be worth LP-ing.
Phase 1.5 replaces the hard gate with **evidence-adaptive degradation**: the less
historical data available, the more the recommendation leans on synthetic scenario
simulation and applies calibration adjustments (width floors, fee shrinkage, uncertainty
penalties) to prevent overconfident outputs.

### New module

| Module | File | Purpose |
|---|---|---|
| History Sufficiency | `backend/app/services/history_sufficiency.py` | Layer 0 — computes evidence score, tier, weights, penalty |

### Modified modules

**`range_generator.py`**
- `_apply_width_floor()`: widened candidates are guaranteed ≥ min_width_pct after tick-snap (2% target buffer + one-step expansion fallback)
- `generate_candidates()`: new params `min_width_floor_pct`, `fresh_mode`; fresh mode skips trend-biased candidates, uses conservative sigma multipliers, adds 3 defensive candidates

**`range_scenario.py`**
- Added `LAUNCH_SCENARIO_NAMES`: 5 new scenarios for price-discovery phase pools
- `_simulate_scenario()`: implements `discovery_sideways`, `grind_up`, `fade_down`, `spike_then_mean_revert`, `pump_and_dump`
- Added `compute_scenario_utility()`: `0.60×median + 0.25×min + 0.15×mean`, normalised to [0,1]
- `compute_all_scenario_pnl()`: new `use_launch_scenarios` param

**`range_recommender.py`**
- `_scored_to_profile()`: now propagates `range_type` from `CandidateRange` to `RangeProfile` (schema gap fix)
- Removed hard 24-bar rejection gate
- Multi-resolution OHLCV fetch (1H → 5m → 1m based on pool age)
- Blended utility formula: `FinalUtility = w_replay × ReplayUtility + w_scenario × ScenarioUtility − penalty`
- Fee persistence shrinkage gated to young pools only (`fresh` / `infant` / `growing`)
- Populates all new `RangeRecommendation` + `RangeProfile` Phase 1.5 fields

**`schemas.py`**
- `RangeProfile`: +5 optional fields (`shrunk_fee_apr`, `replay_utility`, `scenario_utility`, `final_utility`, `young_pool_adjustments`)
- `RangeRecommendation`: +9 optional fields (`history_tier`, `recommendation_mode`, `actionability`, `pool_age_hours`, `effective_evidence_score`, `data_quality_score`, `uncertainty_penalty`, `replay_weight`, `scenario_weight`)
- All new fields have safe defaults → **backward-compatible API contract**

### History tier definitions

| Tier | Pool age | 1H bars | Replay weight | Mode |
|---|---|---|---|---|
| `mature` | ≥ 24h | ≥ 24 | 1.0 | `full_replay` |
| `growing` | 4–24h | 4–23 | 0.3–1.0 | `blended_replay` |
| `fresh` | 1–4h | 1–3 | 0–0.3 | `launch_mode` |
| `infant` | < 1h | 0 | 0 | `observe_only` |

### Age-based width floor

| Pool age | Min width |
|---|---|
| < 2h | 18% |
| 2–6h | 14% |
| 6–24h | 10% |
| ≥ 24h | 0% (disabled) |

### Validation results (2026-03-25)

Run: `backend/validate_backend.py` (venv)
**161 PASS / 0 FAIL / 2 WARN (network)** — see `docs/backend_validation_report.md`

---

## What is and is not reliable

### Reliable (use for decisions)
- **Relative ranking** of candidate ranges for the same pool — wider vs. narrower, IL risk, breach probability ordering
- **Tier classification** — correctly identifies mature / growing / fresh / infant
- **Evidence-adaptive degradation logic** — replay-weight and scenario-weight blend correctly
- **Width floor enforcement** — tick-snap violations fixed, 0 violations in test
- **Fee shrinkage** — correctly applied only to young pools, not mature

### Not reliable (do not over-interpret)
- **Absolute fee APR values** — systematically inflated because OKX volume API returns token-level aggregate across all DEXes, not pool-specific. A pool showing 282% APR may realistically earn far less.
- **Absolute utility_score / final_utility values** — score ranges vary by pool type and data quality. Do not compare across pools or treat 0.23 vs 0.50 as a strict threshold.

---

## Phase 2 Backlog (not started)

> Phase 2 is blocked on data quality, not logic correctness.

| Item | Rationale |
|---|---|
| Pool-specific volume feed | Current OKX volume = token-level; inflates fee APR for all pools |
| Real fee tier from on-chain | DexScreener doesn't expose fee tier; currently inferred from dex_id |
| Tick liquidity distribution | Uniform TVL assumption in backtester; real IL depends on bin concentration |
| Pool-specific candle API | OKX candles are token-level; per-pool price may differ (esp. high-TVL tokens on many DEXes) |
| Reward / incentive APR | Some pools have external incentives not captured in fee APR |

---

## Commit checklist (suggested)

```
feat(phase1.5): add history_sufficiency module (Layer 0 evidence assessment)
feat(phase1.5): extend range_generator with width_floor and fresh_mode
feat(phase1.5): add launch scenarios and scenario_utility to range_scenario
feat(phase1.5): rewrite recommend_range with evidence-adaptive pipeline
feat(phase1.5): extend schemas with Phase 1.5 fields (backward-compatible)
feat(frontend): add Phase 1.5 types to api.ts
feat(frontend): add history tier badge, actionability banner, evidence UI
docs: add backend_validation_report.md and implementation_status.md
```
