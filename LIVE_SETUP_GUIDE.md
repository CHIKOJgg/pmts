# PMTS Production and Paper Trading Guide

This guide is the shortest path from a fresh checkout to a safe paper-trading run.
It assumes you want to validate strategy behavior first and only then decide whether
to move toward live capital.

## 1. Prerequisites

- Docker and Docker Compose
- Python 3.11 or newer for local validation
- A populated `.env` file copied from `.env.example`
- A market registry file or `MARKET_REGISTRY_JSON`
- A valid `KILL_SWITCH_TOKEN`

## 2. Recommended paper-trading setup

Start with paper mode and keep live trading disabled.

```bash
cp .env.example .env
```

Edit `.env` and set these values first:

```env
MODE=paper
ENABLE_TRADING=false
MARKETS=BTC-Q4,ETH-Q1,SOL-Q2
KILL_SWITCH_TOKEN=replace-with-a-long-random-secret
```

If you do not want to trade against a database from your host machine, let the
container use its default SQLite path.

## 3. Run paper trading with Docker

Build and start the main service:

```bash
docker compose up --build pmts
```

If you want the monitoring stack as well:

```bash
docker compose --profile monitoring up --build pmts prometheus grafana
```

Useful checks:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/metrics
```

Expected outcome:

- `/health` returns a healthy or ready state for paper mode
- `/metrics` exposes Prometheus metrics
- Logs show proposals being evaluated even when live trading is disabled

## 4. Run the backtest before any live activity

```bash
python main.py --mode backtest --ticks 2000 --capital 10000 --verbose
```

Use the backtest to confirm the strategy stack is wired correctly on your machine.
Do not treat this as proof of live profitability.

## 5. Paper soak validation

For a longer validation run, use the paper soak tooling:

```bash
python scripts/run_paper_validation.py \
  --duration-hours 72 \
  --sample-seconds 300 \
  --obs-port 18080 \
  --min-registry-pairs 25 \
  --min-markets-total 25 \
  --min-markets-polymarket 10 \
  --min-markets-opinion 10
```

This is the best path for checking whether a strategy remains stable over time.

## 6. Dubai VPN routing

Only use a VPN endpoint in Dubai if that is allowed by the exchanges, your local
laws, and your compliance policy.

Recommended operational flow:

1. Connect the VPN before starting Docker.
2. Use a reputable provider that offers a Dubai or UAE exit node.
3. Verify the exit IP before launching the bot.

```bash
curl https://api.ipify.org
```

4. Confirm the exchange sites and APIs are reachable through that route.
5. Keep the kill switch enabled and start in paper mode first.

Do not rely on the VPN as a substitute for jurisdictional or venue compliance.

## 7. Shutdown

Stop the stack cleanly:

```bash
docker compose down
```

If you need to keep the monitoring stack separate from the trading container,
stop only the service you are changing and leave the rest running.

## 8. What to watch

- Proposal count
- Fill rate
- Slippage
- Drawdown
- Kill-switch activations
- Feed freshness

If any of those drift badly, pause trading and investigate before scaling up.

