# Incident Escalation Path

On-call rotation and escalation contacts.

## 1. Severity Levels
- **SEV 1**: System down, massive financial risk, or exchange outage.
- **SEV 2**: Partial functionality loss (one exchange down, metrics failing).
- **SEV 3**: Minor bugs, logging issues, non-critical data lag.

## 2. Escalation Steps
1. **On-Call Engineer**: Primary responder. Must acknowledge within 5 minutes for SEV 1.
2. **Lead Developer**: Escalate if SEV 1 is not resolved within 30 minutes.
3. **Risk Officer**: Notify immediately for any loss > 10% of `initial_cash_usdc`.

## 3. Contact Info
- **Tech Lead**: [redacted]
- **Risk Desk**: [redacted]
- **Exchange Support**:
    - Polymarket: [support@polymarket.com](mailto:support@polymarket.com)
    - Opinion: [support@opinion.markets](mailto:support@opinion.markets)

## 4. Communication
- [ ] Create an incident channel/thread.
- [ ] Document all manual actions taken in SQLite or Exchange UIs.
- [ ] Post a summary after resolution.
