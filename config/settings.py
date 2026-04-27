"""config/settings.py — All configuration from environment variables. Zero deps."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import List, Optional


def _e(k: str, d: str = "") -> str:
    return os.environ.get(k, d)

def _ei(k: str, d: int) -> int:
    try: return int(os.environ.get(k, d))
    except (ValueError, TypeError): return d

def _ef(k: str, d: float) -> float:
    try: return float(os.environ.get(k, d))
    except (ValueError, TypeError): return d

def _eb(k: str, d: bool) -> bool:
    return os.environ.get(k, str(d)).lower() in ("1", "true", "yes", "on")


@dataclass
class PolymarketConfig:
    clob_url:      str = field(default_factory=lambda: _e("PM_CLOB_URL", "https://clob.polymarket.com"))
    ws_url:        str = field(default_factory=lambda: _e("PM_WS_URL",   "wss://ws-subscriptions-clob.polymarket.com/ws/market"))
    api_key:       str = field(default_factory=lambda: _e("PM_API_KEY"))
    api_secret:    str = field(default_factory=lambda: _e("PM_API_SECRET"))
    passphrase:    str = field(default_factory=lambda: _e("PM_PASSPHRASE"))
    wallet_key:    str = field(default_factory=lambda: _e("PM_WALLET_KEY"))
    taker_fee_bps: int = field(default_factory=lambda: _ei("PM_TAKER_FEE_BPS", 20))


@dataclass
class OpinionConfig:
    rest_url:      str = field(default_factory=lambda: _e("OP_REST_URL", "https://api.opinion.markets/v1"))
    ws_url:        str = field(default_factory=lambda: _e("OP_WS_URL",   "wss://ws.opinion.markets"))
    api_key:       str = field(default_factory=lambda: _e("OP_API_KEY"))
    wallet_key:    str = field(default_factory=lambda: _e("OP_WALLET_KEY"))
    taker_fee_bps: int = field(default_factory=lambda: _ei("OP_TAKER_FEE_BPS", 25))


@dataclass
class TradingConfig:
    initial_cash_usdc:   float      = field(default_factory=lambda: _ef("INITIAL_CASH_USDC",   10_000.0))
    markets:             List[str]  = field(default_factory=list)
    enable_trading:      bool       = field(default_factory=lambda: _eb("ENABLE_TRADING",      True))
    enable_arb:          bool       = field(default_factory=lambda: _eb("ENABLE_ARB",           True))
    enable_mm:           bool       = field(default_factory=lambda: _eb("ENABLE_MM",            True))
    enable_hedge:        bool       = field(default_factory=lambda: _eb("ENABLE_HEDGE",         True))
    arb_budget_usdc:     float      = field(default_factory=lambda: _ef("ARB_BUDGET_USDC",     2_000.0))
    mm_budget_usdc:      float      = field(default_factory=lambda: _ef("MM_BUDGET_USDC",      3_000.0))
    kill_switch_token:   str        = field(default_factory=lambda: _e("KILL_SWITCH_TOKEN",    "CHANGE-ME"))
    drawdown_kill_pct:   float      = field(default_factory=lambda: _ef("DRAWDOWN_KILL_PCT",    0.20))
    drawdown_warn_pct:   float      = field(default_factory=lambda: _ef("DRAWDOWN_WARN_PCT",    0.15))
    max_order_usdc:      float      = field(default_factory=lambda: _ef("MAX_ORDER_USDC",       200.0))
    min_order_usdc:      float      = field(default_factory=lambda: _ef("MIN_ORDER_USDC",       1.0))
    max_market_exp_pct:  float      = field(default_factory=lambda: _ef("MAX_MARKET_EXP_PCT",   0.05))
    max_market_exp_usdc: float      = field(default_factory=lambda: _ef("MAX_MARKET_EXP_USDC",  500.0))
    max_net_delta:       float      = field(default_factory=lambda: _ef("MAX_NET_DELTA",        50.0))


@dataclass
class AIConfig:
    enabled:        bool  = field(default_factory=lambda: _eb("AI_ENABLED",               True))
    heuristic_only: bool  = field(default_factory=lambda: _eb("AI_USE_HEURISTIC_ONLY",   False))
    api_timeout_ms: int   = field(default_factory=lambda: _ei("AI_API_TIMEOUT_MS",        200))
    cache_ttl_ms:   int   = field(default_factory=lambda: _ei("AI_CACHE_TTL_MS",         3_000))


@dataclass
class LoggingConfig:
    level:     str           = field(default_factory=lambda: _e("LOG_LEVEL", "INFO"))
    fmt:       str           = field(default_factory=lambda: _e("LOG_FORMAT", "text"))
    file_path: Optional[str] = field(default_factory=lambda: _e("LOG_FILE") or None)


@dataclass
class Settings:
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    opinion:    OpinionConfig    = field(default_factory=OpinionConfig)
    trading:    TradingConfig    = field(default_factory=TradingConfig)
    ai:         AIConfig         = field(default_factory=AIConfig)
    logging:    LoggingConfig    = field(default_factory=LoggingConfig)


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        s = Settings()
        markets_env = os.environ.get("MARKETS", "")
        if markets_env:
            s.trading.markets = [m.strip() for m in markets_env.split(",") if m.strip()]
        _settings = s
    return _settings


def reload_settings() -> Settings:
    global _settings
    _settings = None
    return get_settings()