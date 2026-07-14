"""
Configuration management for Polymarket Reverse Arbitrage Bot.

Loads with precedence: defaults -> YAML -> environment variables -> Fly secrets
Environment variables override YAML. Nested YAML keys map to env vars by joining
with double underscore (e.g., reverse_arb.min_edge_bps -> REVERSE_ARB__MIN_EDGE_BPS)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# =============================================================================
# REVERSE ARB CONFIG
# =============================================================================

class ReverseArbConfig(BaseModel):
    """Reverse arbitrage specific parameters (loaded from yaml/env)."""
    enabled: bool = Field(default=True, description="Enable reverse arb strategy")
    min_edge_bps: int = Field(default=200, ge=0, description="Minimum net edge in bps (2%)")
    max_slippage_bps: int = Field(default=50, ge=0, description="Max slippage per leg in bps (0.5%)")
    max_position_usd: int = Field(default=2000, ge=0, description="Max position size in USD")
    fee_bps: int = Field(default=75, ge=0, description="Taker fee per leg in bps (0.75%)")
    cheap_buy_min: float = Field(default=0.07, ge=0, le=1, description="Min cheap leg price")
    cheap_buy_max: float = Field(default=0.10, ge=0, le=1, description="Max cheap leg price")
    expensive_buy_min: float = Field(default=0.90, ge=0, le=1, description="Min expensive leg price")
    expensive_buy_max: float = Field(default=0.95, ge=0, le=1, description="Max expensive leg price")
    cheap_order_usd: int = Field(default=50, ge=0, description="Cheap leg order size USD")
    expensive_order_usd: int = Field(default=100, ge=0, description="Expensive leg order size USD")
    minutes_before_close_min: int = Field(default=2, ge=0, description="Min minutes before close")
    minutes_before_close_max: int = Field(default=5, ge=0, description="Max minutes before close")
    order_type: str = Field(default="FOK", description="FOK | GTC")
    price_levels: list[int] = Field(default_factory=lambda: [0, 1, 2], description="Tick offsets from best ask for multi-level FOK orders")
    tick_size: float = Field(default=0.001, ge=0, description="Polymarket tick size (0.001 = 0.1¢ per tick)")
    poll_interval_ms: int = Field(default=1000, ge=100, description="Polling interval ms")
    dry_run: bool = Field(default=True, description="Dry run mode")
    btc_markets_only: bool = Field(default=True, description="Only scan BTC markets")
    eth_markets_only: bool = Field(default=False, description="Only scan ETH markets")
    min_liquidity_usd: float = Field(default=500, ge=0, description="Min liquidity USD")
    max_spread_bps: int = Field(default=500, ge=0, description="Max spread bps")

    @field_validator("*", mode="before")
    @classmethod
    def parse_env(cls, v: Any) -> Any:
        if isinstance(v, str):
            v_lower = v.lower()
            if v_lower in ("true", "false"):
                return v_lower == "true"
            try:
                if "." in v:
                    return float(v)
                return int(v)
            except ValueError:
                pass
        return v


logger = logging.getLogger(__name__)


# =============================================================================
# SETTINGS (from environment)
# =============================================================================

class Settings(BaseSettings):
    """Application settings from environment variables."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent.parent.parent / "config" / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_nested_delimiter="__",
    )

    @field_validator(
        "polymarket_private_key", "polymarket_api_key", "polymarket_api_secret",
        "polymarket_api_passphrase", "polymarket_readonly_api_key", "manifold_api_key",
        "polygon_archive_rpc_url", "openrouter_api_key", "api_auth_token",
        "slack_webhook_url", "pagerduty_integration_key",
        "smtp_user", "smtp_password", "alert_email_to",
        "dd_api_key", "new_relic_license_key",
        mode="before"
    )
    @classmethod
    def empty_str_to_none(cls, v: Optional[str]) -> Optional[str]:
        """Convert empty strings to None so they don't create broken signers/clients."""
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @model_validator(mode="after")
    def _validate_required_secrets(self) -> "Settings":
        """Fail fast if live trading is configured without a key."""
        if not self.paper_trading and not self.polymarket_private_key:
            raise ValueError(
                "polymarket_private_key is REQUIRED for LIVE trading "
                "(paper_trading=False) but is not set. Refusing to start."
            )
        return self

    # Polymarket
    polymarket_private_key: Optional[str] = None
    polymarket_api_key: Optional[str] = None
    polymarket_api_secret: Optional[str] = None
    polymarket_api_passphrase: Optional[str] = None
    polymarket_readonly_api_key: Optional[str] = None

    # Other platforms
    manifold_api_key: Optional[str] = None

    # Embedding service
    openrouter_api_key: Optional[str] = None
    sentence_transformer_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    # API/MCP auth
    require_api_auth: bool = True
    api_auth_token: Optional[str] = None
    api_readonly_keys: Optional[str] = None  # Comma-separated list of read-only keys
    api_cors_origin: str = ""
    api_cors_allow_credentials: bool = False
    api_require_signature: bool = False

    # Blockchain
    polygon_rpc_url: str = "https://polygon-rpc.com"
    polygon_archive_rpc_url: Optional[str] = None

    # Database
    database_url: str = "sqlite:///./data/reverse_arb.db"
    redis_url: str = "redis://localhost:6379/0"

    # Monitoring
    slack_webhook_url: Optional[str] = None
    pagerduty_integration_key: Optional[str] = None
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    alert_email_to: Optional[str] = None
    dd_api_key: Optional[str] = None
    new_relic_license_key: Optional[str] = None

    # System
    environment: str = "development"
    log_level: str = "INFO"
    debug_mode: bool = False
    paper_trading: bool = True

    # Feature flags
    enable_reverse_arb: bool = True
    enable_cross_platform_arb: bool = False
    enable_edge_trades: bool = False
    enable_realtime_ws: bool = True
    enable_backtesting: bool = True
    enable_web_dashboard: bool = True

    # Quant model settings
    eloratings_world_tsv: str = "https://www.eloratings.net/World.tsv"
    eloratings_names_tsv: str = "https://www.eloratings.net/en.teams.tsv"

    # Cross-platform matching
    semantic_similarity_threshold: float = 0.85
    text_token_overlap_threshold: float = 0.6
    require_manual_verify: bool = True

    # Risk
    max_daily_loss_pct: float = 0.05
    max_gross_exposure_pct: float = 0.80
    max_concurrent_markets: int = 10
    max_position_pct: float = 0.15

    # Historical data
    historical_data_path: str = "./data/historical"


