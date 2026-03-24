"use client";

import { useEffect, useState } from "react";
import {
  TokenSnapshot, fetchTokens, formatPrice, formatVolume,
  strategyLabel, strategyColor, ilRiskColor, washRiskColor, scoreColor,
} from "@/lib/api";
import { PriceChange } from "./PriceChange";
import { TokenCell } from "./TokenCell";

interface Props {
  selectedChain?: string;
  onSelect?: (token: TokenSnapshot) => void;
  onViewDetails?: (token: TokenSnapshot) => void;
}

const TH = ({
  children, right, sortKey, activeSort, sortDir, onSort,
}: {
  children: React.ReactNode;
  right?: boolean;
  sortKey?: string;
  activeSort?: string | null;
  sortDir?: "asc" | "desc";
  onSort?: (key: string) => void;
}) => (
  <th
    onClick={sortKey && onSort ? () => onSort(sortKey) : undefined}
    style={{
      padding: "6px 10px",
      fontWeight: 500,
      fontSize: "10px",
      color: sortKey && activeSort === sortKey ? "rgba(239,68,68,0.9)" : "rgba(239,68,68,0.55)",
      textAlign: right ? "right" : "left",
      whiteSpace: "nowrap",
      textTransform: "uppercase",
      letterSpacing: "0.05em",
      borderBottom: "1px solid rgba(239,68,68,0.15)",
      background: "rgba(239,68,68,0.05)",
      cursor: sortKey ? "pointer" : "default",
      userSelect: "none",
    }}
  >
    {children}{sortKey && activeSort === sortKey ? (sortDir === "asc" ? " ↑" : " ↓") : ""}
  </th>
);

function LPScoreCell({ token }: { token: TokenSnapshot }) {
  if (!token.lp_eligible || token.lp_net_score == null) {
    return <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "11px" }}>—</span>;
  }
  const score = token.lp_net_score;
  const color = scoreColor(score);
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: "3px" }}>
      {/* Score bar */}
      <div style={{ display: "flex", alignItems: "center", gap: "5px" }}>
        <div style={{ width: "40px", height: "4px", background: "var(--bg-elevated)", borderRadius: "2px", overflow: "hidden" }}>
          <div style={{ width: `${score * 100}%`, height: "100%", background: color, borderRadius: "2px" }} />
        </div>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: "11px", fontWeight: 700, color }}>{(score * 100).toFixed(0)}</span>
      </div>
      {/* Strategy */}
      {token.lp_strategy && (
        <span style={{
          fontSize: "9px", fontWeight: 700, fontFamily: "var(--font-mono)",
          color: strategyColor(token.lp_strategy),
          background: `${strategyColor(token.lp_strategy)}18`,
          border: `1px solid ${strategyColor(token.lp_strategy)}40`,
          borderRadius: "3px", padding: "0 4px",
        }}>
          {strategyLabel(token.lp_strategy).toUpperCase()}
        </span>
      )}
    </div>
  );
}

function LPRiskCell({ token }: { token: TokenSnapshot }) {
  if (!token.lp_eligible) {
    return <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "10px" }}>—</span>;
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: "2px" }}>
      {token.lp_il_risk && (
        <span style={{ fontSize: "10px", fontFamily: "var(--font-mono)", color: ilRiskColor(token.lp_il_risk) }}>
          IL:{token.lp_il_risk}
        </span>
      )}
      {token.lp_wash_risk && (
        <span style={{ fontSize: "9px", color: washRiskColor(token.lp_wash_risk) }}>
          wash:{token.lp_wash_risk}
        </span>
      )}
    </div>
  );
}

