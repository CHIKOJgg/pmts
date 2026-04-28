# Graceful Shutdown Procedure

Proper shutdown ensures all risk reservations are released and orders are handled.

## 1. Initiate Shutdown
- [ ] Send `SIGINT` (Ctrl+C) to the process.
- [ ] **Do NOT** force kill (`SIGKILL`) unless the system hangs for > 30s.

## 2. Monitor Termination Sequence
- [ ] Verify `Orchestrator stopping...` appears in logs.
- [ ] Ensure `MarketDataProvider` stops first (prevents new signals).
- [ ] Wait for `ExecutionEngine` to finish any pending cancellations.

## 3. Verify State
- [ ] Check logs for `Orchestrator stopped.`
- [ ] Ensure the SQLite WAL file is merged (database file size may change).

## 4. Post-Mortem (Optional)
- [ ] If shutdown was due to an error, check `log_file` (if configured) before restarting.
