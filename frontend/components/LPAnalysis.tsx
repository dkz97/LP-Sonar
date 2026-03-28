"use client";

import { useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  CheckCircle,
  ChevronDown,
  ChevronUp,
  Loader2,
  Search,
  TrendingDown,
  TrendingUp,
  Zap,
} from "lucide-react";
import {
  fetchLPRangeRecommendation,
  formatPrice,
  RangeProfile,
  RangeRecommendation,
  Regime,
  ALL_SCENARIO_LABELS,
  HistoryTier,
  Actionability,
} from "@/lib/api";

// Canonical rendering order: mature scenarios first, launch scenarios second.
// ScenarioTable filters this list to only show keys present in scenario_pnl.
const ALL_SCENARIO_ORDER: string[] = [
  "sideways", "slow_up", "slow_down", "breakout_up", "breakdown_down",
  "discovery_sideways", "grind_up", "fade_down", "spike_then_mean_revert", "pump_and_dump",
];

// ── Constants ────────────────────────────────────────────────────────────────

const CHAINS = [
  { label: "SOL",  value: "501" },
  { label: "BASE", value: "8453" },
  { label: "BSC",  value: "56" },
  { label: "ETH",  value: "1" },
];

// ── Sub-components ────────────────────────────────────────────────────────────

// ── Phase 1.5 badge/banner components ────────────────────────────────────────

const TIER_CONFIG: Record<HistoryTier, { label: string; color: string; detail: string }> = {
  mature:  { label: "历史充足",  color: "#6b7280", detail: "" },
  growing: { label: "成长期池",  color: "#06b6d4", detail: "4–24h" },
  fresh:   { label: "新生池",    color: "#eab308", detail: "<4h" },
  infant:  { label: "极新池",    color: "#ef4444", detail: "<1h" },
};

function HistoryTierBadge({ tier }: { tier: HistoryTier }) {
  if (tier === "mature") return null;
  const { label, color, detail } = TIER_CONFIG[tier] ?? TIER_CONFIG.growing;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: "4px",
      fontSize: "11px", fontWeight: 600, fontFamily: "var(--font-mono)",
      color, background: `${color}18`,
      border: `1px solid ${color}40`,
      borderRadius: "5px", padding: "2px 8px",
    }}>
      <Activity size={10} />{label}{detail ? ` (${detail})` : ""}
    </span>
  );
}

function EvidenceScore({ score }: { score: number }) {
  const color = score >= 0.75 ? "#22c55e" : score >= 0.50 ? "#eab308" : "#ef4444";
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}>
      <span style={{ fontSize: "11px", color: "var(--text-muted)" }}>证据得分</span>
      <span style={{ fontSize: "11px", fontWeight: 700, fontFamily: "var(--font-mono)", color }}>
        {(score * 100).toFixed(0)}%
      </span>
    </span>
  );
}

/**
 * Banner shown when actionability is "caution" or "watch_only".
 * Lists applied young-pool adjustments and calibration notes.
 */
function ActionabilityBanner({
  actionability,
  tier,
  evidence,
  adjustments,
}: {
  actionability: Actionability;
  tier: HistoryTier;
  evidence: number;
  adjustments: string[];
}) {
  if (actionability === "standard") return null;

  const isWatch = actionability === "watch_only";
  const color   = isWatch ? "#ef4444" : "#eab308";
  const bg      = isWatch ? "rgba(239,68,68,0.06)"  : "rgba(234,179,8,0.06)";
  const border  = isWatch ? "rgba(239,68,68,0.3)"   : "rgba(234,179,8,0.3)";

  const tierName = TIER_CONFIG[tier]?.label ?? tier;
  const headline = isWatch
    ? "观望建议 — 数据严重不足"
    : "谨慎操作 — 数据有限";
  const body = isWatch
    ? `此池处于${tierName}（证据得分 ${(evidence * 100).toFixed(0)}%），历史数据极少，区间推荐主要依赖场景模拟，不确定性极高，建议先观望。`
    : `此池处于${tierName}（证据得分 ${(evidence * 100).toFixed(0)}%），历史数据较少，推荐结果已根据证据强度自动降级，数值仅供参考，请结合市场判断。`;

  return (
    <div style={{
      background: bg, border: `1px solid ${border}`,
      borderRadius: "10px", padding: "12px 16px",
      display: "flex", alignItems: "flex-start", gap: "10px",
    }}>
      <AlertTriangle size={15} style={{ color, flexShrink: 0, marginTop: "2px" }} />
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: "12px", fontWeight: 600, color, marginBottom: "4px" }}>
          {headline}
        </div>
        <div style={{ fontSize: "11px", color: "var(--text-secondary)", lineHeight: "1.55" }}>
          {body}
        </div>
        {adjustments.length > 0 && (
          <div style={{ marginTop: "8px", display: "flex", flexDirection: "column", gap: "3px" }}>
            <div style={{ fontSize: "10px", color: "var(--text-muted)", fontWeight: 600,
              textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: "2px" }}>
              已应用的修正
            </div>
            {adjustments.map((adj, i) => (
              <div key={i} style={{
                fontSize: "11px", color: "var(--text-muted)",
                display: "flex", alignItems: "flex-start", gap: "5px",
              }}>
                <span style={{ color, flexShrink: 0, marginTop: "1px", fontSize: "9px" }}>▸</span>
                {adj}
              </div>
            ))}
          </div>
        )}
        <div style={{
          marginTop: "10px", padding: "8px 10px",
          background: `${color}10`, borderRadius: "6px",
          fontSize: "11px", color: "var(--text-secondary)", lineHeight: "1.5",
        }}>
          <strong style={{ color }}>注意：</strong>
          目前可信的是<strong>区间相对排序</strong>和<strong>降级逻辑</strong>本身，
          不应过度解读绝对 fee APR 和 utility 数值。
        </div>
      </div>
    </div>
  );
}

