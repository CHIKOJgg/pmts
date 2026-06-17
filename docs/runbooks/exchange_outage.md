# Exchange Outage Runbook

Owner: Ops On-Call

Use this runbook when one exchange is unhealthy, stale, or disconnecting repeatedly.

1. Confirm the outage.
   - Watch `pmts_api_errors_total`.
   - Watch `pmts_reconnect_total`.
   - Confirm the feed timestamp gauge stops advancing.
2. Freeze risky trading if needed.
   - Trip the kill switch if the bot is trading against stale data.
3. Let the system self-heal first.
   - WebSocket adapters reconnect automatically.
   - REST polling should continue to report errors rather than fabricating data.
4. Verify recovery.
   - Confirm feed timestamps resume.
   - Confirm stale snapshots are suppressed before new proposals are evaluated.
5. Escalate if recovery does not occur within the incident window.
