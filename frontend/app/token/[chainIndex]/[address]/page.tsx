"use client";

import { useParams } from "next/navigation";
import { ArrowLeft, ExternalLink } from "lucide-react";
import { chainName, explorerUrl } from "@/lib/api";
import { TokenDetailView, shortAddr } from "@/components/TokenDetailView";

export default function TokenDetailPage() {
  const params = useParams();
  const chainIndex = String(params.chainIndex ?? "");
  const address = String(params.address ?? "");
  const chain = chainName(chainIndex);
  const explorerLink = explorerUrl(chainIndex, address);

  return (
    <div style={{ minHeight: "100dvh", background: "var(--bg-base)", color: "var(--text-primary)", fontFamily: "var(--font-sans)" }}>
      {/* Top bar */}
      <nav style={{ display: "flex", alignItems: "center", gap: "12px", padding: "0 16px", height: "48px", borderBottom: "1px solid var(--border)", background: "var(--bg-surface)" }}>
        <button
          onClick={() => window.close()}
          style={{ display: "flex", alignItems: "center", gap: "5px", background: "none", border: "none", cursor: "pointer", color: "var(--text-secondary)", fontSize: "12px", fontFamily: "var(--font-sans)", padding: "4px 6px", borderRadius: "4px", transition: "color 150ms" }}
          onMouseEnter={e => (e.currentTarget.style.color = "var(--text-primary)")}
          onMouseLeave={e => (e.currentTarget.style.color = "var(--text-secondary)")}
        >
          <ArrowLeft size={14} />Close
        </button>
        <div style={{ width: "1px", height: "16px", background: "var(--border)" }} />
        <span style={{ fontFamily: "var(--font-mono)", fontSize: "12px", color: "var(--text-muted)" }}>{chain} · {shortAddr(address)}</span>
        <a href={explorerLink} target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent-blue)", lineHeight: 1 }}>
          <ExternalLink size={13} />
        </a>
      </nav>

      <div style={{ padding: "16px", maxWidth: "1400px", margin: "0 auto" }}>
        <TokenDetailView chainIndex={chainIndex} address={address} />
      </div>
    </div>
  );
}
