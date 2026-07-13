"""
Polymarket Market Data Layer - Gamma Client, CLOB Client, WebSocket Feed.

Adapted from main Arbitrage system for reverse arb standalone use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

import aiohttp

from src.core.config import get_config, get_settings
from src.core.types import (
    MarketInfo,
    MarketSnapshot,
    OrderBook,
    OrderBookLevel,
    MarketType,
    Decimal,
    datetime,
)

logger = logging.getLogger(__name__)


# =============================================================================
# GAMMA CLIENT (Market Metadata)
# =============================================================================

@dataclass
class GammaMarket:
    """Raw market data from Gamma API."""
    condition_id: str
    question: str
    slug: str
    outcomes: list[str]
    outcome_prices: list[str]  # String prices from API
    clob_token_ids: list[str]
    volume_24h: str
    liquidity: str
    active: bool
    closed: bool
    end_date_iso: Optional[str]
    category: Optional[str] = None
    tags: list[str] = None

    def to_market_info(self) -> MarketInfo:
        """Convert to MarketInfo with proper types."""
        return MarketInfo(
            condition_id=self.condition_id,
            question=self.question,
            slug=self.slug,
            outcomes=self.outcomes,
            outcome_prices=[Decimal(p) for p in self.outcome_prices],
            clob_token_ids=self.clob_token_ids,
            volume_24h=Decimal(self.volume_24h) if self.volume_24h else Decimal("0"),
            liquidity=Decimal(self.liquidity) if self.liquidity else Decimal("0"),
            active=self.active,
            closed=self.closed,
            end_date=datetime.fromisoformat(self.end_date_iso.replace("Z", "+00:00")) if self.end_date_iso else None,
            category=self.category,
            tags=self.tags or [],
            market_type=MarketType.UP_DOWN if self._is_up_down() else MarketType.BINARY,
            minutes_to_close=self._minutes_to_close(),
        )

    def _is_up_down(self) -> bool:
        q = (self.question or "").lower()
        s = (self.slug or "").lower()
        return (
            any(k in q for k in ("btc", "bitcoin", "eth", "ethereum")) and
            any(k in q for k in ("up", "down", "updown")) and
            "15m" in s
        )

    def _minutes_to_close(self) -> Optional[int]:
        if not self.end_date_iso:
            return None
        try:
            end = datetime.fromisoformat(self.end_date_iso.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = end - now
            return max(0, int(delta.total_seconds() / 60))
        except Exception:
            return None


class GammaClient:
    """Client for Polymarket Gamma API (market metadata)."""

    def __init__(self, base_url: Optional[str] = None, session: Optional[aiohttp.ClientSession] = None):
        self.base_url = base_url or get_config().market_data.gamma_api_url
        self._session = session
        self._owned_session = session is None

    async def __aenter__(self) -> "GammaClient":
        if self._owned_session:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *args) -> None:
        if self._owned_session and self._session:
            await self._session.close()
            self._session = None

    async def get_markets(
        self,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MarketInfo]:
        """Get markets from Gamma API with pagination."""
        params = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "offset": offset,
        }

        url = f"{self.base_url}/markets"
        async with self._session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()

        markets = []
        for item in data:
            try:
                raw = GammaMarket(
                    condition_id=item.get("conditionId", ""),
                    question=item.get("question", ""),
                    slug=item.get("slug", ""),
                    outcomes=item.get("outcomes", []),
                    outcome_prices=item.get("outcomePrices", []),
                    clob_token_ids=item.get("clobTokenIds", []),
                    volume_24h=item.get("volume24hr", "0"),
                    liquidity=item.get("liquidity", "0"),
                    active=item.get("active", False),
                    closed=item.get("closed", False),
                    end_date_iso=item.get("endDateIso"),
                    category=item.get("category"),
                    tags=item.get("tags", []),
                )
                markets.append(raw.to_market_info())
            except Exception as e:
                logger.warning(f"Failed to parse market: {e}")
                continue

        return markets

    async def get_active_markets(self, limit: int = 500) -> list[MarketInfo]:
        """Get all active markets (paginated)."""
        all_markets = []
        page_size = 100
        for offset in range(0, limit, page_size):
            page = await self.get_markets(active=True, closed=False, limit=min(page_size, limit - offset), offset=offset)
            if not page:
                break
            all_markets.extend(page)
            if len(page) < page_size:
                break
        return all_markets

    async def get_market(self, condition_id: str) -> Optional[MarketInfo]:
        """Get single market by condition_id."""
        url = f"{self.base_url}/markets/{condition_id}"
        async with self._session.get(url) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            item = await resp.json()

        raw = GammaMarket(
            condition_id=item.get("conditionId", ""),
            question=item.get("question", ""),
            slug=item.get("slug", ""),
            outcomes=item.get("outcomes", []),
            outcome_prices=item.get("outcomePrices", []),
            clob_token_ids=item.get("clobTokenIds", []),
            volume_24h=item.get("volume24hr", "0"),
            liquidity=item.get("liquidity", "0"),
            active=item.get("active", False),
            closed=item.get("closed", False),
            end_date_iso=item.get("endDateIso"),
            category=item.get("category"),
            tags=item.get("tags", []),
        )
        return raw.to_market_info()


# =============================================================================
# CLOB CLIENT (Order Placement, Orderbook Fetching)
# =============================================================================

@dataclass
class ClobOrderBook:
    """Orderbook from CLOB API."""
    token_id: str
    bids: list[dict]  # [{"price": "...", "size": "..."}, ...]
    asks: list[dict]
    timestamp: str

    def to_order_book(self) -> OrderBook:
        return OrderBook(
            token_id=self.token_id,
            bids=[OrderBookLevel(price=Decimal(b["price"]), size=Decimal(b["size"])) for b in self.bids],
            asks=[OrderBookLevel(price=Decimal(a["price"]), size=Decimal(a["size"])) for a in self.asks],
            timestamp=datetime.fromisoformat(self.timestamp.replace("Z", "+00:00")),
            sequence=0,
        )


class ClobClient:
    """CLOB REST Client for orderbook fetching and order placement."""

    def __init__(
        self,
        host: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        api_passphrase: Optional[str] = None,
        private_key: Optional[str] = None,
        chain_id: int = 137,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.host = host or get_config().market_data.clob_host
        self.api_key = api_key or get_settings().polymarket_api_key
        self.api_secret = api_secret or get_settings().polymarket_api_secret
        self.api_passphrase = api_passphrase or get_settings().polymarket_api_passphrase
        self.private_key = private_key or get_settings().polymarket_private_key
        self.chain_id = chain_id
        self._session = session
        self._owned_session = session is None
        self._l2_api_key: Optional[str] = None
        self.signer: Optional[Any] = None

        # Initialize signer if private key provided
        if self.private_key:
            self._init_signer()

    def _init_signer(self) -> None:
        """Initialize EIP-712 signer for order placement."""
        try:
            from eth_account import Account
            from eth_account.messages import encode_typed_data
            self.signer = Account.from_key(self.private_key)
            logger.info(f"Initialized CLOB signer: {self.signer.address}")
        except ImportError:
            logger.warning("eth-account not installed, order placement unavailable")
        except Exception as e:
            logger.error(f"Failed to initialize signer: {e}")

    async def __aenter__(self) -> "ClobClient":
        if self._owned_session:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *args) -> None:
        if self._owned_session and self._session:
            await self._session.close()
            self._session = None

    def _headers(self, auth: bool = False) -> dict:
        """Build request headers."""
        headers = {"Content-Type": "application/json"}
        if auth and self._l2_api_key:
            headers["POLY-API-KEY"] = self._l2_api_key
        return headers

    async def derive_api_key(self) -> Optional[str]:
        """Derive L2 API key from private key (EIP-712)."""
        if not self.signer:
            return None
        try:
            from eth_account.messages import encode_typed_data

            # EIP-712 typed data for L2 key derivation
            typed_data = {
                "types": {
                    "EIP712Domain": [
                        {"name": "name", "type": "string"},
                        {"name": "version", "type": "string"},
                        {"name": "chainId", "type": "uint256"},
                    ],
                    "Action": [
                        {"name": "action", "type": "string"},
                        {"name": "nonce", "type": "uint256"},
                    ],
                },
                "primaryType": "Action",
                "domain": {
                    "name": "Clob",
                    "version": "1",
                    "chainId": self.chain_id,
                },
                "message": {
                    "action": "derive",
                    "nonce": int(time.time() * 1000),
                },
            }
            message = encode_typed_data(full_message=typed_data)
            signed = self.signer.sign_message(message)
            self._l2_api_key = signed.signature.hex()
            logger.info("Derived L2 API key from private key")
            return self._l2_api_key
        except Exception as e:
            logger.error(f"Failed to derive API key: {e}")
            return None

    async def get_orderbook(self, token_id: str, depth: int = 50) -> Optional[OrderBook]:
        """Get orderbook for a token."""
        url = f"{self.host}/books/{token_id}"
        params = {"depth": depth}
        async with self._session.get(url, params=params) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            data = await resp.json()

        raw = ClobOrderBook(
            token_id=token_id,
            bids=data.get("bids", []),
            asks=data.get("asks", []),
            timestamp=data.get("timestamp", datetime.now(timezone.utc).isoformat()),
        )
        return raw.to_order_book()

    async def get_orderbooks(self, token_ids: list[str]) -> dict[str, OrderBook]:
        """Get multiple orderbooks concurrently."""
        tasks = [self.get_orderbook(tid) for tid in token_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        orderbooks = {}
        for tid, result in zip(token_ids, results):
            if isinstance(result, OrderBook):
                orderbooks[tid] = result
            elif isinstance(result, Exception):
                logger.warning(f"Failed to fetch orderbook for {tid}: {result}")
        return orderbooks

    async def place_order(self, request: Any) -> Any:
        """Place an order via CLOB API."""
        from src.core.types import OrderRequest, OrderResult, OrderStatus, Side, OrderType

        if not self._l2_api_key:
            await self.derive_api_key()
        if not self._l2_api_key and not self.signer:
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                error="No L2 API key or signer available",
                timestamp=datetime.now(timezone.utc),
            )

        # Build order payload
        payload = {
            "token_id": request.token_id,
            "price": str(request.price),
            "size": str(request.size),
            "side": request.side.value,
            "order_type": request.order_type.value,
            "signature_type": "EOA",
        }
        if request.expiration:
            payload["expiration"] = request.expiration
        if request.post_only:
            payload["post_only"] = True

        # In paper mode or without signer, simulate
        if not self.signer:
            return OrderResult(
                success=True,
                order_id=f"paper-{uuid4().hex[:12]}",
                client_order_id=request.client_order_id,
                status=OrderStatus.FILLED,
                filled_size=request.size,
                avg_fill_price=request.price,
                fees_paid=Decimal("0"),
                timestamp=datetime.now(timezone.utc),
            )

        # Sign and send order
        try:
            from eth_account.messages import encode_typed_data

            typed_data = {
                "types": {
                    "EIP712Domain": [
                        {"name": "name", "type": "string"},
                        {"name": "version", "type": "string"},
                        {"name": "chainId", "type": "uint256"},
                    ],
                    "Order": [
                        {"name": "tokenId", "type": "string"},
                        {"name": "price", "type": "uint256"},
                        {"name": "size", "type": "uint256"},
                        {"name": "side", "type": "uint8"},
                        {"name": "orderType", "type": "uint8"},
                        {"name": "feeRateBps", "type": "uint16"},
                        {"name": "nonce", "type": "uint256"},
                        {"name": "expiration", "type": "uint256"},
                    ],
                },
                "primaryType": "Order",
                "domain": {
                    "name": "Clob",
                    "version": "1",
                    "chainId": self.chain_id,
                },
                "message": {
                    "tokenId": request.token_id,
                    "price": int(request.price * 1_000_000),  # Convert to fixed point
                    "size": int(request.size * 1_000_000),
                    "side": 0 if request.side == Side.BUY else 1,
                    "orderType": {"GTC": 0, "FOK": 1, "FAK": 2, "GTD": 3}.get(request.order_type.value, 0),
                    "feeRateBps": getattr(request, "fee_rate_bps", 75),
                    "nonce": int(time.time() * 1_000_000),
                    "expiration": request.expiration or 0,
                },
            }
            message = encode_typed_data(full_message=typed_data)
            signed = self.signer.sign_message(message)
            payload["signature"] = signed.signature.hex()

            url = f"{self.host}/order"
            async with self._session.post(url, json=payload, headers=self._headers(auth=True)) as resp:
                data = await resp.json()
                if resp.status >= 400:
                    return OrderResult(
                        success=False,
                        status=OrderStatus.REJECTED,
                        error=data.get("error", "Unknown error"),
                        timestamp=datetime.now(timezone.utc),
                    )
                return OrderResult(
                    success=True,
                    order_id=data.get("orderID") or data.get("id"),
                    client_order_id=request.client_order_id,
                    status=OrderStatus.OPEN,
                    filled_size=Decimal("0"),
                    avg_fill_price=None,
                    remaining_size=request.size,
                    raw_response=data,
                    timestamp=datetime.now(timezone.utc),
                )
        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                error=str(e),
                timestamp=datetime.now(timezone.utc),
            )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order."""
        if not self._l2_api_key:
            async with self._session.delete(f"{self.host}/order/{order_id}", headers=self._headers(auth=True)) as resp:
                return resp.status == 200
        return False

    async def get_balance_allowance(self) -> dict:
        """Get wallet balance and allowance."""
        if not self._l2_api_key:
            return {}
        url = f"{self.host}/balance_allowance"
        async with self._session.get(url, headers=self._headers(auth=True)) as resp:
            if resp.status == 200:
                return await resp.json()
            return {}

    async def get_time(self) -> int:
        """Get server time."""
        url = f"{self.host}/time"
        async with self._session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("timestamp", int(time.time() * 1000))
            return int(time.time() * 1000)


