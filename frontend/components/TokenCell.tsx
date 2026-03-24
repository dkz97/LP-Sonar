"use client";

import { ExternalLink, Copy, Check, BarChart2 } from "lucide-react";
import { useState, useRef, useEffect } from "react";
import { chainName, explorerUrl } from "@/lib/api";

interface Props {
  chainIndex: string;
  address: string;
  symbol: string;
  name?: string;
  logoUrl?: string;
  /** Called when "View Details" is clicked. If absent, falls back to opening a new tab. */
  onViewDetails?: () => void;
}

function shortAddr(addr: string) {
  if (addr.length <= 12) return addr;
  return addr.slice(0, 6) + "…" + addr.slice(-4);
}

export function TokenCell({ chainIndex, address, symbol, name, logoUrl, onViewDetails }: Props) {
  const [copied, setCopied] = useState(false);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState({ top: 0, left: 0 });
  const triggerRef = useRef<HTMLDivElement>(null);
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  function handleMouseEnter() {
    if (hideTimer.current) clearTimeout(hideTimer.current);
    if (triggerRef.current) {
      const rect = triggerRef.current.getBoundingClientRect();
      setPos({ top: rect.bottom + 4, left: rect.left });
    }
    setOpen(true);
  }

  function handleMouseLeave() {
    hideTimer.current = setTimeout(() => setOpen(false), 100);
  }

  // 滚动或 resize 时关闭
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  function copyAddress(e: React.MouseEvent) {
    e.stopPropagation();
    navigator.clipboard.writeText(address).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }

  const chain = chainName(chainIndex);
  const url = explorerUrl(chainIndex, address);
  const displayName = symbol || shortAddr(address);
  const avatarLetter = (symbol || "?").charAt(0).toUpperCase();

  return (
    <>
      <div
        ref={triggerRef}
        className="relative flex items-center gap-1.5"
        onMouseEnter={handleMouseEnter}
        onMouseLeave={handleMouseLeave}
      >
        {/* Token logo / letter avatar */}
        {logoUrl ? (
          <img
            src={logoUrl}
            alt={symbol}
            width={18}
            height={18}
            style={{ borderRadius: "50%", flexShrink: 0, objectFit: "cover" }}
            onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
          />
        ) : (
          <div style={{
            width: "18px", height: "18px", borderRadius: "50%", flexShrink: 0,
            background: `hsl(${avatarLetter.charCodeAt(0) * 47 % 360}, 55%, 32%)`,
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: "9px", fontWeight: 700, color: "#fff", fontFamily: "var(--font-mono)",
          }}>
            {avatarLetter}
          </div>
        )}

        {/* Chain badge */}
        <span
          style={{
            background: "var(--bg-elevated)",
            color: "var(--text-secondary)",
            border: "1px solid var(--border)",
            borderRadius: "4px",
            padding: "1px 5px",
            fontSize: "10px",
            fontFamily: "var(--font-mono)",
            flexShrink: 0,
          }}
        >
          {chain}
        </span>

        {/* Symbol */}
        <span
          style={{
            fontFamily: "var(--font-display)",
            fontWeight: 600,
            color: "var(--text-primary)",
            maxWidth: "100px",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {displayName}
        </span>
      </div>

      {/* Tooltip — fixed 定位，脱离所有 overflow 容器 */}
      {open && (
        <div
          onMouseEnter={handleMouseEnter}
          onMouseLeave={handleMouseLeave}
          style={{
            position: "fixed",
            top: pos.top,
            left: pos.left,
            background: "var(--bg-elevated)",
            border: "1px solid var(--border-light)",
            borderRadius: "8px",
            padding: "10px 12px",
            minWidth: "260px",
            maxWidth: "320px",
            boxShadow: "0 8px 32px rgba(0,0,0,0.6)",
            zIndex: 9999,
          }}
        >
          {/* Token name */}
          {name && name !== symbol && (
            <div style={{ color: "var(--text-primary)", fontWeight: 600, marginBottom: "6px", fontSize: "13px" }}>
              {name}
            </div>
          )}

          {/* Symbol + chain */}
          <div style={{ display: "flex", alignItems: "center", gap: "6px", marginBottom: "8px" }}>
            <span style={{ color: "var(--accent-cyan)", fontFamily: "var(--font-mono)", fontWeight: 600 }}>
              {symbol}
            </span>
            <span style={{ color: "var(--text-muted)", fontSize: "11px" }}>on {chain}</span>
          </div>

          {/* Contract address */}
          <div style={{ marginBottom: "10px" }}>
            <div style={{ color: "var(--text-muted)", fontSize: "10px", marginBottom: "3px", textTransform: "uppercase", letterSpacing: "0.05em" }}>
              Contract
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
              <span
                style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: "11px",
                  color: "var(--text-secondary)",
                  wordBreak: "break-all",
                  flex: 1,
                }}
              >
                {address}
              </span>
              <button
                onClick={copyAddress}
                title="Copy address"
                style={{
                  background: "none",
                  border: "none",
                  cursor: "pointer",
                  color: copied ? "var(--accent-green)" : "var(--text-muted)",
                  padding: "2px",
                  flexShrink: 0,
                  transition: "color 150ms",
                }}
              >
                {copied ? <Check size={12} /> : <Copy size={12} />}
              </button>
              <a
                href={url}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => e.stopPropagation()}
                title="Open in explorer"
                style={{
                  color: "var(--accent-blue)",
                  flexShrink: 0,
                  lineHeight: 1,
                  transition: "color 150ms",
                }}
              >
                <ExternalLink size={12} />
              </a>
            </div>
          </div>

          {/* View Details button */}
          <button
            onClick={(e) => {
              e.stopPropagation();
              if (onViewDetails) {
                onViewDetails();
              } else {
                window.open(`/token/${chainIndex}/${address}`, "_blank");
              }
              setOpen(false);
            }}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: "5px",
              width: "100%",
              padding: "6px 0",
              borderRadius: "6px",
              background: "var(--accent-blue)",
              color: "#fff",
              fontSize: "12px",
              fontWeight: 600,
              fontFamily: "var(--font-sans)",
              border: "none",
              cursor: "pointer",
              transition: "opacity 150ms",
            }}
            onMouseEnter={(e) => (e.currentTarget.style.opacity = "0.85")}
            onMouseLeave={(e) => (e.currentTarget.style.opacity = "1")}
          >
            <BarChart2 size={12} />
            View Details
          </button>
        </div>
      )}
    </>
  );
}
