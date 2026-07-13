"""
Risk Engine for Polymarket Reverse Arbitrage Bot.

Pre-trade risk checks, position limits, circuit breakers, Kelly sizing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from src.core.config import get_config
from src.core.types import (
    ArbitrageOpportunity,
    ArbitrageLeg,
    RiskLimits,
    RiskCheckResult,
    KellySizing,
    OpportunityType,
    ExecutionRisk,
    Platform,
    OrderType,
    Side,
    Decimal,
    datetime,
)

logger = logging.getLogger(__name__)


# =============================================================================
# RISK CONFIGURATION
# =============================================================================

@dataclass
class RiskConfig:
    """Risk engine configuration."""
    max_position_per_market_usd: Decimal = Decimal("2000")
    max_daily_loss_usd: Decimal = Decimal("500")
    max_gross_exposure_usd: Decimal = Decimal("10000")
    max_concurrent_positions: int = 5
    max_slippage_bps: int = 50
    max_order_latency_ms: int = 200
    position_concentration_limit: Decimal = Decimal("0.20")
    correlation_limit: Decimal = Decimal("0.70")

    # Daily loss tracking (reset at UTC midnight)
    daily_loss_usd: Decimal = Decimal("0")
    daily_pnl_usd: Decimal = Decimal("0")
    last_reset_date: Optional[datetime] = None

    # Circuit breaker
    circuit_breaker_triggered: bool = False
    circuit_breaker_reason: Optional[str] = None


class RiskEngine:
    """Risk management engine with pre-trade checks and position monitoring."""

    def __init__(
        self,
        config: Optional[RiskConfig] = None,
        position_manager: Optional[object] = None,
    ):
        self.config = config or RiskConfig()
        self._position_manager = position_manager
        self._lock = asyncio.Lock()
        self._bankroll_usd: Decimal = Decimal("10000")
        self._daily_pnl: Decimal = Decimal("0")
        self._last_reset: datetime = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    async def initialize(self) -> None:
        """Initialize risk engine (load state from DB if available)."""
        # Reset daily PnL at midnight UTC
        await self._maybe_reset_daily()

    async def _maybe_reset_daily(self) -> None:
        """Reset daily PnL tracking at UTC midnight."""
        now = datetime.now(timezone.utc)
        midnight_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self._last_reset < midnight_today:
            self._daily_pnl = Decimal("0")
            self.config.daily_loss_usd = Decimal("0")
            self.config.circuit_breaker_triggered = False
            self.config.circuit_breaker_reason = None
            self._last_reset = midnight_today
            logger.info("Daily PnL reset at UTC midnight")

    def update_bankroll(self, bankroll_usd: Decimal) -> None:
        """Update bankroll for Kelly sizing."""
        self._bankroll_usd = bankroll_usd

    async def update_positions(self, positions: list) -> None:
        """Update position state and recalculate PnL."""
        async with self._lock:
            # Update position manager if provided
            if self._position_manager and hasattr(self._position_manager, 'aupdate'):
                for pos in positions:
                    await self._position_manager.aupdate(pos)

            # Calculate current PnL
            total_pnl = sum((p.total_pnl for p in positions), Decimal("0"))
            self._daily_pnl = total_pnl

            # Check daily loss limit
            if -self._daily_pnl >= self.config.max_daily_loss_usd:
                if not self.config.circuit_breaker_triggered:
                    self.config.circuit_breaker_triggered = True
                    self.config.circuit_breaker_reason = f"Daily loss limit exceeded: {-self._daily_pnl} >= {self.config.max_daily_loss_usd}"
                    logger.critical(f"CIRCUIT BREAKER: {self.config.circuit_breaker_reason}")

    async def check_trade(
        self,
        opportunity: ArbitrageOpportunity,
        bankroll: Optional[Decimal] = None,
    ) -> RiskCheckResult:
        """Pre-trade risk validation for an opportunity.

        Returns RiskCheckResult with approved=True/False and any violations.
        """
        await self._maybe_reset_daily()

        violations = []
        warnings = []

        if bankroll:
            self._bankroll_usd = bankroll

        # Circuit breaker
        if self.config.circuit_breaker_triggered:
            violations.append(f"Circuit breaker active: {self.config.circuit_breaker_reason}")

        # Daily loss limit
        if -self._daily_pnl >= self.config.max_daily_loss_usd:
            violations.append(
                f"Daily loss limit exceeded: {-self._daily_pnl} >= {self.config.max_daily_loss_usd}"
            )

        # Position size limit
        required_capital = opportunity.required_capital_usd
        if required_capital > self.config.max_position_per_market_usd:
            violations.append(
                f"Position size {required_capital} exceeds max {self.config.max_position_per_market_usd}"
            )

        # Gross exposure limit
        if self._position_manager:
            current_positions = await self._position_manager.get_all_positions()
            current_exposure = sum((p.notional_value for p in current_positions), Decimal("0"))
            if current_exposure + required_capital > self.config.max_gross_exposure_usd:
                warnings.append(
                    f"Gross exposure would exceed limit: {current_exposure + required_capital} > {self.config.max_gross_exposure_usd}"
                )

        # Concurrent positions limit
        if self._position_manager:
            current_positions = await self._position_manager.get_all_positions()
            if len(current_positions) >= self.config.max_concurrent_positions:
                warnings.append(
                    f"Max concurrent positions ({self.config.max_concurrent_positions}) reached"
                )

        # Slippage limit per leg
        for leg in opportunity.legs:
            if leg.max_slippage_bps > self.config.max_slippage_bps:
                violations.append(
                    f"Leg slippage {leg.max_slippage_bps} exceeds max {self.config.max_slippage_bps}"
                )

        # Risk level specific checks
        if opportunity.risk_level == ExecutionRisk.HIGH:
            warnings.append("High execution risk opportunity")

        # Kelly sizing
        kelly_fraction = self._calculate_kelly_fraction(opportunity)

        # Recommended size (minimum of Kelly and limits)
        kelly_size = self._bankroll_usd * kelly_fraction
        max_size = self.config.max_position_per_market_usd

        recommended_size = min(kelly_size, max_size, required_capital)
        recommended_size = max(recommended_size, Decimal("0"))

        approved = len(violations) == 0

        return RiskCheckResult(
            approved=approved,
            action="allow" if approved else "reject",
            violations=violations,
            warnings=warnings,
            recommended_size_usd=recommended_size,
            kelly_fraction=kelly_fraction,
            current_daily_pnl=self._daily_pnl,
            current_drawdown=Decimal("0"),  # Could compute from peak equity
            positions_count=len(await self._position_manager.get_all_positions()) if self._position_manager else 0,
        )

    def _calculate_kelly_fraction(self, opportunity: ArbitrageOpportunity) -> Decimal:
        """Calculate Kelly fraction for opportunity.

        For reverse arb, the edge is deterministic at settlement (guaranteed $1 payout
        minus cost). Win probability is near 1.0, but we use a conservative estimate.
        """
        if opportunity.net_edge_bps <= 0:
            return Decimal("0")

        # Convert edge to win probability and win/loss ratio
        # For a guaranteed arb: win_prob ≈ 1.0, win/loss ratio = edge / cost
        total_cost = sum(leg.target_price * leg.size for leg in opportunity.legs)
        if total_cost == 0:
            return Decimal("0")

        win_prob = Decimal("0.99")  # Nearly certain for internal/reverse arb
        edge = Decimal(opportunity.net_edge_bps) / Decimal(10000)
        win_loss_ratio = edge / (total_cost / 100) if total_cost > 0 else Decimal("1")

        kelly = KellySizing(
            win_probability=float(win_prob),
            win_loss_ratio=float(win_loss_ratio) if win_loss_ratio > 0 else 1.0,
            max_fraction=0.25,  # Quarter Kelly
        )

        return Decimal(str(kelly.calculate_fraction()))

    def get_risk_state(self) -> dict:
        """Get current risk state for monitoring."""
        return {
            "bankroll_usd": float(self._bankroll_usd),
            "daily_pnl_usd": float(self._daily_pnl),
            "daily_loss_limit_usd": float(self.config.max_daily_loss_usd),
            "circuit_breaker_triggered": self.config.circuit_breaker_triggered,
            "circuit_breaker_reason": self.config.circuit_breaker_reason,
            "max_position_per_market_usd": float(self.config.max_position_per_market_usd),
            "max_gross_exposure_usd": float(self.config.max_gross_exposure_usd),
            "max_concurrent_positions": self.config.max_concurrent_positions,
        }


# =============================================================================
# DYNAMIC KELLY SIZING (Adaptive based on performance)
# =============================================================================

class DynamicKellySizer:
    """Kelly sizer with drawdown and volatility adjustments."""

    def __init__(self, config: Optional[Any] = None):
        # DynamicKellyConfig from main system
        self.drawdown_scaling = True
        self.max_drawdown_pct = Decimal("0.15")
        self.drawdown_recovery_pct = Decimal("0.05")
        self.volatility_scaling = True
        self.max_volatility_factor = Decimal("2.0")
        self.streak_awareness = True
        self.losing_streak_threshold = 3
        self.winning_streak_threshold = 5

        # State
        self._peak_equity: Decimal = Decimal("10000")
        self._current_equity: Decimal = Decimal("10000")
        self._losing_streak: int = 0
        self._winning_streak: int = 0
        self._returns: list[float] = []

    def update_equity(self, equity: Decimal) -> None:
        """Update equity and track streaks."""
        if equity > self._peak_equity:
            self._peak_equity = equity
        self._current_equity = equity

        # Track returns for volatility
        if len(self._returns) > 100:
            self._returns.pop(0)
        if self._bankroll_usd > 0:
            ret = float((equity - self._bankroll_usd) / self._bankroll_usd)
            self._returns.append(ret)

    def update_pnl(self, pnl: Decimal) -> None:
        """Update streak tracking based on PnL."""
        if pnl > 0:
            self._winning_streak += 1
            self._losing_streak = 0
        elif pnl < 0:
            self._losing_streak += 1
            self._winning_streak = 0

    def calculate_adjusted_kelly(self, base_kelly: Decimal) -> Decimal:
        """Calculate Kelly with drawdown, volatility, and streak adjustments."""
        adjusted = base_kelly

        # Drawdown scaling
        if self.drawdown_scaling and self._peak_equity > 0:
            drawdown = (self._peak_equity - self._current_equity) / self._peak_equity
            if drawdown > self.max_drawdown_pct:
                # Scale down proportionally
                scale = max(Decimal("0.1"), Decimal("1.0") - drawdown)
                adjusted *= scale
            elif drawdown < self.drawdown_recovery_pct:
                # Recovering, can scale back up slowly
                adjusted *= Decimal("1.02")  # 2% recovery per trade

        # Volatility scaling
        if self.volatility_scaling and len(self._returns) >= 20:
            import statistics
            vol = statistics.stdev(self._returns)
            if vol > 0:
                # Target ~10% annualized vol
                target_vol = 0.10 / (252 ** 0.5)  # Daily
                vol_ratio = target_vol / max(vol, 0.001)
                vol_factor = min(max(vol_ratio, Decimal("0.2")), self.max_volatility_factor)
                adjusted *= vol_factor

        # Streak awareness
        if self.streak_awareness:
            if self._losing_streak >= self.losing_streak_threshold:
                adjusted *= Decimal("0.5")  # Cut in half
            elif self._winning_streak >= self.winning_streak_threshold:
                adjusted *= Decimal("1.25")  # Boost 25%

        return max(Decimal("0"), min(adjusted, Decimal("0.25")))  # Cap at quarter Kelly

    def set_bankroll(self, bankroll: Decimal) -> None:
        self._bankroll_usd = bankroll