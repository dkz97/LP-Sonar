"use client";

import { useEffect, useState, useRef } from "react";
import {
  AlertRecord, AlertType, fetchAlerts, alertColor, chainName, formatVolume,
  strategyLabel, strategyColor, ilRiskColor, scoreColor,
} from "@/lib/api";

function timeAgo(ts: number): string {
  const diff = Math.floor(Date.now() / 1000 - ts);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

function AlertBadge({ type }: { type: AlertType }) {
  const color = alertColor(type);
  const label = type.replace(/_/g, " ");
  return (
    <span style={{
      fontSize: "10px", fontWeight: 700, fontFamily: "var(--font-mono)",
      color, letterSpacing: "0.03em",
    }}>
      {label}
    </span>
  );
}

function TokenAlertBody({ a }: { a: AlertRecord }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "6px", marginTop: "2px", fontSize: "10px", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
      {a.z_score != null && <span>Z={a.z_score.toFixed(2)}</span>}
      {a.price_change_5m != null && (
        <span style={{ color: (a.price_change_5m ?? 0) >= 0 ? "var(--accent-green)" : "var(--accent-red)" }}>
          {(a.price_change_5m ?? 0) >= 0 ? "+" : ""}{(a.price_change_5m ?? 0).toFixed(2)}%
        </span>
      )}
      {a.volume_5m != null && <span>{formatVolume(a.volume_5m)}</span>}
    </div>
  );
}

