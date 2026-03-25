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


settings = Settings()