function RegimeBadge({ regime }: { regime: Regime }) {
  const config: Record<Regime, { label: string; color: string; icon: React.ReactNode }> = {
    range_bound: { label: "震荡",    color: "#06b6d4", icon: <Activity size={11} /> },
    trend_up:    { label: "上升趋势", color: "#22c55e", icon: <TrendingUp size={11} /> },
    trend_down:  { label: "下降趋势", color: "#ef4444", icon: <TrendingDown size={11} /> },
    chaotic:     { label: "混沌",    color: "#f97316", icon: <Zap size={11} /> },
  };
  const { label, color, icon } = config[regime] ?? config.chaotic;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: "4px",
      fontSize: "11px", fontWeight: 600, fontFamily: "var(--font-mono)",
      color, background: `${color}18`,
      border: `1px solid ${color}40`,
      borderRadius: "5px", padding: "2px 8px",
    }}>
      {icon}{label}
    </span>
  );
}

function ConfidenceBar({ value }: { value: number }) {
  const color = value >= 0.6 ? "#22c55e" : value >= 0.35 ? "#eab308" : "#ef4444";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
      <div style={{
        width: "80px", height: "4px",
        background: "var(--bg-elevated)", borderRadius: "2px", overflow: "hidden",
      }}>
        <div style={{ width: `${value * 100}%`, height: "100%", background: color, borderRadius: "2px" }} />
      </div>
      <span style={{ fontSize: "11px", color, fontFamily: "var(--font-mono)", fontWeight: 600 }}>
        {(value * 100).toFixed(0)}%
      </span>
    </div>
  );
}

function MetricRow({ label, value, highlight }: { label: string; value: string; highlight?: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "5px 0" }}>
      <span style={{ fontSize: "11px", color: "var(--text-secondary)" }}>{label}</span>
      <span style={{
        fontSize: "12px", fontWeight: 600, fontFamily: "var(--font-mono)",
        color: highlight ?? "var(--text-primary)",
      }}>{value}</span>
    </div>
  );
}