function LPAlertBody({ a }: { a: AlertRecord }) {
  const score = a.net_lp_score ?? 0;
  return (
    <div style={{ marginTop: "3px" }}>
      {/* Pair + protocol */}
      <div style={{ display: "flex", alignItems: "center", gap: "5px", fontSize: "11px" }}>
        <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>{a.pair_label || a.token_symbol}</span>
        {a.protocol && (
          <span style={{ fontSize: "9px", color: "var(--text-muted)", background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: "3px", padding: "0 4px" }}>
            {a.protocol}
          </span>
        )}
      </div>
      {/* Score bar + strategy */}
      <div style={{ display: "flex", alignItems: "center", gap: "6px", marginTop: "4px" }}>
        {/* Score bar */}
        <div style={{ flex: 1, height: "4px", background: "var(--bg-elevated)", borderRadius: "2px", overflow: "hidden" }}>
          <div style={{ width: `${score * 100}%`, height: "100%", background: scoreColor(score), borderRadius: "2px", transition: "width 300ms" }} />
        </div>
        <span style={{ fontSize: "10px", fontFamily: "var(--font-mono)", color: scoreColor(score), fontWeight: 700, flexShrink: 0 }}>
          {(score * 100).toFixed(0)}
        </span>
        {/* Strategy badge */}
        {a.strategy_type && (
          <span style={{
            fontSize: "9px", fontWeight: 700, fontFamily: "var(--font-mono)",
            color: strategyColor(a.strategy_type),
            background: `${strategyColor(a.strategy_type)}18`,
            border: `1px solid ${strategyColor(a.strategy_type)}40`,
            borderRadius: "3px", padding: "0 4px", flexShrink: 0,
          }}>
            {strategyLabel(a.strategy_type).toUpperCase()}
          </span>
        )}
      </div>
      {/* IL risk + holding */}
      {(a.il_risk_level || a.suggested_holding) && (
        <div style={{ display: "flex", gap: "6px", marginTop: "2px", fontSize: "10px", color: "var(--text-muted)" }}>
          {a.il_risk_level && (
            <span style={{ color: ilRiskColor(a.il_risk_level) }}>IL:{a.il_risk_level}</span>
          )}
          {a.suggested_holding && <span>⏱ {a.suggested_holding}</span>}
        </div>
      )}
      {/* Top reason */}
      {a.main_reasons && a.main_reasons.length > 0 && (
        <div style={{ marginTop: "2px", fontSize: "10px", color: "var(--text-muted)", lineHeight: 1.4, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {a.main_reasons[0]}
        </div>
      )}
    </div>
  );
}

export function AlertFeed() {
  const [alerts, setAlerts] = useState<AlertRecord[]>([]);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const load = async () => {
      const data = await fetchAlerts(80);
      setAlerts(data);
    };
    load();
    const id = setInterval(load, 15_000);
    return () => clearInterval(id);
  }, []);

  const lpAlerts = alerts.filter(a => a.alert_type === "LP_OPPORTUNITY" || a.alert_type === "LP_RISK_WARN");
  const tokenAlerts = alerts.filter(a => a.alert_type !== "LP_OPPORTUNITY" && a.alert_type !== "LP_RISK_WARN");

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>

      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: "6px", padding: "8px 12px", borderBottom: "1px solid var(--border)", background: "var(--bg-surface)", flexShrink: 0 }}>
        <span style={{ fontSize: "11px", fontWeight: 600, color: "var(--text-secondary)", textTransform: "uppercase", letterSpacing: "0.06em" }}>Alerts</span>
        {alerts.length > 0 && (
          <span style={{ background: "#dc2626", color: "#fff", fontSize: "10px", borderRadius: "10px", padding: "0 6px", lineHeight: "16px", fontFamily: "var(--font-mono)", fontWeight: 700, minWidth: "20px", textAlign: "center" }}>
            {alerts.length}
          </span>
        )}
      </div>

      <div ref={listRef} style={{ flex: 1, overflowY: "auto" }}>
        {alerts.length === 0 ? (
          <p style={{ color: "var(--text-muted)", fontSize: "12px", padding: "12px" }}>No alerts yet</p>
        ) : (
          <>
            {/* LP alerts section */}
            {lpAlerts.length > 0 && (
              <>
                <div style={{ padding: "5px 12px 3px", fontSize: "9px", fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.08em", background: "rgba(6,182,212,0.05)", borderBottom: "1px solid var(--border)" }}>
                  LP Decisions
                </div>
                {lpAlerts.map(a => (
                  <div key={a.id} style={{ padding: "8px 12px", borderBottom: "1px solid var(--border)", background: "rgba(6,182,212,0.03)" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "2px" }}>
                      <AlertBadge type={a.alert_type} />
                      <div style={{ display: "flex", alignItems: "center", gap: "5px" }}>
                        <span style={{ fontSize: "9px", background: "var(--bg-elevated)", color: "var(--text-muted)", border: "1px solid var(--border)", borderRadius: "3px", padding: "0 4px", fontFamily: "var(--font-mono)" }}>
                          {chainName(a.chain_index)}
                        </span>
                        <span style={{ fontSize: "10px", color: "var(--text-muted)" }}>{timeAgo(a.timestamp)}</span>
                      </div>
                    </div>
                    <LPAlertBody a={a} />
                  </div>
                ))}
              </>
            )}

            {/* Token alerts section */}
            {tokenAlerts.length > 0 && (
              <>
                <div style={{ padding: "5px 12px 3px", fontSize: "9px", fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.08em", borderBottom: "1px solid var(--border)" }}>
                  Token Signals
                </div>
                {tokenAlerts.map(a => (
                  <div key={a.id} style={{ padding: "7px 12px", borderBottom: "1px solid var(--border)" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "1px" }}>
                      <AlertBadge type={a.alert_type} />
                      <span style={{ fontSize: "10px", color: "var(--text-muted)" }}>{timeAgo(a.timestamp)}</span>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: "5px", marginTop: "2px" }}>
                      <span style={{ fontSize: "9px", background: "var(--bg-elevated)", color: "var(--text-muted)", border: "1px solid var(--border)", borderRadius: "3px", padding: "0 4px", fontFamily: "var(--font-mono)" }}>
                        {chainName(a.chain_index)}
                      </span>
                      <span style={{ fontSize: "11px", color: "var(--text-primary)", fontWeight: 600, maxWidth: "80px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {a.token_symbol || a.token_address.slice(0, 6) + "…"}
                      </span>
                    </div>
                    <TokenAlertBody a={a} />
                  </div>
                ))}
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
