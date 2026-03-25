const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Layer = "universe" | "hot" | "focus";

export type AlertType =
  | "VOLUME_SPIKE"
  | "BREAKOUT"
  | "NEW_HOT"
  | "SAFETY_RISK"
  | "LP_OPPORTUNITY"
  | "LP_RISK_WARN";

export type StrategyType = "event" | "tactical" | "structural" | "";
export type ILRiskLevel  = "low" | "medium" | "high" | "";
export type WashRisk     = "low" | "medium" | "high" | "";

export interface TokenSnapshot {
  chain_index: string;
  token_address: string;
  token_symbol: string;
  token_name: string;
  logo_url: string;
  price_usd: number;
  price_change_5m: number;
  price_change_1h: number;
  price_change_4h: number;
  price_change_24h: number;
  volume_5m: number;
  volume_1h: number;
  volume_4h: number;
  volume_24h: number;
  txs_5m: number;
  z_score: number;
  layer: Layer;
  updated_at: number;
  // Focus enrichment
  tvl_usd?: number;
  top_pool_protocol?: string;
  risk_level?: number;
  is_lp_burnt?: string;
  is_mint?: string;
  smart_money_buys_10?: number;
  smart_money_sells_10?: number;
  // LP Decision (attached when primary pool has a decision)
  lp_eligible?: boolean;
  lp_pool_address?: string;
  lp_pair_label?: string;
  lp_net_score?: number;
  lp_strategy?: StrategyType;
  lp_holding?: string;
  lp_il_risk?: ILRiskLevel;
  lp_wash_risk?: WashRisk;
}

export interface AlertRecord {
  id: string;
  chain_index: string;
  token_address: string;
  token_symbol: string;
  alert_type: AlertType;
  timestamp: number;
  // Token-level alert fields
  z_score?: number;
  price_change_5m?: number;
  volume_5m?: number;
  layer?: Layer;
  // LP alert fields
  pool_address?: string;
  pair_label?: string;
  protocol?: string;
  strategy_type?: StrategyType;
  suggested_holding?: string;
  net_lp_score?: number;
  il_risk_level?: ILRiskLevel;
  wash_risk?: WashRisk;
  main_reasons?: string[];
  main_risks?: string[];
}

export interface LPOpportunity {
  chain_index: string;
  pool_address: string;
  token_address: string;
  token_symbol: string;
  logo_url?: string;
  pair_label: string;
  protocol: string;
  fee_rate: number;
  tvl_usd: number;
  eligible: boolean;
  strategy_type: StrategyType;
  suggested_holding: string;
  net_lp_score: number;
  fee_income_score: number;
  market_quality_score: number;
  il_risk_level: ILRiskLevel;
  wash_risk: WashRisk;
  main_reasons: string[];
  main_risks: string[];
  confidence: number;
  timestamp: number;
}

// ── Range Recommendation types ─────────────────────────────────────────────────

export type Regime = "range_bound" | "trend_up" | "trend_down" | "chaotic";

// Phase 1 — standard (mature pool) scenario names
export type ScenarioName = "sideways" | "slow_up" | "slow_down" | "breakout_up" | "breakdown_down";

// Phase 1.5 — launch-mode scenario names (fresh / infant pools)
export type LaunchScenarioName =
  | "discovery_sideways"
  | "grind_up"
  | "fade_down"
  | "spike_then_mean_revert"
  | "pump_and_dump";

export const SCENARIO_LABELS: Record<ScenarioName, string> = {
  sideways:       "横盘",
  slow_up:        "缓慢上涨",
  slow_down:      "缓慢下跌",
  breakout_up:    "快速突破上涨",
  breakdown_down: "快速崩盘下跌",
};

export const LAUNCH_SCENARIO_LABELS: Record<LaunchScenarioName, string> = {
  discovery_sideways:     "初始震荡",
  grind_up:               "缓慢积累",
  fade_down:              "热度消退",
  spike_then_mean_revert: "拉升后回归",
  pump_and_dump:          "拉高砸盘",
};

