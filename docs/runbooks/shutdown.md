# Shutdown Runbook

Owner: Ops Primary

Use this runbook for planned shutdowns or operator-driven stops.

1. Pause trading intent if needed.
   - Set `ENABLE_TRADING=false` for paper-only continuation, or stop data flow if you are ending the session.
2. Stop the process cleanly.
   - Send `SIGINT` first.
   - Avoid `SIGKILL` unless the process is hung.
3. Confirm shutdown logs.
   - `Orchestrator stopping...`
   - `ExecutionEngine stopped`
   - `Orchestrator stopped.`
4. Confirm persistence.
   - SQLite should contain the latest reservations, fills, and terminal order state.
5. Confirm restart readiness.
   - A fresh boot should reconcile open orders and reservations before trading resumes.
