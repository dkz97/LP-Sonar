import json
import logging
import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # OKX MCP
    okx_mcp_url: str = "https://web3.okx.com/api/v1/onchainos-mcp"
    okx_access_key: str = ""

    # OKX CEX
    okx_cex_base_url: str = "https://www.okx.com"

    # Solana DEX APIs (direct protocol integration)
    meteora_api_url: str = "https://dlmm-api.meteora.ag"
    meteora_damm_api_url: str = "https://damm-v2.datapi.meteora.ag"
    raydium_api_url: str = "https://api-v3.raydium.io"

    # DEX Screener (cross-chain pool discovery, 300 req/min, no auth)
    dexscreener_api_url: str = "https://api.dexscreener.com"

    # Public EVM RPC endpoints for protocol-native fee tier (P2.1.2 fee tier via eth_call)
    # Defaults are free public nodes — override in .env for higher rate limits or private RPCs.
    # Set to empty string to disable RPC fee fetch for that chain (falls back to static lookup).
    eth_rpc_url: str = "https://ethereum.publicnode.com"
    base_rpc_url: str = "https://base.publicnode.com"
    polygon_rpc_url: str = "https://polygon-bor.publicnode.com"
    bsc_rpc_url: str = "https://bsc-dataseed.bnbchain.org"

    # Uniswap V3 subgraph URLs (optional, secondary to RPC)
    # Set these if you have a private Graph node or a paid API key.
    # Leave empty (default) — RPC path above is used instead.
    uniswap_v3_subgraph_ethereum: str = ""
    uniswap_v3_subgraph_base: str = ""
    uniswap_v3_subgraph_polygon: str = ""

    # Uniswap V4 — The Graph subgraph URLs (optional, requires API key).
    # Format: https://gateway.thegraph.com/api/{api_key}/subgraphs/id/{subgraph_id}
    # Without this, V4 pools fall back to on-chain RPC (price/fee/liquidity only, no volume/TVL).
    uniswap_v4_subgraph_bsc: str = ""

    # DeFiLlama (TVL aggregation, optional)
    defillama_api_url: str = "https://yields.llama.fi"

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_flush_on_shutdown: bool = True

    # Scheduler intervals (seconds)
    universe_scan_interval: int = 900
    hot_poll_interval: int = 300
    focus_poll_interval: int = 60

    # Admission thresholds
    min_tvl_usd: float = 50_000
    min_volume_5m_usd: float = 10_000
    hot_to_focus_z_score: float = 2.0
    focus_to_hot_z_score: float = 1.0
    focus_cooldown_rounds: int = 3

    # Chains
    monitored_chains: str = "501,8453,56"
    universe_top_n: int = 200

    # ── Range Recommendation Engine ────────────────────────────────────
    # Utility scoring weights (must sum to 1.0 for fee; penalties are subtracted)
    range_weight_fee: float = 0.30
    range_weight_il: float = 0.25
    range_weight_breach: float = 0.25
    range_weight_rebalance: float = 0.10
    range_weight_quality: float = 0.10

    # OHLCV bars to fetch per recommendation (1H bars; 300 = ~12.5 days)
    range_ohlcv_bars: int = 300

    # Redis TTL for cached recommendations (seconds)
    range_cache_ttl: int = 300

    # ── Calibration parameters (P2.X: confidence / offline replay calibration) ──
    # Actionability thresholds: evidence_score level needed for "standard" rating.
    # growing pools require stronger evidence than mature pools.
    # Set by running scripts/calibrate.py; defaults preserve original hand-crafted values.
    calibration_growing_standard_threshold: float = 0.65
    calibration_mature_standard_threshold: float = 0.55

    # Confidence floor: recommendation_confidence values below this trigger a
    # caution downgrade + "Confidence below calibrated floor" risk_flag.
    # 0.0 = disabled (backward-compatible default).
    confidence_floor: float = 0.0

    # Regime-aware confidence scale factors (JSON dict, string for .env compat).
    # Applied multiplicatively: calibrated_conf = raw_conf × scale[regime].
    # Boundary-constrained by calibrate.py to [0.5, 1.0] per regime.
    confidence_regime_scales: str = (
        '{"range_bound":1.0,"trend_up":0.85,"trend_down":0.85,"chaotic":0.70}'
    )

    # Replay weight interpolation boundaries: evidence_score in [lo, hi] maps
    # replay_weight linearly 0 → 1.  Outside this range: clamp to 0 or 1.
    replay_weight_lower_bound: float = 0.25
    replay_weight_upper_bound: float = 0.75

    # Path to the offline calibration output file (relative to working directory).
    # Override via CALIBRATION_FILE_PATH env var if needed.
    calibration_file_path: str = "data/calibration.json"

    @property
    def chain_list(self) -> list[str]:
        return [c.strip() for c in self.monitored_chains.split(",")]

    @property
    def range_scoring_weights(self) -> dict[str, float]:
        return {
            "fee":       self.range_weight_fee,
            "il":        self.range_weight_il,
            "breach":    self.range_weight_breach,
            "rebalance": self.range_weight_rebalance,
            "quality":   self.range_weight_quality,
        }


