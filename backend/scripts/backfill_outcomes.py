"""
Backfill 48h outcomes for logged recommendations.

Usage (from backend/):
    python -m scripts.backfill_outcomes [--min-age-hours 48] [--dry-run]

Reads:   data/lp_validation_log.jsonl
Writes:  data/lp_outcomes.jsonl  (append, deduped by pool_address+timestamp)

For each log entry that is >=48h old and not yet in outcomes, fetches current
pool data from DexScreener and records:
  - terminal_price         : current price (best available proxy for 48h terminal)
  - is_oor_terminal        : whether terminal_price is outside [lower_price, upper_price]
  - actual_vol_48h         : DexScreener 24h rolling vol × 2 (proxy; marked as estimated)
  - actual_fee_proxy       : actual_vol_48h × fee_rate / tvl_usd (fraction per capital)
  - observation_timestamp  : ISO timestamp when this outcome was observed

Fail-safe: missing data is written as null, never raises on partial failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

_HERE = os.path.dirname(__file__)
_DATA_DIR = os.path.normpath(os.path.join(_HERE, "..", "data"))
_LOG_PATH = os.path.join(_DATA_DIR, "lp_validation_log.jsonl")
_OUT_PATH = os.path.join(_DATA_DIR, "lp_outcomes.jsonl")

# Chain index → DexScreener chainId (mirrors range_recommender.py)
_DS_CHAIN: dict[str, str] = {
    "1":    "ethereum",
    "56":   "bsc",
    "8453": "base",
    "501":  "solana",
    "137":  "polygon_pos",
}
_DEXSCREENER_BASE = "https://api.dexscreener.com"


def _load_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def _outcome_key(rec: dict) -> str:
    return f"{rec.get('pool_address', '')}|{rec.get('timestamp', '')}"


def _fetch_terminal_price(chain_index: str, pool_address: str) -> Optional[float]:
    chain_id = _DS_CHAIN.get(str(chain_index))
    if not chain_id:
        return None
    url = f"{_DEXSCREENER_BASE}/latest/dex/pairs/{chain_id}/{pool_address}"
    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
        if not pairs:
            return None
        pair = pairs[0]
        price_str = pair.get("priceUsd") or pair.get("priceNative")
        return float(price_str) if price_str else None
    except Exception as exc:
        print(f"  [warn] DexScreener fetch failed for {pool_address[:8]}: {exc}", file=sys.stderr)
        return None


def _fetch_vol_24h(chain_index: str, pool_address: str) -> Optional[float]:
    """Return current 24h rolling volume from DexScreener as a proxy for vol over 48h."""
    chain_id = _DS_CHAIN.get(str(chain_index))
    if not chain_id:
        return None
    url = f"{_DEXSCREENER_BASE}/latest/dex/pairs/{chain_id}/{pool_address}"
    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
        if not pairs:
            return None
        vol = pairs[0].get("volume") or {}
        val = vol.get("h24")
        return float(val) if val is not None else None
    except Exception:
        return None


def process_entry(log_rec: dict, dry_run: bool) -> Optional[dict]:
    pool_address = log_rec.get("pool_address", "")
    chain_index = str(log_rec.get("chain_index", ""))
    lower_price = log_rec.get("lower_price")
    upper_price = log_rec.get("upper_price")
    fee_rate = log_rec.get("fee_rate")
    tvl_usd = log_rec.get("tvl_usd") or 1.0

    print(f"  Fetching outcome for pool={pool_address[:12]}… chain={chain_index}")

    # Two separate requests (could be same data but keeps logic clear)
    terminal_price: Optional[float] = None
    actual_vol_48h: Optional[float] = None
    is_oor_terminal: Optional[bool] = None
    actual_fee_proxy: Optional[float] = None

    if not dry_run:
        terminal_price = _fetch_terminal_price(chain_index, pool_address)
        vol_24h_now = _fetch_vol_24h(chain_index, pool_address)
        # 24h rolling vol × 2 as 48h proxy (labelled "estimated")
        if vol_24h_now is not None:
            actual_vol_48h = round(vol_24h_now * 2.0, 2)

    if terminal_price is not None and lower_price is not None and upper_price is not None:
        is_oor_terminal = not (lower_price <= terminal_price <= upper_price)

    if actual_vol_48h is not None and fee_rate is not None and tvl_usd > 0:
        # Fraction of capital earned as fees over 48h (rough proxy)
        actual_fee_proxy = round(actual_vol_48h * fee_rate / tvl_usd, 6)

    return {
        # Link back to the original recommendation
        "pool_address": pool_address,
        "recommendation_timestamp": log_rec.get("timestamp"),
        # Observed fields
        "observation_timestamp": datetime.now(timezone.utc).isoformat(),
        "terminal_price": terminal_price,
        "is_oor_terminal": is_oor_terminal,
        "actual_vol_48h": actual_vol_48h,
        "actual_vol_48h_note": "estimated: DexScreener h24 × 2",
        "actual_fee_proxy": actual_fee_proxy,
        # Carry-through for analysis joins
        "lower_price": lower_price,
        "upper_price": upper_price,
        "expected_fee_apr": log_rec.get("expected_fee_apr"),
        "breach_probability": log_rec.get("breach_probability"),
        "history_tier": log_rec.get("history_tier"),
        "recommendation_confidence": log_rec.get("recommendation_confidence"),
        "regime": log_rec.get("regime"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill 48h LP outcomes")
    parser.add_argument("--min-age-hours", type=float, default=48.0,
                        help="Only process log entries older than this many hours (default 48)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip DexScreener fetches; write null outcome records for testing")
    args = parser.parse_args()

    logs = _load_jsonl(_LOG_PATH)
    if not logs:
        print(f"No log records found at {_LOG_PATH}")
        return

    existing_outcomes = _load_jsonl(_OUT_PATH)
    done_keys = {_outcome_key(r) for r in existing_outcomes}

    now_ts = time.time()
    cutoff_ts = now_ts - args.min_age_hours * 3600.0

    pending = []
    for rec in logs:
        key = _outcome_key(rec)
        if key in done_keys:
            continue
        try:
            rec_ts = datetime.fromisoformat(rec["timestamp"]).timestamp()
        except (KeyError, ValueError):
            continue
        if rec_ts <= cutoff_ts:
            pending.append(rec)

    print(f"Total log records : {len(logs)}")
    print(f"Already have outcomes: {len(done_keys)}")
    print(f"Pending (>= {args.min_age_hours}h old): {len(pending)}")

    if not pending:
        print("Nothing to backfill.")
        return

    os.makedirs(_DATA_DIR, exist_ok=True)
    written = 0
    with open(_OUT_PATH, "a", encoding="utf-8") as fh:
        for rec in pending:
            try:
                outcome = process_entry(rec, dry_run=args.dry_run)
                if outcome is not None:
                    fh.write(json.dumps(outcome) + "\n")
                    written += 1
            except Exception as exc:
                print(f"  [error] skipping {rec.get('pool_address','?')[:8]}: {exc}", file=sys.stderr)

    print(f"Written {written} outcome records to {_OUT_PATH}")


if __name__ == "__main__":
    main()