# =============================================================================
# NESTED CONFIG MODELS (from YAML)
# =============================================================================

class ExecutionConfig(BaseModel):
    default_slippage_tolerance_bps: int = Field(default=50, ge=0)
    emergency_slippage_tolerance_bps: int = Field(default=200, ge=0)
    order_timeout_seconds: int = Field(default=30, ge=1)
    heartbeat_interval_seconds: int = Field(default=10, ge=1)
    max_order_retries: int = Field(default=3, ge=0)
    retry_backoff_ms: int = Field(default=100, ge=0)
    price_levels: list[int] = Field(default_factory=lambda: [0, 1, 2], description="Tick offsets from best ask for multi-level FOK orders")
    tick_size: float = Field(default=0.001, ge=0, description="Polymarket tick size (0.001 = 0.1¢ per tick)")

    # Iteration 7: GTC limit order fallback after FOK levels exhausted
    gtc_fallback_enabled: bool = Field(default=True, description="Enable GTC fallback after FOK levels exhausted")
    gtc_fallback_timeout_sec: int = Field(default=5, ge=1, description="Seconds to wait for GTC fill before canceling")
    gtc_fallback_price_levels: list[int] = Field(default_factory=lambda: [0, 1, 2], description="Tick offsets for GTC fallback levels")


class MarketDataConfig(BaseModel):
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_host: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polygon_rpc_url: str = "https://polygon-rpc.com"
    chain_id: int = 137
    neg_risk: bool = True


class ManifoldConfig(BaseModel):
    manifold_api_url: str = "https://api.manifold.markets/v0"
    manifold_ws_url: str = "wss://api.manifold.markets/v0/ws"


class ScanConfig(BaseModel):
    scan_interval_ms: int = Field(default=5000, ge=100)
    opportunity_ttl_ms: int = Field(default=60000, ge=1000)
    price_staleness_threshold_ms: int = Field(default=5000, ge=100)


class LiquidityConfig(BaseModel):
    min_liquidity_usd: float = Field(default=1000, ge=0)
    min_volume_24h_usd: float = Field(default=5000, ge=0)
    min_orderbook_depth_usd: float = Field(default=500, ge=0)


class CrossPlatformConfig(BaseModel):
    semantic_similarity_threshold: float = Field(default=0.85, ge=0, le=1)
    text_token_overlap_threshold: float = Field(default=0.6, ge=0, le=1)
    embedding_cache_ttl_seconds: int = Field(default=300, ge=0)
    require_manual_verify: bool = True