async def create_clob_client(
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    api_passphrase: Optional[str] = None,
    private_key: Optional[str] = None,
) -> ClobClient:
    """Factory for creating ClobClient with settings."""
    settings = get_settings()
    cfg = get_config()
    client = ClobClient(
        host=cfg.market_data.clob_host,
        api_key=api_key or settings.polymarket_api_key,
        api_secret=api_secret or settings.polymarket_api_secret,
        api_passphrase=api_passphrase or settings.polymarket_api_passphrase,
        private_key=private_key or settings.polymarket_private_key,
        chain_id=cfg.market_data.chain_id,
    )
    await client.__aenter__()
    return client


# =============================================================================
# WEBSOCKET FEED (Real-time Orderbook Updates)
# =============================================================================

@dataclass
class WSConfig:
    """WebSocket configuration."""
    url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    reconnect_interval: int = 5
    max_reconnect_attempts: int = 10
    ping_interval: int = 30


class ClobWebSocketFeed:
    """WebSocket feed for real-time CLOB orderbook updates."""

    def __init__(self, config: Optional[WSConfig] = None):
        self.config = config or WSConfig()
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._subscriptions: dict[str, set[Callable[[OrderBook], None]]] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def start(self) -> None:
        """Start the WebSocket connection."""
        if self._running:
            return
        self._running = True
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the WebSocket connection."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()

    async def subscribe(self, token_id: str, callback: Callable[[OrderBook], None]) -> None:
        """Subscribe to orderbook updates for a token."""
        should_send = False
        async with self._lock:
            if token_id not in self._subscriptions:
                self._subscriptions[token_id] = set()
            was_empty = len(self._subscriptions[token_id]) == 0
            self._subscriptions[token_id].add(callback)
            # Only send if this is the first subscriber for this token and we're connected
            should_send = was_empty and self.is_connected

        # Send subscription message outside lock if needed
        if should_send and self.is_connected:
            msg = {
                "type": "subscribe",
                "channel": "tokens",
                "token_ids": [token_id],
            }
            try:
                await self._ws.send_json(msg)
            except Exception as e:
                logger.warning(f"Failed to subscribe {token_id}: {e}")

    async def unsubscribe(self, token_id: str, callback: Optional[Callable] = None) -> None:
        """Unsubscribe from a token."""
        should_send = False
        async with self._lock:
            if token_id in self._subscriptions:
                if callback:
                    self._subscriptions[token_id].discard(callback)
                else:
                    self._subscriptions[token_id].clear()
                if not self._subscriptions[token_id]:
                    del self._subscriptions[token_id]
                    # Only send unsubscribe if we were the last subscriber and we're connected
                    should_send = self.is_connected

        # Send unsubscribe message outside lock if needed
        if should_send:
            msg = {
                "type": "unsubscribe",
                "channel": "tokens",
                "token_ids": [token_id],
            }
            try:
                await self._ws.send_json(msg)
            except Exception as e:
                logger.warning(f"Failed to unsubscribe {token_id}: {e}")

    async def _run(self) -> None:
        """Main WebSocket loop."""
        reconnect_attempts = 0
        while self._running and reconnect_attempts < self.config.max_reconnect_attempts:
            try:
                logger.info(f"Connecting to CLOB WebSocket: {self.config.url}")
                self._ws = await self._session.ws_connect(self.config.url)
                logger.info("CLOB WebSocket connected")

                # Resubscribe to all active tokens - copy list under lock, send outside
                async with self._lock:
                    token_ids = list(self._subscriptions.keys())

                for token_id in token_ids:
                    msg = {
                        "type": "subscribe",
                        "channel": "tokens",
                        "token_ids": [token_id],
                    }
                    try:
                        await self._ws.send_json(msg)
                    except Exception as e:
                        logger.warning(f"Failed to resubscribe {token_id}: {e}")

                reconnect_attempts = 0

                # Message loop
                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_message(msg.json())
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error(f"WebSocket error: {self._ws.exception()}")
                        break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                        break

            except Exception as e:
                logger.error(f"WebSocket connection error: {e}")

            reconnect_attempts += 1
            if self._running:
                logger.info(f"Reconnecting in {self.config.reconnect_interval}s (attempt {reconnect_attempts})")
                await asyncio.sleep(self.config.reconnect_interval)

    async def _handle_message(self, data: dict) -> None:
        """Handle incoming WebSocket message."""
        try:
            # Handle different message types
            if data.get("type") == "orderbook" or "bids" in data and "asks" in data:
                token_id = data.get("token_id") or data.get("asset_id")
                if token_id:
                    await self._dispatch_orderbook(token_id, data)
        except Exception as e:
            logger.warning(f"Error handling WS message: {e}")

    async def _dispatch_orderbook(self, token_id: str, data: dict) -> None:
        """Dispatch orderbook update to subscribers."""
        try:
            bids = [OrderBookLevel(price=Decimal(b["price"]), size=Decimal(b["size"])) for b in data.get("bids", [])]
            asks = [OrderBookLevel(price=Decimal(a["price"]), size=Decimal(a["size"])) for a in data.get("asks", [])]

            ob = OrderBook(
                token_id=token_id,
                bids=bids,
                asks=asks,
                timestamp=datetime.now(timezone.utc),
                sequence=data.get("sequence", 0),
            )

            # Copy callbacks under lock, then call outside lock
            async with self._lock:
                callbacks = list(self._subscriptions.get(token_id, set()))

            for cb in callbacks:
                try:
                    if asyncio.iscoroutinefunction(cb):
                        await cb(ob)
                    else:
                        cb(ob)
                except Exception as e:
                    logger.warning(f"Callback error for {token_id}: {e}")
        except Exception as e:
            logger.warning(f"Failed to dispatch orderbook for {token_id}: {e}")


