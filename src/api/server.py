"""
REST API Server with Monitoring Dashboard for Reverse Arbitrage Bot.

Provides endpoints for:
- Health checks
- Engine status
- Opportunity history
- Position/PnL tracking
- Risk state
- System metrics
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.api.auth import AdminDependency, ReadOnlyDependency, init_auth
from src.core.arbitrage_engine import (
    ReverseArbEngine,
    start_engine,
    stop_engine,
)
from src.core.config import get_settings

logger = logging.getLogger(__name__)

# Initialize auth on module load
init_auth()

# Global engine reference
_engine: ReverseArbEngine | None = None
_engine_lock = asyncio.Lock()


# ============================================================================
# Pydantic Models for API
# ============================================================================

class HealthResponse(BaseModel):
    status: str
    timestamp: str
    version: str = "1.0.0"
    paper_trading: bool
    engine_running: bool


class EngineStatusResponse(BaseModel):
    running: bool
    paper_trading: bool
    hft_mode: bool
    metrics: dict
    hft_detector: dict | None = None


class OpportunityResponse(BaseModel):
    id: str
    timestamp: str
    type: str
    market_id: str
    condition_id: str | None
    gross_edge_bps: int
    net_edge_bps: int
    estimated_profit_usd: float
    required_capital_usd: float
    kelly_fraction: float
    confidence: float
    legs: list[dict]
    executed: bool
    execution_results: list[dict] | None = None


class PositionResponse(BaseModel):
    token_id: str
    market_id: str
    platform: str
    side: str
    size: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    realized_pnl: float
    total_pnl: float
    timestamp: str


class RiskStateResponse(BaseModel):
    bankroll_usd: float
    daily_pnl_usd: float
    daily_loss_limit_usd: float
    circuit_breaker_triggered: bool
    circuit_breaker_reason: str | None
    max_position_per_market_usd: float
    max_gross_exposure_usd: float
    max_concurrent_positions: int


class MetricsResponse(BaseModel):
    uptime_seconds: int
    opportunities_found: int
    opportunities_executed: int
    orders_placed: int
    orders_filled: int
    orders_rejected: int
    total_pnl_usd: float
    daily_pnl_usd: float
    current_drawdown_pct: float
    peak_equity_usd: float
    active_positions: int
    open_orders: int
    errors_last_hour: int
    latency_p50_ms: float
    latency_p99_ms: float
    last_scan_timestamp: str | None
    last_execution_timestamp: str | None


# ============================================================================
# Dashboard HTML Template - WCAG 2.1 AA Compliant
# ============================================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Polymarket Reverse Arbitrage Dashboard</title>
    <style>
        /* CSS Custom Properties for theming */
        :root {
            --bg-primary: #0d1117;
            --bg-secondary: #161b22;
            --bg-tertiary: #21262d;
            --border-color: #30363d;
            --text-primary: #e6edf3;
            --text-secondary: #8b949e;
            --text-muted: #6e7681;
            --accent-blue: #58a6ff;
            --accent-green: #3fb950;
            --accent-red: #f85149;
            --accent-yellow: #d29922;
            --accent-purple: #a371f7;
            --focus-ring: #58a6ff;
            --focus-ring-offset: #0d1117;
            --shadow: rgba(0, 0, 0, 0.3);
        }

        /* Reset & Base */
        *, *::before, *::after { box-sizing: border-box; }
        html { font-size: 16px; }
        body {
            margin: 0;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.6;
            min-height: 100vh;
        }

        /* Skip Link - FIRST focusable element */
        .skip-link {
            position: absolute;
            top: -100%;
            left: 50%;
            transform: translateX(-50%);
            padding: 12px 24px;
            background: var(--accent-blue);
            color: var(--bg-primary);
            font-weight: 600;
            text-decoration: none;
            border-radius: 0 0 8px 8px;
            z-index: 10000;
            transition: top 0.2s ease;
        }
        .skip-link:focus {
            top: 0;
            outline: none;
            box-shadow: 0 0 0 3px var(--focus-ring), 0 0 0 6px var(--focus-ring-offset);
        }

        /* Focus Visible - Global */
        :focus-visible {
            outline: none;
            box-shadow: 0 0 0 3px var(--focus-ring), 0 0 0 6px var(--focus-ring-offset);
            border-radius: 4px;
        }
        button:focus-visible,
        a:focus-visible,
        [tabindex]:focus-visible,
        select:focus-visible,
        input:focus-visible {
            outline: none;
            box-shadow: 0 0 0 3px var(--focus-ring), 0 0 0 6px var(--focus-ring-offset);
        }

        /* Remove default button focus */
        button:focus:not(:focus-visible) { box-shadow: none; }

        /* Container & Layout */
        .container { max-width: 1400px; margin: 0 auto; padding: 20px; }

        /* Header - Banner Landmark */
        header[role="banner"] {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            padding-bottom: 16px;
            border-bottom: 2px solid var(--border-color);
            flex-wrap: wrap;
            gap: 16px;
        }
        header h1 {
            font-size: 1.75rem;
            font-weight: 700;
            margin: 0;
            color: var(--text-primary);
        }

        /* Status Badges - with text + icon, not color-only */
        .status-group { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 8px 16px;
            border-radius: 9999px;
            font-size: 0.8125rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            min-width: 120px;
            justify-content: center;
            border: 2px solid transparent;
        }
        .status-badge::before {
            content: "";
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            flex-shrink: 0;
        }
        .status-badge.running { background: rgba(63, 185, 80, 0.15); border-color: var(--accent-green); color: var(--accent-green); }
        .status-badge.running::before { background: var(--accent-green); box-shadow: 0 0 8px var(--accent-green); }
        .status-badge.stopped { background: rgba(248, 81, 73, 0.15); border-color: var(--accent-red); color: var(--accent-red); }
        .status-badge.stopped::before { background: var(--accent-red); box-shadow: 0 0 8px var(--accent-red); }
        .status-badge.paper { background: rgba(88, 166, 255, 0.15); border-color: var(--accent-blue); color: var(--accent-blue); }
        .status-badge.paper::before { background: var(--accent-blue); box-shadow: 0 0 8px var(--accent-blue); }
        .status-badge.live { background: rgba(248, 81, 73, 0.15); border-color: var(--accent-red); color: var(--accent-red); }
        .status-badge.live::before { background: var(--accent-red); box-shadow: 0 0 8px var(--accent-red); }

        /* Controls */
        .controls {
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
            margin-bottom: 24px;
            padding: 16px;
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
        }
        .controls[role="group"] > * { flex: 0 0 auto; }

        button {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            padding: 12px 24px;
            border: 2px solid transparent;
            border-radius: 8px;
            font-size: 0.9375rem;
            font-weight: 600;
            font-family: inherit;
            cursor: pointer;
            transition: all 0.15s ease;
            min-height: 44px;
            min-width: 120px;
        }
        button:hover { transform: translateY(-1px); }
        button:active { transform: translateY(0); }
        button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none;
        }
        .btn-start { background: var(--accent-green); color: var(--bg-primary); border-color: var(--accent-green); }
        .btn-start:hover { background: #5fd96a; border-color: #5fd96a; }
        .btn-stop { background: var(--accent-red); color: #fff; border-color: var(--accent-red); }
        .btn-stop:hover { background: #ff6b63; border-color: #ff6b63; }
        .btn-refresh { background: var(--accent-blue); color: var(--bg-primary); border-color: var(--accent-blue); }
        .btn-refresh:hover { background: #79c0ff; border-color: #79c0ff; }

        /* Grid Layout */
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
            gap: 24px;
            margin-bottom: 24px;
        }

        /* Panel - Article/Section Landmark */
        .panel {
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            transition: border-color 0.2s ease;
        }
        .panel:focus-within { border-color: var(--accent-blue); }
        .panel-title {
            font-size: 0.8125rem;
            font-weight: 600;
            margin: 0 0 20px 0;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.08em;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .panel-title::before {
            content: "";
            width: 4px;
            height: 16px;
            background: var(--accent-blue);
            border-radius: 2px;
        }

        /* Metric Rows */
        .metric-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 0;
            border-bottom: 1px solid var(--border-color);
        }
        .metric-row:last-child { border-bottom: none; }
        .metric-label {
            color: var(--text-secondary);
            font-size: 0.9375rem;
            flex: 1;
            min-width: 0;
        }
        .metric-value {
            font-weight: 600;
            font-size: 0.9375rem;
            text-align: right;
            flex-shrink: 0;
            margin-left: 16px;
        }
        .metric-value.positive { color: var(--accent-green); }
        .metric-value.negative { color: var(--accent-red); }
        .metric-value.warning { color: var(--accent-yellow); }
        .metric-value.neutral { color: var(--text-secondary); }

        /* Table - Accessible */
        .opportunities-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.875rem;
        }
        .opportunities-table th {
            text-align: left;
            padding: 12px 16px;
            color: var(--text-secondary);
            font-weight: 600;
            border-bottom: 2px solid var(--border-color);
            white-space: nowrap;
        }
        .opportunities-table td { padding: 12px 16px; border-bottom: 1px solid var(--border-color); }
        .opportunities-table tbody tr { transition: background 0.15s ease; }
        .opportunities-table tbody tr:hover { background: var(--bg-tertiary); }
        .opportunities-table tbody tr:focus-within { outline: 2px solid var(--focus-ring); outline-offset: -2px; }

        /* Badges - with text + icon, not color-only */
        .status-badge-table {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 12px;
            border-radius: 9999px;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            border: 2px solid;
        }
        .status-badge-table.executed { background: rgba(63, 185, 80, 0.15); border-color: var(--accent-green); color: var(--accent-green); }
        .status-badge-table.executed::before { content: "✓"; }
        .status-badge-table.pending { background: rgba(210, 153, 34, 0.15); border-color: var(--accent-yellow); color: var(--accent-yellow); }
        .status-badge-table.pending::before { content: "⟳"; }
        .status-badge-table.rejected { background: rgba(248, 81, 73, 0.15); border-color: var(--accent-red); color: var(--accent-red); }
        .status-badge-table.rejected::before { content: "✕"; }

        /* Leg Rows */
        .leg-row { display: flex; gap: 12px; padding: 6px 0; font-size: 0.8125rem; align-items: center; }
        .leg-side-buy { color: var(--accent-green); font-weight: 600; }
        .leg-side-sell { color: var(--accent-red); font-weight: 600; }

        /* Log Panel - Live Region */
        .log-panel {
            height: 300px;
            overflow-y: auto;
            font-size: 0.8125rem;
            font-family: 'SF Mono', 'Fira Code', 'Monaco', monospace;
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 12px;
        }
        .log-entry {
            display: flex;
            gap: 8px;
            padding: 6px 4px;
            border-bottom: 1px solid var(--border-color);
            white-space: pre-wrap;
            word-break: break-word;
        }
        .log-entry:last-child { border-bottom: none; }
        .log-time { color: var(--text-muted); font-variant-numeric: tabular-nums; flex-shrink: 0; }
        .log-level { font-weight: 600; text-transform: uppercase; font-size: 0.6875rem; flex-shrink: 0; }
        .log-message { color: var(--text-primary); flex: 1; min-width: 0; }
        .log-entry.info .log-level { color: var(--accent-blue); }
        .log-entry.warn .log-level { color: var(--accent-yellow); }
        .log-entry.error .log-level { color: var(--accent-red); }
        .log-entry.success .log-level { color: var(--accent-green); }

        /* Responsive */
        @media (max-width: 768px) {
            .container { padding: 12px; }
            .grid { grid-template-columns: 1fr; }
            header { flex-direction: column; align-items: flex-start; }
            .controls { justify-content: center; }
            button { width: 100%; }
            .opportunities-table { font-size: 0.75rem; }
            .opportunities-table th, .opportunities-table td { padding: 8px 10px; }
        }

        /* Reduced Motion */
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after {
                animation-duration: 0.01ms !important;
                transition-duration: 0.01ms !important;
            }
        }

        /* High Contrast Mode */
        @media (prefers-contrast: high) {
            :root {
                --accent-blue: #8ab4f8;
                --accent-green: #7fd880;
                --accent-red: #f28b82;
                --accent-yellow: #ffe066;
            }
            .panel { border-width: 2px; }
            .status-badge { border-width: 3px; }
            button { border-width: 3px; }
        }
    </style>
</head>
<body>
    <!-- Skip Link -->
    <a href="#main-content" class="skip-link">Skip to main content</a>

    <!-- Banner Landmark -->
    <header role="banner">
        <h1>🤖 Polymarket Reverse Arbitrage Dashboard</h1>
        <div class="status-group" role="status" aria-live="polite" aria-label="System status">
            <span id="engineStatus" class="status-badge stopped" aria-label="Engine status: Stopped">
                <span class="status-text">Stopped</span>
            </span>
            <span id="modeBadge" class="status-badge paper" aria-label="Trading mode: Paper trading">
                <span class="status-text">Paper</span>
            </span>
        </div>
    </header>

    <!-- Main Content -->
    <main id="main-content" role="main">
        <!-- Controls Group -->
        <div class="controls" role="group" aria-label="Engine controls">
            <button id="btnStart" class="btn-start" onclick="startEngine()" aria-label="Start the trading engine">▶ Start Engine</button>
            <button id="btnStop" class="btn-stop" onclick="stopEngine()" disabled aria-label="Stop the trading engine">■ Stop Engine</button>
            <button id="btnRefresh" class="btn-refresh" onclick="refreshAll()" aria-label="Refresh all dashboard data">⟳ Refresh</button>
        </div>

        <!-- System Metrics Panel -->
        <section class="grid" aria-labelledby="metrics-heading">
            <article class="panel">
                <h2 id="metrics-heading" class="panel-title">System Metrics</h2>
                <div id="metricsContent" role="region" aria-label="System metrics details">
                    <div class="metric-row"><span class="metric-label">Uptime</span><span id="mUptime" class="metric-value">0s</span></div>
                    <div class="metric-row"><span class="metric-label">Opportunities Found</span><span id="mFound" class="metric-value">0</span></div>
                    <div class="metric-row"><span class="metric-label">Opportunities Executed</span><span id="mExecuted" class="metric-value">0</span></div>
                    <div class="metric-row"><span class="metric-label">Orders Placed</span><span id="mPlaced" class="metric-value">0</span></div>
                    <div class="metric-row"><span class="metric-label">Orders Filled</span><span id="mFilled" class="metric-value positive">0</span></div>
                    <div class="metric-row"><span class="metric-label">Orders Rejected</span><span id="mRejected" class="metric-value negative">0</span></div>
                    <div class="metric-row"><span class="metric-label">Total PnL</span><span id="mTotalPnl" class="metric-value">$0.00</span></div>
                    <div class="metric-row"><span class="metric-label">Daily PnL</span><span id="mDailyPnl" class="metric-value">$0.00</span></div>
                    <div class="metric-row"><span class="metric-label">Drawdown</span><span id="mDrawdown" class="metric-value">0%</span></div>
                    <div class="metric-row"><span class="metric-label">Active Positions</span><span id="mPositions" class="metric-value">0</span></div>
                    <div class="metric-row"><span class="metric-label">Open Orders</span><span id="mOrders" class="metric-value">0</span></div>
                    <div class="metric-row"><span class="metric-label">Errors (1h)</span><span id="mErrors" class="metric-value negative">0</span></div>
                    <div class="metric-row"><span class="metric-label">Last Scan</span><span id="mLastScan" class="metric-value">Never</span></div>
                    <div class="metric-row"><span class="metric-label">Last Execution</span><span id="mLastExec" class="metric-value">Never</span></div>
                </div>
            </article>

            <!-- Risk State Panel -->
            <article class="panel">
                <h2 id="risk-heading" class="panel-title">Risk State</h2>
                <div id="riskContent" role="region" aria-label="Risk state details">
                    <div class="metric-row"><span class="metric-label">Bankroll</span><span id="rBankroll" class="metric-value">$0.00</span></div>
                    <div class="metric-row"><span class="metric-label">Daily Loss Limit</span><span id="rDailyLimit" class="metric-value">$0.00</span></div>
                    <div class="metric-row"><span class="metric-label">Circuit Breaker</span><span id="rCircuit" class="metric-value neutral">OK</span></div>
                    <div class="metric-row"><span class="metric-label">Max Position</span><span id="rMaxPos" class="metric-value">$0.00</span></div>
                    <div class="metric-row"><span class="metric-label">Max Exposure</span><span id="rMaxExp" class="metric-value">$0.00</span></div>
                    <div class="metric-row"><span class="metric-label">Max Positions</span><span id="rMaxCnt" class="metric-value">0</span></div>
                </div>
            </article>

            <!-- HFT Detector Panel -->
            <article class="panel">
                <h2 id="hft-heading" class="panel-title">HFT Detector</h2>
                <div id="hftContent" role="region" aria-label="HFT detector status" aria-live="polite">
                    <div class="metric-row"><span class="metric-label">Status</span><span id="hftStatus" class="metric-value neutral">Inactive</span></div>
                    <div class="metric-row"><span class="metric-label">Tokens Subscribed</span><span id="hftTokens" class="metric-value">0</span></div>
                    <div class="metric-row"><span class="metric-label">Orderbook Updates</span><span id="hftUpdates" class="metric-value">0</span></div>
                    <div class="metric-row"><span class="metric-label">Opportunities Detected</span><span id="hftOpps" class="metric-value">0</span></div>
                </div>
            </article>
        </section>

        <!-- Opportunities Panel -->
        <section aria-labelledby="opps-heading">
            <article class="panel" style="grid-column: 1 / -1;">
                <h2 id="opps-heading" class="panel-title">Recent Opportunities</h2>
                <div role="region" aria-label="Opportunities table" tabindex="0">
                    <table class="opportunities-table">
                        <thead>
                            <tr>
                                <th scope="col">Time</th>
                                <th scope="col">Market</th>
                                <th scope="col">Type</th>
                                <th scope="col">Edge (bps)</th>
                                <th scope="col">Profit</th>
                                <th scope="col">Capital</th>
                                <th scope="col">Kelly %</th>
                                <th scope="col">Status</th>
                                <th scope="col">Legs</th>
                            </tr>
                        </thead>
                        <tbody id="oppsTable"></tbody>
                    </table>
                </div>
            </article>
        </section>

        <!-- System Log Panel -->
        <section aria-labelledby="log-heading">
            <article class="panel" style="grid-column: 1 / -1;">
                <h2 id="log-heading" class="panel-title">System Log</h2>
                <div id="logPanel" class="log-panel" role="log" aria-live="polite" aria-label="System log messages" tabindex="0"></div>
            </article>
        </section>
    </main>

    <script>
        let pollingInterval = null;

        // API helper with auth support
        async function apiGet(path) {
            try {
                const res = await fetch(path, {
                    headers: {
                        'X-API-Key': getApiKey()
                    }
                });
                if (!res.ok) throw new Error(`${res.status}`);
                return await res.json();
            } catch (e) {
                log('error', `API ${path} failed: ${e}`);
                return null;
            }
        }

        async function apiPost(path) {
            try {
                const res = await fetch(path, {
                    method: 'POST',
                    headers: {
                        'X-API-Key': getApiKey()
                    }
                });
                if (!res.ok) throw new Error(`${res.status}`);
                return await res.json();
            } catch (e) {
                log('error', `API POST ${path} failed: ${e}`);
                return null;
            }
        }

        function getApiKey() {
            // Allow API key to be set via localStorage for dashboard use
            return localStorage.getItem('dashboard_api_key') || '';
        }

        function log(level, message) {
            const panel = document.getElementById('logPanel');
            const entry = document.createElement('div');
            entry.className = `log-entry ${level}`;
            const time = new Date().toLocaleTimeString();
            entry.innerHTML = `<span class="log-time">[${time}]</span><span class="log-level">${level.toUpperCase()}</span><span class="log-message">${escapeHtml(message)}</span>`;
            panel.insertBefore(entry, panel.firstChild);
            while (panel.children.length > 200) panel.removeChild(panel.lastChild);
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function fmtTime(iso) {
            if (!iso) return 'Never';
            const d = new Date(iso);
            return d.toLocaleTimeString();
        }

        function fmtMoney(v) {
            return '$' + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        }

        function fmtPct(v) {
            return (Number(v) * 100).toFixed(2) + '%';
        }

        async function refreshAll() {
            await Promise.all([
                refreshStatus(),
                refreshMetrics(),
                refreshRisk(),
                refreshOpportunities(),
            ]);
        }

        async function refreshStatus() {
            const data = await apiGet('/api/status');
            if (!data) return;
            const engineStatus = document.getElementById('engineStatus');
            const modeBadge = document.getElementById('modeBadge');

            engineStatus.textContent = '';
            engineStatus.className = 'status-badge ' + (data.running ? 'running' : 'stopped');
            engineStatus.setAttribute('aria-label', `Engine status: ${data.running ? 'Running' : 'Stopped'}`);
            const engText = document.createElement('span');
            engText.className = 'status-text';
            engText.textContent = data.running ? 'Running' : 'Stopped';
            engineStatus.appendChild(engText);

            modeBadge.textContent = '';
            modeBadge.className = 'status-badge ' + (data.paper_trading ? 'paper' : 'live');
            modeBadge.setAttribute('aria-label', `Trading mode: ${data.paper_trading ? 'Paper trading' : 'Live trading'}`);
            const modeText = document.createElement('span');
            modeText.className = 'status-text';
            modeText.textContent = data.paper_trading ? 'Paper' : 'LIVE';
            modeBadge.appendChild(modeText);

            document.getElementById('btnStart').disabled = data.running;
            document.getElementById('btnStop').disabled = !data.running;
            log('info', `Status: ${data.running ? 'Running' : 'Stopped'} (${data.paper_trading ? 'Paper' : 'LIVE'})`);
        }

        async function refreshMetrics() {
            const data = await apiGet('/api/metrics');
            if (!data) return;
            document.getElementById('mUptime').textContent = data.uptime_seconds + 's';
            document.getElementById('mFound').textContent = data.opportunities_found;
            document.getElementById('mExecuted').textContent = data.opportunities_executed;
            document.getElementById('mPlaced').textContent = data.orders_placed;
            document.getElementById('mFilled').textContent = data.orders_filled;
            document.getElementById('mRejected').textContent = data.orders_rejected;
            document.getElementById('mTotalPnl').textContent = fmtMoney(data.total_pnl_usd);
            document.getElementById('mTotalPnl').className = 'metric-value ' + (data.total_pnl_usd >= 0 ? 'positive' : 'negative');
            document.getElementById('mDailyPnl').textContent = fmtMoney(data.daily_pnl_usd);
            document.getElementById('mDailyPnl').className = 'metric-value ' + (data.daily_pnl_usd >= 0 ? 'positive' : 'negative');
            document.getElementById('mDrawdown').textContent = fmtPct(data.current_drawdown_pct);
            document.getElementById('mDrawdown').className = 'metric-value ' + (data.current_drawdown_pct > 0.05 ? 'negative' : 'positive');
            document.getElementById('mPositions').textContent = data.active_positions;
            document.getElementById('mOrders').textContent = data.open_orders;
            document.getElementById('mErrors').textContent = data.errors_last_hour;
            document.getElementById('mLastScan').textContent = fmtTime(data.last_scan_timestamp);
            document.getElementById('mLastExec').textContent = fmtTime(data.last_execution_timestamp);
        }

        async function refreshRisk() {
            const data = await apiGet('/api/risk');
            if (!data) return;
            document.getElementById('rBankroll').textContent = fmtMoney(data.bankroll_usd);
            document.getElementById('rDailyLimit').textContent = fmtMoney(data.daily_loss_limit_usd);
            const cb = data.circuit_breaker_triggered ? `TRIGGERED: ${data.circuit_breaker_reason || 'Unknown'}` : 'OK';
            document.getElementById('rCircuit').textContent = cb;
            document.getElementById('rCircuit').className = 'metric-value ' + (data.circuit_breaker_triggered ? 'negative' : 'neutral');
            document.getElementById('rMaxPos').textContent = fmtMoney(data.max_position_per_market_usd);
            document.getElementById('rMaxExp').textContent = fmtMoney(data.max_gross_exposure_usd);
            document.getElementById('rMaxCnt').textContent = data.max_concurrent_positions;
        }

        async function refreshOpportunities() {
            const data = await apiGet('/api/opportunities?limit=50');
            if (!data) return;
            const tbody = document.getElementById('oppsTable');
            tbody.innerHTML = '';
            data.forEach(opp => {
                const tr = document.createElement('tr');
                const legsHtml = opp.legs.map(l =>
                    `<div class="leg-row"><span class="leg-side-${l.side.toLowerCase()}">${l.side}</span> ${l.outcome} @ ${Number(l.price).toFixed(4)} x ${Number(l.size).toFixed(2)}</div>`
                ).join('');
                const statusClass = opp.executed ? 'executed' : 'pending';
                const statusText = opp.executed ? 'Executed' : 'Detected';
                tr.innerHTML = `
                    <td>${fmtTime(opp.timestamp)}</td>
                    <td><code>${opp.market_id.slice(0, 12)}…</code></td>
                    <td>${opp.type}</td>
                    <td>${opp.gross_edge_bps}</td>
                    <td>${fmtMoney(opp.estimated_profit_usd)}</td>
                    <td>${fmtMoney(opp.required_capital_usd)}</td>
                    <td>${opp.kelly_fraction.toFixed(2)}%</td>
                    <td><span class="status-badge-table ${statusClass}" aria-label="Status: ${statusText}">${statusText}</span></td>
                    <td>${legsHtml}</td>
                `;
                tbody.appendChild(tr);
            });
        }

        async function startEngine() {
            const res = await apiPost('/api/engine/start');
            if (res) {
                log('success', 'Engine start requested');
                setTimeout(refreshAll, 1000);
            }
        }

        async function stopEngine() {
            const res = await apiPost('/api/engine/stop');
            if (res) {
                log('warn', 'Engine stop requested');
                setTimeout(refreshAll, 1000);
            }
        }

        // Initialize on load
        document.addEventListener('DOMContentLoaded', () => {
            // Check for saved API key
            const savedKey = localStorage.getItem('dashboard_api_key');
            if (!savedKey) {
                log('warn', 'No API key set. Use localStorage.setItem("dashboard_api_key", "your-key") to authenticate.');
            }

            refreshAll();
            pollingInterval = setInterval(refreshAll, 5000);

            // Keyboard shortcut: Ctrl+R to refresh
            document.addEventListener('keydown', (e) => {
                if ((e.ctrlKey || e.metaKey) && e.key === 'r') {
                    e.preventDefault();
                    refreshAll();
                }
            });
        });

        // Reconnect on visibility change
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) refreshAll();
        });
    </script>
</body>
</html>
"""