class InternalArbConfig(BaseModel):
    include_internal_arb: bool = True
    internal_min_edge_bps: int = Field(default=50, ge=0)


class EdgeVsFairConfig(BaseModel):
    include_edge_trades: bool = True
    market_blend_lambda: float = Field(default=0.3, ge=0, le=1)
    min_edge_confidence: float = Field(default=0.6, ge=0, le=1)
    fair_value_sources: list[str] = ["elo", "fundamentals", "market_consensus"]


class EntityVerificationConfig(BaseModel):
    year_mismatch_penalty: float = Field(default=0.5, ge=0, le=1)
    date_mismatch_penalty: float = Field(default=0.4, ge=0, le=1)
    threshold_mismatch_penalty: float = Field(default=0.4, ge=0, le=1)
    person_team_mismatch_penalty: float = Field(default=0.3, ge=0, le=1)
    numeric_difference_penalty: float = Field(default=0.2, ge=0, le=1)
    needs_review_threshold: float = Field(default=0.5, ge=0, le=1)


class StrategyRiskLimits(BaseModel):
    max_position_usd: float = Field(default=5000, ge=0)
    max_slippage_bps: int = Field(default=20, ge=0)
    max_latency_ms: int = Field(default=100, ge=0)
    max_concurrent: int = Field(default=10, ge=0)


class RiskLimitsConfig(BaseModel):
    reverse_arb: StrategyRiskLimits = Field(default_factory=lambda: StrategyRiskLimits(
        max_position_usd=2000, max_slippage_bps=50, max_latency_ms=500, max_concurrent=5
    ))
    cross_platform: StrategyRiskLimits = Field(default_factory=lambda: StrategyRiskLimits(
        max_position_usd=3000, max_slippage_bps=50, max_latency_ms=500, max_concurrent=5
    ))
    edge_vs_fair: StrategyRiskLimits = Field(default_factory=lambda: StrategyRiskLimits(
        max_position_usd=2000, max_slippage_bps=100, max_latency_ms=1000, max_concurrent=3
    ))


class PlatformRiskScores(BaseModel):
    polymarket: int = 20
    manifold: int = 25
    betfair: int = 10
    smarkets: int = 15
    predictit: int = 30
    drift: int = 25
    metaculus: int = 35


class CorrelationMatrix(BaseModel):
    same_market_yes_no: float = Field(default=-0.95, ge=-1, le=1)
    same_event_cross_platform: float = Field(default=0.7, ge=-1, le=1)
    different_events: float = Field(default=0.0, ge=-1, le=1)


class DynamicKellyConfig(BaseModel):
    drawdown_scaling: bool = True
    max_drawdown_pct: float = Field(default=0.15, ge=0, le=1)
    drawdown_recovery_pct: float = Field(default=0.05, ge=0, le=1)
    volatility_scaling: bool = True
    max_volatility_factor: float = Field(default=2.0, ge=1)
    streak_awareness: bool = True
    losing_streak_threshold: int = Field(default=3, ge=1)
    winning_streak_threshold: int = Field(default=5, ge=1)


class MonitoringConfig(BaseModel):
    prometheus_port: int = Field(default=9090, ge=1, le=65535)
    grafana_port: int = Field(default=3000, ge=1, le=65535)
    log_level: str = "INFO"
    structured_logging: bool = True


class ApiConfig(BaseModel):
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8080, ge=1, le=65535)
    api_workers: int = Field(default=4, ge=1)


class WebConfig(BaseModel):
    web_port: int = Field(default=3001, ge=1, le=65535)
    web_host: str = "localhost"


class DatabaseConfig(BaseModel):
    database_url: str = "sqlite:///./data/reverse_arb.db"
    redis_url: str = "redis://localhost:6379/0"


class HistoricalDataConfig(BaseModel):
    historical_data_root: str = "data/historical"
    polymarket_historical_path: str = "data/historical/polymarket"
    parquet_partition_hours: int = Field(default=1, ge=1)


class BacktestConfig(BaseModel):
    initial_bankroll: float = Field(default=10000, gt=0)
    fee_model: str = "polymarket_2026"
    slippage_model: str = "orderbook_depth"
    max_position_pct: float = Field(default=0.15, ge=0, le=1)
    include_fees: bool = True
    include_slippage: bool = True
    lambda_blend: float = Field(default=0.3, ge=0, le=1)
    kelly_fraction: float = Field(default=0.25, ge=0, le=1)
    edge_threshold_bps: int = Field(default=300, ge=0)


