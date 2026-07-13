"""
Core Type Definitions for Polymarket Reverse Arbitrage Bot.

All types use @dataclass(frozen=True) for immutability.
Matches patterns from main Arbitrage system for consistency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Optional
from uuid import uuid4


# =============================================================================
# ENUMS
# =============================================================================

class Platform(str, Enum):
    """Supported prediction market platforms."""
    POLYMARKET = "polymarket"
    MANIFOLD = "manifold"
    METACULUS = "metaculus"
    PREDICTIT = "predictit"
    DRIFT = "drift"
    BETFAIR = "betfair"
    SMARKETS = "smarkets"


class Side(str, Enum):
    """Order side."""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """Order type."""
    GTC = "GTC"   # Good Till Cancelled
    GTD = "GTD"   # Good Till Date
    FOK = "FOK"   # Fill Or Kill
    FAK = "FAK"   # Fill And Kill


class OrderStatus(str, Enum):
    """Order status."""
    OPEN = "open"
    LIVE = "live"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    PENDING = "pending"
    TIMEOUT = "timeout"


class OpportunityType(str, Enum):
    """Type of arbitrage opportunity."""
    INTERNAL = "internal"              # YES + NO < $1 on same market
    CROSS_PLATFORM = "cross_platform"  # Same event, different platforms
    EDGE_VS_FAIR = "edge_vs_fair"      # Model vs market price
    REVERSE_ARB = "reverse_arb"        # Underdog + Favorite on 15m markets


class ExecutionMode(str, Enum):
    """Trading execution mode."""
    PAPER = "paper"
    LIVE = "live"


class OpportunityStatus(str, Enum):
    """Opportunity lifecycle status."""
    ACTIVE = "active"
    TAKEN = "taken"
    EXPIRED = "expired"
    CLOSED = "closed"
    REJECTED = "rejected"


class ExecutionRisk(str, Enum):
    """Execution risk level."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MarketType(str, Enum):
    """Market type classification."""
    BINARY = "binary"
    MULTI_OUTCOME = "multi_outcome"
    SCALAR = "scalar"
    UP_DOWN = "up_down"     # 15m BTC/ETH up-down markets


# =============================================================================
# MARKET DATA TYPES
# =============================================================================

@dataclass(frozen=True)
class TokenPrice:
    """Price point for a token."""
    token_id: str
    outcome: str
    bid: Decimal
    ask: Decimal
    last_price: Decimal
    bid_size: Decimal
    ask_size: Decimal
    timestamp: datetime


@dataclass(frozen=True)
class OrderBookLevel:
    """Single level in order book."""
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    """Order book snapshot."""
    token_id: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    timestamp: datetime
    sequence: int


@dataclass(frozen=True)
class MarketInfo:
    """Core market information from Gamma API."""
    condition_id: str
    question: str
    slug: str
    outcomes: list[str]                    # e.g., ["YES", "NO"] or ["UP", "DOWN"]
    outcome_prices: list[Decimal]          # Current mid prices
    clob_token_ids: list[str]              # Token IDs for CLOB
    volume_24h: Decimal
    liquidity: Decimal
    active: bool
    closed: bool
    end_date: Optional[datetime]
    category: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    market_type: MarketType = MarketType.BINARY
    minutes_to_close: Optional[int] = None

    @property
    def is_binary(self) -> bool:
        return len(self.outcomes) == 2

    @property
    def is_up_down_market(self) -> bool:
        """Check if this is a 15m BTC/ETH Up/Down market."""
        q = (self.question or "").lower()
        slug = (self.slug or "").lower()
        return (
            any(k in q for k in ("btc", "bitcoin", "eth", "ethereum")) and
            any(k in q for k in ("up", "down", "updown")) and
            "15m" in slug
        )


