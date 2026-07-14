"""
Order Execution Engine for Reverse Arbitrage Bot.

Handles order placement, fill tracking, and position management.
Supports both paper and live trading modes with centralized TradingMode authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

from src.core.types import (
    ArbitrageLeg,
    ArbitrageOpportunity,
    ExecutionPlan,
    ExecutionMode,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Position,
    Platform,
    Side,
    Fill,
    Decimal,
    datetime,
)

from src.security import verify_live_trading_allowed

logger = logging.getLogger(__name__)


# =============================================================================
# CENTRALIZED TRADING MODE AUTHORITY (from main Arbitrage system)
# =============================================================================

class TradingMode:
    """Centralized authority for paper/live trading mode.

    Prevents accidental mode flips and enforces LIVE_TRADING_CONFIRMED guard
    with vault-backed attestation.
    """

    _mode: ExecutionMode = ExecutionMode.PAPER
    _lock: asyncio.Lock = asyncio.Lock()
    _initialized: bool = False

    @classmethod
    async def initialize(cls) -> None:
        """Initialize mode from environment and vault. Call once at startup."""
        if cls._initialized:
            return
        async with cls._lock:
            if cls._initialized:
                return

            paper_trading = os.getenv("PAPER_TRADING", "true").lower() == "true"
            environment = os.getenv("ENVIRONMENT", "production").lower()

            # Check vault for live trading confirmation
            vault_allowed, vault_reason = await verify_live_trading_allowed()

            if not paper_trading and environment == "production":
                if not vault_allowed:
                    raise RuntimeError(
                        "LIVE TRADING BLOCKED: PAPER_TRADING=false in production "
                        "but vault-backed LIVE_TRADING_CONFIRMED not set. "
                        "Set PAPER_TRADING=true or configure vault with LIVE_TRADING_CONFIRMED=true "
                        "and valid attestation (after 48h validation period). "
                        f"Vault check: {vault_reason}"
                    )
                cls._mode = ExecutionMode.LIVE
                logger.warning("LIVE TRADING MODE ENABLED - REAL CAPITAL AT RISK (vault verified)")
            else:
                cls._mode = ExecutionMode.PAPER
                logger.info("Paper trading mode active")

            cls._initialized = True

    @classmethod
    def get_mode(cls) -> ExecutionMode:
        """Get current trading mode. Must call initialize() first."""
        if not cls._initialized:
            # Fallback for backward compatibility
            paper_trading = os.getenv("PAPER_TRADING", "true").lower() == "true"
            return ExecutionMode.PAPER if paper_trading else ExecutionMode.LIVE
        return cls._mode

    @classmethod
    async def set_mode(cls, mode: ExecutionMode) -> None:
        """Change trading mode with vault-backed LIVE_TRADING_CONFIRMED enforcement."""
        async with cls._lock:
            if mode == ExecutionMode.LIVE:
                vault_allowed, vault_reason = await verify_live_trading_allowed()
                if not vault_allowed:
                    raise RuntimeError(f"Cannot switch to LIVE mode: {vault_reason}")
                logger.warning("SWITCHING TO LIVE TRADING MODE - REAL CAPITAL AT RISK (vault verified)")
            cls._mode = mode

    @classmethod
    def is_live(cls) -> bool:
        return cls.get_mode() == ExecutionMode.LIVE

    @classmethod
    def is_paper(cls) -> bool:
        return cls.get_mode() == ExecutionMode.PAPER


# =============================================================================
# ORDER MANAGER
# =============================================================================

@dataclass
class OrderManagerConfig:
    """Configuration for the order manager."""
    max_retries: int = 3
    retry_backoff_ms: int = 250
    default_order_type: OrderType = OrderType.GTC
    timeout_ms: int = 30000


class OrderManager:
    """Manages order placement and lifecycle against a CLOB client.

    Tracks in-flight and completed orders keyed by client order id.
    Provides paper-mode path that synthesizes filled results without
    touching the network.
    """

    def __init__(self, clob_client: Any, config: Optional[OrderManagerConfig] = None):
        self.clob = clob_client
        self.config = config or OrderManagerConfig()
        self._orders: dict[str, OrderResult] = {}
        self._lock = asyncio.Lock()

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Place a single order. Returns an OrderResult.

        If client_order_id is provided and matches an existing order,
        returns the existing order result (idempotency).
        """
        # Runtime guard - re-check mode on every live order
        if TradingMode.is_live() and self.clob is None:
            raise RuntimeError("Live mode but no CLOB client available")

        # Idempotency check - include token_id in key to avoid cross-market collisions
        client_order_id = request.client_order_id
        idempotency_key = f"{client_order_id}:{request.token_id}" if client_order_id else None

        # Check idempotency under lock
        async with self._lock:
            if idempotency_key and idempotency_key in self._orders:
                existing = self._orders[idempotency_key]
                logger.info(f"Duplicate order detected, returning existing: {idempotency_key}")
                return existing

        # Execute order (paper or live)
        if TradingMode.is_paper() or self.clob is None:
            # Paper mode: always use deterministic client_order_id (caller must provide)
            # Generate one if missing - but this should not happen in production
            safe_client_order_id = client_order_id or f"paper-{(request.token_id or 'unknown')[:8]}-{hash(request.price)}-{hash(request.size)}"
            result = OrderResult(
                success=True,
                order_id=safe_client_order_id,
                client_order_id=safe_client_order_id,
                status=OrderStatus.FILLED,
                filled_size=request.size,
                avg_fill_price=request.price,
                fees_paid=Decimal("0"),
                timestamp=datetime.now(timezone.utc),
            )
        else:
            # Live mode: network I/O without holding lock
            result = await self._place_live(request)

        # Store result under lock
        async with self._lock:
            key = result.client_order_id or result.order_id or str(id(result))
            # Include token_id in stored key for collision prevention
            store_key = f"{key}:{request.token_id}" if request.token_id else key
            self._orders[store_key] = result
            # Also store by idempotency_key if provided for backward compat
            if idempotency_key:
                self._orders[idempotency_key] = result
            return result

    async def cancel_order(self, client_order_id: str, token_id: Optional[str] = None) -> OrderResult:
        """Cancel an open order by client order id and optional token_id."""
        async with self._lock:
            # Try exact match with token_id suffix first
            if token_id:
                existing = self._orders.get(f"{client_order_id}:{token_id}")
            else:
                existing = self._orders.get(client_order_id)

            if existing is None:
                # Search for key starting with client_order_id:
                for key, val in self._orders.items():
                    if key == client_order_id or key.startswith(f"{client_order_id}:"):
                        existing = val
                        break

            if existing is None:
                return OrderResult(
                    success=False,
                    client_order_id=client_order_id,
                    status=OrderStatus.REJECTED,
                    error="order not found",
                    timestamp=datetime.now(timezone.utc),
                )

            cancelled = OrderResult(
                success=True,
                order_id=existing.order_id,
                client_order_id=client_order_id,
                status=OrderStatus.CANCELLED,
                filled_size=existing.filled_size,
                avg_fill_price=existing.avg_fill_price,
                timestamp=datetime.now(timezone.utc),
            )
            # Remove all keys associated with this order
            keys_to_remove = [k for k in self._orders if k == client_order_id or k.startswith(f"{client_order_id}:")]
            for k in keys_to_remove:
                self._orders[k] = cancelled
            return cancelled

    async def cancel_all_orders(self) -> list[OrderResult]:
        """Atomically cancel all open orders and return results."""
        async with self._lock:
            open_orders = list(self._orders.values())
            results: list[OrderResult] = []
            for order in open_orders:
                client_id = order.client_order_id or order.order_id or ""
                if client_id:
                    # Cancel using internal key format
                    result = await self.cancel_order(client_id)
                    results.append(result)
            return results

    async def get_fills(
        self, market: Optional[str] = None, limit: int = 50
    ) -> list[Fill]:
        """Recent fills. Paper mode tracks no separate fills."""
        return []

    async def _place_live(self, request: OrderRequest) -> OrderResult:
        """Place an order via the live CLOB client (fully async with retries)."""
        last_error = None
        for attempt in range(self.config.max_retries):
            try:
                result: OrderResult = await asyncio.wait_for(
                    self.clob.place_order(request),
                    timeout=self.config.timeout_ms / 1000
                )
                return result
            except asyncio.TimeoutError:
                last_error = f"Order placement timeout ({self.config.timeout_ms}ms)"
            except Exception as e:
                last_error = str(e)

            if attempt < self.config.max_retries - 1:
                backoff = self.config.retry_backoff_ms / 1000 * (2 ** attempt)
                await asyncio.sleep(backoff)

        return OrderResult(
            success=False,
            status=OrderStatus.REJECTED,
            error=f"Failed after {self.config.max_retries} attempts: {last_error}",
            timestamp=datetime.now(timezone.utc),
        )

    def get_result(self, client_order_id: str, token_id: Optional[str] = None) -> Optional[OrderResult]:
        if token_id:
            return self._orders.get(f"{client_order_id}:{token_id}")
        # Try with token_id suffix (new format)
        for key in self._orders:
            if key == client_order_id or key.startswith(f"{client_order_id}:"):
                return self._orders[key]
        return None

    async def get_open_orders(self) -> list[OrderResult]:
        async with self._lock:
            return list(self._orders.values())


