from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel


class Layer(str, Enum):
    universe = "universe"
    hot = "hot"
    focus = "focus"


class AlertType(str, Enum):
    volume_spike = "VOLUME_SPIKE"
    breakout = "BREAKOUT"
    new_hot = "NEW_HOT"
    safety_risk = "SAFETY_RISK"
    lp_opportunity = "LP_OPPORTUNITY"
    lp_risk_warn = "LP_RISK_WARN"


class TokenSnapshot(BaseModel):
    chain_index: str
    token_address: str
    token_symbol: str = ""
    token_name: str = ""
    logo_url: str = ""
    price_usd: float = 0.0
    price_change_5m: float = 0.0
    price_change_1h: float = 0.0
    price_change_4h: float = 0.0
    price_change_24h: float = 0.0
    volume_5m: float = 0.0
    volume_1h: float = 0.0
    volume_4h: float = 0.0
    volume_24h: float = 0.0
    txs_5m: int = 0
    z_score: float = 0.0
    layer: Layer = Layer.hot
    updated_at: int = 0  # unix timestamp
    # Focus enrichment fields
    tvl_usd: Optional[float] = None
    top_pool_protocol: Optional[str] = None
    risk_level: Optional[int] = None
    is_lp_burnt: Optional[str] = None
    is_mint: Optional[str] = None
    smart_money_buys_10: Optional[int] = None
    smart_money_sells_10: Optional[int] = None
    # LP Decision fields (populated when primary pool has a decision)
    lp_eligible: Optional[bool] = None
    lp_pool_address: Optional[str] = None
    lp_pair_label: Optional[str] = None
    lp_net_score: Optional[float] = None
    lp_strategy: Optional[str] = None
    lp_holding: Optional[str] = None
    lp_il_risk: Optional[str] = None
    lp_wash_risk: Optional[str] = None


class AlertRecord(BaseModel):
    id: str
    chain_index: str
    token_address: str
    token_symbol: str = ""
    alert_type: AlertType
    timestamp: int  # unix timestamp
    # Token-level alert fields (VOLUME_SPIKE / BREAKOUT / SAFETY_RISK)
    z_score: float = 0.0
    price_change_5m: float = 0.0
    volume_5m: float = 0.0
    layer: Optional[Layer] = None
    # LP Decision alert fields (LP_OPPORTUNITY / LP_RISK_WARN)
    pool_address: str = ""
    pair_label: str = ""
    protocol: str = ""
    strategy_type: str = ""
    suggested_holding: str = ""
    net_lp_score: float = 0.0
    il_risk_level: str = ""
    wash_risk: str = ""
    main_reasons: list = []
    main_risks: list = []

    class Config:
        # Allow extra fields so future alert types don't break deserialization
        extra = "ignore"


class PoolInfo(BaseModel):
    pool_address: str
    protocol: str = ""
    tvl_usd: float = 0.0
    fee_percent: float = 0.0
    token0_symbol: str = ""
    token1_symbol: str = ""


# ── LP Decision Layer dataclasses ─────────────────────────────────────

@dataclass
class MarketQualityResult:
    pool_address: str
    wash_risk: str              # "low" | "medium" | "high"
    wash_score: float           # 0.0~1.0
    vol_tvl_ratio: float        # volume_24h / tvl_usd
    imbalance_ratio: float      # buy_vol / (buy_vol + sell_vol)
    avg_trade_size_usd: float
    flags: list = field(default_factory=list)


@dataclass
class EligibilityResult:
    eligible: bool
    failed_reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


@dataclass
class ILRiskResult:
    level: str                  # "low" | "medium" | "high"
    score: int                  # 0~100 (越高风险越大)
    main_driver: str
    factors: dict = field(default_factory=dict)


@dataclass
class HoldingPeriodResult:
    strategy_type: str          # "event" | "tactical" | "structural"
    suggested_range: str        # "2h-12h" | "1d-3d" | "7d+"
    reasons: list = field(default_factory=list)
    confidence: float = 0.5


# ── Range Recommendation Layer dataclasses ────────────────────────────

@dataclass
class RegimeResult:
    regime: Literal["range_bound", "trend_up", "trend_down", "chaotic"]
    confidence: float           # 0–1
    realized_vol: float         # annualised realised volatility (fraction, e.g. 0.80 = 80%)
    drift_slope: float          # log-price regression slope per bar
    jump_ratio: float           # fraction of bars with |return| > 3σ


