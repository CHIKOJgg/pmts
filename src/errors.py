"""src/errors.py — Typed exception hierarchy."""
from __future__ import annotations

from typing import Any


class PMTSError(Exception):
    code: str = "pmts_error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.context}


class ConnectivityError(PMTSError):
    code = "connectivity_error"


class ExchangeRejected(ConnectivityError):
    code = "exchange_rejected"

    def __init__(self, message: str, *, platform: str, proposal_id: str,
                 status_code: int, exchange_error: str, **kw: Any) -> None:
        super().__init__(message, platform=platform, proposal_id=proposal_id,
                         status_code=status_code, exchange_error=exchange_error, **kw)
        self.status_code    = status_code
        self.exchange_error = exchange_error


class CrossedBookError(PMTSError):
    code = "crossed_book"


class NegativeHoldings(PMTSError):
    code = "negative_holdings"

    def __init__(self, message: str, *, market_id: str, platform: str,
                 token_side: str, current_holdings: float, fill_size: float, **kw: Any) -> None:
        super().__init__(message, market_id=market_id, platform=platform,
                         token_side=token_side, current_holdings=current_holdings,
                         fill_size=fill_size, **kw)


class ConfigError(PMTSError):
    code = "config_error"