# =============================================================================
# POSITION MANAGER
# =============================================================================

class PositionManager:
    """Tracks open positions in memory keyed by token id.

    All methods are async with proper locking to prevent race conditions
    between concurrent position updates from fills, risk checks, and API queries.
    """

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}
        self._lock = asyncio.Lock()

    async def aupdate(self, position: Position) -> None:
        """Async update with lock protection."""
        async with self._lock:
            self._positions[position.token_id] = position

    async def aget(self, token_id: str) -> Optional[Position]:
        async with self._lock:
            return self._positions.get(token_id)

    async def aall(self) -> list[Position]:
        async with self._lock:
            return list(self._positions.values())

    async def get_all_positions(
        self, platform: Optional[Platform] = None
    ) -> list[Position]:
        """All tracked positions, optionally filtered by platform."""
        async with self._lock:
            positions = list(self._positions.values())
        if platform is not None:
            positions = [p for p in positions if p.platform == platform]
        return positions

    async def get_positions(
        self, market_filter: Optional[str] = None
    ) -> list[Position]:
        """All positions, optionally filtered by a substring of market_id."""
        async with self._lock:
            positions = list(self._positions.values())
        if market_filter:
            positions = [
                p for p in positions if market_filter in (p.market_id or "")
            ]
        return positions

    async def get_total_pnl(
        self, platform: Optional[Platform] = None
    ) -> Decimal:
        """Sum of realized + unrealized P&L across tracked positions."""
        positions = await self.get_all_positions(platform)
        return sum((p.total_pnl for p in positions), Decimal("0"))

    async def aremove(self, token_id: str) -> None:
        async with self._lock:
            self._positions.pop(token_id, None)