// Combined label map for all scenario names (use when rendering scenario_pnl dynamically)
export const ALL_SCENARIO_LABELS: Record<string, string> = {
  ...SCENARIO_LABELS,
  ...LAUNCH_SCENARIO_LABELS,
};

// Phase 1.5 — history tier / evidence adaptive types
export type HistoryTier = "mature" | "growing" | "fresh" | "infant";
export type RecommendationMode = "full_replay" | "blended_replay" | "launch_mode" | "observe_only";
export type Actionability = "standard" | "caution" | "watch_only";

export interface RangeProfile {
  lower_price: number;
  upper_price: number;
  lower_tick: number;
  upper_tick: number;
  width_pct: number;       // already in % (e.g. 29.2 means 29.2%)
  expected_fee_apr: number;   // fraction (e.g. 0.15 = 15% APR)
  expected_il_cost: number;   // fraction
  breach_probability: number; // fraction
  expected_rebalance_frequency: number; // per 7 days
  expected_net_pnl: number;   // fraction
  utility_score: number;      // 0–1, raw replay-based score
  reasons: string[];
  risk_flags: string[];
  scenario_pnl: Partial<Record<string, number>>;  // net PnL per scenario (mature or launch)
  range_type?: string;  // volatility_band | volume_profile | trend_biased | defensive
  // Phase 1.5 additions (optional; absent for mature pools without young-pool adjustments)
  shrunk_fee_apr?: number | null;   // fee APR after persistence shrinkage (young pools only)
  replay_utility?: number | null;   // same as utility_score (replay-based component)
  scenario_utility?: number | null; // utility computed from scenario PnL simulation
  final_utility?: number | null;    // blended final utility: w_replay*replay + w_scenario*scenario - penalty
  young_pool_adjustments?: string[]; // human-readable list of adjustments applied for young pools
}

export interface RangeRecommendation {
  is_recommended: boolean;
  recommendation_confidence: number;
  regime: Regime;
  holding_horizon: string;
  recommended_profile_default: string;
  profiles: {
    conservative: RangeProfile | null;
    balanced: RangeProfile | null;
    aggressive: RangeProfile | null;
  };
  pool_quality_summary: string;
  no_recommendation_reason: string | null;
  alternative_ranges: RangeProfile[];
  timestamp: number;
  data_freshness: string;
  // Phase 1.5 additions (all optional with backward-compatible defaults)
  history_tier?: HistoryTier;               // defaults to "mature" for old responses
  recommendation_mode?: RecommendationMode; // defaults to "full_replay"
  actionability?: Actionability;            // defaults to "standard"
  pool_age_hours?: number;                  // 0.0 if unknown
  effective_evidence_score?: number;        // 0–1
  data_quality_score?: number;              // 0–1
  uncertainty_penalty?: number;             // 0–0.40
  replay_weight?: number;                   // 0–1
  scenario_weight?: number;                 // 0–1
}

// ── Fetch functions ────────────────────────────────────────────────────────────

export async function fetchTokens(
  layer: Layer = "hot",
  chain?: string,
  limit = 200
): Promise<TokenSnapshot[]> {
  const params = new URLSearchParams({ layer, limit: String(limit) });
  if (chain) params.set("chain", chain);
  const res = await fetch(`${API_BASE}/api/v1/tokens?${params}`, {
    next: { revalidate: 0 },
  });
  if (!res.ok) return [];
  return res.json();
}

export async function fetchAlerts(limit = 50): Promise<AlertRecord[]> {
  const res = await fetch(`${API_BASE}/api/v1/alerts?limit=${limit}`, {
    next: { revalidate: 0 },
  });
  if (!res.ok) return [];
  return res.json();
}

export async function fetchLPOpportunities(
  chain?: string,
  limit = 50
): Promise<LPOpportunity[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (chain) params.set("chain", chain);
  const res = await fetch(`${API_BASE}/api/v1/lp-opportunities?${params}`, {
    next: { revalidate: 0 },
  });
  if (!res.ok) return [];
  return res.json();
}