_calibration_logger = logging.getLogger(__name__)

# Float fields that calibration.json may override.
# confidence_floor is intentionally excluded — operational parameter, ENV only.
_CALIBRATION_FLOAT_FIELDS: tuple[str, ...] = (
    "calibration_growing_standard_threshold",
    "calibration_mature_standard_threshold",
    "replay_weight_lower_bound",
    "replay_weight_upper_bound",
)


def _load_calibration_overrides(s: "Settings") -> None:
    """
    Load calibration.json and apply eligible fields to a Settings instance.

    Priority: ENV variable > calibration.json > Settings default.
    Fields already present in s.model_fields_set (set via ENV / .env) are skipped.
    confidence_floor is never overridden here.

    All errors are logged as WARNING; the function never raises.
    """
    path = s.calibration_file_path

    # ── Read JSON ──────────────────────────────────────────────────────────────
    try:
        with open(path) as fh:
            data: dict = json.load(fh)
    except FileNotFoundError:
        _calibration_logger.info(
            "calibration: file not found at %s — using settings defaults", path
        )
        return
    except json.JSONDecodeError as exc:
        _calibration_logger.warning(
            "calibration: JSON decode error in %s — using settings defaults: %s",
            path, exc,
        )
        return
    except OSError as exc:
        _calibration_logger.warning(
            "calibration: cannot read %s — using settings defaults: %s",
            path, exc,
        )
        return

    applied: list[str] = []
    skipped_env: list[str] = []

    # ── Float fields ───────────────────────────────────────────────────────────
    for field in _CALIBRATION_FLOAT_FIELDS:
        if field not in data:
            continue
        if field in s.model_fields_set:
            skipped_env.append(field)
            _calibration_logger.debug(
                "calibration: field '%s' skipped (set by env var)", field
            )
            continue
        try:
            object.__setattr__(s, field, float(data[field]))
            applied.append(field)
        except (TypeError, ValueError) as exc:
            _calibration_logger.warning(
                "calibration: field '%s' has unexpected type %s — skipped: %s",
                field, type(data[field]).__name__, exc,
            )

    # ── confidence_regime_scales: dict → JSON string ───────────────────────────
    scales_key = "confidence_regime_scales"
    if scales_key in data:
        if scales_key in s.model_fields_set:
            skipped_env.append(scales_key)
            _calibration_logger.debug(
                "calibration: field '%s' skipped (set by env var)", scales_key
            )
        else:
            raw = data[scales_key]
            if isinstance(raw, dict):
                try:
                    object.__setattr__(s, scales_key, json.dumps(raw))
                    applied.append(scales_key)
                except Exception as exc:
                    _calibration_logger.warning(
                        "calibration: failed to serialize '%s' — skipped: %s",
                        scales_key, exc,
                    )
            else:
                _calibration_logger.warning(
                    "calibration: field '%s' expected dict, got %s — skipped",
                    scales_key, type(raw).__name__,
                )

    # ── Log summary (include meta fields if present) ───────────────────────────
    meta = data.get("meta", {}) if isinstance(data.get("meta"), dict) else {}
    meta_parts: list[str] = []
    if "version" in meta:
        meta_parts.append(f"version={meta['version']}")
    if "generated_at" in meta:
        meta_parts.append(f"generated_at={meta['generated_at']}")
    if "real_samples" in meta:
        meta_parts.append(f"real_samples={meta['real_samples']}")
    meta_str = f" ({', '.join(meta_parts)})" if meta_parts else ""

    _calibration_logger.info(
        "calibration: loaded from %s — "
        "growing_threshold=%.3f mature_threshold=%.3f "
        "rw_bounds=[%.2f, %.2f] "
        "fields applied=%d, env-overrides skipped=%d%s",
        path,
        s.calibration_growing_standard_threshold,
        s.calibration_mature_standard_threshold,
        s.replay_weight_lower_bound,
        s.replay_weight_upper_bound,
        len(applied),
        len(skipped_env),
        meta_str,
    )


settings = Settings()
_load_calibration_overrides(settings)
