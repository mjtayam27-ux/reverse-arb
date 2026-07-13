#!/usr/bin/env python
"""
Deploy Entry Point for Reverse Arbitrage Bot.

Runs the engine with full HFT WebSocket detection, risk management,
and API server. Designed for Fly.io / Docker deployment.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from contextlib import suppress
from decimal import Decimal

from src.core.arbitrage_engine import ReverseArbEngine, EngineConfig, start_engine, stop_engine
from src.core.config import get_config, get_settings


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _parse_bool(val: str | None, default: bool = False) -> bool:
    """Parse boolean from environment variable."""
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes", "on")


def _parse_int(val: str | None, default: int = 0) -> int:
    """Parse integer from environment variable."""
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _parse_float(val: str | None, default: float = 0.0) -> float:
    """Parse float from environment variable."""
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _get_engine_config() -> EngineConfig:
    """Build EngineConfig from environment variables and config model."""
    settings = get_settings()
    cfg = get_config()
    strat = cfg.reverse_arb

    # Read from environment (Fly secrets / .env) with fallbacks to config model
    btc_only = _parse_bool(os.getenv("REVERSE_ARB_BTC_ONLY"), strat.btc_markets_only)
    eth_only = _parse_bool(os.getenv("REVERSE_ARB_ETH_ONLY"), strat.eth_markets_only)
    min_close_min = _parse_int(os.getenv("REVERSE_ARB_MIN_CLOSE_MIN"), strat.minutes_before_close_min)
    min_close_max = _parse_int(os.getenv("REVERSE_ARB_MIN_CLOSE_MAX"), strat.minutes_before_close_max)
    scan_interval_ms = _parse_int(os.getenv("REVERSE_ARB_SCAN_INTERVAL"), strat.poll_interval_ms)
    hft_mode = _parse_bool(os.getenv("REVERSE_ARB_HFT_MODE"), True)
    dry_run = _parse_bool(os.getenv("REVERSE_ARB_DRY_RUN"), strat.dry_run)

    # Paper trading from settings (top-level env var)
    paper_trading = settings.paper_trading

    # API config from environment with fallback to config model
    api_enabled = _parse_bool(os.getenv("API_ENABLED"), True)
    api_host = os.getenv("API_HOST", cfg.api.api_host)
    api_port = _parse_int(os.getenv("API_PORT"), cfg.api.api_port)
    metrics_port = _parse_int(os.getenv("METRICS_PORT"), cfg.monitoring.prometheus_port)

    # Feature flags from settings
    reverse_arb_enabled = settings.enable_reverse_arb
    hft_enabled = settings.enable_reverse_arb and hft_mode

    return EngineConfig(
        enable_reverse_arb=reverse_arb_enabled,
        enable_hft_reverse_arb=hft_enabled,
        enable_internal_arb=False,
        enable_cross_platform_arb=False,
        btc_only_focus=btc_only,
        eth_only_focus=eth_only,
        minutes_before_close_min=min_close_min,
        minutes_before_close_max=min_close_max,
        scan_interval_seconds=max(1, scan_interval_ms // 1000),  # Convert ms to seconds
        hft_mode=hft_mode,
        paper_trading=paper_trading,
        api_enabled=api_enabled,
        api_host=api_host,
        api_port=api_port,
        metrics_port=metrics_port,
    )


async def main() -> int:
    """Main entry point."""
    config = _get_engine_config()

    # Safety guard for live trading
    paper_trading = config.paper_trading
    environment = os.getenv("ENVIRONMENT", "production").lower()
    live_confirmed = os.getenv("LIVE_TRADING_CONFIRMED", "false").lower() == "true"

    if not paper_trading and environment == "production":
        if not live_confirmed:
            logger.critical(
                "LIVE TRADING BLOCKED: paper_trading=False in production "
                "but LIVE_TRADING_CONFIRMED=true not set. "
                "Set paper_trading=True or explicitly confirm with "
                "fly secrets set LIVE_TRADING_CONFIRMED=true (after 48h validation)."
            )
            return 1
        logger.warning("⚠️  LIVE TRADING ENABLED — Real capital at risk!")

    # Create and start engine
    engine = ReverseArbEngine(config)

    # Signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def signal_handler(sig: int):
        logger.info(f"Received signal {sig}, initiating shutdown...")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler, sig)
        except NotImplementedError:
            # Windows doesn't support signal handlers
            pass

    try:
        await engine.start()
        logger.info("Engine started successfully, waiting for shutdown signal...")
        await shutdown_event.wait()
    except Exception as e:
        logger.exception(f"Engine error: {e}")
        return 1
    finally:
        logger.info("Shutting down...")
        await engine.stop()
        logger.info("Shutdown complete")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))# Cache bust 2026-07-14 20:30