function ScenarioTable({ scenarioPnl }: { scenarioPnl: Partial<Record<string, number>> }) {
  // Render whichever scenario keys are present, in canonical display order.
  const presentScenarios = ALL_SCENARIO_ORDER.filter(s => s in scenarioPnl);
  if (presentScenarios.length === 0) return null;

  // Detect if these are launch-mode scenarios (first key is a launch key)
  const isLaunch = presentScenarios[0] === "discovery_sideways" || presentScenarios[0] === "grind_up";

  return (
    <div style={{ marginTop: "12px" }}>
      <div style={{
        fontSize: "10px", color: "var(--text-muted)", fontWeight: 600,
        textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: "6px",
        display: "flex", alignItems: "center", gap: "6px",
      }}>
        场景 PnL 模拟
        {isLaunch && (
          <span style={{
            fontSize: "9px", color: "#eab308",
            background: "rgba(234,179,8,0.12)", border: "1px solid rgba(234,179,8,0.3)",
            borderRadius: "4px", padding: "1px 5px", fontWeight: 600,
          }}>
            新池模式
          </span>
        )}
      </div>
      <div style={{
        display: "grid", gridTemplateColumns: "1fr auto",
        background: "var(--bg-elevated)",
        borderRadius: "6px", overflow: "hidden",
        border: "1px solid var(--border)",
      }}>
        {presentScenarios.map((s, i) => {
          const val = scenarioPnl[s];
          if (val === undefined) return null;
          const color = val >= 0.02 ? "#22c55e" : val >= 0 ? "#eab308" : "#ef4444";
          const isLast = i === presentScenarios.length - 1;
          return (
            <div key={s} style={{ display: "contents" }}>
              <div style={{
                padding: "5px 10px",
                fontSize: "11px", color: "var(--text-secondary)",
                borderBottom: isLast ? "none" : "1px solid var(--border)",
              }}>
                {ALL_SCENARIO_LABELS[s] ?? s}
              </div>
              <div style={{
                padding: "5px 10px",
                fontSize: "11px", fontWeight: 700, fontFamily: "var(--font-mono)",
                color, textAlign: "right",
                borderBottom: isLast ? "none" : "1px solid var(--border)",
              }}>
                {val >= 0 ? "+" : ""}{(val * 100).toFixed(2)}%
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function AlternativeRangeRow({ profile, idx }: { profile: RangeProfile; idx: number }) {
  const [expanded, setExpanded] = useState(false);
  const pnlColor = profile.expected_net_pnl >= 0 ? "#22c55e" : "#ef4444";

  return (
    <div style={{
      border: "1px solid var(--border)", borderRadius: "8px", overflow: "hidden",
      marginBottom: "6px",
    }}>
      <button
        onClick={() => setExpanded(v => !v)}
        style={{
          width: "100%", display: "flex", alignItems: "center", gap: "10px",
          padding: "8px 12px", background: "none", border: "none", cursor: "pointer", textAlign: "left",
        }}
      >
        <span style={{
          fontSize: "10px", fontWeight: 600, fontFamily: "var(--font-mono)",
          color: "var(--text-muted)", background: "var(--bg-elevated)",
          border: "1px solid var(--border)", borderRadius: "4px", padding: "1px 6px", flexShrink: 0,
        }}>
          #{idx + 1} {profile.range_type ?? ""}
        </span>
        <span style={{ flex: 1, fontFamily: "var(--font-mono)", fontSize: "11px", color: "var(--text-primary)" }}>
          {formatPrice(profile.lower_price)} → {formatPrice(profile.upper_price)}
        </span>
        <span style={{ fontSize: "11px", color: "var(--text-muted)", fontFamily: "var(--font-mono)", flexShrink: 0 }}>
          ±{(profile.width_pct / 2).toFixed(1)}%
        </span>
        <span style={{ fontSize: "11px", fontWeight: 700, fontFamily: "var(--font-mono)", color: pnlColor, flexShrink: 0 }}>
          {profile.expected_net_pnl >= 0 ? "+" : ""}{(profile.expected_net_pnl * 100).toFixed(2)}%
        </span>
        {expanded
          ? <ChevronUp size={12} style={{ color: "var(--text-muted)", flexShrink: 0 }} />
          : <ChevronDown size={12} style={{ color: "var(--text-muted)", flexShrink: 0 }} />}
      </button>
      {expanded && (
        <div style={{ borderTop: "1px solid var(--border)", padding: "8px 12px" }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 16px" }}>
            <MetricRow label="预期手续费 APR" value={`${(profile.expected_fee_apr * 100).toFixed(1)}%`} />
            <MetricRow label="IL 成本" value={`${(profile.expected_il_cost * 100).toFixed(2)}%`} />
            <MetricRow label="Breach 概率" value={`${(profile.breach_probability * 100).toFixed(1)}%`} />
            <MetricRow label="Utility Score" value={(profile.utility_score * 100).toFixed(0)} />
          </div>
          <ScenarioTable scenarioPnl={profile.scenario_pnl} />
        </div>
      )}
    </div>
  );
}

interface ProfileCardProps {
  name: "conservative" | "balanced" | "aggressive";
  profile: RangeProfile;
  isDefault: boolean;
}

function ProfileCard({ name, profile, isDefault }: ProfileCardProps) {
  const [expanded, setExpanded] = useState(isDefault);

  const config = {
    conservative: { label: "保守",   accent: "#22c55e", desc: "最宽区间 · 最低 Breach 风险" },
    balanced:     { label: "均衡",   accent: "#06b6d4", desc: "最优综合评分 · 推荐默认" },
    aggressive:   { label: "激进",   accent: "#f97316", desc: "最窄区间 · 最高资本效率" },
  } as const;

  const { label, accent, desc } = config[name];
  const feeColor = profile.expected_fee_apr >= 0.5 ? "#22c55e" : profile.expected_fee_apr >= 0.2 ? "#eab308" : "#6b7280";
  const breachColor = profile.breach_probability <= 0.2 ? "#22c55e" : profile.breach_probability <= 0.5 ? "#eab308" : "#ef4444";
  const ilColor = profile.expected_il_cost <= 0.05 ? "#22c55e" : profile.expected_il_cost <= 0.15 ? "#eab308" : "#ef4444";

  return (
    <div style={{
      background: isDefault ? `${accent}08` : "var(--bg-card)",
      border: `1px solid ${isDefault ? accent + "40" : "var(--border)"}`,
      borderRadius: "10px",
      overflow: "hidden",
      transition: "box-shadow 150ms",
    }}>
      {/* Header */}
      <button
        onClick={() => setExpanded(v => !v)}
        style={{
          width: "100%", display: "flex", alignItems: "center",
          padding: "10px 14px", gap: "10px",
          background: "none", border: "none", cursor: "pointer",
          textAlign: "left",
        }}
      >
        {/* Badge */}
        <span style={{
          fontSize: "10px", fontWeight: 700, fontFamily: "var(--font-mono)",
          color: accent, background: `${accent}20`,
          border: `1px solid ${accent}50`, borderRadius: "4px",
          padding: "1px 6px", flexShrink: 0,
        }}>
          {label}
          {isDefault && (
            <span style={{ marginLeft: "4px", opacity: 0.7 }}>· 默认</span>
          )}
        </span>

        {/* Price range */}
        <span style={{ flex: 1, fontFamily: "var(--font-mono)", fontSize: "12px", color: "var(--text-primary)", fontWeight: 600 }}>
          {formatPrice(profile.lower_price)} <ArrowRight size={10} style={{ display: "inline", verticalAlign: "middle", color: "var(--text-muted)" }} /> {formatPrice(profile.upper_price)}
        </span>

        {/* Width */}
        <span style={{ fontSize: "11px", color: "var(--text-secondary)", fontFamily: "var(--font-mono)", flexShrink: 0 }}>
          ±{(profile.width_pct / 2).toFixed(1)}%
        </span>

        {/* Primary score: use final_utility (P2 blended) when available, fall back to utility_score */}
        <span style={{
          fontSize: "11px", fontWeight: 700, fontFamily: "var(--font-mono)",
          color: accent, flexShrink: 0,
        }}>
          {((profile.final_utility ?? profile.utility_score) * 100).toFixed(0)}分
        </span>

        {expanded ? <ChevronUp size={13} style={{ color: "var(--text-muted)", flexShrink: 0 }} /> : <ChevronDown size={13} style={{ color: "var(--text-muted)", flexShrink: 0 }} />}
      </button>

      {/* Desc line */}
      <div style={{ padding: "0 14px 6px", fontSize: "11px", color: "var(--text-muted)" }}>{desc}</div>

      {/* Expanded metrics */}
      {expanded && (
        <div style={{ borderTop: "1px solid var(--border)", padding: "10px 14px 12px" }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 20px" }}>
            <MetricRow label="Tick 下限" value={String(profile.lower_tick)} />
            <MetricRow label="Tick 上限" value={String(profile.upper_tick)} />
            <MetricRow label="区间宽度" value={`${profile.width_pct.toFixed(1)}%`} />
            {/* Fee APR: show shrunk version for young pools, with annotation */}
            <MetricRow
              label={profile.shrunk_fee_apr != null ? "手续费 APR (调整后)" : "预期手续费 APR"}
              value={`${((profile.shrunk_fee_apr ?? profile.expected_fee_apr) * 100).toFixed(1)}%`}
              highlight={feeColor}
            />
            <MetricRow label="预估 IL 成本" value={`${(profile.expected_il_cost * 100).toFixed(2)}%`} highlight={ilColor} />
            <MetricRow label="Breach 概率" value={`${(profile.breach_probability * 100).toFixed(1)}%`} highlight={breachColor} />
            <MetricRow label="预计再平衡" value={`${profile.expected_rebalance_frequency.toFixed(1)} 次/周`} />
            <MetricRow
              label="预估净 PnL"
              value={`${profile.expected_net_pnl >= 0 ? "+" : ""}${(profile.expected_net_pnl * 100).toFixed(2)}%`}
              highlight={profile.expected_net_pnl >= 0 ? "#22c55e" : "#ef4444"}
            />
            {/* Phase 1.5: blended scoring breakdown (shown when available) */}
            {profile.final_utility != null && (
              <MetricRow
                label="综合评分"
                value={`${(profile.final_utility * 100).toFixed(0)}分`}
                highlight="#06b6d4"
              />
            )}
            {profile.scenario_utility != null && (
              <MetricRow
                label="场景模拟得分"
                value={`${(profile.scenario_utility * 100).toFixed(0)}分`}
                highlight="#a78bfa"
              />
            )}
            {profile.execution_cost_fraction != null && profile.execution_cost_fraction > 0 && (
              <MetricRow
                label="执行成本"
                value={`${(profile.execution_cost_fraction * 100).toFixed(3)}%`}
                highlight="#f97316"
              />
            )}
            {profile.fee_haircut_factor != null && profile.fee_haircut_factor < 0.99 && (
              <MetricRow
                label="手续费竞争折扣"
                value={`${(profile.fee_haircut_factor * 100).toFixed(0)}%`}
                highlight={profile.fee_haircut_factor < 0.75 ? "#f97316" : "#94a3b8"}
              />
            )}
          </div>

          {/* Fee shrinkage note for young pools */}
          {profile.shrunk_fee_apr != null && (
            <div style={{
              marginTop: "8px", padding: "7px 10px",
              background: "rgba(234,179,8,0.06)", borderRadius: "6px",
              border: "1px solid rgba(234,179,8,0.2)",
              fontSize: "11px", color: "var(--text-secondary)", lineHeight: "1.5",
            }}>
              <span style={{ color: "#eab308", fontWeight: 600 }}>⚠ </span>
              此 APR 为经数据证据强度修正后的估算值，原始基础估计值更高。
              此池历史数据有限，绝对 APR 不应过度解读，仅用于区间相对排序参考。
            </div>
          )}

          {profile.reasons.length > 0 && (
            <div style={{ marginTop: "10px" }}>
              <div style={{ fontSize: "10px", color: "var(--text-muted)", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: "5px" }}>推荐理由</div>
              {profile.reasons.map((r, i) => (
                <div key={i} style={{ display: "flex", alignItems: "flex-start", gap: "6px", marginBottom: "3px" }}>
                  <CheckCircle size={10} style={{ color: "#22c55e", flexShrink: 0, marginTop: "2px" }} />
                  <span style={{ fontSize: "11px", color: "var(--text-secondary)" }}>{r}</span>
                </div>
              ))}
            </div>
          )}

          {profile.risk_flags.length > 0 && (
            <div style={{ marginTop: "8px" }}>
              <div style={{ fontSize: "10px", color: "var(--text-muted)", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: "5px" }}>风险提示</div>
              {profile.risk_flags.map((r, i) => (
                <div key={i} style={{ display: "flex", alignItems: "flex-start", gap: "6px", marginBottom: "3px" }}>
                  <AlertTriangle size={10} style={{ color: "#f97316", flexShrink: 0, marginTop: "2px" }} />
                  <span style={{ fontSize: "11px", color: "var(--text-secondary)" }}>{r}</span>
                </div>
              ))}
            </div>
          )}

          {/* Young pool adjustment notes */}
          {profile.young_pool_adjustments && profile.young_pool_adjustments.length > 0 && (
            <div style={{ marginTop: "8px" }}>
              <div style={{ fontSize: "10px", color: "var(--text-muted)", fontWeight: 600,
                textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: "5px" }}>
                新池修正记录
              </div>
              {profile.young_pool_adjustments.map((adj, i) => (
                <div key={i} style={{ display: "flex", alignItems: "flex-start", gap: "6px", marginBottom: "3px" }}>
                  <span style={{ color: "#eab308", flexShrink: 0, fontSize: "10px", marginTop: "1px" }}>◦</span>
                  <span style={{ fontSize: "11px", color: "var(--text-muted)" }}>{adj}</span>
                </div>
              ))}
            </div>
          )}

          <ScenarioTable scenarioPnl={profile.scenario_pnl} />
        </div>
      )}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

interface LPAnalysisProps {
  initialChain?: string;
  initialPool?: string;
}

export function LPAnalysis({ initialChain = "8453", initialPool = "" }: LPAnalysisProps) {
  const [chain, setChain] = useState(initialChain);
  const [poolInput, setPoolInput] = useState(initialPool);
  const [positionInput, setPositionInput] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<RangeRecommendation | null>(null);
  const [queriedPool, setQueriedPool] = useState<string>("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const addr = poolInput.trim();
    if (!addr) return;

    const parsedPos = positionInput.trim() ? parseFloat(positionInput) : undefined;
    const positionUsd = parsedPos != null && parsedPos > 0 ? parsedPos : undefined;

    setLoading(true);
    setError(null);
    setResult(null);
    setQueriedPool(addr);

    try {
      const data = await fetchLPRangeRecommendation(addr, chain, positionUsd);
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "请求失败，请检查网络或合约地址");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={{ maxWidth: "820px", margin: "0 auto", display: "flex", flexDirection: "column", gap: "20px" }}>

      {/* ── Search form ── */}
      <div style={{
        background: "var(--bg-card)",
        border: "1px solid var(--border)",
        borderRadius: "12px",
        padding: "20px",
      }}>
        <div style={{ marginBottom: "14px" }}>
          <div style={{
            fontFamily: "var(--font-display)", fontWeight: 700, fontSize: "16px",
            color: "var(--text-primary)", marginBottom: "4px",
          }}>
            LP 区间分析
          </div>
          <div style={{ fontSize: "12px", color: "var(--text-secondary)" }}>
            输入 LP 池合约地址，获取保守 / 均衡 / 激进三档价格区间推荐，含预期手续费、IL 风险和 Breach 概率分析
          </div>
        </div>

        <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          {/* Row 1: chain + pool address + submit */}
          <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
            {/* Chain selector */}
            <select
              value={chain}
              onChange={e => setChain(e.target.value)}
              style={{
                background: "var(--bg-elevated)", border: "1px solid var(--border)",
                borderRadius: "6px", color: "var(--text-primary)",
                fontSize: "11px", fontFamily: "var(--font-mono)", fontWeight: 600,
                padding: "0 8px", height: "36px", cursor: "pointer", outline: "none", flexShrink: 0,
              }}
            >
              {CHAINS.map(c => (
                <option key={c.value} value={c.value}>{c.label}</option>
              ))}
            </select>

            {/* Pool address input */}
            <input
              type="text"
              value={poolInput}
              onChange={e => setPoolInput(e.target.value)}
              placeholder="粘贴 LP 池合约地址…"
              style={{
                flex: 1, background: "var(--bg-elevated)",
                border: "1px solid var(--border)", borderRadius: "6px",
                color: "var(--text-primary)", fontSize: "12px",
                fontFamily: "var(--font-mono)", padding: "0 12px",
                height: "36px", outline: "none", transition: "border-color 150ms",
              }}
              onFocus={e => (e.currentTarget.style.borderColor = "var(--accent-cyan)")}
              onBlur={e => (e.currentTarget.style.borderColor = "var(--border)")}
            />

            {/* Submit button */}
            <button
              type="submit"
              disabled={!poolInput.trim() || loading}
              style={{
                display: "flex", alignItems: "center", gap: "6px",
                height: "36px", padding: "0 16px", flexShrink: 0,
                background: poolInput.trim() && !loading ? "var(--accent-cyan)" : "var(--bg-elevated)",
                color: poolInput.trim() && !loading ? "#fff" : "var(--text-muted)",
                border: "1px solid " + (poolInput.trim() && !loading ? "var(--accent-cyan)" : "var(--border)"),
                borderRadius: "6px", fontSize: "12px", fontWeight: 600,
                fontFamily: "var(--font-sans)", cursor: poolInput.trim() && !loading ? "pointer" : "default",
                transition: "background 150ms, color 150ms",
              }}
            >
              {loading ? <Loader2 size={13} style={{ animation: "spin 1s linear infinite" }} /> : <Search size={13} />}
              {loading ? "分析中…" : "分析"}
            </button>
          </div>

          {/* Row 2: optional position size */}
          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <span style={{ fontSize: "11px", color: "var(--text-muted)", flexShrink: 0 }}>仓位规模</span>
            <span style={{ fontSize: "11px", color: "var(--text-muted)", flexShrink: 0 }}>$</span>
            <input
              type="number"
              min="1"
              value={positionInput}
              onChange={e => setPositionInput(e.target.value)}
              placeholder="留空使用默认仓位"
              style={{
                width: "140px", background: "var(--bg-elevated)",
                border: "1px solid var(--border)", borderRadius: "6px",
                color: "var(--text-primary)", fontSize: "12px",
                fontFamily: "var(--font-mono)", padding: "0 10px",
                height: "30px", outline: "none", transition: "border-color 150ms",
                flexShrink: 0,
              }}
              onFocus={e => (e.currentTarget.style.borderColor = "var(--accent-cyan)")}
              onBlur={e => (e.currentTarget.style.borderColor = "var(--border)")}
            />
            <span style={{ fontSize: "11px", color: "var(--text-muted)" }}>
              USD — 未填时使用代表性默认值 (min $10k, TVL×1%)
            </span>
          </div>
        </form>
      </div>

      {/* ── Loading state ── */}
      {loading && (
        <div style={{
          display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
          padding: "48px 0", gap: "12px", color: "var(--text-muted)",
        }}>
          <Loader2 size={28} style={{ animation: "spin 1s linear infinite", color: "var(--accent-cyan)" }} />
          <span style={{ fontSize: "13px" }}>正在获取行情数据并计算最优区间…</span>
          <span style={{ fontSize: "11px", opacity: 0.7 }}>通常需要 5–15 秒</span>
        </div>
      )}

      {/* ── Error state ── */}
      {!loading && error && (
        <div style={{
          background: "rgba(239,68,68,0.06)", border: "1px solid rgba(239,68,68,0.3)",
          borderRadius: "10px", padding: "16px 18px",
          display: "flex", alignItems: "flex-start", gap: "10px",
        }}>
          <AlertTriangle size={16} style={{ color: "#ef4444", flexShrink: 0, marginTop: "1px" }} />
          <div>
            <div style={{ fontSize: "13px", fontWeight: 600, color: "#ef4444", marginBottom: "4px" }}>请求失败</div>
            <div style={{ fontSize: "12px", color: "var(--text-secondary)" }}>{error}</div>
          </div>
        </div>
      )}

      {/* ── Result: not recommended ── */}
      {!loading && result && !result.is_recommended && (() => {
        const tier      = result.history_tier ?? "mature";
        const isWatch   = result.actionability === "watch_only";
        const isInfant  = tier === "infant";
        const color     = isWatch || isInfant ? "#ef4444" : "#eab308";
        const bg        = isWatch || isInfant ? "rgba(239,68,68,0.06)"  : "rgba(234,179,8,0.06)";
        const border    = isWatch || isInfant ? "rgba(239,68,68,0.3)"   : "rgba(234,179,8,0.3)";
        const headline  = isWatch || isInfant
          ? "此池数据不足，建议观望"
          : "暂不推荐在此池做 LP";
        return (
          <div style={{
            background: bg, border: `1px solid ${border}`,
            borderRadius: "10px", padding: "16px 18px",
            display: "flex", alignItems: "flex-start", gap: "10px",
          }}>
            <AlertTriangle size={16} style={{ color, flexShrink: 0, marginTop: "1px" }} />
            <div style={{ flex: 1 }}>
              <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "6px", flexWrap: "wrap" }}>
                <span style={{ fontSize: "13px", fontWeight: 600, color }}>{headline}</span>
                {tier !== "mature" && <HistoryTierBadge tier={tier} />}
              </div>
              <div style={{ fontSize: "12px", color: "var(--text-secondary)" }}>
                {result.no_recommendation_reason ?? "该池当前不满足 LP 推荐条件"}
              </div>
              {(result.effective_evidence_score ?? 1) < 0.75 && (
                <div style={{ marginTop: "8px", fontSize: "11px", color: "var(--text-muted)" }}>
                  证据得分：{((result.effective_evidence_score ?? 0) * 100).toFixed(0)}%
                  {" · "}推荐模式：{result.recommendation_mode ?? "observe_only"}
                </div>
              )}
            </div>
          </div>
        );
      })()}

      {/* ── Result: recommended ── */}
      {!loading && result && result.is_recommended && (() => {
        const tier         = result.history_tier ?? "mature";
        const actionability = result.actionability ?? "standard";
        const evidence     = result.effective_evidence_score ?? 1.0;
        // Collect young_pool_adjustments from any available profile
        const anyProfile   = result.profiles.balanced ?? result.profiles.conservative ?? result.profiles.aggressive;
        const adjustments  = anyProfile?.young_pool_adjustments ?? [];

        return (
        <>
          {/* Summary header */}
          <div style={{
            background: "rgba(6,182,212,0.04)",
            border: "1px solid rgba(6,182,212,0.2)",
            borderRadius: "10px", padding: "14px 18px",
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap", marginBottom: "8px" }}>
              <RegimeBadge regime={result.regime} />
              {tier !== "mature" && <HistoryTierBadge tier={tier} />}
              <span style={{ fontSize: "12px", color: "var(--text-secondary)" }}>
                建议持仓：<strong style={{ color: "var(--text-primary)" }}>{result.holding_horizon}</strong>
              </span>
              <span style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: "10px" }}>
                {tier !== "mature" && <EvidenceScore score={evidence} />}
                <span style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                  <span style={{ fontSize: "11px", color: "var(--text-muted)" }}>推荐置信度</span>
                  <ConfidenceBar value={result.recommendation_confidence} />
                </span>
              </span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "16px", flexWrap: "wrap" }}>
              <span style={{ fontSize: "11px", color: "var(--text-secondary)", fontFamily: "var(--font-mono)" }}>
                {result.pool_quality_summary}
              </span>
              {result.effective_position_usd != null && (
                <span style={{
                  fontSize: "10px", color: "var(--text-muted)",
                  fontFamily: "var(--font-mono)", flexShrink: 0,
                }}>
                  计算仓位: <strong style={{ color: "var(--text-secondary)" }}>
                    {formatVolume(result.effective_position_usd)}
                  </strong>
                </span>
              )}
            </div>
          </div>

          {/* Actionability banner (caution / watch_only only) */}
          <ActionabilityBanner
            actionability={actionability}
            tier={tier}
            evidence={evidence}
            adjustments={adjustments}
          />

          {/* Evidence strength card: shown for non-mature pools or when blending is active */}
          {(tier !== "mature" || (result.replay_weight != null && result.replay_weight < 1.0)) && (
            <div style={{
              display: "flex", gap: "20px", flexWrap: "wrap",
              padding: "9px 14px", borderRadius: "8px",
              background: "rgba(139,92,246,0.04)",
              border: "1px solid rgba(139,92,246,0.18)",
              fontSize: "11px",
            }}>
              <span style={{ color: "var(--text-muted)" }}>
                证据强度&nbsp;
                <strong style={{ color: "#a78bfa" }}>
                  {((result.effective_evidence_score ?? 0) * 100).toFixed(0)}%
                </strong>
              </span>
              {result.replay_weight != null && (
                <span style={{ color: "var(--text-muted)" }}>
                  历史回放权重&nbsp;
                  <strong style={{ color: "#06b6d4" }}>
                    {(result.replay_weight * 100).toFixed(0)}%
                  </strong>
                </span>
              )}
              {result.scenario_weight != null && result.scenario_weight > 0 && (
                <span style={{ color: "var(--text-muted)" }}>
                  场景模拟权重&nbsp;
                  <strong style={{ color: "#f59e0b" }}>
                    {(result.scenario_weight * 100).toFixed(0)}%
                  </strong>
                </span>
              )}
            </div>
          )}

          {/* Profile cards */}
          <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
            <div style={{ fontSize: "12px", fontWeight: 600, color: "var(--text-secondary)", paddingLeft: "2px" }}>
              价格区间推荐 — 均衡档默认展开
            </div>
            {(["balanced", "conservative", "aggressive"] as const).map(name => {
              const profile = result.profiles[name];
              if (!profile) return null;
              return (
                <ProfileCard
                  key={name}
                  name={name}
                  profile={profile}
                  isDefault={name === result.recommended_profile_default}
                />
              );
            })}
          </div>

          {/* Alternative ranges */}
          {result.alternative_ranges.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
              <div style={{ fontSize: "12px", fontWeight: 600, color: "var(--text-secondary)", paddingLeft: "2px" }}>
                其他候选区间
              </div>
              {result.alternative_ranges.map((p, i) => (
                <AlternativeRangeRow key={i} profile={p} idx={i} />
              ))}
            </div>
          )}

          {/* Footer note */}
          <div style={{
            fontSize: "11px", color: "var(--text-muted)", lineHeight: "1.6",
            paddingBottom: "8px",
          }}>
            ⚠ 以上区间基于历史价格 Replay 和规则评分生成，不构成投资建议。实际 PnL 受市场行情影响，请结合自身风险偏好决策。
            {tier !== "mature" && " 此池历史数据有限，绝对 APR 与 utility 数值不应过度解读，仅供区间相对排序参考。"}
            {" "}数据缓存 5 分钟，池合约：
            <span style={{ fontFamily: "var(--font-mono)", marginLeft: "4px" }}>
              {queriedPool.length > 16 ? `${queriedPool.slice(0, 8)}…${queriedPool.slice(-6)}` : queriedPool}
            </span>
          </div>
        </>
        );
      })()}

      <style>{`
        @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
      `}</style>
    </div>
  );
}
