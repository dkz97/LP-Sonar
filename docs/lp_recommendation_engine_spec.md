# LP Recommendation Engine Spec

## 1. Objective
Build a production-grade LP recommendation engine for concentrated-liquidity AMMs (starting from Uniswap V3 style pools and compatible CLMM designs).

The user inputs a pool contract address. The system returns:
- whether LP is recommended now
- recommended LP price range / lower tick / upper tick
- conservative / balanced / aggressive range options
- expected net PnL and supporting risk metrics
- explanation of why this range is recommended

This engine is not a generic prediction model. It is a constrained strategy optimization engine.

Core optimization target:

`Best Range = argmax over candidate ranges of Expected Utility`

Where Expected Utility is approximated by:

`U(r) = E[Fee(r)] - E[IL(r)] - OutOfRangePenalty(r) - RebalanceCost(r) - MarketQualityPenalty(r)`

---

## 2. Core Design Principle
Do **not** directly predict one lower tick and one upper tick with a single model.

Instead use a 3-layer architecture:

1. **Risk Gating Layer**
   - decide whether the pool is even suitable for LP recommendation
2. **Market Regime Layer**
   - classify the current market state
3. **Range Optimization Layer**
   - generate candidate ranges and select the best one using replay/scoring

This is better than a single algorithm because the problem is not pure price prediction. It is a strategy optimization problem under risk constraints.

---

## 3. Architecture

### 3.1 Layer A: Risk Gating / Pool Eligibility
Purpose:
- filter out bad pools
- avoid recommending ranges in structurally bad conditions

Inputs:
- current TVL
- volume/TVL
- pool age
- fee tier
- active liquidity
- price jump frequency
- wash-trading suspicion
- buy/sell imbalance
- token quote type
- protocol / pool type

Outputs:
- `eligible_for_lp_range_recommendation: bool`
- `risk_flags: []`
- `market_quality_score`
- `il_risk_score`
- `recommended_holding_horizon`

Hard reject examples:
- liquidity too thin
- volume too low or obviously fake
- frequent price jumps / unstable pool
- severe one-sided flow
- data quality insufficient

If not eligible, engine should return **No Recommendation** instead of forcing a range.

---

### 3.2 Layer B: Market Regime Detection
Purpose:
- identify what market condition the pool is currently in
- determine symmetry / asymmetry / width bias of ranges

Minimum regimes:
1. **Range-bound**
2. **Trend-up**
3. **Trend-down**
4. **Chaotic / jumpy / distorted**

Suggested regime features:
- realized volatility
- rolling drift / trend slope
- return autocorrelation
- drawdown / rebound shape
- jump ratio
- price dwell concentration
- volume-follow-through

Regime behavior:
- **Range-bound** -> tighter symmetric or mildly center-adjusted ranges
- **Trend-up** -> upward-shifted asymmetric ranges
- **Trend-down** -> downward-shifted or conservative / often no-LP recommendation
- **Chaotic** -> wide defensive range or no recommendation

Important:
Regime detection should control which candidate generators are emphasized. It should not directly output the final range.

---

### 3.3 Layer C: Candidate Range Generation
Purpose:
Generate a diverse but structured set of candidate LP ranges.

Each candidate range should contain:
- lower_price
- upper_price
- lower_tick
- upper_tick
- width_pct
- center_price
- range_type

Candidate families:

#### A. Volatility-band candidates
Use historical volatility / realized sigma to build width baselines around current price or adjusted center.
Examples:
- center ± 0.5 sigma
- center ± 1.0 sigma
- center ± 1.5 sigma
- center ± 2.0 sigma

Role:
- determines **range width baseline**

#### B. Volume-profile / value-area candidates
Use historical price-volume concentration / dwell density / volume profile to identify the consensus price zone.

Role:
- determines **range center**
- prevents naïve symmetric ranges around current spot if spot is temporarily displaced

#### C. Trend-biased asymmetric candidates
For uptrend:
- lower closer to spot
- upper farther from spot

For downtrend:
- upper closer to spot
- lower farther from spot
or reject LP depending on risk rules.

Role:
- determines **range asymmetry**

#### D. Defensive fallback candidates
Very wide candidate ranges used for chaotic or low-confidence regimes.

Role:
- fallback when confidence is low

---

## 4. Range Scoring and Final Selection
For each candidate range `r`, compute:

`Score(r) = w1*FeeScore(r) - w2*ILScore(r) - w3*BreachRisk(r) - w4*RebalanceCost(r) - w5*QualityPenalty(r)`

Recommended final engine behavior:
- compute all candidate scores
- rank candidates
- select top 3
- label them conservative / balanced / aggressive
- balanced is default

### 4.1 FeeScore
Approximation inputs:
- fee tier
- recent volume
- forecasted volume stability
- estimated in-range time ratio
- capital efficiency implied by width

Business intuition:
- narrower range -> higher capital efficiency -> potentially higher fee capture
- but also higher out-of-range probability

### 4.2 ILScore
Approximation inputs:
- realized volatility
- expected directional drift
- quote asset type
- pool type
- holding horizon
- distance from range center

Business intuition:
- more directional movement and more volatility -> larger IL pressure
- volatile quote assets and complex CLMM variants usually deserve higher penalty

### 4.3 BreachRisk
This is one of the most important penalty terms.

Approximation inputs:
- probability of price leaving the range within target horizon
- expected magnitude after breach
- expected lost fee opportunity after breach

Business intuition:
- if this penalty is weak, the engine will over-prefer ultra-narrow ranges

### 4.4 RebalanceCost
Approximation inputs:
- expected rebalance frequency
- gas cost
- slippage cost
- operational friction

Business intuition:
- a theoretically optimal narrow range may be bad in real trading if it needs constant maintenance

