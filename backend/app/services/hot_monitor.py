"""
Hot Layer Monitor: Runs every 5 minutes.

For each chain:
1. Pull all tokens in hot:{chainIndex} sorted set
2. Batch-fetch price info (100 per API call)
3. Compute Z-Score using 48-point rolling window (4 hours of 5-min data)
4. Persist snapshot + history
5. Promote to focus if Z > threshold
6. Emit alert records
"""
from __future__ import annotations
import json
import logging
import time
import uuid
from typing import Any

import numpy as np

from app.core.config import settings
from app.core.redis_client import get_redis
from app.models.schemas import AlertType, Layer
from app.services import mcp_client

logger = logging.getLogger(__name__)

HISTORY_WINDOW = 48   # 48 × 5-min = 4 hours
MIN_WINDOW_FOR_ZSCORE = 5  # need at least 5 points before computing Z-score


def compute_z_score(current: float, history: list[float]) -> float:
    if len(history) < MIN_WINDOW_FOR_ZSCORE:
        return 0.0
    arr = np.array(history, dtype=float)
    mean = arr.mean()
    std = arr.std()
    if std < 1e-9:
        return 0.0
    return float((current - mean) / std)


async def _enrich_metadata(chain_index: str, addrs: list[str], redis) -> None:
    """Enrich tokens missing symbol, name, or logo_url via dex-okx-market-token-search.

    Runs for ALL addresses in the hot/focus set, not just those without symbols.
    Uses meta_tried:{chain}:{addr} to throttle:
      - 24h TTL when all three fields are present (no re-fetch needed)
      - 1h TTL on partial/failed result (will retry sooner)
    Limits to 15 API calls per cycle to avoid rate-limit bursts.
    """
    api_calls = 0
    for addr in addrs:
        if api_calls >= 15:
            break

        tried_key = f"meta_tried:{chain_index}:{addr}"
        if await redis.exists(tried_key):
            continue

        snap_key = f"snapshot:{chain_index}:{addr}"
        fields = await redis.hmget(snap_key, "token_symbol", "token_name", "logo_url")
        has_symbol = bool(fields[0])
        has_name = bool(fields[1])
        has_logo = bool(fields[2])

        if has_symbol and has_name and has_logo:
            # All metadata present — mark done for 24h, no API call needed
            await redis.set(tried_key, "1", ex=86400)
            continue

        # Need to fetch — mark tried with 1h TTL (retry sooner if partial)
        await redis.set(tried_key, "1", ex=3600)
        api_calls += 1

        try:
            result = await mcp_client.call_tool("dex-okx-market-token-search", {
                "chains": chain_index,
                "search": addr,
            })
            okx: dict = {}
            if isinstance(result, dict):
                data = result.get("data", [])
                okx = data[0] if isinstance(data, list) and data else result
            elif isinstance(result, list) and result:
                okx = result[0]

            sym = okx.get("tokenSymbol", "")
            name = okx.get("tokenName", "")
            logo = okx.get("tokenLogoUrl", "") or okx.get("logoUrl", "") or okx.get("logo", "")

            updates: dict = {}
            if sym and not has_symbol:
                updates["token_symbol"] = sym
            if name and not has_name:
                updates["token_name"] = name
            if logo and not has_logo:
                updates["logo_url"] = logo

            if updates:
                await redis.hset(snap_key, mapping=updates)
                # If we now have all fields, extend tried TTL to 24h
                all_found = (sym or has_symbol) and (name or has_name) and (logo or has_logo)
                if all_found:
                    await redis.set(tried_key, "1", ex=86400)
                logger.info(
                    "Metadata enriched %s/%s: sym=%s name=%s logo=%s",
                    chain_index, addr, sym or "(kept)", name or "(kept)", bool(logo or has_logo),
                )
        except Exception as e:
            logger.debug("Metadata search failed %s/%s: %s", chain_index, addr, e)


