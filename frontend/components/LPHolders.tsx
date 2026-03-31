"use client";

import { useEffect, useState } from "react";
import { Copy, Check, ExternalLink } from "lucide-react";
import {
  fetchPoolPositions,
  fetchWashAnalysis,
  PoolPositionSummary,
  WashAnalysis,
  LPPosition,
  SuspiciousTrader,
} from "@/lib/api";

// ── Helpers ───────────────────────────────────────────────────────────────────

function shortAddr(a: string) {
  if (!a || a.length <= 12) return a;
  return a.slice(0, 6) + "…" + a.slice(-4);
}

function fmtPct(v: number, decimals = 1) {
  return `${(v * 100).toFixed(decimals)}%`;
}

function fmtPrice(v: number) {
  if (v === 0) return "—";
  if (v < 0.000001) return v.toExponential(2);
  if (v < 0.001) return v.toFixed(6);
  if (v < 1) return v.toFixed(4);
  if (v < 10000) return v.toFixed(2);
  return v.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function fmtUsd(v: number | null | undefined) {
  if (v == null || v === 0) return "—";
  if (Math.abs(v) >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (Math.abs(v) >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toFixed(2)}`;
}

function fmtTs(ts: number) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const now = Date.now();
  const diff = now - ts * 1000;
  if (diff < 3_600_000) return `${Math.floor(diff / 60000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return d.toLocaleDateString();
}

function pnlColor(v: number | null) {
  if (v == null) return "var(--text-muted)";
  if (v > 0) return "var(--accent-green)";
  if (v < 0) return "var(--accent-red)";
  return "var(--text-secondary)";
}

function washRiskColor(risk: string) {
  if (risk === "high") return "var(--accent-red)";
  if (risk === "medium") return "#eab308";
  return "var(--accent-green)";
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
      style={{
        background: "none", border: "none", cursor: "pointer",
        color: copied ? "var(--accent-green)" : "var(--text-muted)",
        padding: "1px", lineHeight: 1,
      }}
    >
      {copied ? <Check size={10} /> : <Copy size={10} />}
    </button>
  );
}

// ── Unavailable positions state ────────────────────────────────────────────────

const UNAVAILABLE_MESSAGES: Record<string, { title: string; detail: string }> = {
  v4_positions_not_supported_with_current_subgraph: {
    title: "LP positions not available on this network",
    detail: "The BSC V4 subgraph does not expose per-position data in the required format.",
  },
  meteora_positions_api_unavailable: {
    title: "LP positions not available for Solana",
    detail: "The Meteora positions API is currently unreachable (HTTP 404).",
  },
  v3_subgraph_not_configured: {
    title: "LP positions not available on this network",
    detail: "No V3 subgraph URL is configured for this chain.",
  },
  pool_not_found_in_subgraph: {
    title: "Pool not found in subgraph",
    detail: "This pool has no indexed position data.",
  },
};

function UnavailablePositions({ reason }: { reason: string }) {
  const msg = UNAVAILABLE_MESSAGES[reason] ?? {
    title: "LP positions unavailable",
    detail: reason.replace(/_/g, " "),
  };
  return (
    <div style={{
      padding: "14px 16px", display: "flex", alignItems: "flex-start", gap: "10px",
      borderTop: "2px solid rgba(234,179,8,0.3)", background: "rgba(234,179,8,0.04)",
    }}>
      <span style={{ fontSize: "16px", lineHeight: 1.2 }}>⚠</span>
      <div>
        <div style={{ fontSize: "12px", fontWeight: 600, color: "#eab308", marginBottom: "2px" }}>
          {msg.title}
        </div>
        <div style={{ fontSize: "11px", color: "var(--text-muted)" }}>
          {msg.detail}
        </div>
        <div style={{ fontSize: "10px", color: "var(--text-muted)", marginTop: "4px", fontStyle: "italic" }}>
          This network / protocol is not currently supported for LP holder reconstruction.
        </div>
      </div>
    </div>
  );
}

// ── Section header ─────────────────────────────────────────────────────────────

const SectionHeader = ({ title, sub }: { title: string; sub?: string }) => (
  <div style={{
    padding: "10px 14px", borderBottom: "1px solid var(--border)",
    display: "flex", alignItems: "center", justifyContent: "space-between",
  }}>
    <span style={{ fontFamily: "var(--font-display)", fontWeight: 600, fontSize: "13px" }}>
      {title}
    </span>
    {sub && <span style={{ color: "var(--text-muted)", fontSize: "11px" }}>{sub}</span>}
  </div>
);

const TH: React.CSSProperties = {
  padding: "6px 10px", textAlign: "right",
  color: "var(--text-muted)", fontSize: "10px", fontWeight: 500,
  textTransform: "uppercase", letterSpacing: "0.06em",
  borderBottom: "1px solid var(--border)", background: "var(--bg-card)",
  whiteSpace: "nowrap",
};
const THL: React.CSSProperties = { ...TH, textAlign: "left" };
const TD: React.CSSProperties = {
  padding: "7px 10px", textAlign: "right",
  fontFamily: "var(--font-mono)", fontSize: "11px",
  borderBottom: "1px solid rgba(255,255,255,0.04)", color: "var(--text-secondary)",
};
const TDL: React.CSSProperties = { ...TD, textAlign: "left" };

// ── LP Holders panel ───────────────────────────────────────────────────────────

interface LPHoldersProps {
  chainIndex: string;
  poolAddress: string;
  tokenAddress: string;
}

export function LPHolders({ chainIndex, poolAddress, tokenAddress }: LPHoldersProps) {
  const [summary, setSummary] = useState<PoolPositionSummary | null>(null);
  const [washData, setWashData] = useState<WashAnalysis | null>(null);
  const [loadingPos, setLoadingPos] = useState(false);
  const [loadingWash, setLoadingWash] = useState(false);
  const [posError, setPosError] = useState("");
  const [showAllPos, setShowAllPos] = useState(false);
  const [showWashTraders, setShowWashTraders] = useState(false);

  useEffect(() => {
    if (!poolAddress || !chainIndex) return;

    setLoadingPos(true);
    setPosError("");
    setSummary(null);

    fetchPoolPositions(chainIndex, poolAddress)
      .then(setSummary)
      .catch((e) => setPosError(String(e)))
      .finally(() => setLoadingPos(false));
  }, [chainIndex, poolAddress]);

  useEffect(() => {
    if (!tokenAddress || !chainIndex) return;

    setLoadingWash(true);
    setWashData(null);

    fetchWashAnalysis(chainIndex, tokenAddress, poolAddress || undefined)
      .then(setWashData)
      .catch(() => {/* silent */})
      .finally(() => setLoadingWash(false));
  }, [chainIndex, tokenAddress, poolAddress]);

  const displayPositions = summary
    ? (showAllPos ? summary.positions : summary.positions.slice(0, 10))
    : [];

  const displayTraders = washData
    ? (showWashTraders ? washData.suspicious_traders : washData.suspicious_traders.slice(0, 5))
    : [];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>

      {/* ── LP Position Holders ── */}
      <div style={{
        background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: "10px",
        overflow: "hidden",
      }}>
        <SectionHeader
          title="LP Position Holders"
          sub={
            summary && !summary.unavailable_reason && summary.total_positions > 0
              ? `${summary.total_positions} positions · ${summary.active_positions} in-range · top 10 hold ${fmtPct(summary.top10_liquidity_pct)} of ${summary.positions_fetched} fetched`
              : loadingPos ? "Loading…" : undefined
          }
        />

        {loadingPos && (
          <div style={{ padding: "24px", textAlign: "center", color: "var(--text-muted)", fontSize: "12px" }}>
            Fetching position data…
          </div>
        )}

        {!loadingPos && posError && (
          <div style={{ padding: "16px 14px", display: "flex", alignItems: "flex-start", gap: "8px" }}>
            <span style={{ color: "var(--accent-red)", fontSize: "13px" }}>⚠</span>
            <span style={{ color: "var(--accent-red)", fontSize: "12px" }}>Failed to load positions: {posError}</span>
          </div>
        )}

        {!loadingPos && summary && summary.unavailable_reason && (
          <UnavailablePositions reason={summary.unavailable_reason} />
        )}

        {!loadingPos && summary && !summary.unavailable_reason && summary.positions.length === 0 && (
          <div style={{ padding: "16px 14px", color: "var(--text-muted)", fontSize: "12px" }}>
            No active positions in this pool
          </div>
        )}

        {!loadingPos && summary && summary.positions.length > 0 && (
          <>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "11px" }}>
                <thead>
                  <tr>
                    <th style={THL}>Owner</th>
                    <th style={TH}>Price Range</th>
                    <th style={TH}>Status</th>
                    <th style={TH}>Fees Collected</th>
                    <th style={TH}>Est. PnL</th>
                    <th style={TH}>Cur. Value</th>
                  </tr>
                </thead>
                <tbody>
                  {displayPositions.map((pos, i) => (
                    <PositionRow key={`${pos.owner}-${i}`} pos={pos} chainIndex={chainIndex} />
                  ))}
                </tbody>
              </table>
            </div>

            {summary.positions.length > 10 && (
              <div style={{ padding: "8px 14px", textAlign: "center" }}>
                <button
                  onClick={() => setShowAllPos((v) => !v)}
                  style={{
                    background: "none", border: "1px solid var(--border)", borderRadius: "5px",
                    padding: "4px 14px", fontSize: "11px", cursor: "pointer",
                    color: "var(--text-secondary)", fontFamily: "var(--font-sans)",
                  }}
                >
                  {showAllPos
                    ? "Show less"
                    : `Show all ${summary.positions.length} positions`}
                </button>
              </div>
            )}
          </>
        )}
      </div>

      {/* ── Sniper-Tagged Wallet Activity ── */}
      {(washData || loadingWash) && (
        <div style={{
          background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: "10px",
          overflow: "hidden",
        }}>
          <SectionHeader
            title="Sniper-Tagged Wallet Activity"
            sub={
              washData
                ? `pool wash risk: ${washData.pool_wash_risk} · ${washData.sniper_count} early-entry wallets · ${fmtPct(washData.sniper_volume_pct)} of volume`
                : "Loading…"
            }
          />

          {loadingWash && (
            <div style={{ padding: "16px 14px", color: "var(--text-muted)", fontSize: "12px" }}>
              Loading sniper-tag data…
            </div>
          )}

          {washData && (
            <div style={{ padding: "10px 14px", display: "flex", flexDirection: "column", gap: "10px" }}>

              {/* Risk summary strip */}
              <div style={{ display: "flex", gap: "16px", flexWrap: "wrap" }}>
                <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                  <span style={{ fontSize: "10px", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>Pool Wash Risk</span>
                  <span style={{
                    padding: "1px 7px", borderRadius: "4px", fontSize: "11px", fontWeight: 600,
                    background: "rgba(0,0,0,0.2)",
                    color: washRiskColor(washData.pool_wash_risk),
                    border: `1px solid ${washRiskColor(washData.pool_wash_risk)}40`,
                  }}>
                    {washData.pool_wash_risk.toUpperCase()}
                  </span>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                  <span style={{ fontSize: "10px", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>Wash Score</span>
                  <span style={{ fontFamily: "var(--font-mono)", fontSize: "12px", color: washRiskColor(washData.pool_wash_risk) }}>
                    {(washData.pool_wash_score * 100).toFixed(0)}%
                  </span>
                  <span style={{ fontSize: "9px", color: "var(--text-muted)" }}>(pool heuristic)</span>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                  <span style={{ fontSize: "10px", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>Sniper Vol %</span>
                  <span style={{ fontFamily: "var(--font-mono)", fontSize: "12px", color: "var(--text-primary)" }}>
                    {washData.sniper_volume_pct > 0 ? fmtPct(washData.sniper_volume_pct) : "n/a"}
                  </span>
                  <span style={{ fontSize: "9px", color: "var(--text-muted)" }}>(OKX tag)</span>
                </div>
              </div>

              {washData.suspicious_traders.length > 0 ? (
                <>
                  <div style={{ overflowX: "auto" }}>
                    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "11px" }}>
                      <thead>
                        <tr>
                          <th style={THL}>Address</th>
                          <th style={TH}>Tag</th>
                          <th style={TH}>Trades</th>
                          <th style={TH}>Buy Vol</th>
                          <th style={TH}>Sell Vol</th>
                          <th style={TH}>Last Active</th>
                        </tr>
                      </thead>
                      <tbody>
                        {displayTraders.map((trader, i) => (
                          <TraderRow key={`${trader.address}-${i}`} trader={trader} chainIndex={chainIndex} />
                        ))}
                      </tbody>
                    </table>
                  </div>

                  {washData.suspicious_traders.length > 5 && (
                    <div style={{ textAlign: "center" }}>
                      <button
                        onClick={() => setShowWashTraders((v) => !v)}
                        style={{
                          background: "none", border: "1px solid var(--border)", borderRadius: "5px",
                          padding: "4px 14px", fontSize: "11px", cursor: "pointer",
                          color: "var(--text-secondary)", fontFamily: "var(--font-sans)",
                        }}
                      >
                        {showWashTraders
                          ? "Show less"
                          : `Show all ${washData.suspicious_traders.length} wallets`}
                      </button>
                    </div>
                  )}
                </>
              ) : (
                <div style={{ fontSize: "12px", color: "var(--text-muted)" }}>
                  No Sniper-tagged wallets found in the sampled trades
                </div>
              )}

              <div style={{ fontSize: "10px", color: "var(--text-muted)", borderTop: "1px solid var(--border)", paddingTop: "8px" }}>
                <span>via OKX Sniper tag · {washData.analysis_window}</span>
                <span style={{ marginLeft: "10px", fontStyle: "italic" }}>
                  Heuristic risk + external tag — not a definitive wash-trading verdict
                </span>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Position row ───────────────────────────────────────────────────────────────

function PositionRow({ pos, chainIndex }: { pos: LPPosition; chainIndex: string }) {
  const explorerBase: Record<string, string> = {
    "1": "https://etherscan.io/address/",
    "56": "https://bscscan.com/address/",
    "8453": "https://basescan.org/address/",
    "137": "https://polygonscan.com/address/",
    "501": "https://solscan.io/account/",
  };
  const base = explorerBase[chainIndex] ?? "";

  return (
    <tr style={{ background: "transparent" }}>
      <td style={TDL}>
        <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
          <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-primary)" }}>
            {shortAddr(pos.owner)}
          </span>
          <CopyButton text={pos.owner} />
          {base && (
            <a href={`${base}${pos.owner}`} target="_blank" rel="noopener noreferrer"
              style={{ color: "var(--accent-blue)", lineHeight: 1 }}>
              <ExternalLink size={9} />
            </a>
          )}
        </div>
      </td>
      <td style={TD}>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: "10px" }}>
          {chainIndex === "501"
            ? `Bin ${pos.tick_lower} – ${pos.tick_upper}`
            : `${fmtPrice(pos.price_lower)} – ${fmtPrice(pos.price_upper)}`}
        </span>
      </td>
      <td style={TD}>
        <span style={{
          padding: "1px 6px", borderRadius: "3px", fontSize: "10px",
          background: pos.in_range ? "rgba(34,197,94,0.12)" : "rgba(107,114,128,0.15)",
          color: pos.in_range ? "var(--accent-green)" : "var(--text-muted)",
          border: pos.in_range ? "1px solid rgba(34,197,94,0.3)" : "1px solid rgba(107,114,128,0.3)",
        }}>
          {pos.in_range ? "In Range" : "Out"}
        </span>
      </td>
      <td style={TD}>
        <span style={{ color: pos.fees_usd > 0 ? "var(--accent-cyan)" : "var(--text-muted)" }}>
          {fmtUsd(pos.fees_usd)}
        </span>
      </td>
      <td style={TD}>
        <span style={{ color: pnlColor(pos.pnl_usd) }}>
          {pos.pnl_usd != null
            ? `${pos.pnl_usd >= 0 ? "+" : ""}${fmtUsd(pos.pnl_usd)}`
            : "—"}
        </span>
      </td>
      <td style={TD}>{fmtUsd(pos.current_value_usd)}</td>
    </tr>
  );
}

// ── Trader row ─────────────────────────────────────────────────────────────────

function TraderRow({ trader, chainIndex }: { trader: SuspiciousTrader; chainIndex: string }) {
  const explorerBase: Record<string, string> = {
    "1": "https://etherscan.io/address/",
    "56": "https://bscscan.com/address/",
    "8453": "https://basescan.org/address/",
    "137": "https://polygonscan.com/address/",
    "501": "https://solscan.io/account/",
  };
  const base = explorerBase[chainIndex] ?? "";

  return (
    <tr style={{ background: "transparent" }}>
      <td style={TDL}>
        <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
          <span style={{ fontFamily: "var(--font-mono)", color: "var(--accent-red)" }}>
            {shortAddr(trader.address)}
          </span>
          <CopyButton text={trader.address} />
          {base && (
            <a href={`${base}${trader.address}`} target="_blank" rel="noopener noreferrer"
              style={{ color: "var(--accent-blue)", lineHeight: 1 }}>
              <ExternalLink size={9} />
            </a>
          )}
        </div>
      </td>
      <td style={TD}>
        <span style={{
          padding: "1px 6px", borderRadius: "3px", fontSize: "10px",
          background: "rgba(239,68,68,0.12)", color: "var(--accent-red)",
          border: "1px solid rgba(239,68,68,0.3)",
        }}>
          {trader.tag}
        </span>
      </td>
      <td style={TD}>{trader.trade_count}</td>
      <td style={TD} title={`Buy: ${fmtUsd(trader.buy_volume_usd)}`}>
        <span style={{ color: "var(--accent-green)" }}>{fmtUsd(trader.buy_volume_usd)}</span>
      </td>
      <td style={TD}>
        <span style={{ color: "var(--accent-red)" }}>{fmtUsd(trader.sell_volume_usd)}</span>
      </td>
      <td style={TD}>{fmtTs(trader.last_seen)}</td>
    </tr>
  );
}