@dataclass(frozen=True)
class MarketSnapshot:
    """Complete market state at a point in time."""
    market: MarketInfo
    orderbooks: dict[str, OrderBook]       # token_id -> OrderBook
    timestamp: datetime

    def get_mid_price(self, token_id: str) -> Optional[Decimal]:
        book = self.orderbooks.get(token_id)
        if not book or not book.bids or not book.asks:
            return None
        return (book.bids[0].price + book.asks[0].price) / 2

    def get_spread_bps(self, token_id: str) -> Optional[int]:
        book = self.orderbooks.get(token_id)
        if not book or not book.bids or not book.asks:
            return None
        bid = book.bids[0].price
        ask = book.asks[0].price
        mid = (bid + ask) / 2
        if mid == 0:
            return None
        return int(((ask - bid) / mid) * 10000)

    def get_best_ask(self, token_id: str) -> Optional[Decimal]:
        book = self.orderbooks.get(token_id)
        if not book or not book.asks:
            return None
        return book.asks[0].price

    def get_best_bid(self, token_id: str) -> Optional[Decimal]:
        book = self.orderbooks.get(token_id)
        if not book or not book.bids:
            return None
        return book.bids[0].price


# =============================================================================
# ARBITRAGE TYPES
# =============================================================================

@dataclass(frozen=True)
class ArbitrageLeg:
    """Single leg of an arbitrage."""
    platform: Platform
    market_id: str
    condition_id: str
    token_id: str
    outcome: str
    side: Side
    target_price: Decimal
    max_slippage_bps: int
    size: Decimal
    order_type: OrderType = OrderType.GTC
    fee_rate_bps: int = 0


@dataclass(frozen=True)
class ArbitrageOpportunity:
    """Complete arbitrage opportunity."""
    id: str = field(default_factory=lambda: str(uuid4()))
    type: OpportunityType = OpportunityType.REVERSE_ARB
    legs: tuple[ArbitrageLeg, ...] = ()
    gross_edge_bps: int = 0
    net_edge_bps: int = 0
    total_fees_bps: int = 0
    estimated_profit_usd: Decimal = Decimal("0")
    required_capital_usd: Decimal = Decimal("0")
    max_position_usd: Decimal = Decimal("0")
    kelly_fraction: Decimal = Decimal("0")
    confidence: Decimal = Decimal("1.0")
    risk_level: ExecutionRisk = ExecutionRisk.LOW
    min_liquidity_usd: Decimal = Decimal("0")
    discovered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None
    status: OpportunityStatus = OpportunityStatus.ACTIVE
    match_verification: Optional[dict] = None  # For cross-platform
    metadata: dict = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) > self.expires_at

    @property
    def time_remaining_ms(self) -> int:
        if self.expires_at is None:
            return 0
        delta = self.expires_at - datetime.now(timezone.utc)
        return max(0, int(delta.total_seconds() * 1000))


@dataclass(frozen=True)
class ExecutionStep:
    """Single execution step."""
    order: int
    leg: ArbitrageLeg
    estimated_fill_price: Decimal
    priority: Literal["high", "normal", "low"] = "normal"
    side: Side = Side.BUY
    size: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    order_type: OrderType = OrderType.GTC
    depends_on: Optional[str] = None
    timeout_ms: int = 30000


@dataclass(frozen=True)
class ExecutionPlan:
    """Complete execution plan for an opportunity."""
    opportunity_id: str
    steps: tuple[ExecutionStep, ...]
    total_estimated_cost: Decimal
    estimated_profit: Decimal
    max_slippage_bps: int
    risk_level: ExecutionRisk
    created_at: datetime
    warnings: list[str] = field(default_factory=list)


# =============================================================================
# EXECUTION TYPES
# =============================================================================

@dataclass(frozen=True)
class OrderRequest:
    """Order placement request."""
    market_id: str
    token_id: str
    side: Side
    price: Decimal
    size: Decimal
    order_type: OrderType = OrderType.GTC
    expiration: Optional[int] = None
    post_only: bool = False
    client_order_id: Optional[str] = None
    platform: Platform = Platform.POLYMARKET
    condition_id: Optional[str] = None