# ============================================================================
# FastAPI App
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine
    logger.info("API server starting...")
    yield
    logger.info("API server shutting down...")
    if _engine:
        await stop_engine()
        _engine = None


app = FastAPI(
    title="Polymarket Reverse Arbitrage API",
    version="1.0.0",
    lifespan=lifespan,
)


def create_app(engine: ReverseArbEngine | None = None) -> FastAPI:
    """Create app with pre-initialized engine (for embedding)."""
    global _engine
    if engine:
        _engine = engine
    return app


# ============================================================================
# Health & Status
# ============================================================================

@app.get("/health", response_model=HealthResponse)
async def health():
    settings = get_settings()
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(UTC).isoformat(),
        paper_trading=settings.paper_trading,
        engine_running=_engine._running if _engine else False,
    )


@app.get("/api/status", response_model=EngineStatusResponse, dependencies=[ReadOnlyDependency])
async def get_status():
    if not _engine:
        raise HTTPException(503, "Engine not initialized")
    return EngineStatusResponse(**_engine.get_status())


@app.get("/api/metrics", response_model=MetricsResponse, dependencies=[ReadOnlyDependency])
async def get_metrics():
    if not _engine:
        raise HTTPException(503, "Engine not initialized")
    m = _engine.get_metrics()
    return MetricsResponse(
        uptime_seconds=m.uptime_seconds,
        opportunities_found=m.opportunities_found,
        opportunities_executed=m.opportunities_executed,
        orders_placed=m.orders_placed,
        orders_filled=m.orders_filled,
        orders_rejected=m.orders_rejected,
        total_pnl_usd=float(m.total_pnl_usd),
        daily_pnl_usd=float(m.daily_pnl_usd),
        current_drawdown_pct=float(m.current_drawdown_pct),
        peak_equity_usd=float(m.peak_equity_usd),
        active_positions=m.active_positions,
        open_orders=m.open_orders,
        errors_last_hour=m.errors_last_hour,
        latency_p50_ms=m.latency_p50_ms,
        latency_p99_ms=m.latency_p99_ms,
        last_scan_timestamp=m.last_scan_timestamp.isoformat() if m.last_scan_timestamp else None,
        last_execution_timestamp=m.last_execution_timestamp.isoformat() if m.last_execution_timestamp else None,
    )


