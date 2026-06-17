# Paper Soak Runbook

Use this to run the trading stack for multiple days in paper mode against live market data.

## What this tests

- Live Polymarket and Opinion market-data ingestion
- Strategy evaluation across a broad market universe
- Order lifecycle, fill accounting, risk gating, and kill-switch behavior
- Realized and unrealized paper PnL over time

## What this does not prove

- Exact live profitability
- Queue position on the real exchanges
- Hidden liquidity or private order flow

Treat the result as a strong signal, not a guarantee.

## Recommended setup

1. Prepare a broad market registry.
   - Put many logical markets in `MARKET_REGISTRY_JSON` or a JSON file.
   - Each logical market must map to venue-specific Polymarket and Opinion IDs.
2. Use paper mode only.
   - No live trading keys are required.
3. Keep observability on a dedicated port.
   - Default soak port: `18080`.

## Example

```bash
python scripts/run_paper_validation.py ^
  --duration-hours 72 ^
  --sample-seconds 300 ^
  --obs-port 18080 ^
  --min-registry-pairs 25 ^
  --min-markets-total 25 ^
  --min-markets-polymarket 10 ^
  --min-markets-opinion 10
```

The runner builds `market_registry.json` automatically before the soak starts.

## Success criteria

- `/health` stays `ALIVE`
- `/ready` stays `READY` or `DEGRADED` for paper mode
- Feed age stays below the stale threshold
- The soak sees enough markets from both venues
- Final PnL, fills, and drawdown are logged at exit

## Failure signals

- The runner exits early
- Feed data goes stale
- Coverage thresholds are not met
- Readiness drops to `NOT_READY`
