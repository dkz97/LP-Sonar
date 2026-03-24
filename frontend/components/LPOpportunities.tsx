"use client";

import React, { useEffect, useState } from "react";
import {
  LPOpportunity, fetchLPOpportunities, formatVolume, chainName,
  strategyLabel, strategyColor, ilRiskColor, washRiskColor, scoreColor,
} from "@/lib/api";

interface Props {
  selectedChain?: string;
  onViewToken?: (chainIndex: string, address: string) => void;
}

function shortAddr(a: string) {
  if (!a || a.length <= 12) return a;
  return a.slice(0, 6) + "…" + a.slice(-4);
}

function ScoreBar({ score }: { score: number }) {
  const color = scoreColor(score);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
      <div style={{ width: "52px", height: "5px", background: "var(--bg-elevated)", borderRadius: "3px", overflow: "hidden", flexShrink: 0 }}>
        <div style={{ width: `${score * 100}%`, height: "100%", background: color, borderRadius: "3px", transition: "width 300ms" }} />
      </div>
      <span style={{ fontFamily: "var(--font-mono)", fontSize: "12px", fontWeight: 700, color }}>{(score * 100).toFixed(0)}</span>
    </div>
  );
}

function StrategyBadge({ strategy, holding }: { strategy: string; holding: string }) {
  const color = strategyColor(strategy as "event" | "tactical" | "structural" | "");
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: "2px" }}>
      <span style={{
        fontSize: "10px", fontWeight: 700, fontFamily: "var(--font-mono)",
        color, background: `${color}18`, border: `1px solid ${color}40`,
        borderRadius: "4px", padding: "1px 6px",
      }}>
        {strategyLabel(strategy as "event" | "tactical" | "structural" | "").toUpperCase()}
      </span>
      {holding && (
        <span style={{ fontSize: "9px", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>⏱ {holding}</span>
      )}
    </div>
  );
}

function RiskCell({ il, wash }: { il: string; wash: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: "2px" }}>
      {il && (
        <span style={{ fontSize: "10px", fontFamily: "var(--font-mono)", color: ilRiskColor(il as "low" | "medium" | "high" | "") }}>
          IL:{il}
        </span>
      )}
      {wash && (
        <span style={{ fontSize: "9px", color: washRiskColor(wash as "low" | "medium" | "high" | "") }}>
          wash:{wash}
        </span>
      )}
    </div>
  );
}

