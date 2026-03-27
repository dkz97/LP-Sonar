"""
Execution Cost Model (P2.3.1)

Estimates total execution cost of rebalancing a CLMM LP position.
Cost has two independent components:

  1. Gas cost  — FIXED per rebalance, chain-dependent.
                 Does not scale with position size.
                 Expressed in USD, then divided by position_usd to get a fraction.

  2. Slippage  — SCALE-DEPENDENT per rebalance.
                 Proportional to depth_ratio = position_usd / tvl_usd.
                 Represents market-impact cost of entering/exiting the position.

Formula:
  gas_fraction       = gas_cost_usd(chain) / position_usd
  slippage_fraction  = min(BASE + DEPTH_K × depth_ratio, CAP)
  cost_per_rebalance = gas_fraction + slippage_fraction   (capped at 1.0)
  total_cost         = rebalance_count × cost_per_rebalance  (clamped to [0, 1])

Representative position sizing (when user does not provide position_usd):
  position_usd = min(DEFAULT_POSITION_USD, tvl_usd × DEFAULT_POSITION_SHARE_CAP)

  Default: min($10k, 1% of TVL)
  Rationale: $10k represents a typical active LP position.
             1% of TVL cap prevents overestimating slippage for small pools.
             Using pool TVL directly would assume the user deploys capital equal
             to the entire pool — an unrealistic and overly conservative assumption.

These constants are ORDER-OF-MAGNITUDE proxies. A real-time gas feed or
on-chain RPC call would be needed for production-grade accuracy.
"""
from __future__ import annotations

# ── Gas cost proxy (USD per rebalance, per chain) ───────────────────────────
# Covers a typical LP rebalance: exit position + swap + re-enter.
# Not real-time — conservative static estimates.
_GAS_COST_USD: dict[str, float] = {
    "1":     15.0,   # Ethereum mainnet
    "10":     0.5,   # Optimism
    "8453":   0.3,   # Base
    "42161":  0.5,   # Arbitrum
    "56":     0.2,   # BSC
    "137":    0.05,  # Polygon
    "501":    0.01,  # Solana
    "default": 1.0,
}

# ── Slippage model ───────────────────────────────────────────────────────────
# slippage = min(BASE + DEPTH_K × (position_usd / tvl_usd), CAP)
# At depth_ratio = 0.00: 0.05% (tight spread, negligible impact)
# At depth_ratio = 0.01 (1% of TVL): ≈ 0.055% (still very small)
# At depth_ratio = 0.10 (10% of TVL): 0.05% + 0.5% = 0.55%
# At depth_ratio ≥ 4.0: capped at 2%
_SLIPPAGE_BASE    = 0.0005   # 0.05% — floor for tight, deep pools
_SLIPPAGE_DEPTH_K = 0.005    # 0.5% per unit of depth_ratio (linear proxy)
_SLIPPAGE_CAP     = 0.02     # 2% — hard cap; avoids over-penalising extreme cases

# ── Representative position sizing ──────────────────────────────────────────
DEFAULT_POSITION_USD       = 10_000.0   # $10k — typical active LP position
DEFAULT_POSITION_SHARE_CAP = 0.01       # 1% of TVL — max share before slippage matters


# ── Public helpers ───────────────────────────────────────────────────────────

def gas_cost_usd(chain_index: str) -> float:
    """Gas cost proxy in USD per rebalance for the given chain."""
    return _GAS_COST_USD.get(chain_index, _GAS_COST_USD["default"])


def representative_position_usd(
    tvl_usd: float,
    user_position_usd: float | None = None,
) -> float:
    """
    Return the representative LP position size in USD.

    Priority:
      1. User-provided value (when present and > 0).
      2. min(DEFAULT_POSITION_USD, tvl_usd × DEFAULT_POSITION_SHARE_CAP).
         Floor of $100 so tiny pools don't produce division-by-zero gas fractions.
    """
    if user_position_usd is not None and user_position_usd > 0:
        return user_position_usd
    return min(DEFAULT_POSITION_USD, max(tvl_usd * DEFAULT_POSITION_SHARE_CAP, 100.0))


def slippage_fraction(position_usd: float, tvl_usd: float) -> float:
    """
    Round-trip slippage as a fraction of position capital.
    Scales linearly with depth_ratio = position_usd / tvl_usd, capped.
    """
    if tvl_usd <= 0:
        return _SLIPPAGE_CAP
    depth_ratio = position_usd / tvl_usd
    return min(_SLIPPAGE_BASE + _SLIPPAGE_DEPTH_K * depth_ratio, _SLIPPAGE_CAP)


def cost_per_rebalance(
    chain_index: str,
    position_usd: float,
    tvl_usd: float,
) -> float:
    """
    Total cost per rebalance as a fraction of position capital.
      gas_frac  = gas_cost_usd(chain) / position_usd   (fixed component)
      slip_frac = slippage_fraction(position_usd, tvl_usd)  (scale component)
    Returns sum, capped at 1.0.
    """
    if position_usd <= 0:
        return 0.0
    gas_frac  = gas_cost_usd(chain_index) / position_usd
    slip_frac = slippage_fraction(position_usd, tvl_usd)
    return min(gas_frac + slip_frac, 1.0)


def total_execution_cost_fraction(
    rebalance_count: int,
    chain_index: str,
    position_usd: float,
    tvl_usd: float,
) -> float:
    """
    Total execution cost as a fraction of position capital over full holding period.
    = rebalance_count × cost_per_rebalance(chain, position, tvl), clamped to [0, 1].
    Returns 0.0 for zero rebalances.
    """
    if rebalance_count <= 0 or position_usd <= 0:
        return 0.0
    per = cost_per_rebalance(chain_index, position_usd, tvl_usd)
    return round(min(rebalance_count * per, 1.0), 6)


def execution_cost_breakdown(
    rebalance_count: int,
    chain_index: str,
    position_usd: float,
    tvl_usd: float,
) -> dict[str, float]:
    """
    Return per-component breakdown for interpretability / risk-flag generation.

    Keys:
      total_fraction       cumulative gas + slippage over all rebalances
      gas_fraction         gas component only (total across rebalances)
      slippage_fraction    slippage component only (total across rebalances)
      cost_per_rebalance   per-rebalance cost fraction
    """
    if position_usd <= 0 or rebalance_count <= 0:
        return {
            "total_fraction":    0.0,
            "gas_fraction":      0.0,
            "slippage_fraction": 0.0,
            "cost_per_rebalance": 0.0,
        }
    gas_frac  = gas_cost_usd(chain_index) / position_usd
    slip_frac = slippage_fraction(position_usd, tvl_usd)
    per       = min(gas_frac + slip_frac, 1.0)
    total     = round(min(rebalance_count * per, 1.0), 6)
    return {
        "total_fraction":    total,
        "gas_fraction":      round(gas_frac  * rebalance_count, 6),
        "slippage_fraction": round(slip_frac * rebalance_count, 6),
        "cost_per_rebalance": round(per, 6),
    }