@dataclass
class OrderResult:
    """Order placement result."""
    success: bool
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_size: Decimal = Decimal("0")
    avg_fill_price: Optional[Decimal] = None
    remaining_size: Decimal = Decimal("0")
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_response: Optional[dict] = None
    fees_paid: Decimal = Decimal("0")
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        # Correctness: mirror error fields
        if self.error and not self.error_message:
            self.error_message = self.error
        elif self.error_message and not self.error:
            self.error = self.error_message


@dataclass
class Position:
    """Current position in a market."""
    market_id: str
    condition_id: str
    token_id: str
    outcome: str
    size: Decimal
    avg_entry_price: Decimal
    current_price: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal = Decimal("0")
    platform: Platform = Platform.POLYMARKET
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def notional_value(self) -> Decimal:
        return self.size * self.current_price

    @property
    def total_pnl(self) -> Decimal:
        return self.unrealized_pnl + self.realized_pnl


@dataclass(frozen=True)
class Fill:
    """Trade fill information."""
    order_id: str
    trade_id: str
    market_id: str
    token_id: str
    side: Side
    price: Decimal
    size: Decimal
    fee: Decimal
    timestamp: datetime
    fee_rate_bps: int = 0
    fill_id: Optional[str] = None
    client_order_id: Optional[str] = None
    platform: Platform = Platform.POLYMARKET
    condition_id: Optional[str] = None
    outcome: str = ""


# =============================================================================
# RISK TYPES
# =============================================================================

@dataclass(frozen=True)
class RiskLimits:
    """Risk limits configuration."""
    max_position_per_market_usd: Decimal
    max_daily_loss_usd: Decimal
    max_gross_exposure_usd: Decimal
    max_concurrent_positions: int
    max_slippage_bps: int
    max_order_latency_ms: int
    position_concentration_limit: Decimal = Decimal("0.20")
    correlation_limit: Decimal = Decimal("0.70")


@dataclass(frozen=True)
class RiskCheckResult:
    """Result of risk validation."""
    approved: bool
    action: str = "allow"
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recommended_size_usd: Decimal = Decimal("0")
    kelly_fraction: Decimal = Decimal("0")
    current_daily_pnl: Decimal = Decimal("0")
    current_drawdown: Decimal = Decimal("0")
    positions_count: int = 0


@dataclass(frozen=True)
class KellySizing:
    """Kelly criterion sizing input and output."""
    win_probability: float
    win_loss_ratio: float
    max_fraction: float = 0.25
    volatility_adjustment: float = 1.0
    drawdown_adjustment: float = 1.0

    def calculate_fraction(self) -> float:
        """Calculate Kelly fraction capped at max_fraction."""
        full_kelly = self.win_probability - (1 - self.win_probability) / self.win_loss_ratio
        full_kelly = max(0.0, full_kelly)
        return min(full_kelly, self.max_fraction)

    def calculate_adjusted_fraction(self) -> float:
        """Calculate Kelly fraction with volatility and drawdown adjustments."""
        base = self.calculate_fraction()
        return base * self.volatility_adjustment * self.drawdown_adjustment


# =============================================================================
# MONITORING TYPES
# =============================================================================

@dataclass
class SystemMetrics:
    """System health metrics - MUTABLE by design for live updates."""
    timestamp: Optional[datetime] = None
    uptime_seconds: int = 0
    opportunities_found: int = 0
    opportunities_executed: int = 0
    active_markets_scanned: int = 0
    orders_placed: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0
    total_pnl_usd: Decimal = Decimal("0")
    daily_pnl_usd: Decimal = Decimal("0")
    active_positions: int = 0
    open_orders: int = 0
    errors_last_hour: int = 0
    current_drawdown_pct: Decimal = Decimal("0")
    peak_equity_usd: Decimal = Decimal("0")
    last_scan_timestamp: Optional[datetime] = None
    last_execution_timestamp: Optional[datetime] = None
    latency_p50_ms: float = 0.0
    latency_p99_ms: float = 0.0


