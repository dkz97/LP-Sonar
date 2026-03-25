"use client";

import { useState } from "react";
import { BarChart2, MessageSquare, Wallet, Radio, Search, FlaskConical } from "lucide-react";
import { FocusPanel } from "@/components/FocusPanel";
import { HotTable } from "@/components/HotTable";
import { AlertFeed } from "@/components/AlertFeed";
import { LPOpportunities } from "@/components/LPOpportunities";
import { TokenDetailView } from "@/components/TokenDetailView";
import { LPAnalysis } from "@/components/LPAnalysis";
import { TokenSnapshot } from "@/lib/api";

const CHAINS = [
  { label: "All", value: undefined },
  { label: "SOL", value: "501" },
  { label: "BASE", value: "8453" },
  { label: "BSC", value: "56" },
  { label: "ETH", value: "1" },
];

type Feature = "lp-monitor" | "token-analyse" | "lp-analyse" | "ai-chat" | "portfolio";

export default function Dashboard() {
  const [activeFeature, setActiveFeature] = useState<Feature>("lp-monitor");
  const [selectedChain, setSelectedChain] = useState<string | undefined>(undefined);
  const [selectedToken, setSelectedToken] = useState<TokenSnapshot | null>(null);

  // Manual search state (independent of table-selected token)
  const [searchChain, setSearchChain] = useState("56");
  const [searchInput, setSearchInput] = useState("");
  const [manualToken, setManualToken] = useState<{ chainIndex: string; address: string } | null>(null);

  function handleViewDetails(token: TokenSnapshot) {
    setManualToken(null); // clear any manual search so the snapshot is used
    setSelectedToken(token);
    setActiveFeature("token-analyse");
    // Sync the search chain selector to the token's chain so subsequent searches use the right chain
    if (token.chain_index) setSearchChain(token.chain_index);
  }

  function handleSearch() {
    const addr = searchInput.trim();
    if (!addr) return;
    // Always treat as a fresh manual search — clear selectedToken to force key change
    setSelectedToken(null);
    setManualToken({ chainIndex: searchChain, address: addr });
  }

  // What the token-analyse tab actually shows
  const activeChainIndex = manualToken?.chainIndex ?? selectedToken?.chain_index ?? "";
  const activeAddress    = manualToken?.address    ?? selectedToken?.token_address ?? "";
  const activeSnapshot   = manualToken ? null : selectedToken;

  const FEATURES: { id: Feature; label: string; icon: React.ReactNode; available: boolean }[] = [
    { id: "lp-monitor",    label: "Token Monitor",    icon: <BarChart2 size={14} />,    available: true },
    {
      id: "token-analyse",
      label: selectedToken ? `${selectedToken.token_symbol || "Token"}` : "Token Analyse",
      icon: <Search size={14} />,
      available: true,
    },
    { id: "lp-analyse",  label: "LP 分析",   icon: <FlaskConical size={14} />, available: true },
    { id: "ai-chat",     label: "AI Chat",   icon: <MessageSquare size={14} />, available: false },
    { id: "portfolio",   label: "Portfolio", icon: <Wallet size={14} />,        available: false },
  ];

  return (
    <div className="min-h-dvh flex flex-col" style={{ background: "var(--bg-base)", color: "var(--text-primary)" }}>

      {/* ── Top feature nav ── */}
      <nav
        style={{
          borderBottom: "1px solid var(--border)",
          background: "var(--bg-surface)",
          display: "flex",
          alignItems: "center",
          paddingLeft: "16px",
          height: "40px",
          gap: "2px",
          flexShrink: 0,
        }}
      >
        {/* Logo */}
        <div
          style={{
            fontFamily: "var(--font-display)",
            fontWeight: 700,
            fontSize: "15px",
            color: "var(--text-primary)",
            paddingRight: "20px",
            display: "flex",
            alignItems: "center",
            gap: "6px",
          }}
        >
          <Radio size={14} style={{ color: "var(--accent-cyan)" }} />
          LP-Sonar
        </div>

        <div style={{ width: "1px", height: "20px", background: "var(--border)", marginRight: "8px" }} />

        {FEATURES.map((f) => (
          <button
            key={f.id}
            disabled={!f.available}
            onClick={() => f.available && setActiveFeature(f.id)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "5px",
              height: "40px",
              padding: "0 12px",
              fontSize: "12px",
              fontWeight: 500,
              fontFamily: "var(--font-sans)",
              border: "none",
              borderBottom: activeFeature === f.id
                ? "2px solid var(--accent-blue)"
                : "2px solid transparent",
              background: "none",
              cursor: f.available ? "pointer" : "not-allowed",
              color: !f.available
                ? "var(--text-muted)"
                : activeFeature === f.id
                ? "var(--accent-blue)"
                : "var(--text-secondary)",
              transition: "color 150ms, border-color 150ms",
              whiteSpace: "nowrap",
            }}
          >
            {f.icon}
            {f.label}
            {!f.available && (
              <span
                style={{
                  fontSize: "9px",
                  background: "var(--bg-elevated)",
                  color: "var(--text-muted)",
                  border: "1px solid var(--border)",
                  borderRadius: "3px",
                  padding: "0 4px",
                  lineHeight: "14px",
                  fontFamily: "var(--font-mono)",
                }}
              >
                soon
              </span>
            )}
            {/* Dot indicator on token-analyse when a token is selected */}
            {f.id === "token-analyse" && selectedToken && activeFeature !== "token-analyse" && (
              <span style={{
                width: "5px", height: "5px", borderRadius: "50%",
                background: "var(--accent-cyan)", flexShrink: 0,
              }} />
            )}
          </button>
        ))}
      </nav>

      {/* ── Sub-header ── */}
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "0 16px",
          height: "44px",
          borderBottom: "1px solid var(--border)",
          background: "var(--bg-surface)",
          flexShrink: 0,
          gap: "12px",
        }}
      >
        {/* Left: live indicator */}
        <div style={{ display: "flex", alignItems: "center", gap: "8px", flexShrink: 0 }}>
          <span style={{
            width: "7px", height: "7px", borderRadius: "50%",
            background: "var(--accent-green)", display: "inline-block",
            boxShadow: "0 0 6px var(--accent-green)",
            animation: "pulse 2s ease-in-out infinite",
          }} />
          <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>Live</span>
          <span style={{ color: "var(--text-muted)", fontSize: "12px" }}>·</span>
          <span style={{ color: "var(--text-muted)", fontSize: "12px", fontFamily: "var(--font-mono)" }}>
            30s / 60s refresh
          </span>
        </div>

        {/* Right: context controls per feature */}
        {activeFeature === "lp-analyse" ? (
          <span style={{ fontSize: "12px", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
            输入 LP 池合约地址 · 按需分析 · 结果缓存 5 分钟
          </span>
        ) : activeFeature === "token-analyse" ? (
          <form
            onSubmit={e => { e.preventDefault(); handleSearch(); }}
            style={{ display: "flex", alignItems: "center", gap: "6px", flex: 1, maxWidth: "640px", marginLeft: "auto" }}
          >
            {/* Chain selector */}
            <select
              value={searchChain}
              onChange={e => setSearchChain(e.target.value)}
              style={{
                background: "var(--bg-elevated)",
                border: "1px solid var(--border)",
                borderRadius: "6px",
                color: "var(--text-primary)",
                fontSize: "11px",
                fontFamily: "var(--font-mono)",
                fontWeight: 600,
                padding: "0 8px",
                height: "28px",
                cursor: "pointer",
                outline: "none",
                flexShrink: 0,
              }}
            >
              {CHAINS.filter(c => c.value).map(c => (
                <option key={c.value} value={c.value}>{c.label}</option>
              ))}
            </select>

            {/* Address input */}
            <input
              type="text"
              value={searchInput}
              onChange={e => setSearchInput(e.target.value)}
              placeholder="Paste token contract address…"
              style={{
                flex: 1,
                background: "var(--bg-elevated)",
                border: "1px solid var(--border)",
                borderRadius: "6px",
                color: "var(--text-primary)",
                fontSize: "12px",
                fontFamily: "var(--font-mono)",
                padding: "0 10px",
                height: "28px",
                outline: "none",
                transition: "border-color 150ms",
              }}
              onFocus={e => (e.currentTarget.style.borderColor = "var(--accent-blue)")}
              onBlur={e => (e.currentTarget.style.borderColor = "var(--border)")}
            />

            {/* Search button */}
            <button
              type="submit"
              disabled={!searchInput.trim()}
              style={{
                display: "flex", alignItems: "center", gap: "5px",
                height: "28px", padding: "0 12px",
                background: searchInput.trim() ? "var(--accent-blue)" : "var(--bg-elevated)",
                color: searchInput.trim() ? "#fff" : "var(--text-muted)",
                border: "1px solid " + (searchInput.trim() ? "var(--accent-blue)" : "var(--border)"),
                borderRadius: "6px",
                fontSize: "12px", fontWeight: 600, fontFamily: "var(--font-sans)",
                cursor: searchInput.trim() ? "pointer" : "default",
                flexShrink: 0,
                transition: "background 150ms, color 150ms",
              }}
            >
              <Search size={12} />
              Search
            </button>
          </form>
        ) : (
          <div style={{
            display: "flex", gap: "2px", background: "var(--bg-card)",
            border: "1px solid var(--border)", borderRadius: "8px", padding: "3px",
          }}>
            {CHAINS.map((c) => (
              <button
                key={String(c.value)}
                onClick={() => setSelectedChain(c.value)}
                style={{
                  padding: "2px 10px", borderRadius: "5px", fontSize: "11px", fontWeight: 600,
                  fontFamily: "var(--font-mono)", border: "none", cursor: "pointer",
                  transition: "background 150ms, color 150ms",
                  background: selectedChain === c.value ? "var(--accent-blue)" : "none",
                  color: selectedChain === c.value ? "#fff" : "var(--text-secondary)",
                }}
              >
                {c.label}
              </button>
            ))}
          </div>
        )}
      </header>

      {/* ── Main content ── */}
      {activeFeature === "lp-analyse" ? (
        /* LP Analyse — full width, on-demand only */
        <div style={{ flex: 1, overflowY: "auto", padding: "20px 16px" }}>
          <LPAnalysis />
        </div>
      ) : activeFeature === "token-analyse" ? (
        /* Token Analyse — full width, no sidebar */
        <div style={{ flex: 1, overflowY: "auto", padding: "16px" }}>
          {activeAddress ? (
            <TokenDetailView
              key={`${activeChainIndex}:${activeAddress}`}
              chainIndex={activeChainIndex}
              address={activeAddress}
              snapshot={activeSnapshot}
            />
          ) : (
            <div style={{
              display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
              height: "300px", gap: "10px", color: "var(--text-muted)",
            }}>
              <Search size={32} style={{ opacity: 0.3 }} />
              <span style={{ fontSize: "14px" }}>Search a token or select one from Token Monitor</span>
              <span style={{ fontSize: "12px" }}>Paste a contract address in the search bar above, or click a row in the table</span>
            </div>
          )}
        </div>
      ) : (
        /* LP Monitor — two-column layout */
        <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>
          <main style={{ flex: 1, overflowY: "auto", padding: "12px", display: "flex", flexDirection: "column", gap: "12px" }}>
            <LPOpportunities
              selectedChain={selectedChain}
              onViewToken={(chainIndex, address) => {
                setSelectedToken(null);
                setManualToken({ chainIndex, address });
                setActiveFeature("token-analyse");
              }}
            />
            <FocusPanel
              selectedChain={selectedChain}
              onSelect={handleViewDetails}
              onViewDetails={handleViewDetails}
            />
            <HotTable
              selectedChain={selectedChain}
              onSelect={handleViewDetails}
              onViewDetails={handleViewDetails}
            />
          </main>
          <aside style={{ width: "260px", flexShrink: 0, borderLeft: "1px solid var(--border)", overflowY: "auto" }}>
            <AlertFeed />
          </aside>
        </div>
      )}

      <style>{`
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.4; }
        }
      `}</style>
    </div>
  );
}