### 4.5 MarketQualityPenalty
Approximation inputs:
- wash-trading suspicion
- thin liquidity
- jumpy price path
- one-sided trading flow
- stale or noisy data

Business intuition:
- fake activity and distorted pools should be penalized even if fees look attractive

---

## 5. Why Not Pure Model Averaging
Do **not** simply ask 4 models to each propose a range and average them.

Recommended fusion logic:

1. **Regime detector** decides market condition
2. **Sub-models** contribute by role:
   - volatility model -> width baseline
   - volume profile -> center anchor
   - trend/regime model -> asymmetry
   - historical replay -> final judge
3. **Replay/scoring engine** ranks all candidate ranges

This is better than flat weighted averaging because:
- different models solve different sub-problems
- market conditions matter
- explainability is higher
- failure modes are easier to diagnose

---

## 6. Historical Replay / Backtest Kernel
A strong recommendation engine must include a replay kernel.

Purpose:
- validate candidate ranges on historical paths
- compare how ranges behave under real price movement
- support score calibration

Replay should estimate:
- in-range time ratio
- cumulative fee income proxy
- IL path / end-state cost proxy
- first breach time
- breach count or sustained out-of-range duration
- rebalance need
- realized net PnL proxy

Two replay modes:

### 6.1 Historical replay
Given:
- entry time t0
- pool state at t0
- candidate range r
- horizon H

Replay forward on historical path and estimate realized range performance.

### 6.2 Regime-segment replay
Build a replay dataset segmented by:
- range-bound periods
- uptrend periods
- downtrend periods
- chaotic periods

Use this to calibrate which candidate templates work better in which regime.

---

## 7. PnL Definition
The engine should explicitly distinguish 3 concepts:

### 7.1 Realized PnL
Backtest / replay based.

`PnL_realized = Fee_realized - IL_realized - Gas - RebalanceCost +/- AssetDelta`

### 7.2 Expected PnL
Used online for recommendation.

`PnL_expected = E[Fee] - E[IL] - E[Cost]`

### 7.3 Scenario PnL
Useful for UI and risk reporting.

Examples:
- sideways
- slow uptrend
- slow downtrend
- breakout up
- breakdown down

Scenario outputs help users understand recommendation robustness.

Important:
Do not pretend the engine knows the true future best PnL. It should optimize expected utility, not claim certainty.

---

## 8. Required Inputs
### V1 Mandatory Inputs
- pool contract address
- chain / protocol
- current spot price
- fee tier
- TVL
- recent volume (1h / 4h / 24h / 7d where possible)
- OHLCV history
- swap history / trade direction aggregates
- pool age
- active liquidity
- base token / quote token metadata
- quote asset type classification
- pool type classification
- tick spacing / price granularity

### V2 Important Inputs
- tick liquidity distribution
- historical liquidity migration
- LP position crowding estimate
- CEX/DEX price divergence
- volatility regime labels from offline calibration

### V3 Advanced Inputs
- full tick-by-tick event stream
- richer execution-cost model
- cross-pool routing and competitive fee capture model
- advanced liquidity density estimation

---

## 9. Output Contract
The engine output should include more than lower/upper tick.

Recommended response fields:
- `is_recommended`
- `recommendation_confidence`
- `regime`
- `holding_horizon`
- `recommended_profile_default` = balanced
- `profiles.conservative`
- `profiles.balanced`
- `profiles.aggressive`

Each profile should include:
- lower_price
- upper_price
- lower_tick
- upper_tick
- width_pct
- expected_fee_apr
- expected_il_cost
- breach_probability
- expected_rebalance_frequency
- expected_net_pnl
- utility_score
- reasons
- risk_flags

Global fields:
- pool_quality_summary
- no_recommendation_reason if applicable
- alternative_ranges
- timestamp / data freshness

---

## 10. Engineering Recommendation
Best integration path:

### New modules
- `range_recommender`
- `regime_detector`
- `range_generator`
- `range_backtester`
- `range_scorer`

### Responsibilities
- `range_recommender`: orchestrates the full flow
- `regime_detector`: classifies market state
- `range_generator`: builds candidate ranges
- `range_backtester`: replay / estimation kernel
- `range_scorer`: calculates final utility score

### API suggestion
- `GET /api/v1/lp-range/{chain}/{pool}`
or
- `POST /api/v1/lp-range/recommend`

### Recommended invocation pattern
This is better as an on-demand analysis route rather than part of a periodic discovery scheduler.

Reason:
- discovery pipelines rank opportunities at scale
- range recommendation is a single-pool intensive analysis task

---

## 11. Recommended Phased Roadmap
Even if aiming for a strong engine, implementation should still be layered.

### Phase 1
- risk gating
- regime detection
- volatility + volume-profile candidate generation
- replay-based scoring
- 3-profile output

### Phase 2
- better asymmetry logic
- tick liquidity distribution support
- improved breach/rebalance model
- confidence calibration

### Phase 3
- richer offline calibration
- better execution model
- advanced liquidity density and competitive-position modeling

---

## 12. What Not To Do First
Avoid these as initial architecture choices:
- pure RL-based range recommendation
- black-box ML directly predicting lower/upper tick
- equal-weight voting across unrelated models
- over-engineered tick-level simulation before utility function is stable

Reason:
- too hard to validate
- weak interpretability
- high implementation cost
- likely worse product iteration speed

---

## 13. Final Recommended Strategy
The best overall design is:

**Risk Gating + Market Regime Detection + Candidate Range Generation + Historical Replay Scoring + Multi-profile Output**

This is the strongest architecture because it is:
- practical
- explainable
- calibratable
- extensible
- suitable for direct LP execution support later

Claude Code should implement the engine under this principle instead of trying to train one model to directly predict ticks.