@dataclass
class CandidateRange:
    lower_price: float
    upper_price: float
    lower_tick: int             # tick (V3) or bin_id (Meteora DLMM)
    upper_tick: int
    width_pct: float            # (upper - lower) / center as fraction
    center_price: float
    range_type: str             # "volatility_band" | "volume_profile" | "trend_biased" | "defensive"


@dataclass
class BacktestResult:
    in_range_time_ratio: float          # fraction of bars where price inside range
    cumulative_fee_proxy: float         # estimated fee income proxy (USD / $1 capital)
    il_cost_proxy: float                # estimated IL cost at end of horizon (fraction)
    first_breach_bar: Optional[int]     # index of first breach, None if no breach
    breach_count: int                   # number of range exits
    rebalance_count: int                # estimated rebalances needed
    realized_net_pnl_proxy: float       # fee_proxy - il_cost_proxy


@dataclass
class ScoredRange:
    candidate: CandidateRange
    backtest: BacktestResult
    fee_score: float            # 0–1, higher is better
    il_score: float             # 0–1, higher means more IL (worse)
    breach_risk: float          # 0–1, higher means more breach risk (worse)
    rebalance_cost: float       # 0–1, higher means more cost (worse)
    quality_penalty: float      # 0–1, higher means worse quality (worse)
    utility_score: float        # final composite (higher is better)
    reasons: list = field(default_factory=list)
    risk_flags: list = field(default_factory=list)


class RangeProfile(BaseModel):
    lower_price: float
    upper_price: float
    lower_tick: int
    upper_tick: int
    width_pct: float
    range_type: str = ""            # volatility_band | volume_profile | trend_biased | defensive
    expected_fee_apr: float
    expected_il_cost: float         # fraction, e.g. 0.05 = 5%
    breach_probability: float       # fraction, e.g. 0.30 = 30%
    expected_rebalance_frequency: float     # per 7 days
    expected_net_pnl: float         # fraction, net of fees and IL
    utility_score: float
    reasons: list[str]
    risk_flags: list[str]
    # Scenario PnL (spec §7.3): net PnL under 5 hypothetical future price paths
    scenario_pnl: dict[str, float] = {}  # sideways/slow_up/slow_down/breakout_up/breakdown_down → net_pnl
    # Phase 1.5: young-pool evidence fields
    shrunk_fee_apr: Optional[float] = None      # fee APR after persistence shrinkage (young pools)
    replay_utility: Optional[float] = None      # utility from historical replay component
    scenario_utility: Optional[float] = None    # utility from scenario simulation component
    final_utility: Optional[float] = None       # blended final utility (replay + scenario - penalty)
    young_pool_adjustments: list[str] = []      # human-readable list of adjustments applied


class RangeRecommendation(BaseModel):
    is_recommended: bool
    recommendation_confidence: float
    regime: str
    holding_horizon: str            # e.g. "1d-3d"
    recommended_profile_default: str = "balanced"
    profiles: dict[str, Optional[RangeProfile]]  # conservative, balanced, aggressive
    pool_quality_summary: str
    no_recommendation_reason: Optional[str] = None
    # Alternative ranges (spec §9): additional scored candidates beyond the 3 profiles
    alternative_ranges: list[RangeProfile] = []
    timestamp: float
    data_freshness: str
    # Phase 1.5: evidence / young-pool fields (all optional with safe defaults)
    history_tier: str = "mature"           # "mature" | "growing" | "fresh" | "infant"
    recommendation_mode: str = "full_replay"   # "full_replay" | "blended_replay" | "launch_mode" | "observe_only"
    actionability: str = "standard"        # "standard" | "caution" | "watch_only"
    pool_age_hours: float = 0.0
    effective_evidence_score: float = 1.0
    data_quality_score: float = 1.0
    uncertainty_penalty: float = 0.0
    replay_weight: float = 1.0
    scenario_weight: float = 0.0


@dataclass
class LPDecision:
    chain_index: str
    pool_address: str
    token_address: str
    token_symbol: str
    pair_label: str
    protocol: str
    fee_rate: float
    tvl_usd: float
    eligible: bool
    failed_reasons: list
    warnings: list
    strategy_type: str          # "" if not eligible
    suggested_holding: str
    net_lp_score: float         # 0.0~1.0
    main_reasons: list
    main_risks: list
    confidence: float
    il_risk_level: str
    wash_risk: str
    market_quality_score: float
    fee_income_score: float
    timestamp: int
