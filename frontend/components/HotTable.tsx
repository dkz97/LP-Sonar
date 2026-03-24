"use client";

import { useEffect, useState } from "react";
import { TokenSnapshot, fetchTokens, formatPrice, formatVolume } from "@/lib/api";
import { PriceChange } from "./PriceChange";
import { VolumeSparkline } from "./VolumeSparkline";
import { TokenCell } from "./TokenCell";

interface Props {
  selectedChain?: string;
  onSelect?: (token: TokenSnapshot) => void;
  onViewDetails?: (token: TokenSnapshot) => void;
}

const TH = ({ children, right, sortKey, activeSort, sortDir, onSort }: {
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
      fontSize: "11px",
      color: sortKey && activeSort === sortKey ? "var(--text-primary)" : "var(--text-muted)",
      textAlign: right ? "right" : "left",
      whiteSpace: "nowrap",
      textTransform: "uppercase",
      letterSpacing: "0.05em",
      borderBottom: "1px solid var(--border)",
      background: "var(--bg-card)",
      cursor: sortKey ? "pointer" : "default",
      userSelect: "none",
    }}
  >
    {children}{sortKey && activeSort === sortKey ? (sortDir === "asc" ? " ↑" : " ↓") : ""}
  </th>
);

export function HotTable({ selectedChain, onSelect, onViewDetails }: Props) {
  const [tokens, setTokens] = useState<TokenSnapshot[]>([]);
  const [loading, setLoading] = useState(true);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  function handleSort(key: string) {
    if (sortKey === key) {
      setSortDir(d => d === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  const sortedTokens = sortKey
    ? [...tokens].sort((a, b) => {
        const av = (a as unknown as Record<string, unknown>)[sortKey];
        const bv = (b as unknown as Record<string, unknown>)[sortKey];
        if (typeof av === "string" && typeof bv === "string") {
          return sortDir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
        }
        return sortDir === "asc" ? Number(av ?? 0) - Number(bv ?? 0) : Number(bv ?? 0) - Number(av ?? 0);
      })
    : tokens;

  useEffect(() => {
    const load = async () => {
      const data = await fetchTokens("hot", selectedChain, 200);
      setTokens(data);
      setLastUpdate(new Date());
      setLoading(false);
    };
    load();
    const id = setInterval(load, 60_000);
    return () => clearInterval(id);
  }, [selectedChain]);

  return (
    <div
      style={{
        background: "var(--bg-card)",
        border: "1px solid var(--border)",
        borderRadius: "10px",
        overflow: "hidden",
        flex: 1,
      }}
    >
      {/* Panel header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "8px",
          padding: "8px 14px",
          background: "var(--bg-elevated)",
          borderBottom: "1px solid var(--border)",
        }}
      >
        <span style={{ color: "var(--accent-yellow)", fontSize: "10px" }}>●</span>
        <span
          style={{
            fontFamily: "var(--font-display)",
            fontWeight: 600,
            fontSize: "12px",
            color: "var(--text-secondary)",
            textTransform: "uppercase",
            letterSpacing: "0.08em",
          }}
        >
          Hot {!loading && `(${tokens.length})`}
        </span>
        {lastUpdate && (
          <span style={{ color: "var(--text-muted)", fontSize: "11px", fontFamily: "var(--font-mono)", marginLeft: "auto" }}>
            {lastUpdate.toLocaleTimeString()} · 60s
          </span>
        )}
      </div>

      {loading ? (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "160px", color: "var(--text-muted)", fontSize: "13px" }}>
          Loading…
        </div>
      ) : tokens.length === 0 ? (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "160px", color: "var(--text-muted)", fontSize: "13px" }}>
          No tokens yet — waiting for first Universe scan
        </div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "12px" }}>
            <thead>
              <tr>
                <TH sortKey="token_symbol" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>Token</TH>
                <TH right sortKey="price_usd" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>Price</TH>
                <TH right sortKey="price_change_5m" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>Δ5M</TH>
                <TH right sortKey="price_change_1h" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>Δ1H</TH>
                <TH right sortKey="price_change_4h" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>Δ4H</TH>
                <TH right sortKey="price_change_24h" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>Δ24H</TH>
                <TH right sortKey="volume_5m" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>Vol5M</TH>
                <TH right sortKey="volume_24h" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>Vol24H</TH>
                <TH right sortKey="z_score" activeSort={sortKey} sortDir={sortDir} onSort={handleSort}>Z</TH>
                <TH right>Trend</TH>
              </tr>
            </thead>
            <tbody>
              {sortedTokens.map((t) => {
                const isHighZ = t.z_score >= 2.0;
                const isMedZ  = t.z_score >= 1.5;
                const rowBg = isHighZ
                  ? "rgba(249,115,22,0.06)"
                  : isMedZ
                  ? "rgba(234,179,8,0.04)"
                  : "transparent";

                return (
                  <tr
                    key={`${t.chain_index}:${t.token_address}`}
                    onClick={() => onSelect?.(t)}
                    style={{ cursor: "pointer", background: rowBg, transition: "background 120ms" }}
                    onMouseEnter={(e) => {
                      (e.currentTarget as HTMLElement).style.background = isHighZ
                        ? "rgba(249,115,22,0.14)"
                        : "var(--bg-elevated)";
                    }}
                    onMouseLeave={(e) => {
                      (e.currentTarget as HTMLElement).style.background = rowBg;
                    }}
                  >
                    <td style={{ padding: "7px 10px", borderBottom: "1px solid var(--border)", minWidth: "180px" }}>
                      <TokenCell
                        chainIndex={t.chain_index}
                        address={t.token_address}
                        symbol={t.token_symbol}
                        name={t.token_name}
                        logoUrl={t.logo_url}
                        onViewDetails={onViewDetails ? () => onViewDetails(t) : undefined}
                      />
                    </td>
                    <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid var(--border)", fontFamily: "var(--font-mono)", color: "var(--text-primary)" }}>
                      {formatPrice(t.price_usd)}
                    </td>
                    <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid var(--border)" }}>
                      <PriceChange value={t.price_change_5m} />
                    </td>
                    <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid var(--border)" }}>
                      <PriceChange value={t.price_change_1h} />
                    </td>
                    <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid var(--border)" }}>
                      <PriceChange value={t.price_change_4h} />
                    </td>
                    <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid var(--border)" }}>
                      <PriceChange value={t.price_change_24h} />
                    </td>
                    <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid var(--border)", fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>
                      {formatVolume(t.volume_5m)}
                    </td>
                    <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid var(--border)", fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
                      {formatVolume(t.volume_24h)}
                    </td>
                    <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid var(--border)" }}>
                      <span
                        style={{
                          fontFamily: "var(--font-mono)",
                          fontWeight: 700,
                          color: t.z_score >= 2
                            ? "var(--accent-orange)"
                            : t.z_score >= 1.5
                            ? "var(--accent-yellow)"
                            : "var(--text-muted)",
                        }}
                      >
                        {t.z_score.toFixed(1)}σ
                      </span>
                    </td>
                    <td style={{ padding: "7px 10px", textAlign: "right", borderBottom: "1px solid var(--border)" }}>
                      <VolumeSparkline
                        data={[]}
                        color={isHighZ ? "#f97316" : "#60a5fa"}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