async def _process_chain(chain_index: str) -> None:
    redis = await get_redis()
    hot_key = f"hot:{chain_index}"

    token_addrs: list[str] = await redis.zrange(hot_key, 0, -1)
    if not token_addrs:
        logger.debug("Hot monitor chain=%s: empty hot set", chain_index)
        return

    # Batch-fetch price info
    all_price_data: list[dict] = []
    for i in range(0, len(token_addrs), 100):
        batch = list(token_addrs[i:i + 100])
        try:
            price_data = await mcp_client.get_token_price_info_batch(chain_index, batch)
            all_price_data.extend(price_data)
        except Exception as e:
            logger.error("Hot monitor price fetch failed chain=%s batch=%d: %s", chain_index, i, e)

    if not all_price_data:
        return

    now = int(time.time())
    alerts_to_push: list[str] = []

    pipe = redis.pipeline()
    for item in all_price_data:
        addr = item.get("tokenContractAddress", "")
        if not addr:
            continue

        try:
            vol5m = float(item.get("volume5M", 0) or 0)
            price = float(item.get("price", 0) or 0)
            pc5m = float(item.get("priceChange5M", 0) or 0)
            pc1h = float(item.get("priceChange1H", 0) or 0)
            pc4h = float(item.get("priceChange4H", 0) or 0)
            pc24h = float(item.get("priceChange24H", 0) or 0)
            vol1h = float(item.get("volume1H", 0) or 0)
            vol4h = float(item.get("volume4H", 0) or 0)
            vol24h = float(item.get("volume24H", 0) or 0)
            txs5m = int(item.get("txs5M", 0) or 0)
            symbol = item.get("tokenSymbol", "")
        except (ValueError, TypeError):
            continue

        # Rolling history
        hist_key = f"history:{chain_index}:{addr}"
        raw_hist = await redis.lrange(hist_key, 0, HISTORY_WINDOW - 1)
        history = []
        for v in raw_hist:
            try:
                history.append(float(v))
            except ValueError:
                pass

        z_score = compute_z_score(vol5m, history)

        # Update history (LPUSH = newest first, LTRIM to window size)
        pipe.lpush(hist_key, str(vol5m))
        pipe.ltrim(hist_key, 0, HISTORY_WINDOW - 1)

        # Determine current layer
        snap_key = f"snapshot:{chain_index}:{addr}"
        current_layer_raw = await redis.hget(snap_key, "layer")
        current_layer = Layer(current_layer_raw) if current_layer_raw else Layer.hot

        # Promotion / demotion logic
        new_layer = current_layer
        if z_score >= settings.hot_to_focus_z_score or abs(pc5m) >= 3.0:
            new_layer = Layer.focus
        elif current_layer == Layer.focus:
            # Check cooldown - count consecutive low-z rounds
            cd_key = f"focus_cooldown:{chain_index}:{addr}"
            if z_score < settings.focus_to_hot_z_score:
                pipe.incr(cd_key)
                pipe.expire(cd_key, 600)
                cd_count_raw = await redis.get(cd_key)
                cd_count = int(cd_count_raw or 0)
                if cd_count >= settings.focus_cooldown_rounds:
                    new_layer = Layer.hot
                    pipe.delete(cd_key)
            else:
                pipe.delete(cd_key)

        # Update hot/focus sorted sets
        if new_layer == Layer.focus:
            pipe.zadd(f"focus:{chain_index}", {addr: z_score})
            pipe.expire(f"focus:{chain_index}", 600)
            # Remove from hot set if promoted
            pipe.zrem(hot_key, addr)
        else:
            pipe.zadd(hot_key, {addr: z_score})
            if current_layer == Layer.focus:
                pipe.zrem(f"focus:{chain_index}", addr)

        # Persist snapshot (only overwrite token_symbol if non-empty to preserve
        # the symbol set by universe_scanner; price batch API doesn't return it)
        snap_mapping: dict[str, Any] = {
            "chain_index": chain_index,
            "token_address": addr,
            "price_usd": str(price),
            "price_change_5m": str(pc5m),
            "price_change_1h": str(pc1h),
            "price_change_4h": str(pc4h),
            "price_change_24h": str(pc24h),
            "volume_5m": str(vol5m),
            "volume_1h": str(vol1h),
            "volume_4h": str(vol4h),
            "volume_24h": str(vol24h),
            "txs_5m": str(txs5m),
            "z_score": str(round(z_score, 4)),
            "layer": new_layer.value,
            "updated_at": str(now),
        }
        if symbol:
            snap_mapping["token_symbol"] = symbol
        pipe.hset(snap_key, mapping=snap_mapping)
        pipe.expire(snap_key, 600)

        # Emit alert
        alert_type: AlertType | None = None
        if z_score >= settings.hot_to_focus_z_score:
            alert_type = AlertType.volume_spike
        if abs(pc5m) >= 3.0 and z_score >= 1.5:
            alert_type = AlertType.breakout

        if alert_type and current_layer != Layer.focus:
            alert = {
                "id": str(uuid.uuid4()),
                "chain_index": chain_index,
                "token_address": addr,
                "token_symbol": symbol,
                "alert_type": alert_type.value,
                "z_score": round(z_score, 4),
                "price_change_5m": pc5m,
                "volume_5m": vol5m,
                "layer": new_layer.value,
                "timestamp": now,
            }
            alerts_to_push.append(json.dumps(alert))

    await pipe.execute()

    await _enrich_metadata(chain_index, list(token_addrs), redis)

    if alerts_to_push:
        # Push alerts and cap list at 500
        await redis.lpush("alerts", *alerts_to_push)
        await redis.ltrim("alerts", 0, 499)
        logger.info("Hot monitor chain=%s: pushed %d alerts", chain_index, len(alerts_to_push))

    logger.info("Hot monitor chain=%s: processed %d tokens", chain_index, len(all_price_data))


async def run_hot_monitor() -> None:
    import asyncio
    await asyncio.gather(
        *[_process_chain(chain) for chain in settings.chain_list],
        return_exceptions=True,
    )
    logger.info("Hot monitor cycle complete")
