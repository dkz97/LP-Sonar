"""
OKX OnchainOS MCP client.
Protocol: MCP Streamable HTTP (JSON-RPC 2.0 over HTTP POST)
"""
from __future__ import annotations
import asyncio
import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
_request_id = 0


def _next_id() -> int:
    global _request_id
    _request_id += 1
    return _request_id


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=settings.okx_mcp_url,
            headers={
                "OK-ACCESS-KEY": settings.okx_access_key,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
    return _client


async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
    """Call an OKX OnchainOS MCP tool by name with given arguments.

    Returns the parsed result content, or raises on error.
    """
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": arguments,
        },
        "id": _next_id(),
    }
    client = get_client()
    try:
        resp = await client.post("", json=payload)
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPStatusError as e:
        logger.error("MCP HTTP error %s: %s", e.response.status_code, e.response.text[:200])
        raise
    except Exception as e:
        logger.error("MCP request failed: %s", e)
        raise

    if "error" in body:
        logger.error("MCP tool error for %s: %s", name, body["error"])
        raise RuntimeError(f"MCP error: {body['error']}")

    result = body.get("result", {})
    # MCP returns content as list of text blocks
    content = result.get("content", [])
    if content and isinstance(content, list) and content[0].get("type") == "text":
        import json
        try:
            return json.loads(content[0]["text"])
        except (json.JSONDecodeError, KeyError):
            return content[0].get("text", result)
    return result


async def get_token_ranking(
    chain_index: str,
    sort_by: str = "5",   # 2=price volatility, 5=volume, 6=market cap
    time_frame: str = "4",  # 1=5min, 2=1h, 3=4h, 4=24h
) -> list[dict]:
    """Fetch top 100 tokens by ranking for a given chain.

    Note: API returns max 100 results per call.
    chains param is plural and required (different from other endpoints).
    """
    result = await call_tool("dex-okx-market-token-ranking", {
        "chains": chain_index,
        "sortBy": sort_by,
        "timeFrame": time_frame,
    })
    if isinstance(result, dict):
        return result.get("data", result.get("tokens", []))
    return result if isinstance(result, list) else []


async def get_hot_tokens(
    chain_index: str,
    ranking_type: str = "4",  # 4=Trending score, 5=Twitter mentions
    rank_by: str = "5",       # 5=volume USD
    time_frame: str = "4",    # 1=5min, 2=1h, 3=4h, 4=24h
) -> list[dict]:
    """Fetch trending/hot tokens for a chain."""
    result = await call_tool("dex-okx-market-token-hot-token", {
        "rankingType": ranking_type,
        "chainIndex": chain_index,
        "rankBy": rank_by,
        "rankingTimeFrame": time_frame,
    })
    if isinstance(result, dict):
        return result.get("data", result.get("tokens", []))
    return result if isinstance(result, list) else []


async def get_token_price_info_batch(
    chain_index: str,
    token_addresses: list[str],
) -> list[dict]:
    """Batch fetch price + volume info for up to 100 tokens.

    The API expects an `items` array of {chainIndex, tokenContractAddress} objects.
    """
    items = [
        {"chainIndex": chain_index, "tokenContractAddress": addr}
        for addr in token_addresses[:100]
    ]
    result = await call_tool("dex-okx-market-token-price-info", {"items": items})
    if isinstance(result, dict):
        return result.get("data", result.get("tokens", []))
    return result if isinstance(result, list) else []


async def get_token_liquidity(chain_index: str, token_address: str) -> list[dict]:
    """Get top 5 liquidity pools for a token."""
    result = await call_tool("dex-okx-market-token-liquidity", {
        "chainIndex": chain_index,
        "tokenContractAddress": token_address,
    })
    if isinstance(result, dict):
        return result.get("data", result.get("pools", []))
    return result if isinstance(result, list) else []


async def get_token_advanced_info(chain_index: str, token_address: str) -> dict:
    """Get safety info: LP burn, mint authority, freeze authority, risk level."""
    result = await call_tool("dex-okx-market-token-advanced-info", {
        "chainIndex": chain_index,
        "tokenContractAddress": token_address,
    })
    if isinstance(result, dict):
        return result.get("data", result)
    return {}


async def get_recent_trades(
    chain_index: str,
    token_address: str,
    limit: int = 20,
    tag_filter: str | None = None,
) -> list[dict]:
    """Get recent DEX trades, optionally filtered by tag (KOL/SmartMoney/Sniper)."""
    args: dict[str, Any] = {
        "chainIndex": chain_index,
        "tokenContractAddress": token_address,
        "limit": str(limit),
    }
    if tag_filter:
        args["tagFilter"] = tag_filter
    result = await call_tool("dex-okx-market-trades", args)
    if isinstance(result, dict):
        return result.get("data", result.get("trades", []))
    return result if isinstance(result, list) else []


async def get_meme_token_list(
    chain_index: str,
    min_market_cap_usd: str | None = None,
    min_volume_usd: str | None = None,
    min_holders: str | None = None,
) -> list[dict]:
    """Fetch meme/pump token list (Solana-optimized) with optional filters."""
    args: dict[str, Any] = {"chainIndex": chain_index}
    if min_market_cap_usd:
        args["minMarketCapUsd"] = min_market_cap_usd
    if min_volume_usd:
        args["minVolumeUsd"] = min_volume_usd
    if min_holders:
        args["minHolders"] = min_holders
    result = await call_tool("dex-okx-market-memepump-token-list", args)
    if isinstance(result, dict):
        return result.get("data", result.get("tokens", []))
    return result if isinstance(result, list) else []
