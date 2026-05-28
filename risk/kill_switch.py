"""risk/kill_switch.py — Atomic circuit breaker with confirmation-token reset."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ActivationRecord:
    activated_at: int
    reason: str
    mtm_drawdown: float
    peak_equity: float
    current_equity: float
    triggering_id: Optional[str] = None


@dataclass
class ResetRecord:
    reset_at: int
    operator_id: Optional[str]
    reason: str


class KillSwitch:
    """
    Atomic circuit breaker.
    Activation is synchronous (no I/O).
    Reset requires the correct confirmation token - prevents automation.
    """

    def __init__(self, confirmation_token: str) -> None:
        if not confirmation_token:
            raise ValueError("confirmation_token must be non-empty")

        # Enforce minimum security requirements
        if len(confirmation_token) < 16:
            raise ValueError(f"confirmation_token must be at least 16 characters, got {len(confirmation_token)}")

        # Check for complexity (at least 2 of: upper, lower, digit, special)
        has_upper = bool(re.search(r"[A-Z]", confirmation_token))
        has_lower = bool(re.search(r"[a-z]", confirmation_token))
        has_digit = bool(re.search(r"\d", confirmation_token))
        has_special = bool(re.search(r"[^A-Za-z0-9]", confirmation_token))

        complexity_score = sum([has_upper, has_lower, has_digit, has_special])
        if complexity_score < 2:
            raise ValueError(
                "confirmation_token must contain at least 2 of: uppercase, lowercase, digit, special character"
            )

        self._token: str = confirmation_token
        self._active: bool = False
        self._activations: list[ActivationRecord] = []
        self._resets: list[ResetRecord] = []

    def sync_state(self, active: bool) -> None:
        """Update active status silently (no logs/audit) - used for startup restoration."""
        self._active = active

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def last_activation(self) -> Optional[ActivationRecord]:
        return self._activations[-1] if self._activations else None

    @property
    def activation_count(self) -> int:
        return len(self._activations)

    def activate(
        self,
        reason: str,
        mtm_drawdown: float,
        peak_equity: float,
        current_equity: float,
        triggering_id: Optional[str] = None,
    ) -> ActivationRecord:
        record = ActivationRecord(
            activated_at=_now_ms(),
            reason=reason,
            mtm_drawdown=mtm_drawdown,
            peak_equity=peak_equity,
            current_equity=current_equity,
            triggering_id=triggering_id,
        )
        self._activations.append(record)
        self._active = True
        logger.critical(
            "KILL SWITCH ACTIVATED reason=%s drawdown=%.2f%% equity=$%.2f peak=$%.2f",
            reason,
            mtm_drawdown * 100,
            current_equity,
            peak_equity,
        )
        return record

    def reset(self, token: str, operator_id: Optional[str] = None) -> bool:
        if not self._active:
            return False
        if token != self._token:
            logger.warning("Kill switch reset rejected — wrong token (operator=%s)", operator_id)
            return False
        self._resets.append(
            ResetRecord(
                reset_at=_now_ms(),
                operator_id=operator_id,
                reason="operator_manual_reset",
            )
        )
        self._active = False
        logger.warning("KILL SWITCH RESET by operator=%s", operator_id or "unknown")
        return True

    def audit_trail(self) -> dict:
        return {
            "active": self._active,
            "activation_count": len(self._activations),
            "reset_count": len(self._resets),
            "activations": [
                {
                    "at": r.activated_at,
                    "reason": r.reason,
                    "drawdown": r.mtm_drawdown,
                    "triggering": r.triggering_id,
                }
                for r in self._activations
            ],
            "resets": [{"at": r.reset_at, "operator": r.operator_id} for r in self._resets],
        }


def _now_ms() -> int:
    return int(time.time() * 1000)
