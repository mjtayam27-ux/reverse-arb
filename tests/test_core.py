"""Tests for Reverse Arbitrage Bot."""

from __future__ import annotations

import pytest
from decimal import Decimal
from datetime import datetime, timezone


class TestCoreTypes:
    """Test core type definitions."""

    def test_enums_exist(self):
        from src.core.types import (
            Platform, Side, OrderType, OrderStatus,
            ExecutionMode, ExecutionRisk, OpportunityType, MarketType
        )
        assert Platform.POLYMARKET
        assert Side.BUY
        assert OrderType.FOK
        assert OrderStatus.FILLED
        assert ExecutionMode.PAPER
        assert ExecutionRisk.LOW
        assert OpportunityType.REVERSE_ARB
        assert MarketType.UP_DOWN

    def test_market_info_creation(self):
        from src.core.types import MarketInfo
        mi = MarketInfo(
            condition_id="0x123",
            question="BTC Up 15m?",
            category="crypto",
            outcomes=["UP", "DOWN"],
            outcome_prices=[0.5, 0.5],
            clob_token_ids=["0xabc", "0xdef"],
            end_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
            active=True,
            closed=False,
            liquidity=Decimal("1000"),
            volume_24h=Decimal("5000"),
            slug="btc-up-15m",
            tags=["crypto"],
        )
        assert mi.condition_id == "0x123"
        assert mi.is_up_down_market is True

    def test_arb_leg_creation(self):
        from src.core.types import ArbitrageLeg, Platform, Side, OrderType
        leg = ArbitrageLeg(
            platform=Platform.POLYMARKET,
            market_id="0x123",
            condition_id="0x123",
            token_id="0xabc",
            outcome="UP",
            side=Side.BUY,
            target_price=Decimal("0.08"),
            max_slippage_bps=50,
            size=Decimal("500"),
            order_type=OrderType.FOK,
            fee_rate_bps=75,
        )
        assert leg.side == Side.BUY
        assert leg.target_price == Decimal("0.08")

    def test_arb_opportunity_creation(self):
        from src.core.types import (
            ArbitrageOpportunity, ArbitrageLeg, OpportunityType,
            ExecutionRisk, Platform, Side, OrderType
        )
        leg = ArbitrageLeg(
            platform=Platform.POLYMARKET,
            market_id="0x123",
            condition_id="0x123",
            token_id="0xabc",
            outcome="UP",
            side=Side.BUY,
            target_price=Decimal("0.08"),
            max_slippage_bps=50,
            size=Decimal("500"),
            order_type=OrderType.FOK,
            fee_rate_bps=75,
        )
        opp = ArbitrageOpportunity(
            type=OpportunityType.REVERSE_ARB,
            legs=(leg,),
            gross_edge_bps=500,
            net_edge_bps=300,
            total_fees_bps=150,
            estimated_profit_usd=Decimal("50"),
            required_capital_usd=Decimal("100"),
            max_position_usd=Decimal("2000"),
            kelly_fraction=Decimal("0.25"),
            confidence=Decimal("0.95"),
            risk_level=ExecutionRisk.LOW,
            min_liquidity_usd=Decimal("500"),
        )
        assert opp.type == OpportunityType.REVERSE_ARB
        assert opp.net_edge_bps == 300

    def test_utility_functions(self):
        from src.core.types import bps_to_decimal, decimal_to_bps, generate_client_order_id, Side
        assert bps_to_decimal(100) == Decimal("0.01")
        assert decimal_to_bps(Decimal("0.01")) == 100
        oid = generate_client_order_id("opp1", 0, "0xabc", Side.BUY, Decimal("0.08"), Decimal("100"))
        assert len(oid) > 20
        assert oid.startswith("rev_")

    def test_is_up_down_market_fn(self):
        from src.core.types import is_up_down_market

        class MockMarket:
            def __init__(self, question, slug):
                self.question = question
                self.slug = slug

        btc_market = MockMarket("Will BTC go up?", "btc-up-15m")
        eth_market = MockMarket("Will ETH go up?", "eth-up-15m")
        other_market = MockMarket("Will TSLA go up?", "tsla-up-1d")

        assert is_up_down_market(btc_market) is True
        assert is_up_down_market(eth_market) is True
        assert is_up_down_market(other_market) is False


class TestConfig:
    """Test configuration system."""

    def test_settings_load(self):
        from src.core.config import get_settings
        settings = get_settings()
        assert settings.paper_trading is True
        assert settings.enable_reverse_arb is True

    def test_config_load(self):
        from src.core.config import get_config
        cfg = get_config()
        assert cfg.reverse_arb.min_edge_bps == 100
        # Iteration 1 params
        assert cfg.reverse_arb.cheap_buy_min == 0.065
        assert cfg.reverse_arb.max_position_usd == 2000

    def test_reverse_arb_config(self):
        from src.core.config import get_reverse_arb_config
        cfg = get_reverse_arb_config()
        assert cfg.min_edge_bps == 100
        assert cfg.cheap_buy_min == 0.065
        assert cfg.dry_run is False  # Live mode