# =============================================================================
# MARKET DATA AGGREGATOR (Combines REST + WS)
# =============================================================================

class MarketDataAggregator:
    """Aggregates market data from Gamma (REST) + CLOB (REST + WS)."""

    def __init__(
        self,
        gamma: GammaClient,
        ws_feed: ClobWebSocketFeed,
        clob: ClobClient,
    ):
        self.gamma = gamma
        self.ws_feed = ws_feed
        self.clob = clob
        self._market_cache: dict[str, MarketInfo] = {}
        self._orderbook_cache: dict[str, OrderBook] = {}
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """Fetch initial market list and orderbooks."""
        markets = await self.gamma.get_active_markets()
        async with self._lock:
            for market in markets:
                self._market_cache[market.condition_id] = market
        self._initialized = True
        logger.info(f"Initialized aggregator with {len(markets)} markets")

    def get_binary_markets(self) -> list[MarketInfo]:
        """Get all cached binary markets."""
        return [m for m in self._market_cache.values() if m.is_binary and m.active and not m.closed]

    def get_up_down_markets(self) -> list[MarketInfo]:
        """Get BTC/ETH 15m Up/Down markets."""
        markets = []
        for m in self._market_cache.values():
            if m.is_up_down_market and m.active and not m.closed:
                markets.append(m)
        return markets

    async def get_snapshot(self, condition_id: str) -> Optional[MarketSnapshot]:
        """Get market snapshot with orderbooks."""
        market = self._market_cache.get(condition_id)
        if not market or not market.is_binary:
            return None

        # Get orderbooks for both tokens
        if not market.clob_token_ids or len(market.clob_token_ids) < 2:
            return None

        token_yes, token_no = market.clob_token_ids[0], market.clob_token_ids[1]

        # Check cache first
        async with self._lock:
            yes_book = self._orderbook_cache.get(token_yes)
            no_book = self._orderbook_cache.get(token_no)

        # Fetch missing from CLOB
        missing = []
        if not yes_book:
            missing.append(token_yes)
        if not no_book:
            missing.append(token_no)

        if missing:
            fetched = await self.clob.get_orderbooks(missing)
            async with self._lock:
                for tid, book in fetched.items():
                    self._orderbook_cache[tid] = book
                yes_book = self._orderbook_cache.get(token_yes)
                no_book = self._orderbook_cache.get(token_no)

        if not yes_book or not no_book:
            return None

        return MarketSnapshot(
            market=market,
            orderbooks={token_yes: yes_book, token_no: no_book},
            timestamp=datetime.now(timezone.utc),
        )

    def update_orderbook(self, token_id: str, orderbook: OrderBook) -> None:
        """Update orderbook from WebSocket (called by WS callback)."""
        asyncio.get_event_loop().call_soon_threadsafe(
            self._async_update_orderbook, token_id, orderbook
        )

    def _async_update_orderbook(self, token_id: str, orderbook: OrderBook) -> None:
        """Thread-safe async update for orderbook cache."""
        async def _do_update():
            async with self._lock:
                self._orderbook_cache[token_id] = orderbook
        # Schedule on the event loop
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_do_update())
        except RuntimeError:
            # No running loop - shouldn't happen in practice
            asyncio.run(_do_update())