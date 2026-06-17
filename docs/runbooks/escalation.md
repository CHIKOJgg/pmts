# Escalation Runbook

Owner: Lead Developer

Use this runbook when an issue exceeds operator-only resolution.

1. Classify severity.
   - SEV 1: trading unsafe, capital at risk, or exchange unavailable.
   - SEV 2: one venue degraded or observability impaired.
   - SEV 3: minor bug, log noise, or delayed cleanup.
2. Notify the next owner.
   - Ops Primary handles the first response.
   - Risk Officer handles kill-switch and capital-risk questions.
   - Lead Developer handles code-path triage and remediation.
3. Preserve evidence.
   - Keep logs, dashboard screenshots, and SQLite state intact.
4. Communicate the next action.
   - State whether the system is paused, paper-only, or ready for restart.
5. Close the incident only after the runbook is complete and the dashboard is green again.