export async function fetchLPRangeRecommendation(
  poolAddress: string,
  chain: string
): Promise<RangeRecommendation> {
  const res = await fetch(`${API_BASE}/api/v1/lp-range/recommend`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pool_address: poolAddress, chain }),
    next: { revalidate: 0 },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  return res.json();
}

export async function fetchHealth(): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/health`);
  return res.json();
}

// ── Formatters ─────────────────────────────────────────────────────────────────

export function formatVolume(v: number): string {
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toFixed(0)}`;
}

export function formatPrice(p: number): string {
  if (p === 0) return "$0";
  if (p < 0.000001) return `$${p.toExponential(2)}`;
  if (p < 0.01) return `$${p.toFixed(6)}`;
  if (p < 1) return `$${p.toFixed(4)}`;
  return `$${p.toFixed(2)}`;
}

export function formatPct(v: number, decimals = 0): string {
  return `${(v * 100).toFixed(decimals)}%`;
}

export function chainName(chainIndex: string): string {
  const map: Record<string, string> = {
    "1": "ETH", "501": "SOL", "8453": "BASE", "56": "BSC",
    "137": "MATIC", "42161": "ARB", "10": "OP", "43114": "AVAX",
    "324": "ZKSYNC", "59144": "LINEA", "130": "UNICHAIN",
  };
  return map[chainIndex] ?? chainIndex;
}

export function explorerUrl(chainIndex: string, address: string): string {
  const map: Record<string, string> = {
    "1":      `https://etherscan.io/token/${address}`,
    "501":    `https://solscan.io/token/${address}`,
    "8453":   `https://basescan.org/token/${address}`,
    "56":     `https://bscscan.com/token/${address}`,
    "137":    `https://polygonscan.com/token/${address}`,
    "42161":  `https://arbiscan.io/token/${address}`,
    "10":     `https://optimistic.etherscan.io/token/${address}`,
    "43114":  `https://snowtrace.io/token/${address}`,
    "324":    `https://explorer.zksync.io/address/${address}`,
    "59144":  `https://lineascan.build/token/${address}`,
    "130":    `https://unichain.blockscout.com/token/${address}`,
  };
  return map[chainIndex] ?? `https://dexscreener.com/search?q=${address}`;
}

export function alertColor(type: AlertType): string {
  switch (type) {
    case "VOLUME_SPIKE":   return "#f97316"; // orange
    case "BREAKOUT":       return "#22c55e"; // green
    case "SAFETY_RISK":    return "#ef4444"; // red
    case "NEW_HOT":        return "#eab308"; // yellow
    case "LP_OPPORTUNITY": return "#06b6d4"; // cyan
    case "LP_RISK_WARN":   return "#a78bfa"; // purple
    default:               return "#6b7280";
  }
}

export function strategyColor(s: StrategyType): string {
  switch (s) {
    case "event":      return "#f97316"; // orange — short hot
    case "tactical":   return "#06b6d4"; // cyan — medium
    case "structural": return "#22c55e"; // green — stable
    default:           return "#6b7280";
  }
}

export function strategyLabel(s: StrategyType): string {
  switch (s) {
    case "event":      return "Event";
    case "tactical":   return "Tactical";
    case "structural": return "Structural";
    default:           return "—";
  }
}

export function ilRiskColor(level: ILRiskLevel): string {
  switch (level) {
    case "low":    return "#22c55e";
    case "medium": return "#eab308";
    case "high":   return "#ef4444";
    default:       return "#6b7280";
  }
}

export function washRiskColor(level: WashRisk): string {
  switch (level) {
    case "low":    return "#22c55e";
    case "medium": return "#eab308";
    case "high":   return "#ef4444";
    default:       return "#6b7280";
  }
}

/** Score bar color: 0=red → 0.5=yellow → 1=green */
export function scoreColor(score: number): string {
  if (score >= 0.65) return "#22c55e";
  if (score >= 0.45) return "#eab308";
  return "#ef4444";
}
