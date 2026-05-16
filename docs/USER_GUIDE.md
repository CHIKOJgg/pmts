# PMTS — Prediction Market Trading System

## Quick Start Guide

### What is PMTS?

PMTS is an automated trading system for binary prediction markets. It targets two venues:
- **Polymarket** (Polygon / USDC)
- **Opinion Markets** (BNB Chain / USDC)

It implements two trading strategies:
- **Arbitrage** — Buys YES on the cheaper venue and NO on the more expensive venue, locking in the price difference
- **Market Making** — Posts two-sided limit orders using the Stoikov reservation-price formula to earn the spread

Both strategies share a common synchronous risk engine with 12 pre-trade checks.

---

## System Requirements

- **Python**: 3.11+
- **OS**: Linux, macOS, or Windows
- **Memory**: 512MB minimum, 1GB recommended
- **Network**: Stable internet connection with WebSocket support
- **Docker** (optional): For containerized deployment

---

## Installation

### Option 1: Local Installation

```bash
# Clone the repository
git clone https://github.com/your-org/polymarket-arbitrage.git
cd polymarket-arbitrage

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Option 2: Docker

```bash
# Build the image
docker build -t pmts:latest .

# Or use docker-compose
docker-compose up pmts
```

---

## Configuration

### 1. Create Environment File

```bash
cp .env.example .env
```

### 2. Fill in Required Values

#### Exchange Credentials (Required for Live/Paper Trading)

**Polymarket:**
```env
PM_CLOB_URL=https://clob.polymarket.com
PM_WS_URL=wss://ws-subscriptions-clob.polymarket.com/ws/market
PM_API_KEY=your_api_key
PM_API_SECRET=your_api_secret
PM_PASSPHRASE=your_passphrase
PM_WALLET_KEY=0x...your_wallet_private_key
PM_TAKER_FEE_BPS=20
PM_SANDBOX=false
```

**Opinion Markets:**
```env
OP_REST_URL=https://openapi.opinion.trade/openapi
OP_WS_URL=wss://openapi.opinion.trade/openapi/ws
OP_API_KEY=your_api_key
OP_WALLET_KEY=0x...your_wallet_private_key
OP_CTF_EXCHANGE_ADDR=0x...exchange_contract_address
OP_TAKER_FEE_BPS=25
OP_SANDBOX=false
```

#### Markets to Trade (Required)

```env
MARKETS=market_id_1,market_id_2,market_id_3
```

> **Note**: Market IDs are the condition IDs from Polymarket/Opinion. You can find these in the URL of the market page.

#### Capital and Risk Limits

```env
INITIAL_CASH_USDC=10000
KILL_SWITCH_TOKEN=CHANGE-ME-USE-A-SECURE-RANDOM-STRING

DRAWDOWN_KILL_PCT=0.20
DRAWDOWN_WARN_PCT=0.15
MAX_ORDER_USDC=200
MIN_ORDER_USDC=1.0
MAX_MARKET_EXP_PCT=0.05
MAX_MARKET_EXP_USDC=500
MAX_NET_DELTA=50

ARB_BUDGET_USDC=2000
MM_BUDGET_USDC=3000
```

#### Strategy Toggles

```env
ENABLE_TRADING=true
ENABLE_ARB=true
ENABLE_MM=true
ENABLE_HEDGE=true
```

#### AI Module (Optional)

```env
AI_ENABLED=false
AI_USE_HEURISTIC_ONLY=true
AI_API_TIMEOUT_MS=200
AI_CACHE_TTL_MS=3000
```

#### Observability

```env
OBSERVABILITY_BIND_HOST=127.0.0.1
LOG_LEVEL=INFO
LOG_FORMAT=json
LOG_FILE=
```

---

## Running Modes

### 1. Backtest Mode (Default — No Credentials Needed)

Run historical simulation with synthetic data:

```bash
python main.py --mode backtest --ticks 2000 --capital 10000 --verbose
```

**Parameters:**
- `--ticks`: Number of synthetic ticks to simulate (default: 2000)
- `--capital`: Starting capital in USDC (default: 10000)
- `--verbose`: Enable DEBUG logging

**Output:**
```
Backtest Result:
  Total ticks: 2000
  Final equity: $10,234.56
  Total return: 2.35%
  Max drawdown: 1.2%
  Total fills: 45
  Arb fills: 12
  MM fills: 33
  Fill rate: 67.2%
  Sharpe ratio: 1.45