@app.get("/api/risk", response_model=RiskStateResponse, dependencies=[ReadOnlyDependency])
async def get_risk_state():
    if not _engine or not _engine._risk_engine:
        raise HTTPException(503, "Risk engine not initialized")
    state = _engine._risk_engine.get_risk_state()
    return RiskStateResponse(**state)


# ============================================================================
# Opportunities
# ============================================================================

@app.get("/api/opportunities", dependencies=[ReadOnlyDependency])
async def get_opportunities(limit: int = Query(50, le=200), executed_only: bool = False):
    if not _engine:
        raise HTTPException(503, "Engine not initialized")

    opps = _engine._current_opportunities
    if executed_only:
        # Filter to executed (have results)
        opps = [o for o in opps if o.metadata.get("executed")]

    opps = opps[-limit:]

    return [
        {
            "id": o.id,
            "timestamp": o.metadata.get("detected_at", datetime.now(UTC).isoformat()),
            "type": o.type.value,
            "market_id": o.legs[0].market_id if o.legs else "",
            "condition_id": o.legs[0].condition_id if o.legs else None,
            "gross_edge_bps": o.gross_edge_bps,
            "net_edge_bps": o.net_edge_bps,
            "estimated_profit_usd": float(o.estimated_profit_usd),
            "required_capital_usd": float(o.required_capital_usd),
            "kelly_fraction": float(o.kelly_fraction) * 100,
            "confidence": float(o.confidence),
            "legs": [
                {
                    "side": leg.side.value,
                    "outcome": leg.outcome,
                    "price": float(leg.target_price),
                    "size": float(leg.size),
                    "token_id": leg.token_id,
                }
                for leg in o.legs
            ],
            "executed": o.metadata.get("executed", False),
            "execution_results": o.metadata.get("execution_results"),
        }
        for o in opps
    ]


