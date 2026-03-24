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