```

### 2. Paper Trading Mode (Recommended First Step — No Real Capital at Risk)

Run the full live pipeline with simulated order execution:

```bash
python main.py --mode paper --paper-fill-prob 0.85
```

**Parameters:**
- `--paper-fill-prob`: Base fill probability (0.0-1.0, default: 0.85)

**What happens:**
- Connects to real WebSocket market data feeds
- Strategies generate real trading signals
- Orders are simulated (not sent to exchanges)
- Fills are probabilistic based on order aggressiveness
- Full risk engine, portfolio tracking, and observability active

**When to use:**
- Testing the full pipeline before risking real capital
- Validating strategy behavior with live market data
- Monitoring system stability over 48+ hours

### 3. Live Trading Mode (Real Capital — Use with Caution!)

```bash
python main.py --mode live
```

**Prerequisites:**
- All exchange credentials configured in `.env`
- `KILL_SWITCH_TOKEN` set to a secure random string
- Markets list populated with valid market IDs
- System tested in paper trading mode for 48+ hours
- You understand the risks and are prepared to lose capital

---

## Docker Deployment

### Backtest in Docker

```bash
docker-compose --profile backtest up pmts-backtest
```

### Live Trading in Docker

```bash
# Build first
docker build -t pmts:latest .

# Run with .env file
docker-compose up pmts
```

### Full Stack (with Prometheus + Grafana)

```bash
docker-compose --profile full up
```

Access monitoring:
- **Prometheus**: http://localhost:9090
- **Grafana**: http://localhost:3000 (admin/admin)
- **Health Check**: http://localhost:8080/ready
- **Metrics**: http://localhost:8080/metrics

---

## Monitoring and Observability

### Health Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Liveness check — returns 200 if system is running |
| `GET /ready` | Readiness check — returns 200 if all components are healthy |
| `GET /metrics` | Prometheus-compatible metrics |
| `GET /metrics/json` | JSON-formatted metrics |

### Key Metrics

| Metric | Description |
|--------|-------------|
| `pmts_proposals_total` | Total proposals evaluated (by strategy and verdict) |
| `pmts_fills_total` | Total fills (by platform and strategy) |
| `pmts_fill_usdc_total` | Total fill volume in USDC |
| `pmts_order_latency_seconds` | Order execution latency histogram |
| `pmts_api_errors_total` | API errors (by platform and error type) |
| `pmts_reconnect_total` | WebSocket reconnections (by platform) |
| `pmts_drawdown_pct` | Current portfolio drawdown percentage |
| `pmts_kill_switch_active` | Kill switch status (0 or 1) |
| `pmts_active_orders_count` | Number of active orders (by platform) |

### Grafana Dashboard

A pre-built dashboard is included at `docs/grafana-dashboard.json`. It visualizes:
- Portfolio equity over time
- Fill rates and volumes
- Drawdown and risk metrics
- System health indicators

---

## Operations

### Starting the System

```bash
# 1. Verify configuration
python main.py --mode live --log-level DEBUG 2>&1 | head -50

# 2. Start in paper mode first
python main.py --mode paper

# 3. Monitor for 48+ hours

# 4. Switch to live mode (if ready)
python main.py --mode live
```

### Stopping the System

```bash
# Graceful shutdown (recommended)
Ctrl+C  # or SIGTERM

# The system will:
# 1. Cancel all open orders
# 2. Persist state to SQLite
# 3. Close database connections
# 4. Exit cleanly
```

### Emergency Stop

If the system is misbehaving:

```bash
# Activate kill switch via API
curl -X POST http://localhost:8080/kill-switch/activate

