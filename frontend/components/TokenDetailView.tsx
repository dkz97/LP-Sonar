"use client";

import { useEffect, useRef, useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend,
} from "recharts";
import { ExternalLink, Copy, Check, RefreshCw } from "lucide-react";
import { chainName, explorerUrl, formatVolume, formatPrice, TokenSnapshot } from "@/lib/api";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// ─── Types ────────────────────────────────────────────────────────────────────

interface BasicInfo {
  tokenSymbol?: string;
  tokenName?: string;
  logoUrl?: string;
  marketCap?: string;
  fdvUsd?: string;
  circulatingSupply?: string;
  totalSupply?: string;
  holders?: string;
  website?: string;
  officialWebsite?: string;
  twitterUrl?: string;
  telegramUrl?: string;
  priceUsd?: string;
  volume24H?: string;
  [key: string]: unknown;
}

interface TxWindow { buys: number; sells: number }
interface Pool {
  pool_address: string;
  dex_name: string;
  pair_name: string;
  base_token_symbol: string;
  base_token_name: string;
  base_token_address: string;
  quote_token_symbol: string;
  quote_token_address: string;
  price_usd: number;
  liquidity_usd: number;
  fdv_usd: number;
  market_cap_usd: number;
  volume: { m5: number; m15: number; h1: number; h6: number; h24: number };
  txns: { m5: TxWindow; m15: TxWindow; m30: TxWindow; h1: TxWindow; h6: TxWindow; h24: TxWindow };
  price_change: { m5: number; h1: number; h6: number; h24: number };
  pool_created_at?: string;
  fee_rate?: number;
}

interface OhlcvBar { time: number; volume: number }
interface TxBar { time: number; buys: number; sells: number }

// ─── Helpers ─────────────────────────────────────────────────────────────────

export function shortAddr(a: string) {
  if (!a || a.length <= 12) return a;
  return a.slice(0, 6) + "…" + a.slice(-4);
}

