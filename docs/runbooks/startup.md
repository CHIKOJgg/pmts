# Startup Checklist

Follow these steps to ensure a safe system startup.

## 1. Environment Validation
- [ ] Verify all required API keys are set (`PM_API_KEY`, `OP_API_KEY`, etc.).
- [ ] Ensure `KILL_SWITCH_TOKEN` is set to a secure, known value.
- [ ] Check `MARKETS` list for accuracy.

## 2. Infrastructure Check
- [ ] Verify SQLite database integrity:
  ```bash
  sqlite3 pmts.db "PRAGMA integrity_check;"
  ```
- [ ] Ensure port `8080` (Observability) is not in use.

## 3. Execution
- [ ] Start the bot:
  ```bash
  python main.py
  ```
- [ ] Monitor logs for the following sequence:
    - `SqlitePortfolioStore initialized`
    - `ExecutionEngine: Reconciling open orders...`
    - `Orchestrator started. trading=True`

## 4. Verification
- [ ] Check Readiness endpoint:
  ```bash
  curl http://localhost:8080/ready
  ```
  Status should be `200 OK`.
- [ ] Check Prometheus metrics:
  ```bash
  curl http://localhost:8080/metrics
  ```
  Verify `pmts_feed_last_ts_seconds` is updating.
