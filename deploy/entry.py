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

from src.core.arbitrage_engine import ReverseArbEngine, EngineConfig, start_engine, stop_engine
from src.core.config import get_settings


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main() -> int:
    """Main entry point."""
    settings = get_settings()

    # Engine configuration from settings
    config = EngineConfig(
        enable_reverse_arb=settings.reverse_arb_enabled,
        enable_hft_reverse_arb=settings.hft_enabled,
        enable_internal_arb=False,
        enable_cross_platform_arb=False,
        btc_only_focus=settings.btc_only_focus,
        eth_only_focus=settings.eth_only_focus,
        minutes_before_close_min=settings.minutes_before_close_min,
        minutes_before_close_max=settings.minutes_before_close_max,
        scan_interval_seconds=settings.scan_interval_seconds,
        hft_mode=settings.hft_mode,
        paper_trading=settings.paper_trading,
        api_enabled=settings.api_enabled,
        api_host=settings.api_host,
        api_port=settings.api_port,
        metrics_port=settings.metrics_port,
    )

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
        await start_engine(engine)
        logger.info("Engine started successfully, waiting for shutdown signal...")
        await shutdown_event.wait()
    except Exception as e:
        logger.exception(f"Engine error: {e}")
        return 1
    finally:
        logger.info("Shutting down...")
        await stop_engine()
        logger.info("Shutdown complete")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))