function fmtNum(n?: string | number, decimals = 2): string {
  const v = Number(n ?? 0);
  if (!v) return "—";
  if (v >= 1e9) return `${(v / 1e9).toFixed(decimals)}B`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(decimals)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(decimals)}K`;
  return v.toFixed(decimals);
}

function fmtTs(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function pcColor(v: number) {
  if (v > 0) return "var(--accent-green)";
  if (v < 0) return "var(--accent-red)";
  return "var(--text-muted)";
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div style={{ background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: "8px", padding: "10px 14px" }}>
      <div style={{ color: "var(--text-muted)", fontSize: "10px", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: "4px" }}>{label}</div>
      <div style={{ color: "var(--text-primary)", fontFamily: "var(--font-mono)", fontSize: "14px", fontWeight: 600 }}>{value}</div>
      {sub && <div style={{ color: "var(--text-muted)", fontSize: "10px", marginTop: "2px" }}>{sub}</div>}
    </div>
  );
}

const TH_STYLE: React.CSSProperties = {
  padding: "7px 10px", textAlign: "right" as const,
  color: "var(--text-muted)", fontSize: "10px", fontWeight: 500,
  textTransform: "uppercase", letterSpacing: "0.06em", whiteSpace: "nowrap",
  borderBottom: "1px solid var(--border)", background: "var(--bg-card)",
};

type PoolSortKey = "dex_name" | "price_usd" | "liquidity_usd" | "volume_m5" | "volume_h24" | "price_change_h24" | "txns_h24";

function PoolTH({ children, left, sortKey, activeSort, sortDir, onSort }: {
  children: React.ReactNode;
  left?: boolean;
  sortKey?: PoolSortKey;
  activeSort?: PoolSortKey | null;
  sortDir?: "asc" | "desc";
  onSort?: (key: PoolSortKey) => void;
}) {
  const isActive = sortKey && activeSort === sortKey;
  return (
    <th
      onClick={sortKey && onSort ? () => onSort(sortKey) : undefined}
      style={{
        ...TH_STYLE,
        textAlign: left ? "left" : "right",
        color: isActive ? "var(--text-primary)" : "var(--text-muted)",
        cursor: sortKey ? "pointer" : "default",
        userSelect: "none",
      }}
    >
      {children}{isActive ? (sortDir === "asc" ? " ↑" : " ↓") : ""}
    </th>
  );
}

const TX_WINDOWS: { key: keyof Pool["txns"]; label: string }[] = [
  { key: "m5",  label: "5m"  },
  { key: "m15", label: "15m" },
  { key: "m30", label: "30m" },
  { key: "h1",  label: "1h"  },
  { key: "h24", label: "24h" },
];

function PoolRow({ pool, selected, onClick }: { pool: Pool; selected: boolean; onClick: () => void }) {
  const [copiedAddr, setCopiedAddr] = useState(false);

  // Only show windows that have any data
  const activeTxWindows = TX_WINDOWS.filter(w => {
    const tw = pool.txns[w.key];
    return tw.buys + tw.sells > 0;
  });

  const feeRate = pool.fee_rate ?? 0;
  const revenueWindows = [
    { label: "5m",  vol: pool.volume.m5  },
    { label: "15m", vol: pool.volume.m15 },
    { label: "1h",  vol: pool.volume.h1  },
    { label: "24h", vol: pool.volume.h24 },
  ].filter(w => w.vol > 0);

  return (
    <tr
      onClick={onClick}
      style={{ cursor: "pointer", background: selected ? "rgba(59,130,246,0.12)" : "transparent", borderBottom: "1px solid var(--border)", transition: "background 150ms" }}
      onMouseEnter={e => { if (!selected) (e.currentTarget as HTMLTableRowElement).style.background = "rgba(255,255,255,0.03)"; }}
      onMouseLeave={e => { if (!selected) (e.currentTarget as HTMLTableRowElement).style.background = "transparent"; }}
    >
      <td style={{ padding: "8px 10px", whiteSpace: "nowrap" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "5px" }}>
          {selected && <span style={{ width: "3px", height: "14px", background: "var(--accent-blue)", borderRadius: "2px", flexShrink: 0 }} />}
          <span style={{ color: "var(--text-primary)", fontWeight: 600, fontSize: "12px" }}>{pool.dex_name}</span>
          <span style={{ color: "var(--text-muted)", fontSize: "11px" }}>{pool.base_token_symbol}/{pool.quote_token_symbol}</span>
          {pool.pool_address.length > 42 && (
            <span style={{ fontSize: "9px", background: "rgba(139,92,246,0.15)", color: "#a78bfa", border: "1px solid rgba(139,92,246,0.3)", borderRadius: "3px", padding: "0 4px", lineHeight: "14px", fontFamily: "var(--font-mono)" }}>
              V4
            </span>
          )}
          {feeRate > 0 && (
            <span style={{ fontSize: "9px", background: "rgba(34,197,94,0.1)", color: "#86efac", border: "1px solid rgba(34,197,94,0.2)", borderRadius: "3px", padding: "0 4px", lineHeight: "14px", fontFamily: "var(--font-mono)" }}>
              {(feeRate * 100).toFixed(2)}%
            </span>
          )}
        </div>
        <div style={{ fontFamily: "var(--font-mono)", fontSize: "10px", color: "var(--text-muted)", marginTop: "2px", display: "flex", alignItems: "center", gap: "3px" }}>
          <span>{shortAddr(pool.pool_address)}</span>
          <button
            onClick={e => {
              e.stopPropagation();
              navigator.clipboard.writeText(pool.pool_address);
              setCopiedAddr(true);
              setTimeout(() => setCopiedAddr(false), 1500);
            }}
            style={{ background: "none", border: "none", cursor: "pointer", color: copiedAddr ? "var(--accent-green)" : "var(--text-muted)", padding: "1px", lineHeight: 1 }}
          >
            {copiedAddr ? <Check size={10} /> : <Copy size={10} />}
          </button>
        </div>
      </td>
      <td style={{ padding: "8px 10px", fontFamily: "var(--font-mono)", fontSize: "12px", color: "var(--text-primary)", textAlign: "right" }}>
        {pool.price_usd > 0 ? formatPrice(pool.price_usd) : "—"}
      </td>
      {/* TX windows: show B/S for each active time window */}
      <td style={{ padding: "8px 10px", textAlign: "right", verticalAlign: "middle" }}>
        {activeTxWindows.length === 0 ? (
          <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "11px" }}>—</span>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "1px", alignItems: "flex-end" }}>
            {activeTxWindows.map(w => {
              const tw = pool.txns[w.key];
              return (
                <div key={w.key} style={{ display: "flex", alignItems: "center", gap: "3px", fontFamily: "var(--font-mono)", fontSize: "10px", lineHeight: "1.5" }}>
                  <span style={{ color: "var(--text-muted)", width: "22px", textAlign: "right" }}>{w.label}</span>
                  <span style={{ color: "var(--accent-green)", minWidth: "28px", textAlign: "right" }}>{fmtNum(tw.buys, 0)}</span>
                  <span style={{ color: "var(--text-muted)" }}>/</span>
                  <span style={{ color: "var(--accent-red)", minWidth: "28px", textAlign: "left" }}>{fmtNum(tw.sells, 0)}</span>
                </div>
              );
            })}
          </div>
        )}
      </td>
      <td style={{ padding: "8px 10px", fontFamily: "var(--font-mono)", fontSize: "12px", color: "var(--text-primary)", textAlign: "right" }}>
        {pool.liquidity_usd > 0 ? formatVolume(pool.liquidity_usd) : "—"}
      </td>
      <td style={{ padding: "8px 10px", fontFamily: "var(--font-mono)", fontSize: "12px", textAlign: "right" }}>
        <div style={{ color: "var(--text-secondary)" }}>{pool.volume.m5 > 0 ? formatVolume(pool.volume.m5) : "—"}</div>
        <div style={{ color: "var(--text-muted)", fontSize: "10px" }}>{pool.volume.h24 > 0 ? formatVolume(pool.volume.h24) : ""}</div>
      </td>
      <td style={{ padding: "8px 10px", fontFamily: "var(--font-mono)", fontSize: "11px", textAlign: "right" }}>
        <span style={{ color: pcColor(pool.price_change.h24) }}>
          {pool.price_change.h24 !== 0 ? `${pool.price_change.h24 > 0 ? "+" : ""}${pool.price_change.h24.toFixed(2)}%` : "—"}
        </span>
      </td>
      <td style={{ padding: "8px 10px", textAlign: "right", verticalAlign: "middle" }}>
        {feeRate <= 0 ? (
          <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "11px" }}>—</span>
        ) : revenueWindows.length === 0 ? (
          <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "11px" }}>—</span>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "1px", alignItems: "flex-end" }}>
            {revenueWindows.map(w => (
              <div key={w.label} style={{ display: "flex", alignItems: "center", gap: "3px", fontFamily: "var(--font-mono)", fontSize: "10px", lineHeight: "1.5" }}>
                <span style={{ color: "var(--text-muted)", width: "22px", textAlign: "right" }}>{w.label}</span>
                <span style={{ color: "var(--accent-cyan)" }}>{formatVolume(w.vol * feeRate)}</span>
              </div>
            ))}
          </div>
        )}
      </td>
    </tr>
  );
}

function VolTooltip({ active, payload, label }: { active?: boolean; payload?: Array<{ value: number }>; label?: string }) {
  if (!active || !payload?.length) return null;
  return (
    <div style={{ background: "var(--bg-elevated)", border: "1px solid var(--border-light)", borderRadius: "6px", padding: "6px 10px", fontSize: "11px", fontFamily: "var(--font-mono)" }}>
      <div style={{ color: "var(--text-muted)", marginBottom: "2px" }}>{label}</div>
      <div style={{ color: "var(--accent-cyan)" }}>{formatVolume(payload[0].value)}</div>
    </div>
  );
}

function TxTooltip({ active, payload, label }: { active?: boolean; payload?: Array<{ name: string; value: number; color: string }>; label?: string }) {
  if (!active || !payload?.length) return null;
  return (
    <div style={{ background: "var(--bg-elevated)", border: "1px solid var(--border-light)", borderRadius: "6px", padding: "6px 10px", fontSize: "11px", fontFamily: "var(--font-mono)" }}>
      <div style={{ color: "var(--text-muted)", marginBottom: "4px" }}>{label}</div>
      {payload.map(p => (
        <div key={p.name} style={{ color: p.color, display: "flex", gap: "8px", justifyContent: "space-between" }}>
          <span>{p.name}</span><span>{p.value}</span>
        </div>
      ))}
    </div>
  );
}

function IntervalToggle({ value, onChange }: { value: "5m" | "15m"; onChange: (v: "5m" | "15m") => void }) {
  return (
    <div style={{ display: "flex", background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: "6px", padding: "2px", gap: "2px" }}>
      {(["5m", "15m"] as const).map(iv => (
        <button key={iv} onClick={() => onChange(iv)} style={{
          padding: "2px 8px", borderRadius: "4px", fontSize: "11px", fontWeight: 600,
          fontFamily: "var(--font-mono)", border: "none", cursor: "pointer",
          background: value === iv ? "var(--accent-blue)" : "none",
          color: value === iv ? "#fff" : "var(--text-secondary)",
          transition: "background 150ms, color 150ms",
        }}>{iv}</button>
      ))}
    </div>
  );
}

// ─── Main View ────────────────────────────────────────────────────────────────

interface TokenDetailViewProps {
  chainIndex: string;
  address: string;
  /** Pre-filled from the snapshot so the header shows immediately */
  snapshot?: TokenSnapshot | null;
}

export function TokenDetailView({ chainIndex, address, snapshot }: TokenDetailViewProps) {
  // Pre-populate from snapshot so the header shows name/price immediately (no flicker)
  const [basicInfo, setBasicInfo] = useState<BasicInfo>(() => ({
    tokenSymbol: snapshot?.token_symbol || undefined,
    tokenName:   snapshot?.token_name   || undefined,
    priceUsd:    snapshot?.price_usd    ? String(snapshot.price_usd) : undefined,
  }));
  const [pools, setPools] = useState<Pool[]>([]);
  const [ohlcv, setOhlcv] = useState<OhlcvBar[]>([]);
  const [txHistory, setTxHistory] = useState<TxBar[]>([]);
  const [selectedPool, setSelectedPool] = useState<Pool | null>(null);
  const [interval, setIntervalVal] = useState<"5m" | "15m">("5m");
  const [chartTab, setChartTab] = useState<"volume" | "tx">("volume");
  const [copied, setCopied] = useState(false);
  const [loadingBasicInfo, setLoadingBasicInfo] = useState(!snapshot);
  const [loadingPools, setLoadingPools] = useState(true);
  const [loadingChart, setLoadingChart] = useState(false);
  const [chartError, setChartError] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [poolSortKey, setPoolSortKey] = useState<PoolSortKey>("liquidity_usd");
  const [poolSortDir, setPoolSortDir] = useState<"asc" | "desc">("desc");
  const forceRefreshRef = useRef(false);

  function handlePoolSort(key: PoolSortKey) {
    if (poolSortKey === key) {
      setPoolSortDir(d => d === "asc" ? "desc" : "asc");
    } else {
      setPoolSortKey(key);
      setPoolSortDir("desc");
    }
  }

  function getPoolSortValue(p: Pool, key: PoolSortKey): number | string {
    switch (key) {
      case "dex_name": return p.dex_name;
      case "price_usd": return p.price_usd;
      case "liquidity_usd": return p.liquidity_usd;
      case "volume_m5": return p.volume.m5;
      case "volume_h24": return p.volume.h24;
      case "price_change_h24": return p.price_change.h24;
      case "txns_h24": return p.txns.h24.buys + p.txns.h24.sells;
    }
  }

  const chain = chainName(chainIndex);
  const explorerLink = explorerUrl(chainIndex, address);

  // Fetch basic info — AbortController prevents stale response overwriting a newer token
  useEffect(() => {
    if (!chainIndex || !address) return;
    const ctrl = new AbortController();
    setLoadingBasicInfo(true);
    fetch(`${API_BASE}/api/v1/token/${chainIndex}/${address}/basic-info`, { signal: ctrl.signal })
      .then(r => r.ok ? r.json() : {})
      .then(data => {
        if (ctrl.signal.aborted) return;
        // Merge API response over snapshot pre-fill (API wins for non-empty values)
        setBasicInfo(prev => ({ ...prev, ...Object.fromEntries(Object.entries(data as Record<string, unknown>).filter(([, v]) => v !== "" && v !== null && v !== undefined)) }));
        setLoadingBasicInfo(false);
      })
      .catch(() => { if (!ctrl.signal.aborted) setLoadingBasicInfo(false); });
    return () => ctrl.abort();
  }, [chainIndex, address]);

  // Fetch pools — AbortController prevents stale response overwriting a newer token
  useEffect(() => {
    if (!chainIndex || !address) return;
    const ctrl = new AbortController();
    setLoadingPools(true);
    fetch(`${API_BASE}/api/v1/token/${chainIndex}/${address}/pools`, { signal: ctrl.signal })
      .then(r => r.ok ? r.json() : [])
      .then((data: Pool[]) => {
        if (ctrl.signal.aborted) return;
        setPools(data);
        const first = [...data].sort((a, b) => b.liquidity_usd - a.liquidity_usd)[0];
        if (first) setSelectedPool(first);
        setLoadingPools(false);
      })
      .catch(e => { if (!ctrl.signal.aborted) setLoadingPools(false); void e; });
    return () => ctrl.abort();
  }, [chainIndex, address]);

  // Fetch charts when pool/interval/refreshKey changes — proper AbortController cleanup
  useEffect(() => {
    if (!selectedPool?.pool_address) return;
    const ctrl = new AbortController();
    const forceRefresh = forceRefreshRef.current;
    setLoadingChart(true);
    setChartError("");

    const base = `${API_BASE}/api/v1/token/${chainIndex}/${address}`;
    const pool = encodeURIComponent(selectedPool.pool_address);

    const safeFetch = (url: string) =>
      fetch(url, { signal: ctrl.signal }).then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<unknown[]>;
      });

    const endpoint = chartTab === "volume"
      ? `${base}/ohlcv?pool_address=${pool}&interval=${interval}&limit=60&refresh=${forceRefresh ? 1 : 0}`
      : `${base}/tx-history?pool_address=${pool}&interval=${interval}&refresh=${forceRefresh ? 1 : 0}`;

    safeFetch(endpoint)
      .then((payload) => {
        if (ctrl.signal.aborted) return;
        if (chartTab === "volume") {
          setOhlcv(payload as OhlcvBar[]);
        } else {
          setTxHistory(payload as TxBar[]);
        }
      })
      .catch(e => {
        if (ctrl.signal.aborted) return;
        const msg = String(e);
        // AbortError is expected on cleanup — don't show error for that
        if (!msg.includes("abort") && !msg.includes("Abort")) setChartError(msg);
      })
      .finally(() => {
        if (forceRefresh) forceRefreshRef.current = false;
        if (!ctrl.signal.aborted) setLoadingChart(false);
      });
    return () => ctrl.abort();
  }, [selectedPool, interval, chainIndex, address, refreshKey, chartTab]);

  function copyAddress(e: React.MouseEvent) {
    e.stopPropagation();
    navigator.clipboard.writeText(address).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }

  // Uniswap V4 pool IDs are 32-byte hashes (66 chars with 0x prefix), not 20-byte addresses
  const isV4Pool = (addr: string) => addr.length > 42;

  // Aggregate stats
  const totalLiq = pools.reduce((s, p) => s + p.liquidity_usd, 0);
  const totalVol24h = pools.reduce((s, p) => s + p.volume.h24, 0);
  const totalTxs24h = pools.reduce((s, p) => s + p.txns.h24.buys + p.txns.h24.sells, 0);
  const totalTxs5m = pools.reduce((s, p) => s + p.txns.m5.buys + p.txns.m5.sells, 0);
  // Fallback: use basicInfo vol if pools haven't loaded vol data yet
  const displayVol24h = totalVol24h > 0
    ? formatVolume(totalVol24h)
    : basicInfo.volume24H ? formatVolume(parseFloat(basicInfo.volume24H)) : "—";
  const vol24hSub = totalVol24h > 0 ? "all pools" : basicInfo.volume24H ? "token info" : undefined;

  const sortedPools = [...pools].sort((a, b) => {
    const av = getPoolSortValue(a, poolSortKey);
    const bv = getPoolSortValue(b, poolSortKey);
    if (typeof av === "string" && typeof bv === "string") {
      return poolSortDir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
    }
    return poolSortDir === "asc" ? Number(av) - Number(bv) : Number(bv) - Number(av);
  });

  const volChartData = ohlcv.map(b => ({ time: fmtTs(b.time), volume: b.volume }));
  const txChartData = txHistory.map(b => ({ time: fmtTs(b.time), buys: b.buys, sells: b.sells }));

  // Resolved display — prefer basicInfo, fall back to snapshot
  const displaySymbol = basicInfo.tokenSymbol || snapshot?.token_symbol || shortAddr(address);
  const displayName = basicInfo.tokenName || snapshot?.token_name;
  const displayPrice = basicInfo.priceUsd
    ? formatPrice(parseFloat(basicInfo.priceUsd))
    : snapshot?.price_usd ? formatPrice(snapshot.price_usd) : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>

      {/* ── Token header ── */}
      <div style={{ display: "flex", alignItems: "center", gap: "10px", padding: "12px 16px", background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: "10px" }}>
        {basicInfo.logoUrl && (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={basicInfo.logoUrl as string} alt="" style={{ width: 32, height: 32, borderRadius: "50%", objectFit: "cover", flexShrink: 0 }} />
        )}
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <span style={{ fontFamily: "var(--font-display)", fontWeight: 700, fontSize: "16px" }}>{displaySymbol}</span>
            {loadingBasicInfo && (
              <span style={{ color: "var(--text-muted)", fontSize: "11px", fontFamily: "var(--font-mono)", animation: "pulse 1.5s ease-in-out infinite" }}>fetching info…</span>
            )}
            {displayName && displayName !== displaySymbol && (
              <span style={{ color: "var(--text-muted)", fontSize: "13px" }}>{displayName}</span>
            )}
            <span style={{ background: "var(--bg-elevated)", color: "var(--text-secondary)", border: "1px solid var(--border)", borderRadius: "4px", padding: "1px 6px", fontSize: "10px", fontFamily: "var(--font-mono)" }}>{chain}</span>
            {displayPrice && (
              <span style={{ fontFamily: "var(--font-mono)", fontSize: "14px", color: "var(--accent-cyan)", fontWeight: 600 }}>{displayPrice}</span>
            )}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: "6px", marginTop: "3px" }}>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: "11px", color: "var(--text-muted)" }}>{address}</span>
            <button onClick={copyAddress} style={{ background: "none", border: "none", cursor: "pointer", color: copied ? "var(--accent-green)" : "var(--text-muted)", padding: "1px", transition: "color 150ms" }}>
              {copied ? <Check size={12} /> : <Copy size={12} />}
            </button>
            <a href={explorerLink} target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent-blue)", lineHeight: 1 }}>
              <ExternalLink size={12} />
            </a>
          </div>
        </div>

        {/* Social links */}
        <div style={{ marginLeft: "auto", display: "flex", gap: "6px", flexWrap: "wrap" }}>
          {[
            { label: "Website", url: basicInfo.website || basicInfo.officialWebsite },
            { label: "Twitter", url: basicInfo.twitterUrl },
            { label: "Telegram", url: basicInfo.telegramUrl },
          ].filter(l => l.url).map(link => (
            <a key={link.label} href={String(link.url)} target="_blank" rel="noopener noreferrer"
              style={{ display: "flex", alignItems: "center", gap: "3px", fontSize: "11px", color: "var(--accent-blue)", background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: "5px", padding: "3px 8px", textDecoration: "none" }}>
              <ExternalLink size={10} />{link.label}
            </a>
          ))}
        </div>
      </div>

      {/* ── Stats row ── */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(130px, 1fr))", gap: "8px" }}>
        <StatCard label="Market Cap" value={basicInfo.marketCap ? `$${fmtNum(basicInfo.marketCap)}` : "—"} />
        <StatCard label="FDV" value={basicInfo.fdvUsd ? `$${fmtNum(basicInfo.fdvUsd)}` : "—"} />
        <StatCard label="Holders" value={basicInfo.holders ? fmtNum(basicInfo.holders, 0) : "—"} />
        <StatCard label="Total Liquidity" value={totalLiq > 0 ? formatVolume(totalLiq) : "—"} />
        <StatCard label="Vol 24h" value={displayVol24h} sub={vol24hSub} />
        <StatCard label="Txs 5m" value={totalTxs5m > 0 ? String(totalTxs5m) : "—"} sub="all pools" />
        <StatCard label="Txs 24h" value={totalTxs24h > 0 ? fmtNum(totalTxs24h, 0) : "—"} sub="all pools" />
        <StatCard label="Pools" value={loadingPools ? "…" : String(pools.length)} />
      </div>

      {/* ── Main: pools table + chart ── */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 480px", gap: "16px", alignItems: "start" }}>

        {/* Pools table */}
        <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: "10px", overflow: "hidden" }}>
          <div style={{ padding: "10px 14px", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <span style={{ fontFamily: "var(--font-display)", fontWeight: 600, fontSize: "13px" }}>Liquidity Pools</span>
            <span style={{ color: "var(--text-muted)", fontSize: "11px" }}>
              {loadingPools ? "Loading…" : `${pools.length} pools · click row to view chart`}
            </span>
          </div>
          {loadingPools ? (
            <div style={{ padding: "32px", textAlign: "center", color: "var(--text-muted)", fontSize: "13px" }}>Loading pools…</div>
          ) : pools.length === 0 ? (
            <div style={{ padding: "32px", textAlign: "center", color: "var(--text-muted)", fontSize: "13px" }}>No pools found</div>
          ) : (
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "12px" }}>
                <thead>
                  <tr>
                    <PoolTH left sortKey="dex_name" activeSort={poolSortKey} sortDir={poolSortDir} onSort={handlePoolSort}>Pool / DEX</PoolTH>
                    <PoolTH sortKey="price_usd" activeSort={poolSortKey} sortDir={poolSortDir} onSort={handlePoolSort}>Price</PoolTH>
                    <PoolTH sortKey="txns_h24" activeSort={poolSortKey} sortDir={poolSortDir} onSort={handlePoolSort}>Txs (B/S) · 5m→24h</PoolTH>
                    <PoolTH sortKey="liquidity_usd" activeSort={poolSortKey} sortDir={poolSortDir} onSort={handlePoolSort}>Liquidity</PoolTH>
                    <PoolTH sortKey="volume_h24" activeSort={poolSortKey} sortDir={poolSortDir} onSort={handlePoolSort}>Vol 5m / 24h</PoolTH>
                    <PoolTH sortKey="price_change_h24" activeSort={poolSortKey} sortDir={poolSortDir} onSort={handlePoolSort}>Δ24h</PoolTH>
                    <PoolTH>Revenue</PoolTH>
                  </tr>
                </thead>
                <tbody>
                  {sortedPools.map((pool, i) => (
                    <PoolRow
                      key={pool.pool_address || `pool-${i}`}
                      pool={pool}
                      selected={selectedPool?.pool_address === pool.pool_address}
                      onClick={() => setSelectedPool(pool)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Chart panel */}
        <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: "10px", overflow: "hidden", position: "sticky", top: "8px" }}>
          {/* Tabs + controls */}
          <div style={{ padding: "0 14px", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <div style={{ display: "flex" }}>
              {(["volume", "tx"] as const).map(tab => (
                <button key={tab} onClick={() => setChartTab(tab)} style={{
                  padding: "10px 12px", border: "none", cursor: "pointer", fontFamily: "var(--font-sans)", fontSize: "12px", fontWeight: 600,
                  borderBottom: chartTab === tab ? "2px solid var(--accent-blue)" : "2px solid transparent",
                  background: "none", color: chartTab === tab ? "var(--accent-blue)" : "var(--text-secondary)",
                  transition: "color 150ms",
                }}>
                  {tab === "volume" ? "Volume" : "Tx Count"}
                </button>
              ))}
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
              <IntervalToggle value={interval} onChange={setIntervalVal} />
              <button onClick={() => { forceRefreshRef.current = true; setRefreshKey(k => k + 1); }} disabled={loadingChart} title="Refresh" style={{ background: "none", border: "none", cursor: loadingChart ? "default" : "pointer", color: "var(--text-muted)", padding: "2px", opacity: loadingChart ? 0.5 : 1 }}>
                <RefreshCw size={12} style={{ animation: loadingChart ? "spin 1s linear infinite" : "none" }} />
              </button>
            </div>
          </div>

          {/* Pool label */}
          {selectedPool && (
            <div style={{ padding: "6px 14px 0", color: "var(--text-muted)", fontSize: "10px" }}>
              {selectedPool.dex_name} · {selectedPool.base_token_symbol}/{selectedPool.quote_token_symbol} · {shortAddr(selectedPool.pool_address)}
            </div>
          )}

          {/* Chart body */}
          <div style={{ padding: "10px 8px 0" }}>
            {!selectedPool ? (
              <div style={{ height: 220, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-muted)", fontSize: "12px" }}>
                Select a pool to view chart
              </div>
            ) : loadingChart ? (
              <div style={{ height: 220, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-muted)", fontSize: "12px" }}>Loading…</div>
            ) : chartError ? (
              <div style={{ height: 220, display: "flex", alignItems: "center", justifyContent: "center", flexDirection: "column", gap: "8px" }}>
                {chartError.startsWith("No chart") ? (
                  <span style={{ color: "var(--text-muted)", fontSize: "12px" }}>{chartError}</span>
                ) : (
                  <>
                    <span style={{ color: "var(--accent-red)", fontSize: "12px" }}>Failed to load chart data</span>
                    <span style={{ color: "var(--text-muted)", fontSize: "11px" }}>{chartError}</span>
                    <button onClick={() => setRefreshKey(k => k + 1)} style={{ marginTop: "4px", padding: "4px 12px", fontSize: "11px", background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: "5px", color: "var(--text-secondary)", cursor: "pointer" }}>
                      Retry
                    </button>
                  </>
                )}
              </div>
            ) : chartTab === "volume" ? (
              volChartData.length === 0 ? (
                <div style={{ height: 220, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-muted)", fontSize: "12px" }}>No data</div>
              ) : (
                <ResponsiveContainer width="100%" height={220}>
                  <BarChart data={volChartData} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                    <CartesianGrid vertical={false} stroke="rgba(255,255,255,0.04)" />
                    <XAxis dataKey="time" tick={{ fill: "var(--text-muted)", fontSize: 9, fontFamily: "var(--font-mono)" }} tickLine={false} axisLine={false} interval="preserveStartEnd" />
                    <YAxis tick={{ fill: "var(--text-muted)", fontSize: 9, fontFamily: "var(--font-mono)" }} tickLine={false} axisLine={false} tickFormatter={(v: number) => formatVolume(v).replace("$", "")} width={42} />
                    <Tooltip content={<VolTooltip />} cursor={{ fill: "rgba(255,255,255,0.05)" }} />
                    <Bar dataKey="volume" name="Volume" fill="var(--accent-cyan)" radius={[2, 2, 0, 0]} maxBarSize={16} />
                  </BarChart>
                </ResponsiveContainer>
              )
            ) : (
              txChartData.length === 0 ? (
                <div style={{ height: 220, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-muted)", fontSize: "12px" }}>No tx data</div>
              ) : (
                <ResponsiveContainer width="100%" height={220}>
                  <BarChart data={txChartData} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                    <CartesianGrid vertical={false} stroke="rgba(255,255,255,0.04)" />
                    <XAxis dataKey="time" tick={{ fill: "var(--text-muted)", fontSize: 9, fontFamily: "var(--font-mono)" }} tickLine={false} axisLine={false} interval="preserveStartEnd" />
                    <YAxis tick={{ fill: "var(--text-muted)", fontSize: 9, fontFamily: "var(--font-mono)" }} tickLine={false} axisLine={false} width={32} />
                    <Tooltip content={<TxTooltip />} cursor={{ fill: "rgba(255,255,255,0.05)" }} />
                    <Legend wrapperStyle={{ fontSize: "10px", paddingTop: "4px" }} />
                    <Bar dataKey="buys" name="Buys" stackId="a" fill="#22c55e" radius={[0, 0, 0, 0]} maxBarSize={16} />
                    <Bar dataKey="sells" name="Sells" stackId="a" fill="#ef4444" radius={[2, 2, 0, 0]} maxBarSize={16} />
                  </BarChart>
                </ResponsiveContainer>
              )
            )}
          </div>

          <div style={{ padding: "4px 14px 10px", color: "var(--text-muted)", fontSize: "10px" }}>
            {chartTab === "volume" ? "Volume via GeckoTerminal OHLCV" : "Tx from ~3h of trades"} · {interval} candles
          </div>
        </div>
      </div>

      <style>{`
        @keyframes spin { to { transform: rotate(360deg); } }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
      `}</style>
    </div>
  );
}
