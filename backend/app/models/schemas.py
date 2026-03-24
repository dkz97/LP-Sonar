from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
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
