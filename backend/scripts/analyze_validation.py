"""
Minimal validation analysis.

Usage (from backend/):
    python -m scripts.analyze_validation

Reads:
    data/lp_validation_log.jsonl
    data/lp_outcomes.jsonl

Outputs text tables to stdout:
    1. Overall summary
    2. fee_ratio distribution  (actual / predicted)
    3. Breach prediction accuracy (predicted breach_prob vs actual OOR)
    4. Breakdown by history_tier
    5. Breakdown by confidence band
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from typing import Optional

_HERE = os.path.dirname(__file__)
_DATA_DIR = os.path.normpath(os.path.join(_HERE, "..", "data"))
_LOG_PATH = os.path.join(_DATA_DIR, "lp_validation_log.jsonl")
_OUT_PATH = os.path.join(_DATA_DIR, "lp_outcomes.jsonl")


# ── I/O helpers ──────────────────────────────────────────────────────────────

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


def _join(logs: list[dict], outcomes: list[dict]) -> list[dict]:
    """Join on pool_address + recommendation_timestamp."""
    key_map: dict[str, dict] = {}
    for o in outcomes:
        k = f"{o.get('pool_address', '')}|{o.get('recommendation_timestamp', '')}"
        key_map[k] = o

    joined = []
    for l in logs:
        k = f"{l.get('pool_address', '')}|{l.get('timestamp', '')}"
        o = key_map.get(k)
        if o:
            joined.append({**l, **{f"out_{kk}": vv for kk, vv in o.items()}})
    return joined


# ── Stats helpers ─────────────────────────────────────────────────────────────

def _safe(val, default=None):
    return val if val is not None else default


def _mean(vals: list[float]) -> Optional[float]:
    clean = [v for v in vals if v is not None]
    return sum(clean) / len(clean) if clean else None


def _fmt(val, fmt=".3f") -> str:
    if val is None:
        return "  n/a"
    return format(val, fmt)


def _pct(val) -> str:
    if val is None:
        return "  n/a"
    return f"{val * 100:.1f}%"


# ── Table printers ────────────────────────────────────────────────────────────

def _print_sep(width=72):
    print("─" * width)


def _print_overall(rows: list[dict]) -> None:
    n = len(rows)
    fee_ratios = [
        r["out_actual_fee_proxy"] / (r["expected_fee_apr"] / 365 / 100 * 2)
        for r in rows
        if r.get("out_actual_fee_proxy") is not None
        and r.get("expected_fee_apr") and r["expected_fee_apr"] > 0
    ]
    actual_oors = [r["out_is_oor_terminal"] for r in rows if r.get("out_is_oor_terminal") is not None]
    pred_breaches = [r.get("breach_probability") for r in rows if r.get("breach_probability") is not None]

    print("\n=== OVERALL SUMMARY ===")
    _print_sep()
    print(f"  Joined records             : {n}")
    print(f"  With terminal price        : {sum(1 for r in rows if r.get('out_terminal_price'))}")
    print(f"  With actual_fee_proxy      : {sum(1 for r in rows if r.get('out_actual_fee_proxy') is not None)}")
    print(f"  With OOR observation       : {len(actual_oors)}")
    if fee_ratios:
        print(f"  Mean fee_ratio             : {_fmt(_mean(fee_ratios))}  (actual/predicted)")
        print(f"  Median fee_ratio           : {_fmt(sorted(fee_ratios)[len(fee_ratios)//2])}")
    if actual_oors:
        actual_oor_rate = sum(actual_oors) / len(actual_oors)
        pred_mean = _mean(pred_breaches) if pred_breaches else None
        print(f"  Actual OOR rate            : {_pct(actual_oor_rate)}")
        print(f"  Mean predicted breach_prob : {_pct(pred_mean)}")
    _print_sep()


def _print_fee_ratio_dist(rows: list[dict]) -> None:
    pairs = []
    for r in rows:
        afp = r.get("out_actual_fee_proxy")
        efa = r.get("expected_fee_apr")
        if afp is None or not efa or efa <= 0:
            continue
        # predicted fee over 48h window (as fraction): expected_fee_apr% / 365 * 2 days
        predicted_48h = efa / 100.0 / 365.0 * 2.0
        if predicted_48h > 0:
            pairs.append((afp / predicted_48h, r.get("history_tier", "?"), r.get("regime", "?")))

    print("\n=== FEE RATIO DISTRIBUTION (actual_fee / predicted_48h) ===")
    _print_sep()
    if not pairs:
        print("  No fee data available yet.")
        _print_sep()
        return

    buckets: dict[str, int] = {"<0.25": 0, "0.25–0.5": 0, "0.5–1.0": 0, "1.0–2.0": 0, ">2.0": 0}
    for ratio, _, _ in pairs:
        if ratio < 0.25:
            buckets["<0.25"] += 1
        elif ratio < 0.5:
            buckets["0.25–0.5"] += 1
        elif ratio < 1.0:
            buckets["0.5–1.0"] += 1
        elif ratio < 2.0:
            buckets["1.0–2.0"] += 1
        else:
            buckets[">2.0"] += 1

    total = len(pairs)
    for label, count in buckets.items():
        bar = "█" * count
        print(f"  {label:12s}  {count:3d} ({count/total*100:4.0f}%)  {bar}")
    print(f"  Total: {total}")
    _print_sep()


def _print_breach_accuracy(rows: list[dict]) -> None:
    tp = fp = tn = fn = 0
    for r in rows:
        is_oor = r.get("out_is_oor_terminal")
        bp = r.get("breach_probability")
        if is_oor is None or bp is None:
            continue
        predicted_breach = bp >= 0.5
        if predicted_breach and is_oor:
            tp += 1
        elif predicted_breach and not is_oor:
            fp += 1
        elif not predicted_breach and is_oor:
            fn += 1
        else:
            tn += 1

    total = tp + fp + tn + fn
    print("\n=== BREACH PREDICTION (threshold=0.5) ===")
    _print_sep()
    if total == 0:
        print("  No matched breach observations yet.")
        _print_sep()
        return
    print(f"  {'':20s}  Actual OOR   Actual in-range")
    print(f"  {'Predicted breach':20s}  {tp:6d}        {fp:6d}")
    print(f"  {'Predicted in-range':20s}  {fn:6d}        {tn:6d}")
    accuracy = (tp + tn) / total if total else None
    print(f"  Accuracy: {_pct(accuracy)}  (n={total})")
    _print_sep()


def _print_group(rows: list[dict], group_key: str, title: str) -> None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[str(r.get(group_key, "?"))].append(r)

    print(f"\n=== {title} ===")
    _print_sep()
    header = f"  {'Group':18s}  {'n':>3}  {'OOR%':>6}  {'pred_breach':>11}  {'fee_ratio':>9}"
    print(header)
    _print_sep()
    for grp in sorted(groups):
        grp_rows = groups[grp]
        oors = [r["out_is_oor_terminal"] for r in grp_rows if r.get("out_is_oor_terminal") is not None]
        bps  = [r["breach_probability"] for r in grp_rows if r.get("breach_probability") is not None]
        fee_ratios = []
        for r in grp_rows:
            afp = r.get("out_actual_fee_proxy")
            efa = r.get("expected_fee_apr")
            if afp is not None and efa and efa > 0:
                pred_48h = efa / 100.0 / 365.0 * 2.0
                if pred_48h > 0:
                    fee_ratios.append(afp / pred_48h)
        oor_rate = _mean([float(v) for v in oors]) if oors else None
        bp_mean  = _mean(bps) if bps else None
        fr_mean  = _mean(fee_ratios) if fee_ratios else None
        print(f"  {grp:18s}  {len(grp_rows):>3}  {_pct(oor_rate):>6}  {_pct(bp_mean):>11}  {_fmt(fr_mean):>9}")
    _print_sep()


def _print_confidence_bands(rows: list[dict]) -> None:
    bands = {"high (≥0.7)": [], "mid (0.4–0.7)": [], "low (<0.4)": []}
    for r in rows:
        c = r.get("recommendation_confidence")
        if c is None:
            continue
        if c >= 0.7:
            bands["high (≥0.7)"].append(r)
        elif c >= 0.4:
            bands["mid (0.4–0.7)"].append(r)
        else:
            bands["low (<0.4)"].append(r)

    print("\n=== BY CONFIDENCE BAND ===")
    _print_sep()
    header = f"  {'Band':16s}  {'n':>3}  {'OOR%':>6}  {'pred_breach':>11}  {'fee_ratio':>9}"
    print(header)
    _print_sep()
    for band, grp_rows in bands.items():
        if not grp_rows:
            print(f"  {band:16s}  {'0':>3}  {'n/a':>6}  {'n/a':>11}  {'n/a':>9}")
            continue
        oors = [r["out_is_oor_terminal"] for r in grp_rows if r.get("out_is_oor_terminal") is not None]
        bps  = [r["breach_probability"] for r in grp_rows if r.get("breach_probability") is not None]
        fee_ratios = []
        for r in grp_rows:
            afp = r.get("out_actual_fee_proxy")
            efa = r.get("expected_fee_apr")
            if afp is not None and efa and efa > 0:
                pred_48h = efa / 100.0 / 365.0 * 2.0
                if pred_48h > 0:
                    fee_ratios.append(afp / pred_48h)
        oor_rate = _mean([float(v) for v in oors]) if oors else None
        bp_mean  = _mean(bps) if bps else None
        fr_mean  = _mean(fee_ratios) if fee_ratios else None
        print(f"  {band:16s}  {len(grp_rows):>3}  {_pct(oor_rate):>6}  {_pct(bp_mean):>11}  {_fmt(fr_mean):>9}")
    _print_sep()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    logs = _load_jsonl(_LOG_PATH)
    outcomes = _load_jsonl(_OUT_PATH)

    print(f"Loaded {len(logs)} log records, {len(outcomes)} outcome records")

    if not logs:
        print(f"No log records at {_LOG_PATH}  — run a recommendation first.")
        sys.exit(0)

    if not outcomes:
        print(f"No outcome records at {_OUT_PATH}  — run backfill_outcomes.py first.")
        # Still show log summary
        print(f"\nLog-only summary ({len(logs)} recommendations, no outcomes yet):")
        tiers = defaultdict(int)
        for r in logs:
            tiers[r.get("history_tier", "?")] += 1
        for t, c in sorted(tiers.items()):
            print(f"  {t}: {c}")
        sys.exit(0)

    rows = _join(logs, outcomes)
    print(f"Joined records: {len(rows)}")

    if not rows:
        print("No matching records between log and outcomes.")
        sys.exit(0)

    _print_overall(rows)
    _print_fee_ratio_dist(rows)
    _print_breach_accuracy(rows)
    _print_group(rows, "history_tier", "BY HISTORY TIER")
    _print_group(rows, "regime", "BY REGIME")
    _print_confidence_bands(rows)


if __name__ == "__main__":
    main()