export function FocusPanel({ selectedChain, onSelect, onViewDetails }: Props) {
  const [tokens, setTokens] = useState<TokenSnapshot[]>([]);
  const [sortKey, setSortKey] = useState<string>("lp_net_score");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  function handleSort(key: string) {
    if (sortKey === key) {
      setSortDir(d => d === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  const sortedTokens = [...tokens].sort((a, b) => {
    const av = (a as unknown as Record<string, unknown>)[sortKey];
    const bv = (b as unknown as Record<string, unknown>)[sortKey];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "string" && typeof bv === "string") {
      return sortDir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
    }
    return sortDir === "asc" ? Number(av) - Number(bv) : Number(bv) - Number(av);
  });

  useEffect(() => {
    const load = async () => {
      const data = await fetchTokens("focus", selectedChain, 50);
      setTokens(data);
    };
    load();
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, [selectedChain]);

  if (tokens.length === 0) return null;

  const eligibleCount = tokens.filter(t => t.lp_eligible).length;

  return (
    <div style={{
      background: "rgba(239,68,68,0.04)",
      border: "1px solid rgba(239,68,68,0.2)",
      borderRadius: "10px",
      overflow: "hidden",
    }}>
      {/* Panel header */}
      <div style={{
        display: "flex", alignItems: "center", gap: "8px",
        padding: "8px 14px",
        background: "rgba(239,68,68,0.08)",
        borderBottom: "1px solid rgba(239,68,68,0.15)",
      }}>
        <span style={{ color: "var(--accent-red)", fontSize: "10px", animation: "pulse 1.5s ease-in-out infinite" }}>●</span>
        <span style={{
          fontFamily: "var(--font-display)", fontWeight: 600, fontSize: "12px",
          color: "rgba(239,68,68,0.9)", textTransform: "uppercase", letterSpacing: "0.08em",
        }}>
          Focus ({tokens.length})
        </span>
        {eligibleCount > 0 && (
          <span style={{
            fontSize: "10px", fontFamily: "var(--font-mono)", fontWeight: 700,
            color: "#06b6d4", background: "rgba(6,182,212,0.12)",
            border: "1px solid rgba(6,182,212,0.3)", borderRadius: "4px", padding: "1px 6px",
          }}>
            {eligibleCount} LP eligible
          </span>
        )}
        <span style={{ color: "rgba(239,68,68,0.4)", fontSize: "11px", fontFamily: "var(--font-mono)", marginLeft: "auto" }}>
          30s refresh
        </span>
      </div>

      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "12px" }}>
          <thead>
            <tr>
              <TH sortKey="token_symbol" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>Token</TH>
              <TH right sortKey="price_usd" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>Price</TH>
              <TH right sortKey="price_change_5m" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>Δ5M</TH>
              <TH right sortKey="price_change_1h" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>Δ1H</TH>
              <TH right sortKey="volume_5m" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>Vol5M</TH>
              <TH right sortKey="z_score" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>Z-Score</TH>
              <TH right sortKey="risk_level" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>Sec</TH>
              <TH right sortKey="lp_net_score" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>LP Score</TH>
              <TH right>IL / Wash</TH>
            </tr>
          </thead>
          <tbody>
            {sortedTokens.map((t) => (
              <tr
                key={`${t.chain_index}:${t.token_address}`}
                onClick={() => onSelect?.(t)}
                style={{
                  cursor: "pointer", transition: "background 120ms",
                  background: t.lp_eligible ? "rgba(6,182,212,0.03)" : "transparent",
                }}
                onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = "rgba(239,68,68,0.1)"; }}
                onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = t.lp_eligible ? "rgba(6,182,212,0.03)" : "transparent"; }}
              >
                <td style={{ padding: "7px 10px", borderBottom: "1px solid rgba(239,68,68,0.1)", minWidth: "180px" }}>
                  <TokenCell
                    chainIndex={t.chain_index}
                    address={t.token_address}
                    symbol={t.token_symbol}
                    name={t.token_name}
                    logoUrl={t.logo_url}
                    onViewDetails={onViewDetails ? () => onViewDetails(t) : undefined}
                  />
                  {t.lp_pair_label && (
                    <div style={{ fontSize: "9px", color: "#06b6d4", marginTop: "1px", fontFamily: "var(--font-mono)" }}>
                      {t.lp_pair_label}{t.lp_holding ? ` · ${t.lp_holding}` : ""}
                    </div>
                  )}
                </td>
                <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid rgba(239,68,68,0.1)", fontFamily: "var(--font-mono)", color: "var(--text-primary)" }}>
                  {formatPrice(t.price_usd)}
                </td>
                <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid rgba(239,68,68,0.1)" }}>
                  <PriceChange value={t.price_change_5m} />
                </td>
                <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid rgba(239,68,68,0.1)" }}>
                  <PriceChange value={t.price_change_1h} />
                </td>
                <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid rgba(239,68,68,0.1)", fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>
                  {formatVolume(t.volume_5m)}
                </td>
                <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid rgba(239,68,68,0.1)" }}>
                  <span style={{
                    fontFamily: "var(--font-mono)", fontWeight: 700,
                    color: t.z_score >= 3 ? "var(--accent-red)" : t.z_score >= 2 ? "var(--accent-orange)" : "var(--accent-yellow)",
                  }}>
                    {t.z_score.toFixed(2)}σ
                  </span>
                </td>
                <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid rgba(239,68,68,0.1)" }}>
                  {t.risk_level !== undefined && t.risk_level !== null ? (
                    <span style={{
                      borderRadius: "4px", padding: "2px 5px", fontSize: "10px", fontWeight: 700, fontFamily: "var(--font-mono)",
                      background: t.risk_level >= 4 ? "rgba(239,68,68,0.2)" : t.risk_level >= 2 ? "rgba(234,179,8,0.15)" : "rgba(34,197,94,0.15)",
                      color: t.risk_level >= 4 ? "var(--accent-red)" : t.risk_level >= 2 ? "var(--accent-yellow)" : "var(--accent-green)",
                    }}>
                      {["?", "LOW", "MED", "MED+", "HIGH", "V.HIGH"][t.risk_level] ?? "?"}
                    </span>
                  ) : (
                    <span style={{ color: "var(--text-muted)" }}>—</span>
                  )}
                </td>
                <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid rgba(239,68,68,0.1)" }}>
                  <LPScoreCell token={t} />
                </td>
                <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid rgba(239,68,68,0.1)" }}>
                  <LPRiskCell token={t} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
