"""
Main Reverse Arbitrage Engine.

Orchestrates market data, strategy detection, risk management, and execution.
Implements the core detection loop with Maker/Checker isolation.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from src.arbitrage.reverse_arb import (
    HFTReverseArbDetector,
    ReverseArbConfig,
    ReverseArbitrageDetector,
    make_opportunity_callback,
)
from src.core.config import get_config, get_settings
from src.core.types import (
    ArbitrageLeg,
    ArbitrageOpportunity,
    ExecutionPlan,
    MarketSnapshot,
    SystemMetrics,
)
from src.execution.executor import ExecutionEngine, ExecutionMode
from src.market_data.clob_client import (
    ClobClient,
    ClobWebSocketFeed,
    GammaClient,
    MarketDataAggregator,
    WSConfig,
    create_clob_client,
)
from src.risk.risk_engine import RiskConfig, RiskEngine

logger = logging.getLogger(__name__)


# Keywords for BTC/ETH markets
_BTC_KEYWORDS = ("btc", "bitcoin")
_ETH_KEYWORDS = ("eth", "ethereum")


def is_btc_market(market: object) -> bool:
    """True if a market is BTC-related."""
    try:
        q = (getattr(market, "question", "") or "").lower()
        if any(k in q for k in _BTC_KEYWORDS):
            return True
        cat = (getattr(market, "category", "") or "").lower()
        if cat in ("crypto", "bitcoin", "cryptocurrency"):
            return True
        tags = getattr(market, "tags", None) or []
        return any(str(t).lower() in ("btc", "bitcoin", "crypto", "cryptocurrency") for t in tags)
    except Exception:
        return False


def is_eth_market(market: object) -> bool:
    """True if a market is ETH-related."""
    try:
        q = (getattr(market, "question", "") or "").lower()
        if any(k in q for k in _ETH_KEYWORDS):
            return True
        cat = (getattr(market, "category", "") or "").lower()
        if cat in ("crypto", "ethereum", "cryptocurrency"):
            return True
        tags = getattr(market, "tags", None) or []
        return any(str(t).lower() in ("eth", "ethereum", "crypto", "cryptocurrency") for t in tags)
    except Exception:
        return False


def is_up_down_market(market: object) -> bool:
    """True if a market is a 15m BTC/ETH Up/Down market."""
    try:
        q = (getattr(market, "question", "") or "").lower()
        slug = (getattr(market, "slug", "") or "").lower()
        return (
            any(k in q for k in ("btc", "bitcoin", "eth", "ethereum")) and
            any(k in q for k in ("up", "down", "updown")) and
            "15m" in slug
        )
    except Exception:
        return False


def is_short_term_binary_market(market: object) -> bool:
    """True if a market is a short-term binary market suitable for reverse arb.

    Criteria:
    - Binary (YES/NO or UP/DOWN)
    - Active and not closed
    - Minutes to close <= 60 (1 hour) - short expiry for quick resolution
    - Has sufficient liquidity
    """
    try:
        if not getattr(market, "active", False) or getattr(market, "closed", True):
            return False
        if not getattr(market, "is_binary", False):
            return False
        m = getattr(market, "minutes_to_close", None)
        if m is not None and m > 60:
            return False
        # Check liquidity threshold
        liq = getattr(market, "liquidity", Decimal("0"))
        if liq < Decimal("500"):  # Minimum $500 liquidity
            return False
        return True
    except Exception:
        return False


@dataclass
class EngineConfig:
    """Main engine configuration."""
    # Feature flags
    enable_reverse_arb: bool = True
    enable_hft_reverse_arb: bool = True
    enable_internal_arb: bool = False
    enable_cross_platform_arb: bool = False

    # Market focus
    btc_only_focus: bool = True
    eth_only_focus: bool = False
    minutes_before_close_min: int = 2
    minutes_before_close_max: int = 5

    # Scan intervals
    scan_interval_seconds: int = 5  # For batch scanning
    hft_mode: bool = True  # Use WebSocket event-driven

    # Paper/Live mode
    paper_trading: bool = True

    # API
    api_enabled: bool = True
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    # Metrics
    metrics_port: int = 9090


class ReverseArbEngine:
    """
    Main reverse arbitrage engine orchestrating all components.

    Architecture:
    - Market Data Layer: Gamma + CLOB + WebSocket
    - Strategy Layer: Reverse arbitrage detector (batch + HFT)
    - Risk Layer: Pre-trade checks, position limits, circuit breakers
    - Execution Layer: Order management, position tracking, Maker/Checker
    - API Layer: REST endpoints for monitoring/control
    """

    def __init__(self, config: EngineConfig | None = None):
        self._config = config or EngineConfig()
        self._cfg = get_config()
        self._settings = get_settings()

        # Load strategy config
        self._strat_config = self._cfg.reverse_arb

        # Core clients
        self._gamma: GammaClient | None = None
        self._clob: ClobClient | None = None
        self._ws_feed: ClobWebSocketFeed | None = None
        self._aggregator: MarketDataAggregator | None = None

        # Strategies
        self._batch_detector = ReverseArbitrageDetector()
        self._hft_detector: HFTReverseArbDetector | None = None

        # Execution & Risk
        self._execution_engine: ExecutionEngine | None = None
        self._risk_engine: RiskEngine | None = None

        # State
        self._running = False
        self._scan_task: asyncio.Task | None = None
        self._metrics_task: asyncio.Task | None = None
        self._api_task: asyncio.Task | None = None
        self._api_server: Any = None

        # Metrics
        self._metrics = SystemMetrics(
            uptime_seconds=0,
            opportunities_found=0,
            opportunities_executed=0,
            orders_placed=0,
            orders_filled=0,
            orders_rejected=0,
            total_pnl_usd=Decimal("0"),
            daily_pnl_usd=Decimal("0"),
            current_drawdown_pct=Decimal("0"),
            peak_equity_usd=Decimal("0"),
            active_positions=0,
            open_orders=0,
            last_scan_timestamp=None,
            last_execution_timestamp=None,
            errors_last_hour=0,
            latency_p50_ms=0.0,
            latency_p99_ms=0.0,
        )

        # Callbacks
        self._opportunity_callbacks: list[Callable[..., Any]] = []
        self._execution_callbacks: list[Callable[..., Any]] = []

        # Opportunity buffer for dashboard
        self._current_opportunities: list[ArbitrageOpportunity] = []

        # Initialization guard
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize all components."""
        async with self._init_lock:
            if self._initialized:
                logger.warning("Engine already initialized")
                return
            logger.info("Initializing Reverse Arbitrage Engine...")

            # Market data clients
        self._gamma = GammaClient()
        await self._gamma.__aenter__()

        self._clob = await create_clob_client(
            api_key=self._settings.polymarket_api_key,
            api_secret=self._settings.polymarket_api_secret,
            api_passphrase=self._settings.polymarket_api_passphrase,
            private_key=self._settings.polymarket_private_key,
        )
        await self._clob.__aenter__()

        # Derive L2 API key
        if self._clob.signer:
            try:
                await self._clob.derive_api_key()
                logger.info("Derived Polymarket L2 API key from private key")
            except Exception as e:
                logger.warning(f"Failed to derive Polymarket API key: {e}")

        # WebSocket feed - start FIRST so aggregator gets real-time data
        ws_config = WSConfig(url=self._cfg.market_data.clob_ws_url)
        logger.info(f"Starting WebSocket feed: {ws_config.url}")
        self._ws_feed = ClobWebSocketFeed(config=ws_config)
        self._aggregator = MarketDataAggregator(self._gamma, self._ws_feed, self._clob)

        await self._ws_feed.start()

        # Wait for WebSocket to connect
        for _ in range(30):
            if self._ws_feed.is_connected:
                break
            await asyncio.sleep(0.1)
        logger.info(f"WebSocket connected: {self._ws_feed.is_connected}")

        # Initialize aggregator
        await self._aggregator.initialize()

        # Subscribe WebSocket to Up/Down market tokens
        if self._ws_feed and self._ws_feed.is_connected:
            up_down_markets = self._aggregator.get_up_down_markets()
            logger.info(f"Subscribing WebSocket to {len(up_down_markets)} Up/Down market tokens")
            for market in up_down_markets:
                if market.clob_token_ids:
                    for token_id in market.clob_token_ids:
                        if isinstance(token_id, str):
                            await self._ws_feed.subscribe(token_id, callback=self._aggregator.update_orderbook)
                        else:
                            logger.warning(f"Skipping invalid token_id {token_id!r} for market {market.condition_id}")

            # Also subscribe to short-term binary markets (fallback when no Up/Down markets)
            short_term_markets = self._aggregator.get_short_term_binary_markets()
            logger.info(f"Subscribing WebSocket to {len(short_term_markets)} short-term binary market tokens")
            for market in short_term_markets:
                if market.clob_token_ids:
                    for token_id in market.clob_token_ids:
                        if isinstance(token_id, str):
                            await self._ws_feed.subscribe(token_id, callback=self._aggregator.update_orderbook)
                        else:
                            logger.warning(f"Skipping invalid token_id {token_id!r} for market {market.condition_id}")

        # HFT detector
        if self._config.enable_hft_reverse_arb:
            self._hft_detector = HFTReverseArbDetector(
                ReverseArbConfig(
                    min_edge_bps=self._strat_config.min_edge_bps,
                    max_slippage_bps=self._strat_config.max_slippage_bps,
                    max_position_usd=self._strat_config.max_position_usd,
                    fee_bps=self._strat_config.fee_bps,
                    cheap_buy_min=self._strat_config.cheap_buy_min,
                    cheap_buy_max=self._strat_config.cheap_buy_max,
                    expensive_buy_min=self._strat_config.expensive_buy_min,
                    expensive_buy_max=self._strat_config.expensive_buy_max,
                    cheap_order_usd=self._strat_config.cheap_order_usd,
                    expensive_order_usd=self._strat_config.expensive_order_usd,
                    minutes_before_close_min=self._strat_config.minutes_before_close_min,
                    minutes_before_close_max=self._strat_config.minutes_before_close_max,
                    order_type=self._strat_config.order_type,
                    dry_run=self._strat_config.dry_run,
                    btc_markets_only=self._config.btc_only_focus,
                    eth_markets_only=self._config.eth_only_focus,
                )
            )
            # Register markets with HFT detector (both Up/Down and short-term binary)
            # Use list + condition_id tracking instead of set (MarketInfo has unhashable list fields)
            all_markets = []
            seen_condition_ids = set()
            for market in self._aggregator.get_up_down_markets():
                if market.condition_id not in seen_condition_ids:
                    all_markets.append(market)
                    seen_condition_ids.add(market.condition_id)
            for market in self._aggregator.get_short_term_binary_markets():
                if market.condition_id not in seen_condition_ids:
                    all_markets.append(market)
                    seen_condition_ids.add(market.condition_id)
            for market in all_markets:
                await self._hft_detector.register_market(market)

        # Execution engine
        mode = ExecutionMode.PAPER if self._config.paper_trading else ExecutionMode.LIVE
        self._execution_engine = ExecutionEngine(self._clob, mode=mode)

        # Fetch actual USDC wallet balance for risk config
        wallet_balance_usd = await self._fetch_wallet_balance_usd()

        # Risk engine - use wallet balance to set exposure limits, with config fallback
        # Get configured max gross exposure as fallback
        risk_limits_global = getattr(self._cfg.risk_limits, 'global', None)
        if risk_limits_global and hasattr(risk_limits_global, 'max_gross_exposure_usd'):
            configured_max_exposure = Decimal(str(risk_limits_global.max_gross_exposure_usd))
        else:
            configured_max_exposure = Decimal("10000")

        # Use wallet balance if available (> $10), otherwise use configured max
        if wallet_balance_usd > 10:  # Minimum $10 to be meaningful
            max_exposure = min(Decimal(str(wallet_balance_usd)), configured_max_exposure)
            logger.info(f"Using wallet balance for risk limits: ${wallet_balance_usd:.2f}")
        else:
            max_exposure = configured_max_exposure
            logger.warning(f"Wallet balance ${wallet_balance_usd:.2f} too low, using configured max exposure: ${max_exposure}")

        max_daily_loss = max_exposure * Decimal("0.05")  # 5% daily loss limit
        risk_cfg = RiskConfig(
            max_position_per_market_usd=self._strat_config.max_position_usd,
            max_daily_loss_usd=max_daily_loss,
            max_gross_exposure_usd=max_exposure,
            max_concurrent_positions=5,
            max_slippage_bps=self._strat_config.max_slippage_bps,
            max_order_latency_ms=200,
        )
        self._risk_engine = RiskEngine(risk_cfg, self._execution_engine.position_manager)
        await self._risk_engine.initialize()

        # Update risk engine with actual wallet balance as bankroll for Kelly sizing
        if wallet_balance_usd > 10:
            self._risk_engine.update_bankroll(Decimal(str(wallet_balance_usd)))
            logger.info(f"Risk engine bankroll set to wallet balance: ${wallet_balance_usd:.2f}")
        else:
            # Fallback to config bankroll (from YAML/env) or configured max exposure
            config_bankroll = Decimal(str(self._cfg.bankroll_usd))
            if config_bankroll > 0:
                self._risk_engine.update_bankroll(config_bankroll)
                logger.info(f"Risk engine bankroll set from config: ${config_bankroll:.2f}")
            else:
                self._risk_engine.update_bankroll(max_exposure)
                logger.info(f"Risk engine bankroll set to max exposure: ${max_exposure:.2f}")

        # HFT opportunity callback
        if self._hft_detector:
            loop = asyncio.get_event_loop()
            callback = make_opportunity_callback(self._hft_detector, self, loop)
            for token_id in self._hft_detector._token_map:
                await self._ws_feed.subscribe(token_id, callback=callback)

        # Initialize TradingMode with vault-backed LIVE_TRADING_CONFIRMED guard
        from src.execution.executor import TradingMode
        await TradingMode.initialize()

    async def _fetch_wallet_balance_usd(self) -> float:
        """Fetch USDC balance from Polymarket CLOB."""
        try:
            # Derive L2 API key first
            if self._clob.signer and not self._clob._l2_api_key:
                await self._clob.derive_api_key()

            bal = await self._clob.get_balance_allowance()
            if bal and 'usdc' in bal:
                # balance is typically in wei (1e18) or similar
                usdc_balance = Decimal(str(bal['usdc'])) / Decimal("1e18")
                logger.info(f"Wallet USDC balance (CLOB): ${usdc_balance:.2f}")
                return float(usdc_balance)
            else:
                logger.warning(f"Could not parse USDC balance from CLOB: {bal}")
        except Exception as e:
            logger.warning(f"Failed to fetch wallet balance from CLOB: {e}")

        # Fallback: Query USDC balance directly from Polygon blockchain
        return await self._fetch_usdc_balance_via_rpc()

    async def _fetch_usdc_balance_via_rpc(self) -> float:
        """Fetch USDC balance from Polygon via RPC using wallet address from private key."""
        try:
            from eth_account import Account
            from web3 import Web3

            # Get wallet address from private key
            settings = get_settings()
            private_key = settings.polymarket_private_key
            if not private_key:
                logger.warning("No private key available for balance query")
                return 10000.0

            account = Account.from_key(private_key)
            wallet_address = account.address
            logger.info(f"Querying USDC balance for wallet: {wallet_address}")

            # USDC contract on Polygon
            usdc_address = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
            rpc_url = self._cfg.market_data.polygon_rpc_url
            w3 = Web3(Web3.HTTPProvider(rpc_url))

            # ERC20 balanceOf ABI
            abi = [{
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function"
            }]
            contract = w3.eth.contract(address=usdc_address, abi=abi)
            balance = contract.functions.balanceOf(wallet_address).call()

            # USDC has 6 decimals on Polygon
            usdc_balance = Decimal(balance) / Decimal("1e6")
            logger.info(f"Wallet USDC balance (RPC): ${usdc_balance:.2f}")
            return float(usdc_balance)
        except Exception as e:
            logger.warning(f"Failed to fetch USDC balance via RPC: {e}")

        # Default fallback
        logger.info("Using default max exposure: $10000")
        return 10000.0


        # Initialize TradingMode with vault-backed LIVE_TRADING_CONFIRMED guard

        self._initialized = True
        logger.info("Reverse Arbitrage Engine initialized successfully")

    async def start(self) -> None:
        """Start the engine."""
        if self._running:
            logger.warning("Engine already running")
            return

        # P0 SAFETY GUARD
        paper_trading = self._config.paper_trading
        environment = os.getenv("ENVIRONMENT", "production").lower()
        live_confirmed = os.getenv("LIVE_TRADING_CONFIRMED", "false").lower() == "true"

        if not paper_trading and environment == "production":
            if not live_confirmed:
                error_msg = (
                    "LIVE TRADING BLOCKED: paper_trading=False in production "
                    "but LIVE_TRADING_CONFIRMED=true not set. "
                    "Either set paper_trading=True or explicitly confirm with "
                    "fly secrets set LIVE_TRADING_CONFIRMED=true (after 48h validation)."
                )
                logger.critical(error_msg)
                raise RuntimeError(error_msg)
            logger.warning("⚠️  LIVE TRADING ENABLED — Real capital at risk!")

        await self.initialize()
        self._running = True

        # Start background tasks
        if not self._config.hft_mode:
            self._scan_task = asyncio.create_task(self._scan_loop())
        self._metrics_task = asyncio.create_task(self._metrics_loop())

        # Start API server if enabled
        if self._config.api_enabled:
            from src.api.server import create_app
            self._api_server = create_app(self)
            self._api_task = asyncio.create_task(self._run_api_server())

        logger.info("Reverse Arbitrage Engine started")

    async def stop(self) -> None:
        """Stop the engine gracefully."""
        if not self._running:
            return

        logger.info("Stopping Reverse Arbitrage Engine...")
        self._running = False

        # Cancel background tasks
        for task in [self._scan_task, self._metrics_task, self._api_task]:
            if task:
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=10.0)
                except (asyncio.CancelledError, TimeoutError):
                    if not task.done():
                        task.cancel()
                    logger.warning("Task did not finish cancelling within 10s")

        # Cancel all open orders
        if self._execution_engine:
            await self._execution_engine.cancel_all_orders()

        # Close connections
        if self._aggregator:
            await self._aggregator.stop()
        if self._ws_feed:
            await self._ws_feed.stop()
        if self._gamma:
            await self._gamma.__aexit__(None, None, None)
        if self._clob:
            await self._clob.__aexit__(None, None, None)

        logger.info("Reverse Arbitrage Engine stopped")

    async def _scan_loop(self) -> None:
        """Main batch scanning loop (for non-HFT mode)."""
        logger.info("Starting batch scan loop")
        scan_count = 0

        while self._running:
            scan_count += 1
            logger.info(f"=== Batch scan iteration {scan_count} ===")
            start_time = datetime.now(UTC)

            try:
                # Get fresh market data
                snapshots = await self._get_market_snapshots()

                if snapshots:
                    # BTC/ETH focus
                    filtered_snapshots = self._filter_markets(snapshots)

                    # Run batch detector
                    if self._config.enable_reverse_arb:
                        try:
                            opportunities = self._batch_detector.scan_markets(filtered_snapshots)
                            for opp in opportunities:
                                await self._process_opportunity(opp)
                            self._metrics.opportunities_found += len(opportunities)
                        except Exception as e:
                            logger.error(f"Batch reverse arb detector failed: {e}")
                            self._metrics.errors_last_hour += 1

                    self._metrics.last_scan_timestamp = datetime.now(UTC)

                # Update risk state
                if self._risk_engine and self._execution_engine:
                    try:
                        positions = await self._execution_engine.position_manager.get_all_positions()
                        await self._risk_engine.update_positions(positions)
                    except Exception as e:
                        logger.error(f"Risk engine update failed: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scan loop error: {e}")
                self._metrics.errors_last_hour += 1

            # Sleep until next scan
            elapsed = (datetime.now(UTC) - start_time).total_seconds()
            sleep_time = max(0, self._config.scan_interval_seconds - elapsed)
            await asyncio.sleep(sleep_time)

    async def _get_market_snapshots(self) -> list[MarketSnapshot]:
        """Get current market snapshots from aggregator."""
        if not self._aggregator:
            return []

        markets = self._aggregator.get_up_down_markets()
        snapshots = []
        for market in markets:
            snapshot = await self._aggregator.get_snapshot(market.condition_id)
            if snapshot:
                snapshots.append(snapshot)
        logger.info(f"Built {len(snapshots)} Up/Down snapshots with orderbooks")
        return snapshots

    def _filter_markets(self, snapshots: list[MarketSnapshot]) -> list[MarketSnapshot]:
        """Filter snapshots by BTC/ETH focus and time to close."""
        filtered = []
        for snap in snapshots:
            # Time to close filter
            if (
                snap.market.minutes_to_close is not None
                and (
                    snap.market.minutes_to_close < self._config.minutes_before_close_min
                    or snap.market.minutes_to_close > self._config.minutes_before_close_max
                )
            ):
                continue

            # BTC filter
            if self._config.btc_only_focus and not is_btc_market(snap.market):
                continue

            # ETH filter
            if self._config.eth_only_focus and not is_eth_market(snap.market):
                continue

            filtered.append(snap)
        return filtered

    async def _process_opportunity(self, opportunity: ArbitrageOpportunity) -> None:
        """Process a detected opportunity through risk and execution."""
        if self._risk_engine is None:
            return

        # Risk check - use risk engine's bankroll (set from wallet/config)
        risk_result = await self._risk_engine.check_trade(opportunity)

        if not risk_result.approved:
            logger.info(f"Opportunity rejected by risk: {risk_result.violations}")
            return

        # Adjust size based on risk
        if risk_result.recommended_size_usd < opportunity.required_capital_usd:
            scale = risk_result.recommended_size_usd / opportunity.required_capital_usd
            opportunity = self._scale_opportunity(opportunity, scale)

        # Execute
        if self._execution_engine:
            results = await self._execution_engine.execute_opportunity(opportunity)

            filled = [r for r in results if r.status.value == "filled"]
            self._metrics.orders_placed += len(results)
            self._metrics.orders_filled += len(filled)
            self._metrics.orders_rejected += len([r for r in results if r.status.value == "rejected"])

            if filled:
                self._metrics.opportunities_executed += 1
                self._metrics.last_execution_timestamp = datetime.now(UTC)
                self._current_opportunities.append(opportunity)

        # Add to opportunities buffer for dashboard
        self._current_opportunities.append(opportunity)
        if len(self._current_opportunities) > 200:
            self._current_opportunities = self._current_opportunities[-200:]

        # Callbacks
        for cb in self._opportunity_callbacks:
            try:
                await cb(opportunity, risk_result)
            except Exception as e:
                logger.error(f"Opportunity callback error: {e}")

    def _scale_opportunity(
        self,
        opportunity: ArbitrageOpportunity,
        scale: Decimal,
    ) -> ArbitrageOpportunity:
        """Scale opportunity size."""
        scaled_legs = []
        for leg in opportunity.legs:
            scaled_leg = ArbitrageLeg(
                platform=leg.platform,
                market_id=leg.market_id,
                condition_id=leg.condition_id,
                token_id=leg.token_id,
                outcome=leg.outcome,
                side=leg.side,
                target_price=leg.target_price,
                max_slippage_bps=leg.max_slippage_bps,
                size=leg.size * scale,
                order_type=leg.order_type,
                fee_rate_bps=leg.fee_rate_bps,
            )
            scaled_legs.append(scaled_leg)

        return ArbitrageOpportunity(
            type=opportunity.type,
            legs=tuple(scaled_legs),
            gross_edge_bps=opportunity.gross_edge_bps,
            net_edge_bps=opportunity.net_edge_bps,
            total_fees_bps=opportunity.total_fees_bps,
            estimated_profit_usd=opportunity.estimated_profit_usd * scale,
            required_capital_usd=opportunity.required_capital_usd * scale,
            max_position_usd=opportunity.max_position_usd,
            kelly_fraction=opportunity.kelly_fraction * scale,
            confidence=opportunity.confidence,
            risk_level=opportunity.risk_level,
            min_liquidity_usd=opportunity.min_liquidity_usd,
            metadata=opportunity.metadata,
        )

    async def _metrics_loop(self) -> None:
        """Update metrics periodically."""
        start_time = datetime.now(UTC)

        while self._running:
            try:
                # Update uptime
                self._metrics.uptime_seconds = int(
                    (datetime.now(UTC) - start_time).total_seconds()
                )

                # Update position/order counts
                if self._execution_engine:
                    self._metrics.active_positions = len(
                        await self._execution_engine.position_manager.get_all_positions()
                    )
                    self._metrics.open_orders = len(
                        await self._execution_engine.order_manager.get_open_orders()
                    )

                # Update PnL
                if self._execution_engine:
                    total_pnl = await self._execution_engine.position_manager.get_total_pnl()
                    self._metrics.total_pnl_usd = total_pnl
                    if self._metrics.peak_equity_usd == 0:
                        self._metrics.peak_equity_usd = Decimal("10000")
                    if total_pnl > self._metrics.peak_equity_usd:
                        self._metrics.peak_equity_usd = total_pnl + Decimal("10000")

                await asyncio.sleep(10)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Metrics loop error: {e}")

    def add_opportunity_callback(self, callback: Callable[..., Any]) -> None:
        self._opportunity_callbacks.append(callback)

    async def create_execution_plan(self, opportunity: ArbitrageOpportunity) -> ExecutionPlan:
        return opportunity.to_execution_plan()

    def add_execution_callback(self, callback: Callable[..., Any]) -> None:
        self._execution_callbacks.append(callback)

    def get_metrics(self) -> SystemMetrics:
        return self._metrics

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "paper_trading": self._config.paper_trading,
            "hft_mode": self._config.hft_mode,
            "metrics": {
                "uptime_seconds": self._metrics.uptime_seconds,
                "opportunities_found": self._metrics.opportunities_found,
                "opportunities_executed": self._metrics.opportunities_executed,
                "orders_placed": self._metrics.orders_placed,
                "orders_filled": self._metrics.orders_filled,
                "orders_rejected": self._metrics.orders_rejected,
                "total_pnl_usd": float(self._metrics.total_pnl_usd),
                "active_positions": self._metrics.active_positions,
                "open_orders": self._metrics.open_orders,
                "errors_last_hour": self._metrics.errors_last_hour,
            },
            "hft_detector": {
                "active": self._hft_detector is not None,
                "tokens_subscribed": len(self._hft_detector._token_map) if self._hft_detector else 0,
            } if self._config.enable_hft_reverse_arb else None,
        }

    async def _run_api_server(self) -> None:
        """Run the API server as a background task."""
        import uvicorn
        config = uvicorn.Config(
            self._api_server,
            host=self._config.api_host,
            port=self._config.api_port,
            log_level="info",
            loop="asyncio",
        )
        server = uvicorn.Server(config)
        await server.serve()


# Global engine instance for API
_engine_instance: ReverseArbEngine | None = None
_engine_lock = asyncio.Lock()


async def get_engine() -> ReverseArbEngine:
    """Get or create global engine instance."""
    global _engine_instance
    async with _engine_lock:
        if _engine_instance is None:
            _engine_instance = ReverseArbEngine()
            await _engine_instance.initialize()
        return _engine_instance


async def start_engine(config: EngineConfig | None = None) -> ReverseArbEngine:
    """Start the global engine."""
    global _engine_instance
    async with _engine_lock:
        _engine_instance = ReverseArbEngine(config)
        await _engine_instance.start()
        return _engine_instance


async def stop_engine() -> None:
    """Stop the global engine."""
    global _engine_instance
    if _engine_instance:
        await _engine_instance.stop()
        _engine_instance = None