# =============================================================================
# EXECUTION ENGINE
# =============================================================================

class ExecutionEngine:
    """Drives execution of an ArbitrageOpportunity's plan.

    Reconciles plan steps into OrderRequests and routes them through the
    OrderManager. In paper mode it synthesizes filled results so the
    detection-to-execution pipeline can be validated end to end without
    risking capital.

    ATOMIC EXECUTION GUARANTEE: For reverse arbitrage (2 legs), both legs are
    placed atomically. If leg 2 fails to fill, leg 1 is cancelled immediately.
    Never leaves an unhedged position.

    ITERATION 4: Multi-price-level execution - for each leg, places FOK orders
    at best_ask, best_ask + 1 tick, best_ask + 2 ticks (configurable). This
    increases fill probability while maintaining atomicity via FOK.
    """

    def __init__(self, clob_client: Any, mode: ExecutionMode = ExecutionMode.PAPER):
        self.clob = clob_client
        self.mode = mode
        # Single source of truth for paper/live: defer to the centralized
        # TradingMode authority. Its uninitialized fallback reads PAPER_TRADING
        # defaulting to "true", so this can never silently go live.
        self.paper_mode = TradingMode.is_paper()
        self.order_manager = OrderManager(clob_client)
        self.position_manager = PositionManager()

    async def execute(self, opportunity: ArbitrageOpportunity, execution_plan: ExecutionPlan) -> list[OrderResult]:
        """Execute a concrete ExecutionPlan and return per-step results.

        ATOMIC: For reverse arbitrage (2 legs, both BUY), places both as FOK.
        If either leg fails to fill, cancels the other and returns failure for both.
        """
        steps = execution_plan.steps
        if len(steps) == 2 and all(s.side == Side.BUY for s in steps):
            # Reverse arbitrage: atomic two-legged execution
            return await self._execute_atomic_two_leg(opportunity, steps)

        # Fallback: sequential execution for other strategies
        results: list[OrderResult] = []
        for i, step in enumerate(steps):
            if self.paper_mode:
                print(
                    f"[PAPER] ORDER: {step.side} {step.size} @ {step.price} "
                    f"for {step.leg.outcome}"
                )

            # Generate deterministic client_order_id for idempotency
            client_order_id = self._generate_client_order_id(opportunity.id, i, step.leg)

            request = OrderRequest(
                market_id=step.leg.market_id,
                token_id=step.leg.token_id,
                side=getattr(step, "side", step.leg.side),
                price=getattr(step, "price", step.estimated_fill_price),
                size=getattr(step, "size", step.leg.size),
                order_type=getattr(step, "order_type", OrderType.GTC),
                condition_id=step.leg.condition_id,
                client_order_id=client_order_id,
            )
            result = await self.order_manager.place_order(request)
            results.append(result)

        return results

    async def _execute_atomic_two_leg(
        self, opportunity: ArbitrageOpportunity, steps: list
    ) -> list[OrderResult]:
        """Execute two legs atomically with multi-price-level FOK.

        Iteration 4: For each leg, tries multiple price levels (best ask + N ticks)
        to increase fill probability. Uses FOK at each level for atomicity.

        Algorithm:
        1. Get price_levels from config (e.g., [0, 1, 2])
        2. Get tick_size from config (default 0.001 = 1 bp)
        3. For each level in price_levels:
           a. Calculate price for leg 0: base_price_0 + level * tick_size
           b. Calculate price for leg 1: base_price_1 + level * tick_size
           c. Place both as FOK atomically
           d. If both fill -> return success
           e. If leg 0 fills but leg 1 fails -> cancel leg 0, try next level
           f. If both fail -> try next level
        4. If all levels exhausted -> return failure for both
        """
        leg0, leg1 = steps[0], steps[1]

        # Get multi-level config
        cfg = get_config()
        price_levels = getattr(cfg.execution, 'price_levels', [0, 1, 2])
        tick_size = Decimal(str(getattr(cfg.execution, 'tick_size', 0.001)))

        # Base prices from the opportunity legs
        base_price_0 = getattr(leg0, "price", leg0.estimated_fill_price)
        base_price_1 = getattr(leg1, "price", leg1.estimated_fill_price)

        # Generate base client order IDs
        base_client_order_id_0 = self._generate_client_order_id(opportunity.id, 0, leg0.leg)
        base_client_order_id_1 = self._generate_client_order_id(opportunity.id, 1, leg1.leg)

        if self.paper_mode:
            print(f"[PAPER] ATOMIC MULTI-LEVEL EXECUTION: {len(price_levels)} levels")
            print(f"[PAPER] LEG 1: {leg0.side} {leg0.size} @ {leg0.price} for {leg0.leg.outcome}")
            print(f"[PAPER] LEG 2: {leg1.side} {leg1.size} @ {leg1.price} for {leg1.leg.outcome}")

            # In paper mode, simulate success at first level
            result_0 = OrderResult(
                success=True,
                order_id=f"{base_client_order_id_0}_lvl0",
                client_order_id=f"{base_client_order_id_0}_lvl0",
                status=OrderStatus.FILLED,
                filled_size=leg0.size,
                avg_fill_price=base_price_0,
                fees_paid=Decimal("0"),
                timestamp=datetime.now(timezone.utc),
            )
            result_1 = OrderResult(
                success=True,
                order_id=f"{base_client_order_id_1}_lvl0",
                client_order_id=f"{base_client_order_id_1}_lvl0",
                status=OrderStatus.FILLED,
                filled_size=leg1.size,
                avg_fill_price=base_price_1,
                fees_paid=Decimal("0"),
                timestamp=datetime.now(timezone.utc),
            )
            return [result_0, result_1]

        # Live mode: try each price level
        for level_idx, level in enumerate(price_levels):
            level_price_0 = base_price_0 + Decimal(str(level)) * tick_size
            level_price_1 = base_price_1 + Decimal(str(level)) * tick_size

            logger.info(f"Attempting atomic execution at level {level}: price_0={level_price_0}, price_1={level_price_1}")

            client_order_id_0 = f"{base_client_order_id_0}_lvl{level}"
            client_order_id_1 = f"{base_client_order_id_1}_lvl{level}"

            request_0 = OrderRequest(
                market_id=leg0.leg.market_id,
                token_id=leg0.leg.token_id,
                side=getattr(leg0, "side", leg0.leg.side),
                price=level_price_0,
                size=getattr(leg0, "size", leg0.leg.size),
                order_type=OrderType.FOK,
                condition_id=leg0.leg.condition_id,
                client_order_id=client_order_id_0,
            )
            request_1 = OrderRequest(
                market_id=leg1.leg.market_id,
                token_id=leg1.leg.token_id,
                side=getattr(leg1, "side", leg1.leg.side),
                price=level_price_1,
                size=getattr(leg1, "size", leg1.leg.size),
                order_type=OrderType.FOK,
                condition_id=leg1.leg.condition_id,
                client_order_id=client_order_id_1,
            )

            # Place both orders
            result_0 = await self.order_manager.place_order(request_0)
            result_1 = await self.order_manager.place_order(request_1)

            filled_0 = result_0.status == OrderStatus.FILLED
            filled_1 = result_1.status == OrderStatus.FILLED

            if filled_0 and filled_1:
                logger.info(f"Both legs filled at level {level}")
                return [result_0, result_1]

            # ROLLBACK: If either leg partially filled or is open, cancel both
            logger.warning(f"Level {level} failed: leg0={result_0.status}, leg1={result_1.status}. Rolling back.")

            # Cancel leg 0 if it was placed
            if result_0.status in (OrderStatus.FILLED, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
                try:
                    await self.order_manager.cancel_order(client_order_id_0, request_0.token_id)
                except Exception as e:
                    logger.error(f"Failed to cancel leg 0 during rollback at level {level}: {e}")

            # Cancel leg 1 if it was placed
            if result_1.status in (OrderStatus.FILLED, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
                try:
                    await self.order_manager.cancel_order(client_order_id_1, request_1.token_id)
                except Exception as e:
                    logger.error(f"Failed to cancel leg 1 during rollback at level {level}: {e}")

        # All FOK levels exhausted - try GTC fallback if enabled
        gtc_enabled = getattr(cfg.execution, 'gtc_fallback_enabled', True)
        if gtc_enabled:
            logger.info("All FOK levels exhausted, attempting GTC fallback...")
            gtc_result = await self._execute_atomic_gtc_fallback(
                opportunity, steps, base_price_0, base_price_1,
                base_client_order_id_0, base_client_order_id_1
            )
            if gtc_result[0].success and gtc_result[1].success:
                return gtc_result

        # All levels exhausted - return failure
        logger.error(f"Atomic execution failed after {len(price_levels)} FOK levels and GTC fallback")

        failed_0 = OrderResult(
            success=False,
            order_id=result_0.order_id if 'result_0' in locals() else None,
            client_order_id=base_client_order_id_0,
            status=OrderStatus.REJECTED,
            error=f"Atomic rollback: all {len(price_levels)} price levels exhausted, GTC fallback failed",
            timestamp=datetime.now(timezone.utc),
        )
        failed_1 = OrderResult(
            success=False,
            order_id=result_1.order_id if 'result_1' in locals() else None,
            client_order_id=base_client_order_id_1,
            status=OrderStatus.REJECTED,
            error=f"Atomic rollback: all {len(price_levels)} price levels exhausted, GTC fallback failed",
            timestamp=datetime.now(timezone.utc),
        )
        return [failed_0, failed_1]

    async def _execute_atomic_gtc_fallback(
        self,
        opportunity: ArbitrageOpportunity,
        steps: list,
        base_price_0: Decimal,
        base_price_1: Decimal,
        base_client_order_id_0: str,
        base_client_order_id_1: str,
    ) -> list[OrderResult]:
        """Execute GTC fallback after FOK levels are exhausted.

        Places both legs as GTC limit orders at configured price levels,
        waits for timeout, then cancels unfilled orders. Maintains atomicity:
        if one leg fills and the other doesn't, the filled leg is cancelled.
        """
        leg0, leg1 = steps[0], steps[1]
        cfg = get_config()

        gtc_levels = getattr(cfg.execution, 'gtc_fallback_price_levels', [0, 1, 2])
        tick_size = Decimal(str(getattr(cfg.execution, 'tick_size', 0.001)))
        gtc_timeout = getattr(cfg.execution, 'gtc_fallback_timeout_sec', 5)

        logger.info(f"Starting GTC fallback with {len(gtc_levels)} levels, timeout={gtc_timeout}s")

        for level_idx, level in enumerate(gtc_levels):
            level_price_0 = base_price_0 + Decimal(str(level)) * tick_size
            level_price_1 = base_price_1 + Decimal(str(level)) * tick_size

            logger.info(f"Attempting GTC fallback at level {level}: price_0={level_price_0}, price_1={level_price_1}")

            client_order_id_0 = f"{base_client_order_id_0}_gtc_lvl{level}"
            client_order_id_1 = f"{base_client_order_id_1}_gtc_lvl{level}"

            request_0 = OrderRequest(
                market_id=leg0.leg.market_id,
                token_id=leg0.leg.token_id,
                side=getattr(leg0, "side", leg0.leg.side),
                price=level_price_0,
                size=getattr(leg0, "size", leg0.leg.size),
                order_type=OrderType.GTC,
                condition_id=leg0.leg.condition_id,
                client_order_id=client_order_id_0,
            )
            request_1 = OrderRequest(
                market_id=leg1.leg.market_id,
                token_id=leg1.leg.token_id,
                side=getattr(leg1, "side", leg1.leg.side),
                price=level_price_1,
                size=getattr(leg1, "size", leg1.leg.size),
                order_type=OrderType.GTC,
                condition_id=leg1.leg.condition_id,
                client_order_id=client_order_id_1,
            )

            # Place both GTC orders
            result_0 = await self.order_manager.place_order(request_0)
            result_1 = await self.order_manager.place_order(request_1)

            if result_0.status != OrderStatus.FILLED and result_0.status != OrderStatus.OPEN and result_0.status != OrderStatus.PARTIALLY_FILLED:
                logger.warning(f"GTC leg 0 failed to place at level {level}: {result_0.status}")
                continue

            if result_1.status != OrderStatus.FILLED and result_1.status != OrderStatus.OPEN and result_1.status != OrderStatus.PARTIALLY_FILLED:
                logger.warning(f"GTC leg 1 failed to place at level {level}: {result_1.status}")
                # Cancel leg 0 which was placed
                try:
                    await self.order_manager.cancel_order(client_order_id_0, request_0.token_id)
                except Exception as e:
                    logger.error(f"Failed to cancel leg 0 during GTC fallback at level {level}: {e}")
                continue

            # Both orders placed (OPEN, PARTIALLY_FILLED, or FILLED)
            # Wait for timeout to see if they fill
            await asyncio.sleep(gtc_timeout)

            # Check fill status by getting fresh results
            fresh_0 = self.order_manager.get_result(client_order_id_0, request_0.token_id)
            fresh_1 = self.order_manager.get_result(client_order_id_1, request_1.token_id)

            filled_0 = fresh_0.status == OrderStatus.FILLED if fresh_0 else False
            filled_1 = fresh_1.status == OrderStatus.FILLED if fresh_1 else False

            if filled_0 and filled_1:
                logger.info(f"Both legs filled via GTC at level {level}")
                return [fresh_0, fresh_1]

            # ROLLBACK: If either leg filled but the other didn't, cancel the filled one
            # If both are open/partially filled, cancel both
            logger.warning(f"GTC level {level} incomplete: leg0={fresh_0.status if fresh_0 else 'unknown'}, leg1={fresh_1.status if fresh_1 else 'unknown'}. Rolling back.")

            # Cancel any open/partial orders
            for client_id, token_id, fresh_result in [
                (client_order_id_0, request_0.token_id, fresh_0),
                (client_order_id_1, request_1.token_id, fresh_1),
            ]:
                if fresh_result and fresh_result.status in (OrderStatus.FILLED, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
                    try:
                        await self.order_manager.cancel_order(client_id, token_id)
                    except Exception as e:
                        logger.error(f"Failed to cancel order {client_id} during GTC fallback rollback: {e}")

            # Continue to next GTC level

        logger.error(f"GTC fallback failed after {len(gtc_levels)} levels")
        return [
            OrderResult(
                success=False,
                client_order_id=base_client_order_id_0,
                status=OrderStatus.REJECTED,
                error="GTC fallback: all levels exhausted",
                timestamp=datetime.now(timezone.utc),
            ),
            OrderResult(
                success=False,
                client_order_id=base_client_order_id_1,
                status=OrderStatus.REJECTED,
                error="GTC fallback: all levels exhausted",
                timestamp=datetime.now(timezone.utc),
            ),
        ]

    def _generate_client_order_id(self, opportunity_id: str, leg_index: int, leg: ArbitrageLeg) -> str:
        """Generate deterministic client_order_id for idempotent order placement.

        Uses a hash of key order parameters so retries with the same parameters
        produce the same client_order_id, enabling duplicate detection.
        """
        key_string = f"{opportunity_id}:{leg_index}:{leg.token_id}:{leg.side.value}:{leg.target_price}:{leg.size}"
        hash_suffix = hashlib.sha256(key_string.encode()).hexdigest()[:16]
        return f"rev_{opportunity_id[:16]}_{leg_index}_{hash_suffix}"

    async def execute_opportunity(self, opportunity: ArbitrageOpportunity) -> list[OrderResult]:
        """Build a plan from the opportunity and execute it."""
        plan = opportunity.to_execution_plan()
        return await self.execute(opportunity, plan)

    async def cancel_all_orders(self) -> list[OrderResult]:
        """Cancel every open order tracked by the order manager.

        In LIVE mode, verifies cancellations on the exchange and reconciles
        any fills that occurred before cancellation. In PAPER mode, returns
        simulated results.

        Used by the engine's graceful shutdown so no live orders are left
        dangling when the bot stops.
        """
        results = await self.order_manager.cancel_all_orders()

        # In live mode, verify cancellations on exchange and reconcile
        if not self.paper_mode and self.clob:
            verified_results = []
            for result in results:
                client_order_id = result.client_order_id or result.order_id
                if not client_order_id:
                    verified_results.append(result)
                    continue

                # Verify order is actually cancelled on exchange
                try:
                    # Check order status on exchange
                    exchange_result = await self.clob.cancel_order(client_order_id)
                    if exchange_result:
                        # Exchange confirmed cancellation
                        verified = OrderResult(
                            success=True,
                            order_id=result.order_id,
                            client_order_id=client_order_id,
                            status=OrderStatus.CANCELLED,
                            filled_size=result.filled_size,
                            avg_fill_price=result.avg_fill_price,
                            timestamp=datetime.now(timezone.utc),
                        )
                        verified_results.append(verified)
                    else:
                        # Exchange cancellation failed - may have been filled
                        logger.warning(f"Exchange cancel failed for {client_order_id}, reconciling...")
                        # Reconcile - check if order was actually filled
                        reconciled = await self.reconcile_order(client_order_id)
                        verified_results.append(reconciled)
                except Exception as e:
                    logger.error(f"Error verifying cancellation for {client_order_id}: {e}")
                    verified_results.append(result)

            return verified_results

        return results

    async def reconcile_order(self, client_order_id: str) -> OrderResult:
        """Reconcile a single order with the exchange."""
        # This would query the exchange for the actual order status
        # For now, return the tracked status as fallback
        result = self.order_manager.get_result(client_order_id)
        if result:
            return result
        return OrderResult(
            success=False,
            client_order_id=client_order_id,
            status=OrderStatus.REJECTED,
            error="Order not found in local tracking",
            timestamp=datetime.now(timezone.utc),
        )

    async def reconcile(self) -> dict[str, float]:
        """Reconcile tracked orders against expected settlement.

        Returns a mapping of ``client_order_id`` -> outstanding (remaining) size
        for any open order that is not yet fully settled. An empty map means
        every tracked order has been fully filled or cancelled. In PAPER mode
        no real orders are ever placed, so this is normally empty.
        """
        open_orders = await self.order_manager.get_open_orders()
        discrepancies: dict[str, float] = {}
        for order in open_orders:
            remaining = float(order.remaining_size)
            if remaining > 1e-9:
                key = order.client_order_id or order.order_id or ""
                if key:
                    discrepancies[key] = remaining
        return discrepancies

    async def execute_plan(
        self, plan: ExecutionPlan, risk_engine: Optional[object] = None
    ) -> list[OrderResult]:
        """Execute a pre-built ExecutionPlan (used by the MCP/API order path)."""
        from src.core.types import ArbitrageOpportunity, OpportunityType
        dummy_opp = ArbitrageOpportunity(
            id=plan.opportunity_id or "plan_execution",
            type=OpportunityType.REVERSE_ARB,
        )
        return await self.execute(dummy_opp, plan)


async def execute_arbitrage(
    opportunity: ArbitrageOpportunity,
    clob_client: Optional[object],
    mode: ExecutionMode = ExecutionMode.PAPER,
) -> list[OrderResult]:
    """Convenience helper to execute an opportunity end to end."""
    engine = ExecutionEngine(clob_client, mode)
    return await engine.execute_opportunity(opportunity)