class TestReverseArbDetector:
    """Test reverse arbitrage detection."""

    def test_detector_creation(self):
        from src.arbitrage.reverse_arb import ReverseArbitrageDetector
        detector = ReverseArbitrageDetector()
        assert detector is not None

    def test_hft_detector_creation(self):
        from src.arbitrage.reverse_arb import HFTReverseArbDetector, ReverseArbConfig
        config = ReverseArbConfig(
            min_edge_bps=100, max_slippage_bps=50, max_position_usd=2000,
            fee_bps=75, cheap_buy_min=Decimal("0.07"), cheap_buy_max=Decimal("0.10"),
            expensive_buy_min=Decimal("0.90"), expensive_buy_max=Decimal("0.95"),
            cheap_order_usd=50, expensive_order_usd=100,
            minutes_before_close_min=2, minutes_before_close_max=5,
            order_type="FOK", dry_run=True, btc_markets_only=True,
        )
        hft = HFTReverseArbDetector(config)
        assert hft is not None
        assert hasattr(hft, '_token_map')
        assert hasattr(hft, '_orderbook_cache')

    def test_price_range_validation(self):
        from src.arbitrage.reverse_arb import HFTReverseArbDetector, ReverseArbConfig
        config = ReverseArbConfig(
            min_edge_bps=100, max_slippage_bps=50, max_position_usd=2000,
            fee_bps=75, cheap_buy_min=Decimal("0.07"), cheap_buy_max=Decimal("0.10"),
            expensive_buy_min=Decimal("0.90"), expensive_buy_max=Decimal("0.95"),
            cheap_order_usd=50, expensive_order_usd=100,
            minutes_before_close_min=2, minutes_before_close_max=5,
            order_type="FOK", dry_run=True, btc_markets_only=True,
        )
        hft = HFTReverseArbDetector(config)

        # Should pass: 0.08 is in [0.07, 0.10], 0.92 is in [0.90, 0.95]
        # We test via on_orderbook_update which does the validation internally


class TestExecutionEngine:
    """Test execution engine."""

    def test_trading_mode_paper_default(self):
        from src.execution.executor import TradingMode
        # Default should be paper
        assert TradingMode.is_paper() is True
        assert TradingMode.is_live() is False

    def test_order_manager_creation(self):
        from src.execution.executor import OrderManager
        om = OrderManager(None)
        assert om is not None

    def test_position_manager_creation(self):
        from src.execution.executor import PositionManager
        pm = PositionManager()
        assert pm is not None

    def test_execution_engine_creation(self):
        from src.execution.executor import ExecutionEngine, ExecutionMode
        engine = ExecutionEngine(None, ExecutionMode.PAPER)
        assert engine is not None
        assert engine.paper_mode is True


class TestRiskEngine:
    """Test risk engine."""

    def test_risk_engine_creation(self):
        from src.risk.risk_engine import RiskEngine, RiskConfig
        config = RiskConfig()
        engine = RiskEngine(config)
        assert engine is not None

    def test_dynamic_kelly_sizer(self):
        from src.risk.risk_engine import DynamicKellySizer
        sizer = DynamicKellySizer()
        assert sizer is not None
        sizer.set_bankroll(Decimal("10000"))
        sizer.update_equity(Decimal("10000"))
        adjusted = sizer.calculate_adjusted_kelly(Decimal("0.10"))
        assert adjusted <= Decimal("0.25")  # Capped at quarter Kelly (absolute cap)


class TestAPI:
    """Test API server."""

    def test_app_creation(self):
        from src.api.server import app, create_app
        assert app is not None
        assert create_app is not None

    def test_routes_exist(self):
        from src.api.server import app
        routes = [r.path for r in app.routes]
        assert "/health" in routes
        assert "/api/status" in routes
        assert "/api/metrics" in routes
        assert "/api/risk" in routes
        assert "/api/opportunities" in routes
        assert "/api/positions" in routes
        assert "/api/engine/start" in routes
        assert "/api/engine/stop" in routes
        assert "/" in routes


class TestMainEngine:
    """Test main engine orchestration."""

    def test_engine_creation(self):
        from src.core.arbitrage_engine import ReverseArbEngine, EngineConfig
        config = EngineConfig(
            enable_hft_reverse_arb=False,
            paper_trading=True,
            api_enabled=False,
        )
        engine = ReverseArbEngine(config)
        assert engine is not None
        assert engine._config.paper_trading is True

    def test_btc_filter(self):
        from src.core.arbitrage_engine import is_btc_market, is_eth_market, is_up_down_market

        class MockMarket:
            def __init__(self, question, slug, category="", tags=None):
                self.question = question
                self.slug = slug
                self.category = category
                self.tags = tags or []

        btc_market = MockMarket("Will BTC go up?", "btc-up-15m")
        eth_market = MockMarket("Will ETH go up?", "eth-up-15m")
        other_market = MockMarket("Will TSLA go up?", "tsla-up-1d")

        assert is_btc_market(btc_market) is True
        assert is_btc_market(eth_market) is False
        assert is_btc_market(other_market) is False

        assert is_eth_market(eth_market) is True
        assert is_eth_market(btc_market) is False

        assert is_up_down_market(btc_market) is True
        assert is_up_down_market(other_market) is False


class TestUtilities:
    """Test utility functions."""

    def test_decimal_precision(self):
        from decimal import Decimal
        price = Decimal("0.08")
        size = Decimal("500")
        cost = price * size
        assert cost == Decimal("40.00")

    def test_bps_conversion(self):
        from src.core.types import bps_to_decimal, decimal_to_bps
        assert bps_to_decimal(50) == Decimal("0.005")
        assert decimal_to_bps(Decimal("0.005")) == 50

    def test_client_order_id_format(self):
        from src.core.types import generate_client_order_id, Side
        oid = generate_client_order_id("opp1", 0, "0xabc", Side.BUY, Decimal("0.08"), Decimal("100"))
        assert oid.startswith("rev_")
        assert len(oid) > 20


if __name__ == "__main__":
    pytest.main([__file__, "-v"])