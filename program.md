# Reverse Arbitrage Bot — Continuous Optimization Program

## What "Better" Means for This Bot

**Primary Metric: Net PnL per 100 Markets Scanned**
- Target: ≥ $50 net profit per 100 BTC/ETH 15m Up/Down markets scanned
- Current baseline: $0 (paper trading, no live fills yet)

**Secondary Metrics (must not regress):**
- Detection latency (WS → opportunity): ≤ 50ms p99
- Order placement latency: ≤ 100ms p99 (paper), ≤ 50ms p99 (live)
- Fill rate on FOK legs: ≥ 95%
- Zero unhedged positions (atomic execution guarantee)
- Zero security findings (auth, vault, attestation, concurrency)
- WCAG 2.1 AA dashboard compliance

---

## What the Agent May Modify (target.*)

| File | What to Optimize | Constraints |
|------|------------------|-------------|
| `config/config.yaml` | Edge thresholds, position sizing, Kelly fractions, latency budgets | Must pass all 87 evaluate.py checks |
| `src/arbitrage/reverse_arb.py` | Detection logic, price thresholds, market filters | Must keep atomic dual-orderbook read |
| `src/execution/executor.py` | Order sizing, slippage models, retry logic | Must keep FOK atomic execution + rollback |
| `src/risk/risk_engine.py` | Dynamic Kelly, risk limits, circuit breakers | Must enforce max_daily_loss, max_position |
| `src/core/arbitrage_engine.py` | Market selection, scheduling, concurrency | Must keep singleton guard, init guard |

## What the Agent Must NEVER Touch (evaluate.*)

- `evaluate.py` — the verifier (87 automated checks)
- `loop-state.md` — state log (append-only, human reviews)
- `config/config.yaml.example` — template only

---

## Optimization Search Space

### 1. Edge Detection (reverse_arb.py)
- `cheap_buy_min/max`: Current 0.07–0.10 → explore 0.05–0.12 in 0.005 steps
- `expensive_buy_min/max`: Current 0.90–0.95 → explore 0.88–0.97
- `min_edge_bps`: Current 300 → explore 200–500
- `minutes_before_close`: Current 2–5 → explore 1–10
- Market filters: Add/remove volatility, volume, spread filters

### 2. Sizing & Kelly (risk_engine.py)
- `kelly_fraction`: Current 0.25 (quarter-Kelly) → explore 0.1–0.5
- Dynamic Kelly: Adjust based on recent win rate / edge decay
- Per-market position caps vs global caps

### 3. Execution (executor.py)
- Order type: FOK vs IOC vs GTC with cancel
- Slippage model: Fixed bps vs volume-weighted vs orderbook-depth
- Retry logic on partial fill: cancel remainder vs let rest

### 4. Market Selection (arbitrage_engine.py)
- Which markets to subscribe (top N by volume vs all BTC/ETH Up/Down)
- Subscription rotation for >500 markets (Polymarket WS limit)
- Re-scan frequency for Gamma metadata

### 5. Latency (clob_client.py + websocket_feed.py)
- Connection pooling, keep-alive tuning
- Pre-signed order payloads
- Local orderbook cache vs WS-only

---

## Loop Protocol (Each Iteration)

### 1. READ
- Read `loop-state.md` (last attempt, what worked/failed)
- Run `python evaluate.py` → get baseline scores

### 2. PROPOSE
- Pick ONE parameter from search space above
- Make single, small change to `config/config.yaml` OR one `.py` file in target.*
- Document hypothesis in `loop-state.md`

### 3. RUN
- Apply change
- Run `python evaluate.py` (must pass 87/87)
- If deployable: `fly deploy` to staging, run 1h paper trading

### 4. CHECK
- Score = Net PnL / 100 markets scanned (from paper logs)
- Must beat previous best by ≥ $5/100 markets
- All 87 checks must pass
- No regression on latency, fill rate, safety

### 5. DECIDE
- If improved: Commit, update `loop-state.md`, deploy to prod paper
- If regressed: Rollback, update `loop-state.md` with lesson

### 6. STOP CONDITIONS
- **Goal met**: Net PnL ≥ $50/100 markets sustained for 7 days paper
- **Max attempts**: 50 iterations without improvement → human review
- **Safety breach**: Any evaluate.py check fails → immediate stop

---

## Maker / Checker Separation

**Maker (this loop)**: Proposes changes, runs evaluate.py, deploys to staging
**Checker (separate agent/run)**: 
- Runs adversarial tests: fuzz orderbook inputs, race conditions, latency spikes
- Reviews `loop-state.md` for overfitting signs (e.g., edge_bps tuned to noise)
- Must approve before prod paper deployment

---

## Initial State

**Baseline (Attempt 9 - Production Deployed):**
- Config: `config/config.yaml` (current production values)
- Score: $0/100 markets (no live fills yet, 48h paper validation starting)
- All 87 checks: PASS
- Deployment: `polymarket-reverse-arb.fly.dev` (paper trading)