# Or send SIGTERM (cancels all orders)
kill -TERM <pid>

# Or force kill (last resort — may leave orders open)
kill -9 <pid>
```

### Kill Switch Reset

After a kill switch activation:

```bash
# Reset requires the confirmation token
curl -X POST http://localhost:8080/kill-switch/reset \
  -d '{"token": "your-kill-switch-token"}'
```

---

## Troubleshooting

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| `Configuration validation failed` | Missing env vars | Check `.env` against `.env.example` |
| `WebSocket connection failed` | Network/firewall | Check connectivity to WS URLs |
| `API key rejected` | Invalid credentials | Verify keys on exchange dashboard |
| `SQLite database locked` | Concurrent access | Ensure only one instance running |
| `Kill switch active` | Drawdown exceeded | Check drawdown, reset if appropriate |
| `No market data received` | Invalid market IDs | Verify market IDs are correct and active |

### Logs

Logs are output to stdout. With `LOG_FORMAT=json`, they can be parsed by log aggregators:

```json
{"level": "INFO", "ts": "2024-01-01T00:00:00Z", "msg": "SYSTEM LIVE and trading."}
{"level": "WARNING", "ts": "2024-01-01T00:00:01Z", "msg": "REJECT proposal=abc123... strategy=ARB market=mkt-1 $100.00 reason=INSUFFICIENT_CAPITAL"}
```

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                      Trading Pipeline                        │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐               │
│  │  Market  │───▶│ Feature  │───▶│ Strategy │               │
│  │   Data   │    │ Engine   │    │  Engine  │               │
│  └──────────┘    └──────────┘    └──────────┘               │
│       │                                │                     │
│       ▼                                ▼                     │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐               │
│  │   Risk   │◀───│   Risk   │◀───│ Proposal │               │
│  │  Engine  │    │  Gate    │    │  Queue   │               │
│  └──────────┘    └──────────┘    └──────────┘               │
│       │                                                     │
│       ▼                                                     │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐               │
│  │Execution │───▶│ Exchange │───▶│ Portfolio│               │
│  │  Engine  │    │ Client   │    │ Manager  │               │
│  └──────────┘    └──────────┘    └──────────┘               │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | File | Responsibility |
|-----------|------|----------------|
| Market Data Provider | `data/market_data_provider.py` | Ingests and fans out market snapshots |
| WebSocket Adapters | `data/adapters/*.py` | Connect to exchange WebSocket feeds |
| Feature Engine | `engine/feature_engine.py` | Computes features from market data |
| Strategy Engine | `engine/strategy_engine.py` | Runs arbitrage and market making strategies |
| Risk Engine | `risk/engine.py` | 12-check synchronous pre-trade risk gate |
| Kill Switch | `risk/kill_switch.py` | Emergency stop with token-gated reset |
| Execution Engine | `execution/engine.py` | Order lifecycle management |
| Exchange Clients | `execution/clients/*.py` | Venue-specific API implementations |
| Portfolio Manager | `portfolio/manager.py` | Position tracking, MTM, P&L |
| SQLite Store | `portfolio/storage.py` | Persistent state storage |
| Orchestrator | `engine/orchestrator.py` | Wires all components together |
| Observability | `infrastructure/observability.py` | Prometheus metrics and health checks |

---

## Safety Checklist

Before going live with real capital:

- [ ] System runs in paper trading mode for 48+ hours without crashes
- [ ] All exchange credentials verified and tested
- [ ] Kill switch token is a secure random string
- [ ] Risk limits are set appropriately for your capital
- [ ] You understand the arbitrage and market making strategies
- [ ] You have a plan for monitoring and responding to alerts
- [ ] You are prepared to lose the capital you allocate
- [ ] You have tested the emergency stop procedure
- [ ] You have reviewed the runbooks in `docs/runbooks/`

---

## License

This software is provided for educational and research purposes. Trading prediction markets involves significant risk. Use at your own risk.