@dataclass(frozen=True)
class PerformanceMetrics:
    """Strategy performance metrics."""
    strategy_type: OpportunityType
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: Decimal
    total_profit_usd: Decimal
    total_loss_usd: Decimal
    net_profit_usd: Decimal
    avg_edge_bps: Decimal
    sharpe_ratio: Optional[Decimal] = None
    max_drawdown_usd: Optional[Decimal] = None
    avg_hold_time_seconds: Optional[float] = None


# =============================================================================
# BACKTESTING TYPES
# =============================================================================

@dataclass(frozen=True)
class BacktestConfig:
    """Backtest configuration."""
    start_date: datetime
    end_date: datetime
    initial_bankroll: Decimal
    kelly_fraction: Decimal
    edge_threshold_bps: int
    fee_model: str = "polymarket_2026"
    slippage_model: str = "orderbook_depth"
    max_position_pct: Decimal = Decimal("0.15")
    include_fees: bool = True
    include_slippage: bool = True


@dataclass(frozen=True)
class BacktestResult:
    """Backtest results."""
    config: BacktestConfig
    final_bankroll: Decimal
    total_return_pct: Decimal
    annualized_return_pct: Decimal
    sharpe_ratio: Decimal
    max_drawdown_pct: Decimal
    total_trades: int
    win_rate: Decimal
    profit_factor: Decimal
    avg_edge_bps: Decimal
    trades: list[dict]
    equity_curve: list[tuple[datetime, Decimal]]
    monthly_returns: dict[str, Decimal]


# =============================================================================
# REVERSE ARB SPECIFIC TYPES
# =============================================================================

@dataclass(frozen=True)
class ReverseArbConfig:
    """Configuration for reverse arbitrage strategy."""
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


@dataclass(frozen=True)
class ReverseArbLegs:
    """The two legs of a reverse arb: cheap (underdog) + expensive (favorite hedge)."""
    cheap_token_id: str
    cheap_outcome: str      # "UP" or "DOWN" - the underdog
    cheap_price: Decimal    # Best ask for cheap leg (e.g., 0.07-0.10)
    cheap_size: Decimal

    expensive_token_id: str
    expensive_outcome: str  # "UP" or "DOWN" - the favorite
    expensive_price: Decimal  # Best ask for expensive leg (e.g., 0.90-0.95)
    expensive_size: Decimal

    condition_id: str
    market_question: str
    minutes_to_close: int


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def bps_to_decimal(bps: int) -> Decimal:
    """Convert basis points to decimal."""
    return Decimal(bps) / Decimal(10000)


def decimal_to_bps(d: Decimal) -> int:
    """Convert decimal to basis points."""
    return int(d * Decimal(10000))


def round_decimal(d: Decimal, places: int = 6) -> Decimal:
    """Round decimal to specified places."""
    quantize_str = "0." + "0" * places
    return d.quantize(Decimal(quantize_str))


def now_utc() -> datetime:
    """Get current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


def generate_client_order_id(
    opportunity_id: str,
    leg_index: int,
    token_id: str,
    side: Side,
    price: Decimal,
    size: Decimal,
) -> str:
    """Generate deterministic client_order_id for idempotent order placement."""
    import hashlib
    key_string = f"{opportunity_id}:{leg_index}:{token_id}:{side.value}:{price}:{size}"
    hash_suffix = hashlib.sha256(key_string.encode()).hexdigest()[:16]
    return f"rev_{opportunity_id[:16]}_{leg_index}_{hash_suffix}"


# Standalone function for market type detection (used by engine)
def is_up_down_market(market: object) -> bool:
    """Check if a market is a 15m BTC/ETH Up/Down market."""
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


def is_up_down_market(market: object) -> bool:
    """Standalone function to check if a market is a 15m BTC/ETH Up/Down market."""
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