# ============================================================================
# Positions
# ============================================================================

@app.get("/api/positions", dependencies=[ReadOnlyDependency])
async def get_positions():
    if not _engine or not _engine._execution_engine:
        raise HTTPException(503, "Execution engine not initialized")

    positions = await _engine._execution_engine.position_manager.get_all_positions()
    return [
        {
            "token_id": p.token_id,
            "market_id": p.market_id,
            "platform": p.platform.value,
            "side": p.side.value,
            "size": float(p.size),
            "entry_price": float(p.entry_price),
            "current_price": float(p.current_price),
            "unrealized_pnl": float(p.unrealized_pnl),
            "realized_pnl": float(p.realized_pnl),
            "total_pnl": float(p.total_pnl),
            "timestamp": p.timestamp.isoformat() if p.timestamp else datetime.now(UTC).isoformat(),
        }
        for p in positions
    ]


# ============================================================================
# Engine Control (Admin only)
# ============================================================================

@app.post("/api/engine/start", dependencies=[AdminDependency])
async def start_engine_endpoint():
    global _engine
    if _engine and _engine._running:
        return {"status": "already_running"}

    if not _engine:
        _engine = await start_engine()
    else:
        await _engine.start()

    return {"status": "started", "paper_trading": _engine._config.paper_trading}


@app.post("/api/engine/stop", dependencies=[AdminDependency])
async def stop_engine_endpoint():
    global _engine
    if not _engine:
        return {"status": "not_running"}

    await _engine.stop()
    return {"status": "stopped"}


# ============================================================================
# Dashboard (Root)
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)


# ============================================================================
# Main Entry Point
# ============================================================================

def run_server(host: str = "0.0.0.0", port: int = 8080):
    """Run the API server with uvicorn."""
    import uvicorn
    uvicorn.run(
        "src.api.server:app",
        host=host,
        port=port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    run_server()