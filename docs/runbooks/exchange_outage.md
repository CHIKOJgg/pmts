# Exchange Outage Response

Procedures for handling API downtime or WebSocket connectivity issues.

## 1. Detection
- [ ] Metric `pmts_api_errors_total` shows a sustained increase.
- [ ] Metric `pmts_reconnect_total` is incrementing rapidly.
- [ ] Logs show `5xx` or `Connection refused` errors.

## 2. Immediate Action
- [ ] **Activate Kill Switch** if the bot is attempting to place orders against stale data.
- [ ] Check exchange status pages:
    - Polymarket: [status.polymarket.com](https://status.polymarket.com)
    - Opinion: [status.opinion.markets](https://status.opinion.markets)

## 3. Order Management
- [ ] If the WebSocket is down but REST API is up, the bot will attempt to cancel orders.
- [ ] If all APIs are down, log into the Exchange UI manually to verify/cancel open positions.

## 4. Recovery
- [ ] Once connectivity is stable, monitor `pmts_feed_last_ts_seconds` to ensure data is flowing.
- [ ] Perform a Graceful Restart to ensure the `ExecutionEngine` reconciles any fills that happened during the outage.