# =============================================================================
# MAIN CONFIG MODEL
# =============================================================================

class ConfigModel(BaseModel):
    """Full configuration model from YAML."""

    version: str = "1.0.0"

    # Reverse Arb specific
    reverse_arb: ReverseArbConfig = Field(default_factory=ReverseArbConfig)

    # Core trading
    bankroll_usd: float = Field(default=10000, ge=0)
    kelly_fraction: float = Field(default=0.25, ge=0, le=1)
    edge_threshold_bps: int = Field(default=200, ge=0)
    max_position_pct: float = Field(default=0.15, ge=0, le=1)
    max_daily_loss_pct: float = Field(default=0.05, ge=0, le=1)
    max_concurrent_markets: int = Field(default=5, ge=1)
    max_gross_exposure_pct: float = Field(default=0.50, ge=0, le=1)

    # Fee models
    polymarket_taker_fee_bps: int = Field(default=75, ge=0)
    polymarket_maker_rebate_bps: int = Field(default=0, ge=0)
    manifold_taker_fee_bps: int = Field(default=100, ge=0)
    predictit_taker_fee_bps: int = Field(default=500, ge=0)
    betfair_taker_fee_bps: int = Field(default=500, ge=0)
    smarkets_taker_fee_bps: int = Field(default=200, ge=0)
    drift_taker_fee_bps: int = Field(default=100, ge=0)
    metaculus_taker_fee_bps: int = Field(default=0, ge=0)

    # Nested configs
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    market_data: MarketDataConfig = Field(default_factory=MarketDataConfig)
    manifold: ManifoldConfig = Field(default_factory=ManifoldConfig)
    scan: ScanConfig = Field(default_factory=ScanConfig)
    liquidity: LiquidityConfig = Field(default_factory=LiquidityConfig)
    cross_platform: CrossPlatformConfig = Field(default_factory=CrossPlatformConfig)
    internal_arb: InternalArbConfig = Field(default_factory=InternalArbConfig)
    edge_vs_fair: EdgeVsFairConfig = Field(default_factory=EdgeVsFairConfig)
    entity_verification: EntityVerificationConfig = Field(default_factory=EntityVerificationConfig)
    risk_limits: RiskLimitsConfig = Field(default_factory=RiskLimitsConfig)
    platform_risk_scores: PlatformRiskScores = Field(default_factory=PlatformRiskScores)
    correlation_matrix: CorrelationMatrix = Field(default_factory=CorrelationMatrix)
    dynamic_kelly: DynamicKellyConfig = Field(default_factory=DynamicKellyConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    historical_data: HistoricalDataConfig = Field(default_factory=HistoricalDataConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)


# Global config instances
_config: Optional[ConfigModel] = None
_settings: Optional[Settings] = None


# =============================================================================
# CONFIG LOADING
# =============================================================================

def load_config(config_path: Optional[Path] = None) -> ConfigModel:
    """Load configuration from YAML file with environment variable overrides.

    Precedence: defaults -> YAML -> environment variables (.env / Fly secrets)
    Environment variables use double underscore (__) for nested keys.
    Example: REVERSE_ARB__MIN_EDGE_BPS=300
    """
    global _config

    if config_path is None:
        config_path = Path(__file__).parent.parent.parent / "config" / "config.yaml"

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            logger.error(f"Failed to parse YAML config: {e}")
            raise

    if data is None:
        data = {}

    # Apply environment variable overrides using nested delimiter "__"
    env_data = _load_env_overrides()
    _merge_dict(data, env_data)

    _config = ConfigModel(**data)
    return _config


def _load_env_overrides() -> dict:
    """Load all environment variables that start with known prefixes as nested dict.

    Supports env vars like:
    - REVERSE_ARB__MIN_EDGE_BPS=300
    - RISK_LIMITS__REVERSE_ARB__MAX_POSITION_USD=10000
    - EXECUTION__ORDER_TIMEOUT_SECONDS=15
    """
    prefixes = {
        "REVERSE_ARB": "reverse_arb",
        "RISK_LIMITS": "risk_limits",
        "INTERNAL_ARB": "internal_arb",
        "EDGE_VS_FAIR": "edge_vs_fair",
        "EXECUTION": "execution",
        "MARKET_DATA": "market_data",
        "SCAN": "scan",
        "LIQUIDITY": "liquidity",
        "CROSS_PLATFORM": "cross_platform",
        "FEATURES": "features",
        "MONITORING": "monitoring",
        "API": "api",
        "WEB": "web",
        "DATABASE": "database",
        "BACKTEST": "backtest",
        "DYNAMIC_KELLY": "dynamic_kelly",
        "ORDERBOOK_IMBALANCE": "orderbook_imbalance",
        "CORRELATION_MATRIX": "correlation_matrix",
        "PLATFORM_RISK_SCORES": "platform_risk_scores",
        "ENTITY_VERIFICATION": "entity_verification",
        "QUANT": "quant",
    }

    result: dict[str, Any] = {}
    for env_prefix, yaml_key in prefixes.items():
        prefix_len = len(env_prefix) + 2  # +2 for "__"
        for key, value in os.environ.items():
            if key.startswith(env_prefix + "__"):
                nested_key = key[prefix_len:].lower()
                _set_nested(result, yaml_key, nested_key, value)

    # Also handle top-level config keys (bankroll_usd, kelly_fraction, etc.)
    top_level_keys = {
        "bankroll_usd", "kelly_fraction", "edge_threshold_bps",
        "max_position_pct", "max_daily_loss_pct", "max_concurrent_markets",
        "max_gross_exposure_pct", "polymarket_taker_fee_bps",
        "polymarket_maker_rebate_bps", "enable_reverse_arb",
    }
    for key in top_level_keys:
        env_key = key.upper()
        if env_key in os.environ:
            _set_nested_value(result, key, os.environ[env_key])

    return result


def _set_nested(d: dict, top_key: str, nested_key: str, value: str) -> None:
    """Set nested value in dict using double underscore as delimiter."""
    parts = nested_key.split("__")
    if top_key not in d:
        d[top_key] = {}
    current = d[top_key]
    for part in parts[:-1]:
        if part not in current:
            current[part] = {}
        current = current[part]
    current[parts[-1]] = _parse_value(value)


def _set_nested_value(d: dict, key: str, value: str) -> None:
    """Set top-level value with parsing."""
    d[key] = _parse_value(value)


def _parse_value(value: str) -> Any:
    """Parse string value to appropriate type."""
    v_lower = value.lower()
    if v_lower in ("true", "false"):
        return v_lower == "true"
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def _merge_dict(target: dict, source: dict) -> None:
    """Recursively merge source into target."""
    for key, value in source.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            _merge_dict(target[key], value)
        else:
            target[key] = value


# =============================================================================
# ACCESSOR FUNCTIONS
# =============================================================================

def get_config() -> ConfigModel:
    """Get current configuration (loads if not already loaded)."""
    global _config
    if _config is None:
        return load_config()
    return _config


def get_settings() -> Settings:
    """Get environment settings."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def get_reverse_arb_config() -> ReverseArbConfig:
    """Get reverse arbitrage configuration."""
    return get_config().reverse_arb


def reload_config() -> ConfigModel:
    """Force reload configuration."""
    global _config
    _config = None
    return load_config()


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

from decimal import Decimal

def get_bankroll() -> Decimal:
    return Decimal(str(get_config().bankroll_usd))


def get_kelly_fraction() -> Decimal:
    return Decimal(str(get_config().kelly_fraction))


def get_edge_threshold_bps() -> int:
    return get_config().edge_threshold_bps


def get_fee_rate(platform: str) -> Decimal:
    """Get taker fee rate for platform in basis points."""
    cfg = get_config()
    fee_map = {
        "polymarket": cfg.polymarket_taker_fee_bps,
        "manifold": cfg.manifold_taker_fee_bps,
        "predictit": cfg.predictit_taker_fee_bps,
        "betfair": cfg.betfair_taker_fee_bps,
        "smarkets": cfg.smarkets_taker_fee_bps,
        "drift": cfg.drift_taker_fee_bps,
        "metaculus": cfg.metaculus_taker_fee_bps,
    }
    return Decimal(str(fee_map.get(platform.lower(), 0))) / Decimal(10000)


def get_maker_rebate(platform: str) -> Decimal:
    """Get maker rebate rate for platform in basis points."""
    cfg = get_config()
    rebate_map = {
        "polymarket": cfg.polymarket_maker_rebate_bps,
        "manifold": 0,
    }
    return Decimal(str(rebate_map.get(platform.lower(), 0))) / Decimal(10000)