function SubScores({ fee, mq }: { fee: number; mq: number }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: "1px" }}>
      <span style={{ fontSize: "9px", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
        fee <span style={{ color: scoreColor(fee) }}>{(fee * 100).toFixed(0)}</span>
      </span>
      <span style={{ fontSize: "9px", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
        mq <span style={{ color: scoreColor(mq) }}>{(mq * 100).toFixed(0)}</span>
      </span>
    </div>
  );
}

const TH = ({ children, right }: { children: React.ReactNode; right?: boolean }) => (
  <th style={{
    padding: "7px 10px", fontWeight: 500, fontSize: "10px",
    color: "rgba(6,182,212,0.65)", textAlign: right ? "right" : "left",
    whiteSpace: "nowrap", textTransform: "uppercase", letterSpacing: "0.05em",
    borderBottom: "1px solid rgba(6,182,212,0.15)",
    background: "rgba(6,182,212,0.05)",
  }}>
    {children}
  </th>
);

export function LPOpportunities({ selectedChain, onViewToken }: Props) {
  const [opps, setOpps] = useState<LPOpportunity[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      const data = await fetchLPOpportunities(selectedChain, 50);
      setOpps(data);
    };
    load();
    const id = setInterval(load, 60_000);
    return () => clearInterval(id);
  }, [selectedChain]);

  if (opps.length === 0) return null;

  return (
    <div style={{
      background: "rgba(6,182,212,0.03)",
      border: "1px solid rgba(6,182,212,0.25)",
      borderRadius: "10px",
      overflow: "hidden",
    }}>
      {/* Header */}
      <div style={{
        display: "flex", alignItems: "center", gap: "8px",
        padding: "8px 14px",
        background: "rgba(6,182,212,0.08)",
        borderBottom: "1px solid rgba(6,182,212,0.15)",
      }}>
        <span style={{ color: "#06b6d4", fontSize: "10px" }}>◆</span>
        <span style={{
          fontFamily: "var(--font-display)", fontWeight: 600, fontSize: "12px",
          color: "#06b6d4", textTransform: "uppercase", letterSpacing: "0.08em",
        }}>
          LP Opportunities ({opps.length})
        </span>
        <span style={{ color: "rgba(6,182,212,0.4)", fontSize: "11px", fontFamily: "var(--font-mono)", marginLeft: "auto" }}>
          60s refresh · sorted by net LP score
        </span>
      </div>

      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "12px" }}>
          <thead>
            <tr>
              <TH>Pair / Pool</TH>
              <TH right>TVL</TH>
              <TH right>LP Score</TH>
              <TH right>Fee / MQ</TH>
              <TH right>Strategy</TH>
              <TH right>Risk</TH>
              <TH right>Confidence</TH>
            </tr>
          </thead>
          <tbody>
            {opps.map((o) => {
              const rowKey = `${o.chain_index}:${o.pool_address}`;
              const isExpanded = expanded === rowKey;
              return (
                <React.Fragment key={rowKey}>
                  <tr
                    onClick={() => setExpanded(isExpanded ? null : rowKey)}
                    style={{
                      cursor: "pointer",
                      background: isExpanded ? "rgba(6,182,212,0.08)" : "transparent",
                      transition: "background 120ms",
                    }}
                    onMouseEnter={(e) => { if (!isExpanded) (e.currentTarget as HTMLElement).style.background = "rgba(6,182,212,0.06)"; }}
                    onMouseLeave={(e) => { if (!isExpanded) (e.currentTarget as HTMLElement).style.background = "transparent"; }}
                  >
                    {/* Token / Pool */}
                    <td style={{ padding: "8px 10px", borderBottom: isExpanded ? "none" : "1px solid rgba(6,182,212,0.1)", minWidth: "200px" }}>
                      <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                        {/* Token icon */}
                        {o.logo_url ? (
                          <img
                            src={o.logo_url}
                            alt={o.token_symbol}
                            width={28}
                            height={28}
                            style={{ borderRadius: "50%", flexShrink: 0, objectFit: "cover", border: "1px solid rgba(255,255,255,0.15)" }}
                            onError={(e) => {
                              const el = e.currentTarget as HTMLImageElement;
                              el.style.display = "none";
                              const sibling = el.nextSibling as HTMLElement | null;
                              if (sibling) sibling.style.display = "flex";
                            }}
                          />
                        ) : null}
                        <div style={{
                          width: "28px", height: "28px", borderRadius: "50%", flexShrink: 0,
                          background: `hsl(${(o.token_symbol || "?").charCodeAt(0) * 47 % 360}, 60%, 35%)`,
                          border: "1px solid rgba(255,255,255,0.15)",
                          display: o.logo_url ? "none" : "flex", alignItems: "center", justifyContent: "center",
                          fontWeight: 700, fontSize: "11px", color: "#fff", fontFamily: "var(--font-mono)",
                        }}>
                          {(o.token_symbol || "?").charAt(0).toUpperCase()}
                        </div>
                        <div>
                          <div style={{ display: "flex", alignItems: "center", gap: "5px" }}>
                            <span style={{ fontWeight: 700, color: "var(--text-primary)", fontSize: "13px", fontFamily: "var(--font-mono)" }}>
                              {o.token_symbol || shortAddr(o.token_address)}
                            </span>
                            <span style={{ fontSize: "9px", background: "var(--bg-elevated)", color: "var(--text-muted)", border: "1px solid var(--border)", borderRadius: "3px", padding: "0 4px", fontFamily: "var(--font-mono)", flexShrink: 0 }}>
                              {chainName(o.chain_index)}
                            </span>
                            {o.protocol && (
                              <span style={{ fontSize: "9px", color: "var(--text-muted)", background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: "3px", padding: "0 4px" }}>
                                {o.protocol}
                              </span>
                            )}
                          </div>
                          <div style={{ fontSize: "10px", color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginTop: "1px", display: "flex", gap: "6px" }}>
                            <span>{o.pair_label || shortAddr(o.pool_address)}</span>
                            {o.fee_rate > 0 && (
                              <span style={{ color: "var(--accent-cyan)" }}>{(o.fee_rate * 100).toFixed(2)}% fee</span>
                            )}
                          </div>
                        </div>
                      </div>
                    </td>

                    {/* TVL */}
                    <td style={{ padding: "8px 10px", textAlign: "right", borderBottom: isExpanded ? "none" : "1px solid rgba(6,182,212,0.1)", fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>
                      {o.tvl_usd > 0 ? formatVolume(o.tvl_usd) : "—"}
                    </td>

                    {/* LP Score */}
                    <td style={{ padding: "8px 10px", textAlign: "right", borderBottom: isExpanded ? "none" : "1px solid rgba(6,182,212,0.1)" }}>
                      <ScoreBar score={o.net_lp_score} />
                    </td>

                    {/* Fee / MQ sub-scores */}
                    <td style={{ padding: "8px 10px", textAlign: "right", borderBottom: isExpanded ? "none" : "1px solid rgba(6,182,212,0.1)" }}>
                      <SubScores fee={o.fee_income_score} mq={o.market_quality_score} />
                    </td>

                    {/* Strategy */}
                    <td style={{ padding: "8px 10px", textAlign: "right", borderBottom: isExpanded ? "none" : "1px solid rgba(6,182,212,0.1)" }}>
                      <StrategyBadge strategy={o.strategy_type} holding={o.suggested_holding} />
                    </td>

                    {/* IL / Wash */}
                    <td style={{ padding: "8px 10px", textAlign: "right", borderBottom: isExpanded ? "none" : "1px solid rgba(6,182,212,0.1)" }}>
                      <RiskCell il={o.il_risk_level} wash={o.wash_risk} />
                    </td>

                    {/* Confidence */}
                    <td style={{ padding: "8px 10px", textAlign: "right", borderBottom: isExpanded ? "none" : "1px solid rgba(6,182,212,0.1)", fontFamily: "var(--font-mono)", fontSize: "11px", color: "var(--text-muted)" }}>
                      {(o.confidence * 100).toFixed(0)}%
                    </td>
                  </tr>

                  {/* Expanded detail row */}
                  {isExpanded && (
                    <tr key={`${rowKey}-detail`}>
                      <td colSpan={7} style={{ padding: "0 10px 10px", borderBottom: "1px solid rgba(6,182,212,0.1)", background: "rgba(6,182,212,0.05)" }}>
                        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px", paddingTop: "6px" }}>
                          {/* Reasons */}
                          <div>
                            <div style={{ fontSize: "10px", fontWeight: 600, color: "#22c55e", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: "4px" }}>Why LP</div>
                            {o.main_reasons.length > 0
                              ? o.main_reasons.map((r, i) => (
                                <div key={i} style={{ fontSize: "11px", color: "var(--text-secondary)", marginBottom: "2px", display: "flex", gap: "5px" }}>
                                  <span style={{ color: "#22c55e", flexShrink: 0 }}>+</span>{r}
                                </div>
                              ))
                              : <span style={{ fontSize: "11px", color: "var(--text-muted)" }}>—</span>
                            }
                          </div>
                          {/* Risks */}
                          <div>
                            <div style={{ fontSize: "10px", fontWeight: 600, color: "#ef4444", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: "4px" }}>Risks</div>
                            {o.main_risks.length > 0
                              ? o.main_risks.map((r, i) => (
                                <div key={i} style={{ fontSize: "11px", color: "var(--text-secondary)", marginBottom: "2px", display: "flex", gap: "5px" }}>
                                  <span style={{ color: "#ef4444", flexShrink: 0 }}>!</span>{r}
                                </div>
                              ))
                              : <span style={{ fontSize: "11px", color: "var(--text-muted)" }}>—</span>
                            }
                          </div>
                        </div>
                        {/* View token link */}
                        {onViewToken && (
                          <button
                            onClick={(e) => { e.stopPropagation(); onViewToken(o.chain_index, o.token_address); }}
                            style={{
                              marginTop: "8px", padding: "3px 10px", fontSize: "11px", fontWeight: 600,
                              background: "rgba(6,182,212,0.12)", border: "1px solid rgba(6,182,212,0.35)",
                              borderRadius: "5px", color: "#06b6d4", cursor: "pointer", fontFamily: "var(--font-sans)",
                            }}
                          >
                            View Token →
                          </button>
                        )}
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
