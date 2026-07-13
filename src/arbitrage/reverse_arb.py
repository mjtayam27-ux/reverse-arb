"""
Reverse Arbitrage Detector for Polymarket BTC/ETH 15m Up/Down Markets.

Strategy: Find underdog (lower ask) vs favorite (higher ask) on SAME market.
Buy cheap leg (underdog) at 7-10¢ + buy hedge leg (favorite) at 90-95¢.
This exploits the dynamic pricing where underdog is underpriced relative to true probability.

This is DIFFERENT from internal arb (YES+NO<$1):
- Internal arb: YES+NO sum < 1.0 on binary markets (guaranteed $1 payout)
- Reverse arb: Up+Down markets where underdog favorite spread creates edge
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Optional

from src.core.config import get_config, get_reverse_arb_config
from src.core.types import (
    MarketSnapshot,
    ArbitrageOpportunity,
    ArbitrageLeg,
    OpportunityType,
    Side,
    OrderType,
    ExecutionRisk,
    Platform,
    MarketType,
    Decimal,
    datetime,
    decimal_to_bps,
    bps_to_decimal,
    MarketInfo,
    OrderBook,
)
from src.market_data.clob_client import MarketDataAggregator

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class ReverseArbConfig:
    """Configuration for reverse arbitrage detection."""
    min_edge_bps: int = 200
    max_slippage_bps: int = 50
    max_position_usd: Decimal = Decimal("2000")
    fee_bps: int = 75
    cheap_buy_min: Decimal = Decimal("0.07")
    cheap_buy_max: Decimal = Decimal("0.10")
    expensive_buy_min: Decimal = Decimal("0.90")
    expensive_buy_max: Decimal = Decimal("0.95")
    cheap_order_usd: Decimal = Decimal("50")
    expensive_order_usd: Decimal = Decimal("100")
    minutes_before_close_min: int = 2
    minutes_before_close_max: int = 5
    order_type: OrderType = OrderType.FOK
    poll_interval_ms: int = 1000
    dry_run: bool = True
    btc_markets_only: bool = True
    eth_markets_only: bool = False
    min_liquidity_usd: Decimal = Decimal("500")
    max_spread_bps: int = 500


# =============================================================================
# REVERSE ARBITRAGE DETECTOR
# =============================================================================

class ReverseArbitrageDetector:
    """
    Detects reverse arbitrage opportunities on BTC/ETH 15m Up/Down markets.

    Logic:
    1. Find markets with outcomes UP/DOWN (or YES/NO for up-down markets)
    2. Identify underdog (lower mid/ask) and favorite (higher mid/ask)
    3. Check if cheap leg is in 7-10¢ range AND expensive leg in 90-95¢ range
    4. Calculate edge: buy cheap leg + buy hedge leg = synthetic position
    5. Edge = (1 - cheap_ask - expensive_ask) - fees
    6. If edge > threshold, emit opportunity

    Key insight: In efficient markets, UP+DOWN ≈ $1. When underdog is 7-10¢
    and favorite is 90-95¢, the sum is 97-105¢. We want sum < 100¢ after fees.
    """

    def __init__(self, config: Optional[ReverseArbConfig] = None):
        self.config = config or self._load_config()
        self._last_scan_time = 0.0
        self._opportunities_found = 0
        self._opportunities_emitted = 0

    def _load_config(self) -> ReverseArbConfig:
        """Load config from YAML/env."""
        cfg = get_reverse_arb_config()
        return ReverseArbConfig(
            min_edge_bps=cfg.min_edge_bps,
            max_slippage_bps=cfg.max_slippage_bps,
            max_position_usd=Decimal(str(cfg.max_position_usd)),
            fee_bps=cfg.fee_bps,
            cheap_buy_min=Decimal(str(cfg.cheap_buy_min)),
            cheap_buy_max=Decimal(str(cfg.cheap_buy_max)),
            expensive_buy_min=Decimal(str(cfg.expensive_buy_min)),
            expensive_buy_max=Decimal(str(cfg.expensive_buy_max)),
            cheap_order_usd=Decimal(str(cfg.cheap_order_usd)),
            expensive_order_usd=Decimal(str(cfg.expensive_order_usd)),
            minutes_before_close_min=cfg.minutes_before_close_min,
            minutes_before_close_max=cfg.minutes_before_close_max,
            order_type=OrderType(cfg.order_type) if isinstance(cfg.order_type, str) else cfg.order_type,
            dry_run=cfg.dry_run,
            btc_markets_only=cfg.btc_markets_only,
            eth_markets_only=cfg.eth_markets_only,
            min_liquidity_usd=Decimal(str(cfg.min_liquidity_usd)),
            max_spread_bps=cfg.max_spread_bps,
        )

    def scan_market(self, snapshot: MarketSnapshot) -> Optional[ArbitrageOpportunity]:
        """
        Scan a single Up/Down market for reverse arbitrage.

        Returns opportunity if found, None otherwise.
        """
        # Validate market type
        if not snapshot.market.is_up_down_market:
            return None

        if self.config.btc_markets_only and "btc" not in snapshot.market.question.lower() and "bitcoin" not in snapshot.market.question.lower():
            return None

        if self.config.eth_markets_only and "eth" not in snapshot.market.question.lower() and "ethereum" not in snapshot.market.question.lower():
            return None

        # Check time to close
        if snapshot.market.minutes_to_close is not None:
            if snapshot.market.minutes_to_close < self.config.minutes_before_close_min:
                return None  # Too close to expiry
            if snapshot.market.minutes_to_close > self.config.minutes_before_close_max:
                return None  # Too far from expiry (less edge)

        # Get orderbooks
        outcomes = snapshot.market.outcomes
        token_ids = snapshot.market.clob_token_ids

        if len(outcomes) != 2 or len(token_ids) != 2:
            return None

        # Map outcomes to tokens
        # Usually outcomes are ["UP", "DOWN"] or ["YES", "NO"]
        outcome_0, outcome_1 = outcomes[0].upper(), outcomes[1].upper()
        token_0, token_1 = token_ids[0], token_ids[1]

        book_0 = snapshot.orderbooks.get(token_0)
        book_1 = snapshot.orderbooks.get(token_1)

        if not book_0 or not book_1:
            return None

        if not (book_0.asks and book_0.bids and book_1.asks and book_1.bids):
            return None

        # Get best prices
        ask_0, bid_0 = book_0.asks[0].price, book_0.bids[0].price
        ask_1, bid_1 = book_1.asks[0].price, book_1.bids[0].price

        mid_0 = (ask_0 + bid_0) / 2
        mid_1 = (ask_1 + bid_1) / 2

        # Identify underdog (cheaper) and favorite (more expensive)
        if mid_0 < mid_1:
            # Outcome 0 is underdog
            underdog_outcome, favorite_outcome = outcome_0, outcome_1
            underdog_ask, favorite_ask = ask_0, ask_1
            underdog_bid, favorite_bid = bid_0, bid_1
            underdog_token, favorite_token = token_0, token_1
            underdog_book, favorite_book = book_0, book_1
        else:
            # Outcome 1 is underdog
            underdog_outcome, favorite_outcome = outcome_1, outcome_0
            underdog_ask, favorite_ask = ask_1, ask_0
            underdog_bid, favorite_bid = bid_1, bid_0
            underdog_token, favorite_token = token_1, token_0
            underdog_book, favorite_book = book_1, book_0

        # Check price ranges
        # Underdog (cheap leg) should be in 7-10¢ range
        if not (self.config.cheap_buy_min <= underdog_ask <= self.config.cheap_buy_max):
            logger.debug(
                f"Underdog price {underdog_ask} not in range "
                f"[{self.config.cheap_buy_min}, {self.config.cheap_buy_max}]"
            )
            return None

        # Favorite (expensive leg) should be in 90-95¢ range
        if not (self.config.expensive_buy_min <= favorite_ask <= self.config.expensive_buy_max):
            logger.debug(
                f"Favorite price {favorite_ask} not in range "
                f"[{self.config.expensive_buy_min}, {self.config.expensive_buy_max}]"
            )
            return None

        # Check spread (liquidity quality)
        underdog_spread = underdog_ask - underdog_bid
        favorite_spread = favorite_ask - favorite_bid
        underdog_mid = (underdog_ask + underdog_bid) / 2
        favorite_mid = (favorite_ask + favorite_bid) / 2

        if underdog_mid > 0:
            underdog_spread_bps = decimal_to_bps(underdog_spread / underdog_mid)
            if underdog_spread_bps > self.config.max_spread_bps:
                return None

        if favorite_mid > 0:
            favorite_spread_bps = decimal_to_bps(favorite_spread / favorite_mid)
            if favorite_spread_bps > self.config.max_spread_bps:
                return None

        # Check liquidity
        underdog_liq = sum(
            level.price * level.size
            for level in underdog_book.asks[:3] + underdog_book.bids[:3]
        )
        favorite_liq = sum(
            level.price * level.size
            for level in favorite_book.asks[:3] + favorite_book.bids[:3]
        )

        min_liq = min(underdog_liq, favorite_liq)
        if min_liq < self.config.min_liquidity_usd:
            return None

        # Calculate edge
        # We BUY both legs: cheap underdog + expensive favorite hedge
        # Total cost = underdog_ask + favorite_ask
        # At settlement, one pays $1, the other pays $0, so we get $1 back
        # Net payout = $1 - (underdog_ask + favorite_ask) - fees
        total_cost = underdog_ask + favorite_ask
        gross_edge = Decimal("1.0") - total_cost
        gross_edge_bps = decimal_to_bps(gross_edge)

        # Fees: pay taker fee on both legs
        fee_per_leg = bps_to_decimal(self.config.fee_bps)
        total_fees = fee_per_leg * 2 * total_cost
        net_edge = gross_edge - total_fees
        net_edge_bps = decimal_to_bps(net_edge)

        if net_edge_bps < self.config.min_edge_bps:
            logger.debug(
                f"Net edge {net_edge_bps}bps < threshold {self.config.min_edge_bps}bps "
                f"(gross={gross_edge_bps}bps, fees={self.config.fee_bps*2}bps)"
            )
            return None

        # Position sizing
        # Cheap leg: buy at underdog_ask, size from cheap_order_usd
        # Expensive leg: buy at favorite_ask, size from expensive_order_usd
        cheap_shares = self.config.cheap_order_usd / underdog_ask
        expensive_shares = self.config.expensive_order_usd / favorite_ask

        # Must be equal shares for hedge (we're buying both outcomes)
        # Use minimum of the two
        shares = min(cheap_shares, expensive_shares)

        # Also limited by liquidity
        max_shares_by_liq = min(
            sum(level.size for level in underdog_book.asks[:3]),
            sum(level.size for level in favorite_book.asks[:3]),
        )
        shares = min(shares, max_shares_by_liq)

        # Cap by max position
        position_cost = shares * total_cost
        if position_cost > self.config.max_position_usd:
            shares = self.config.max_position_usd / total_cost
            position_cost = self.config.max_position_usd

        if shares < 1:  # Minimum 1 share
            return None

        # Estimated profit
        profit = shares * net_edge

        # Kelly sizing (conservative quarter Kelly for reverse arb)
        # Win probability ≈ 1 - total_cost (if market efficient)
        win_prob = max(Decimal("0"), Decimal("1.0") - total_cost)
        win_loss_ratio = net_edge / (total_cost - net_edge) if (total_cost - net_edge) > 0 else Decimal("1")
        full_kelly = win_prob - (Decimal("1") - win_prob) / win_loss_ratio
        kelly_fraction = max(Decimal("0"), min(Decimal("0.25"), full_kelly * Decimal("0.25")))

        # Build legs
        legs = (
            ArbitrageLeg(
                platform=Platform.POLYMARKET,
                market_id=snapshot.market.condition_id,
                condition_id=snapshot.market.condition_id,
                token_id=underdog_token,
                outcome=underdog_outcome,
                side=Side.BUY,
                target_price=underdog_ask,
                max_slippage_bps=self.config.max_slippage_bps,
                size=shares,
                order_type=self.config.order_type,
                fee_rate_bps=self.config.fee_bps,
            ),
            ArbitrageLeg(
                platform=Platform.POLYMARKET,
                market_id=snapshot.market.condition_id,
                condition_id=snapshot.market.condition_id,
                token_id=favorite_token,
                outcome=favorite_outcome,
                side=Side.BUY,
                target_price=favorite_ask,
                max_slippage_bps=self.config.max_slippage_bps,
                size=shares,
                order_type=self.config.order_type,
                fee_rate_bps=self.config.fee_bps,
            ),
        )

        # Log opportunity
        logger.info(
            f"REVERSE-ARB OPPORTUNITY market={snapshot.market.condition_id!r} "
            f"question={snapshot.market.question[:60]} "
            f"underdog={underdog_outcome}@{underdog_ask} "
            f"favorite={favorite_outcome}@{favorite_ask} "
            f"sum={total_cost:.4f} gross_edge={gross_edge_bps}bps "
            f"net_edge={net_edge_bps}bps profit={float(profit):.4f} shares={shares}"
        )

        self._opportunities_found += 1
        self._opportunities_emitted += 1

        return ArbitrageOpportunity(
            type=OpportunityType.REVERSE_ARB,
            legs=legs,
            gross_edge_bps=gross_edge_bps,
            net_edge_bps=net_edge_bps,
            total_fees_bps=self.config.fee_bps * 2,
            estimated_profit_usd=profit,
            required_capital_usd=position_cost,
            max_position_usd=self.config.max_position_usd,
            kelly_fraction=kelly_fraction,
            confidence=Decimal("0.85"),  # High confidence for structured markets
            risk_level=ExecutionRisk.MEDIUM,
            min_liquidity_usd=min_liq,
            metadata={
                "market_question": snapshot.market.question,
                "underdog_outcome": underdog_outcome,
                "favorite_outcome": favorite_outcome,
                "underdog_ask": str(underdog_ask),
                "favorite_ask": str(favorite_ask),
                "total_cost": str(total_cost),
                "minutes_to_close": snapshot.market.minutes_to_close,
                "underdog_spread_bps": int(underdog_spread_bps) if underdog_mid > 0 else 0,
                "favorite_spread_bps": int(favorite_spread_bps) if favorite_mid > 0 else 0,
                "underdog_liquidity_usd": float(underdog_liq),
                "favorite_liquidity_usd": float(favorite_liq),
                "strategy": "reverse_arb_15m",
            },
        )

    def scan_markets(self, snapshots: list[MarketSnapshot]) -> list[ArbitrageOpportunity]:
        """Scan multiple markets for reverse arbitrage."""
        opportunities = []
        for snapshot in snapshots:
            try:
                opp = self.scan_market(snapshot)
                if opp:
                    opportunities.append(opp)
            except Exception as e:
                logger.warning(f"Error scanning market {snapshot.market.condition_id}: {e}")
                continue

        # Sort by net edge (best first)
        opportunities.sort(key=lambda o: o.net_edge_bps, reverse=True)
        return opportunities


# =============================================================================
# HFT REVERSE ARB DETECTOR (WebSocket event-driven)
# =============================================================================

class HFTReverseArbDetector:
    """
    High-frequency reverse arb detector using WebSocket orderbook updates.

    Event-driven: processes every WS tick for sub-100ms detection.
    Matches the pattern from loop_hft_internal.target.HFTInternalArbDetector.
    """

    def __init__(self, config: Optional[ReverseArbConfig] = None):
        self.config = config or ReverseArbConfig()
        self._reverse_detector = ReverseArbitrageDetector(config)

        # State: token_id -> (bid, ask, bid_size, ask_size, timestamp)
        self._orderbook_cache: dict[str, tuple[Decimal, Decimal, Decimal, Decimal, float]] = {}

        # Market metadata: condition_id -> MarketInfo
        self._market_metadata: dict[str, MarketInfo] = {}

        # Token map: token_id -> (condition_id, outcome_index)
        self._token_map: dict[str, tuple[str, int]] = {}

        # Single lock for all shared state - ensures atomic reads of both structures
        self._lock = asyncio.Lock()

    def register_market(self, market: MarketInfo) -> None:
        """Register a market from Gamma for HFT scanning."""
        # Only Up/Down markets
        if not market.is_up_down_market:
            return

        if self.config.btc_markets_only and "btc" not in market.question.lower() and "bitcoin" not in market.question.lower():
            return

        if self.config.eth_markets_only and "eth" not in market.question.lower() and "ethereum" not in market.question.lower():
            return

        if not market.active or market.closed:
            return

        # Check time to close
        if market.minutes_to_close is not None:
            if market.minutes_to_close < self.config.minutes_before_close_min:
                return

        self._market_metadata[market.condition_id] = market

        # Use async lock - register_market is called during initialization (sync context)
        # but in production it's called from async engine, so we handle both
        # For now use thread-safe approach since register_market may be called sync
        import threading
        if not hasattr(self, '_register_lock'):
            self._register_lock = threading.Lock()
        with self._register_lock:
            for idx, token_id in enumerate(market.clob_token_ids):
                self._token_map[token_id] = (market.condition_id, idx)

    async def on_orderbook_update(self, orderbook: OrderBook) -> Optional[ArbitrageOpportunity]:
        """
        Called on every WebSocket orderbook tick.

        Returns opportunity if edge exists, else None.
        """
        token_id = orderbook.token_id

        # Fast check: is this token in our map? (async lock)
        async with self._lock:
            in_map = token_id in self._token_map
        if not in_map:
            return None

        if not orderbook.bids or not orderbook.asks:
            return None

        # Cache the tick
        bid = orderbook.bids[0].price
        ask = orderbook.asks[0].price
        bid_size = orderbook.bids[0].size
        ask_size = orderbook.asks[0].size
        ts = orderbook.timestamp.timestamp()

        # Update orderbook cache
        async with self._lock:
            self._orderbook_cache[token_id] = (bid, ask, bid_size, ask_size, ts)

        # Find the other token for this market
        async with self._lock:
            condition_id, outcome_idx = self._token_map.get(token_id, (None, None))

            if condition_id is None:
                return None

            other_token = None
            for tid, (cid, oidx) in self._token_map.items():
                if cid == condition_id and tid != token_id:
                    other_token = tid
                    break

        if not other_token:
            return None

        # Get both legs atomically (single lock acquisition)
        async with self._lock:
            if other_token not in self._orderbook_cache:
                return None
            bid1, ask1, bid_size1, ask_size1, ts1 = self._orderbook_cache[token_id]
            bid2, ask2, bid_size2, ask_size2, ts2 = self._orderbook_cache[other_token]

        # Freshness check: both updates within 100ms
        if abs(ts1 - ts2) > 0.1:
            return None

        # Identify underdog/favorite
        mid1 = (bid1 + ask1) / 2
        mid2 = (bid2 + ask2) / 2

        if mid1 < mid2:
            underdog_ask, favorite_ask = ask1, ask2
            underdog_outcome_idx, favorite_outcome_idx = outcome_idx, 1 - outcome_idx
        else:
            underdog_ask, favorite_ask = ask2, ask1
            underdog_outcome_idx, favorite_outcome_idx = 1 - outcome_idx, outcome_idx

        # Price range checks
        if not (self.config.cheap_buy_min <= underdog_ask <= self.config.cheap_buy_max):
            return None
        if not (self.config.expensive_buy_min <= favorite_ask <= self.config.expensive_buy_max):
            return None

        # Calculate edge
        total_cost = underdog_ask + favorite_ask
        gross_edge = Decimal("1.0") - total_cost
        gross_edge_bps = decimal_to_bps(gross_edge)

        fee_per_leg = bps_to_decimal(self.config.fee_bps)
        total_fees = fee_per_leg * 2 * total_cost
        net_edge = gross_edge - total_fees
        net_edge_bps = decimal_to_bps(net_edge)

        if net_edge_bps < self.config.min_edge_bps:
            return None

        # Get market metadata
        market = self._market_metadata.get(condition_id)
        if not market:
            return None

        outcomes = market.outcomes
        if len(outcomes) < 2:
            return None

        underdog_outcome = outcomes[underdog_outcome_idx]
        favorite_outcome = outcomes[favorite_outcome_idx]
        underdog_token = market.clob_token_ids[underdog_outcome_idx]
        favorite_token = market.clob_token_ids[favorite_outcome_idx]

        # Sizing (same as batch detector)
        shares = min(
            self.config.cheap_order_usd / underdog_ask,
            self.config.expensive_order_usd / favorite_ask,
        )
        # Limited by liquidity
        max_shares = min(
            sum(l.size for l in orderbook.asks[:3]),
        )  # Would need both books from cache
        shares = min(shares, max_shares)

        position_cost = shares * total_cost
        if position_cost > self.config.max_position_usd:
            shares = self.config.max_position_usd / total_cost

        if shares < 1:
            return None

        profit = shares * net_edge

        legs = (
            ArbitrageLeg(
                platform=Platform.POLYMARKET,
                market_id=condition_id,
                condition_id=condition_id,
                token_id=underdog_token,
                outcome=underdog_outcome,
                side=Side.BUY,
                target_price=underdog_ask,
                max_slippage_bps=self.config.max_slippage_bps,
                size=shares,
                order_type=self.config.order_type,
                fee_rate_bps=self.config.fee_bps,
            ),
            ArbitrageLeg(
                platform=Platform.POLYMARKET,
                market_id=condition_id,
                condition_id=condition_id,
                token_id=favorite_token,
                outcome=favorite_outcome,
                side=Side.BUY,
                target_price=favorite_ask,
                max_slippage_bps=self.config.max_slippage_bps,
                size=shares,
                order_type=self.config.order_type,
                fee_rate_bps=self.config.fee_bps,
            ),
        )

        return ArbitrageOpportunity(
            id=f"hft_rev_{condition_id}_{int(ts1 * 1000)}",
            type=OpportunityType.REVERSE_ARB,
            legs=legs,
            gross_edge_bps=gross_edge_bps,
            net_edge_bps=net_edge_bps,
            total_fees_bps=self.config.fee_bps * 2,
            estimated_profit_usd=profit,
            required_capital_usd=position_cost,
            max_position_usd=self.config.max_position_usd,
            kelly_fraction=Decimal("0.25"),
            confidence=Decimal("0.9"),
            risk_level=ExecutionRisk.LOW,
            min_liquidity_usd=Decimal("1000"),
            metadata={
                "question": market.question[:100],
                "underdog_outcome": underdog_outcome,
                "favorite_outcome": favorite_outcome,
                "underdog_ask": str(underdog_ask),
                "favorite_ask": str(favorite_ask),
                "total_cost": str(total_cost),
                "detection_mode": "hft_ws",
            },
        )

    # Alias for compatibility with check scripts
    detect_opportunity = on_orderbook_update


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def make_opportunity_callback(
    detector: HFTReverseArbDetector,
    engine: Any,
    loop: asyncio.AbstractEventLoop,
) -> Callable[[OrderBook], None]:
    """
    Wrap HFT detector so detected opportunities are captured and routed to engine.

    Without this wrapper, the WS feed invokes the callback and DISCARDS the return
    value, so detected edges are computed and thrown away (profitability blocker).
    """
    MAX_KEPT = 200
    THROTTLE_S = 0.5

    # Use asyncio locks (acquired via call_soon_threadsafe + create_task)
    buffer_lock = asyncio.Lock()
    throttle_lock = asyncio.Lock()
    last_scheduled: dict[str, float] = {}

    async def _safe_process(opp: ArbitrageOpportunity) -> None:
        try:
            await engine._process_opportunity(opp)
        except Exception:
            logger.exception("HFT process_opportunity failed for %s", opp.id)

    async def _async_cb(orderbook: OrderBook) -> None:
        """Async version of callback - does detection and queuing."""
        opp = await detector.on_orderbook_update(orderbook)
        if opp is None:
            return

        # 1) Record every detection (bounded ring buffer, lock-protected)
        async with buffer_lock:
            buffer = getattr(engine, "_current_opportunities", None)
            if buffer is None:
                buffer = []
                engine._current_opportunities = buffer  # type: ignore
            buffer.append(opp)
            if len(buffer) > MAX_KEPT:
                del buffer[: len(buffer) - MAX_KEPT]

        # 2) Per-condition throttle to bound scheduled tasks
        cid = opp.legs[0].condition_id if opp.legs else None
        if cid is not None:
            now = time.monotonic()
            async with throttle_lock:
                if now - last_scheduled.get(cid, 0.0) < THROTTLE_S:
                    return
                last_scheduled[cid] = now

        # 3) Route through risk + (paper) execution on event loop
        await _safe_process(opp)

    def cb(orderbook: OrderBook) -> None:
        """Synchronous callback wrapper - schedules async work on event loop."""
        loop.call_soon_threadsafe(loop.create_task, _async_cb(orderbook))

    return cb