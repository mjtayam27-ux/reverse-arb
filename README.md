# Polymarket Reverse Arbitrage Bot

Institution-grade HFT reverse arbitrage bot for Polymarket BTC/ETH 15-minute Up/Down markets.

## Strategy: Reverse Arbitrage

Exploits mispricing between underdog and favorite legs in binary markets:
- **Underdog (cheap leg)**: Buy at 7-10¢ (e.g., BTC DOWN when BTC is trending up)
- **Favorite (hedge leg)**: Buy at 90-95¢ (e.g., BTC UP when BTC is trending up)
- **Edge**: Combined cost significantly < $1.00 after fees
- **Settlement**: One leg pays $1.00, the other expires worthless

## Features

| Component | Implementation |
|-----------|----------------|
| **Market Data** | Gamma API (metadata) + CLOB REST + WebSocket (real-time) |
| **Detection** | Batch scan (5s) + HFT event-driven (<100ms) |
| **Execution** | FOK orders, paper/live mode, idempotent client_order_id |
| **Risk** | Pre-trade checks, Kelly sizing, circuit breakers, position limits |
| **Monitoring** | FastAPI + HTML dashboard, Prometheus metrics |
| **Deployment** | Docker + Fly.io, 48h paper validation required |

## Quick Start

```bash
# 1. Install dependencies
pip install -e .[dev]

# 2. Configure environment
cp config/.env.example config/.env
# Edit config/.env with your API keys

# 3. Run in paper mode
python -m deploy.entry

# 4. Open dashboard
open http://localhost:8080
```

## Configuration Precedence

1. **Defaults** (code)
2. **config/config.yaml**
3. **config/.env** (environment variables)
4. **Fly.io secrets** (production)

## Live Trading Guard

Live trading is **blocked by default** in production. Enable only after 48h paper validation:

```bash
# In Fly.io dashboard or CLI:
fly secrets set LIVE_TRADING_CONFIRMED=true
fly secrets set PAPER_TRADING=false
```

## Project Structure

```
Reverse Arbitrage/
├── config/
│   ├── .env.example    # Environment template
│   └── config.yaml     # YAML configuration
├── src/
│   ├── core/           # Config, types, engine
│   ├── market_data/    # Gamma, CLOB, WebSocket
│   ├── arbitrage/      # Reverse arb detectors
│   ├── execution/      # Order manager, position tracker
│   ├── risk/           # Risk engine, Kelly sizing
│   └── api/            # FastAPI server + dashboard
├── deploy/
│   ├── entry.py        # Production entry point
│   └── fly.toml        # Fly.io config
├── Dockerfile
├── pyproject.toml
└── README.md
```

## Key Files

| File | Purpose |
|------|---------|
| `src/core/arbitrage_engine.py` | Main engine orchestrating all components |
| `src/arbitrage/reverse_arb.py` | Batch + HFT detectors |
| `src/execution/executor.py` | TradingMode authority, OrderManager, ExecutionEngine |
| `src/risk/risk_engine.py` | RiskEngine, DynamicKellySizer |
| `src/market_data/clob_client.py` | GammaClient, ClobClient, WebSocketFeed |
| `src/api/server.py` | REST API + inline HTML dashboard |
| `deploy/entry.py` | Production entry point for Fly.io |

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check |
| `GET /api/status` | Engine status |
| `GET /api/metrics` | System metrics |
| `GET /api/risk` | Risk state |
| `GET /api/opportunities` | Recent opportunities |
| `GET /api/positions` | Current positions |
| `POST /api/engine/start` | Start engine |
| `POST /api/engine/stop` | Stop engine |
| `GET /` | HTML Dashboard |

## Deployment

### Fly.io (Recommended)

```bash
# 1. Create app
fly launch --config deploy/fly.toml --no-deploy

# 2. Set secrets
fly secrets set POLYMARKET_PRIVATE_KEY=...
fly secrets set POLYMARKET_API_KEY=...
fly secrets set POLYMARKET_API_SECRET=...
fly secrets set POLYMARKET_API_PASSPHRASE=...

# 3. Deploy (paper mode by default)
fly deploy

# 4. After 48h validation, enable live
fly secrets set LIVE_TRADING_CONFIRMED=true PAPER_TRADING=false
fly deploy
```

### Docker

```bash
docker build -t reverse-arb .
docker run -p 8080:8080 -p 9090:9090 \
  -e POLYMARKET_PRIVATE_KEY=... \
  -e POLYMARKET_API_KEY=... \
  reverse-arb
```

## Testing

```bash
# Run verification script
python -m tests.verify

# Unit tests
pytest tests/ -v

# Type checking
mypy src/

# Linting
ruff check src/
```

## Monitoring

- **Dashboard**: http://localhost:8080
- **Prometheus**: http://localhost:9090/metrics
- **Logs**: Structured JSON logging

## Safety

- Paper trading enforced by default
- `LIVE_TRADING_CONFIRMED` required for production live mode
- Circuit breakers on daily loss
- Position and exposure limits
- FOK orders prevent partial fills
- Idempotent order placement

## License

Proprietary - Internal Use Only
