"""config/settings.py — All configuration from environment variables. Zero deps."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

REQUIRED_REGISTRY_KEYS: set[str] = {"polymarket", "opinion", "question"}
OPTIONAL_REGISTRY_KEYS: set[str] = {"pair_score", "polymarket_question", "opinion_question"}
ALL_REGISTRY_KEYS: set[str] = REQUIRED_REGISTRY_KEYS | OPTIONAL_REGISTRY_KEYS


def validate_market_registry(registry: Dict[str, Dict[str, Any]]) -> list[str]:
    """Validate market registry structure and return list of errors."""
    errors: list[str] = []
    seen_polymarket: set[str] = set()
    seen_opinion: set[str] = set()

    for market_id, mapping in registry.items():
        if not isinstance(market_id, str) or not market_id.strip():
            errors.append(f"Market ID must be a non-empty string, got {type(market_id).__name__}")
            continue
        if not isinstance(mapping, dict):
            errors.append(f"Mapping for {market_id} must be a dict, got {type(mapping).__name__}")
            continue

        unknown = set(mapping.keys()) - ALL_REGISTRY_KEYS
        if unknown:
            errors.append(f"{market_id}: unknown keys: {', '.join(sorted(unknown))}")

        missing = REQUIRED_REGISTRY_KEYS - set(mapping.keys())
        if missing:
            errors.append(f"{market_id}: missing required keys: {', '.join(sorted(missing))}")
            continue

        pm_id = mapping.get("polymarket", "")
        if not isinstance(pm_id, str) or not pm_id.strip():
            errors.append(f"{market_id}.polymarket must be a non-empty string")
        elif pm_id in seen_polymarket:
            errors.append(f"Duplicate polymarket venue ID '{pm_id}' in {market_id}")
        else:
            seen_polymarket.add(pm_id)

        op_id = mapping.get("opinion", "")
        if not isinstance(op_id, str) or not op_id.strip():
            errors.append(f"{market_id}.opinion must be a non-empty string")
        elif op_id in seen_opinion:
            errors.append(f"Duplicate opinion venue ID '{op_id}' in {market_id}")
        else:
            seen_opinion.add(op_id)

        score = mapping.get("pair_score")
        if score is not None:
            try:
                float(score)
            except (ValueError, TypeError):
                errors.append(f"{market_id}.pair_score must be numeric, got '{score}'")

    return errors


def _e(k: str, d: str = "") -> str:
    return os.environ.get(k, d)


def _ei(k: str, d: int) -> int:
    val = os.environ.get(k)
    if val is None:
        return d
    try:
        # Try float first to handle "10.0" style inputs
        result = int(float(val))
        return result
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid value for {k}: '{val}'. Expected an integer. Error: {e}")


def _ef(k: str, d: float) -> float:
    val = os.environ.get(k)
    if val is None:
        return d
    try:
        result = float(val)
        # Validate reasonable ranges
        import math

        if math.isnan(result) or math.isinf(result):
            raise ValueError(f"{k} cannot be NaN or Infinity")
        return result
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid value for {k}: '{val}'. Expected a number. Error: {e}")


def _es(k: str) -> Optional[str]:
    val = os.environ.get(k)
    if val is None or val.strip() == "":
        return None
    return val.strip()


def _secret(k: str, file_k: str, d: str = "") -> str:
    file_path = os.environ.get(file_k, "").strip()
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except OSError:
            return d
    return os.environ.get(k, d)


def _eb(k: str, d: bool) -> bool:
    val = os.environ.get(k)
    if val is None:
        return d

    normalized = val.lower().strip()
    if normalized in ("1", "true", "yes", "on"):
        return True
    elif normalized in ("0", "false", "no", "off"):
        return False
    else:
        raise ValueError(f"Invalid boolean value for {k}: '{val}'. Expected one of: 1/0, true/false, yes/no, on/off")


@dataclass
class PolymarketConfig:
    clob_url: str = field(default_factory=lambda: _e("PM_CLOB_URL", "https://clob.polymarket.com"))
    ws_url: str = field(default_factory=lambda: _e("PM_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"))
    api_key: str = field(default_factory=lambda: _secret("PM_API_KEY", "PM_API_KEY_FILE"))
    api_secret: str = field(default_factory=lambda: _secret("PM_API_SECRET", "PM_API_SECRET_FILE"))
    passphrase: str = field(default_factory=lambda: _secret("PM_PASSPHRASE", "PM_PASSPHRASE_FILE"))
    wallet_key: str = field(default_factory=lambda: _secret("PM_WALLET_KEY", "PM_WALLET_KEY_FILE"))
    taker_fee_bps: int = field(default_factory=lambda: _ei("PM_TAKER_FEE_BPS", 20))
    sandbox: bool = field(default_factory=lambda: _eb("PM_SANDBOX", False))


@dataclass
class OpinionConfig:
    rest_url: str = field(default_factory=lambda: _e("OP_REST_URL", "https://api.opinion.markets/v1"))
    ws_url: str = field(default_factory=lambda: _e("OP_WS_URL", "wss://ws.opinion.markets"))
    api_key: str = field(default_factory=lambda: _secret("OP_API_KEY", "OP_API_KEY_FILE"))
    wallet_key: str = field(default_factory=lambda: _secret("OP_WALLET_KEY", "OP_WALLET_KEY_FILE"))
    ctf_exchange_addr: str = field(default_factory=lambda: _e("OP_CTF_EXCHANGE_ADDR", ""))
    taker_fee_bps: int = field(default_factory=lambda: _ei("OP_TAKER_FEE_BPS", 25))
    sandbox: bool = field(default_factory=lambda: _eb("OP_SANDBOX", False))


@dataclass
class TradingConfig:
    initial_cash_usdc: float = field(default_factory=lambda: _ef("INITIAL_CASH_USDC", 10_000.0))
    markets: List[str] = field(default_factory=list)
    market_registry: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    enable_trading: bool = field(default_factory=lambda: _eb("ENABLE_TRADING", False))
    enable_arb: bool = field(default_factory=lambda: _eb("ENABLE_ARB", True))
    enable_mm: bool = field(default_factory=lambda: _eb("ENABLE_MM", True))
    enable_hedge: bool = field(default_factory=lambda: _eb("ENABLE_HEDGE", True))
    arb_budget_usdc: float = field(default_factory=lambda: _ef("ARB_BUDGET_USDC", 2_000.0))
    mm_budget_usdc: float = field(default_factory=lambda: _ef("MM_BUDGET_USDC", 3_000.0))
    kill_switch_token: str = field(
        default_factory=lambda: _secret("KILL_SWITCH_TOKEN", "KILL_SWITCH_TOKEN_FILE", "CHANGE-ME")
    )
    drawdown_kill_pct: float = field(default_factory=lambda: _ef("DRAWDOWN_KILL_PCT", 0.20))
    drawdown_warn_pct: float = field(default_factory=lambda: _ef("DRAWDOWN_WARN_PCT", 0.15))
    max_order_usdc: float = field(default_factory=lambda: _ef("MAX_ORDER_USDC", 200.0))
    min_order_usdc: float = field(default_factory=lambda: _ef("MIN_ORDER_USDC", 1.0))
    max_market_exposure_pct: float = field(default_factory=lambda: _ef("MAX_MARKET_EXP_PCT", 0.05))
    max_market_exposure_usdc: float = field(default_factory=lambda: _ef("MAX_MARKET_EXP_USDC", 500.0))
    max_net_delta: float = field(default_factory=lambda: _ef("MAX_NET_DELTA", 50.0))
    db_path: Optional[str] = field(default_factory=lambda: _es("DB_PATH"))
    market_registry_path: str = field(default_factory=lambda: _e("MARKET_REGISTRY_PATH", "market_registry.json"))
    database_url: Optional[str] = field(default_factory=lambda: _es("DATABASE_URL"))
    redis_url: Optional[str] = field(default_factory=lambda: _es("REDIS_URL"))
    redis_enabled: bool = field(default_factory=lambda: _eb("REDIS_ENABLED", False))

    # Strategy tuning parameters (previously hardcoded in main.py)
    min_net_edge: float = field(default_factory=lambda: _ef("MIN_NET_EDGE", 0.006))
    hedge_threshold: float = field(default_factory=lambda: _ef("HEDGE_THRESHOLD", 10.0))
    mm_quote_size_usdc: float = field(default_factory=lambda: _ef("MM_QUOTE_SIZE_USDC", 25.0))

    # Session loss limit
    session_loss_limit_usdc: float = field(default_factory=lambda: _ef("SESSION_LOSS_LIMIT_USDC", 500.0))

    # Kill switch grace period (seconds before auto-activation on drawdown breach)
    kill_switch_grace_s: float = field(default_factory=lambda: _ef("KILL_SWITCH_GRACE_S", 5.0))

    # Execution tuning
    max_concurrent_orders: int = field(default_factory=lambda: _ei("MAX_CONCURRENT_ORDERS", 5))
    submit_retry_count: int = field(default_factory=lambda: _ei("SUBMIT_RETRY_COUNT", 3))
    submit_base_delay_s: float = field(default_factory=lambda: _ef("SUBMIT_BASE_DELAY_S", 0.2))
    poll_normal_s: float = field(default_factory=lambda: _ef("POLL_NORMAL_S", 2.0))
    poll_fast_s: float = field(default_factory=lambda: _ef("POLL_FAST_S", 0.5))
    stale_threshold_ms: int = field(default_factory=lambda: _ei("STALE_THRESHOLD_MS", 2000))

    # Order-state reconciliation: compare locally-tracked open orders against the
    # exchange's open-order book at this interval (seconds). 0 disables it.
    reconcile_interval_s: float = field(default_factory=lambda: _ef("RECONCILE_INTERVAL_S", 60.0))


@dataclass
class AIConfig:
    # AI is disabled by default to prevent unintended API calls/costs.
    # Set AI_ENABLED=True to activate AI-based signal enhancement.
    # Provider: "anthropic" (Claude) or "openrouter" (OpenRouter, supports many models).
    enabled: bool = field(default_factory=lambda: _eb("AI_ENABLED", False))
    heuristic_only: bool = field(default_factory=lambda: _eb("AI_USE_HEURISTIC_ONLY", False))
    provider: str = field(default_factory=lambda: _e("AI_PROVIDER", "anthropic"))
    anthropic_api_key: str = field(default_factory=lambda: _secret("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_FILE"))
    openrouter_api_key: str = field(default_factory=lambda: _e("OPENROUTER_API_KEY", ""))
    openrouter_model: str = field(default_factory=lambda: _e("OPENROUTER_MODEL", "anthropic/claude-sonnet-4"))
    api_timeout_ms: int = field(default_factory=lambda: _ei("AI_API_TIMEOUT_MS", 200))
    cache_ttl_ms: int = field(default_factory=lambda: _ei("AI_CACHE_TTL_MS", 3_000))


@dataclass
class LoggingConfig:
    level: str = field(default_factory=lambda: _e("LOG_LEVEL", "INFO"))
    fmt: str = field(default_factory=lambda: _e("LOG_FORMAT", "text"))
    file_path: Optional[str] = field(default_factory=lambda: _e("LOG_FILE") or None)


@dataclass
class AlertConfig:
    slack_webhook_url: str = field(default_factory=lambda: _e("ALERT_SLACK_WEBHOOK", ""))
    email_smtp_host: str = field(default_factory=lambda: _e("ALERT_EMAIL_SMTP_HOST", "smtp.gmail.com"))
    email_smtp_port: int = field(default_factory=lambda: _ei("ALERT_EMAIL_SMTP_PORT", 587))
    email_username: str = field(default_factory=lambda: _e("ALERT_EMAIL_USERNAME", ""))
    email_password: str = field(default_factory=lambda: _e("ALERT_EMAIL_PASSWORD", ""))
    email_recipients: str = field(default_factory=lambda: _e("ALERT_EMAIL_RECIPIENTS", ""))
    webhook_urls: str = field(default_factory=lambda: _e("ALERT_WEBHOOK_URLS", ""))


@dataclass
class ObservabilityConfig:
    bind_host: str = field(default_factory=lambda: _e("OBSERVABILITY_BIND_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _ei("OBSERVABILITY_PORT", 8080))


@dataclass
class Settings:
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    opinion: OpinionConfig = field(default_factory=OpinionConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)

    def validate(self, mode: str = "live") -> None:
        """Strict validation of all configuration before startup."""
        errors = []

        # 1. Trading Config
        if mode in ("paper", "live") and not self.trading.markets:
            errors.append("MARKETS list cannot be empty. Set via MARKETS env var (comma-separated).")
        if self.trading.market_registry:
            reg_errors = validate_market_registry(self.trading.market_registry)
            errors.extend(reg_errors)
            for market_id in self.trading.markets:
                mapping = self.trading.market_registry.get(market_id)
                if not mapping:
                    errors.append(f"MARKET_REGISTRY_JSON missing mapping for logical market {market_id}")

        if self.trading.initial_cash_usdc <= 0:
            errors.append(f"INITIAL_CASH_USDC must be > 0 (current: {self.trading.initial_cash_usdc})")

        if self.trading.arb_budget_usdc <= 0:
            errors.append(f"ARB_BUDGET_USDC must be > 0 (current: {self.trading.arb_budget_usdc})")

        if self.trading.mm_budget_usdc <= 0:
            errors.append(f"MM_BUDGET_USDC must be > 0 (current: {self.trading.mm_budget_usdc})")

        if self.trading.min_order_usdc <= 0:
            errors.append(f"MIN_ORDER_USDC must be > 0 (current: {self.trading.min_order_usdc})")

        if self.trading.max_order_usdc < self.trading.min_order_usdc:
            errors.append(
                f"MAX_ORDER_USDC ({self.trading.max_order_usdc}) must be >= MIN_ORDER_USDC ({self.trading.min_order_usdc})"
            )

        if self.trading.max_market_exposure_usdc <= 0:
            errors.append(f"MAX_MARKET_EXP_USDC must be > 0 (current: {self.trading.max_market_exposure_usdc})")

        if not (0 < self.trading.drawdown_kill_pct < 1):
            errors.append(f"DRAWDOWN_KILL_PCT must be between 0 and 1 (current: {self.trading.drawdown_kill_pct})")

        if self.trading.drawdown_warn_pct >= self.trading.drawdown_kill_pct:
            errors.append(
                f"DRAWDOWN_WARN_PCT ({self.trading.drawdown_warn_pct}) must be < DRAWDOWN_KILL_PCT ({self.trading.drawdown_kill_pct})"
            )

        if not (0 < self.trading.max_market_exposure_pct <= 1.0):
            errors.append(f"MAX_MARKET_EXP_PCT must be between 0 and 1 (current: {self.trading.max_market_exposure_pct})")

        if self.trading.max_net_delta < 0:
            errors.append(f"MAX_NET_DELTA must be >= 0 (current: {self.trading.max_net_delta})")

        if mode in ("paper", "live") and self.trading.kill_switch_token in (
            "CHANGE-ME",
            "CHANGE-ME-USE-A-SECURE-RANDOM-STRING",
            "",
        ):
            errors.append("KILL_SWITCH_TOKEN not set correctly. Please set a secure random string.")

        # Validate kill switch token security
        if mode in ("paper", "live") and self.trading.kill_switch_token and len(self.trading.kill_switch_token) < 16:
            errors.append(
                f"KILL_SWITCH_TOKEN must be at least 16 characters (current: {len(self.trading.kill_switch_token)})"
            )
        elif mode in ("paper", "live") and self.trading.kill_switch_token:
            has_upper = bool(re.search(r"[A-Z]", self.trading.kill_switch_token))
            has_lower = bool(re.search(r"[a-z]", self.trading.kill_switch_token))
            has_digit = bool(re.search(r"\d", self.trading.kill_switch_token))
            has_special = bool(re.search(r"[^A-Za-z0-9]", self.trading.kill_switch_token))
            complexity = sum([has_upper, has_lower, has_digit, has_special])
            if complexity < 2:
                errors.append(
                    "KILL_SWITCH_TOKEN must contain at least 2 of: uppercase, lowercase, digit, special character"
                )

        # 2. Polymarket Keys
        if mode == "live" and self.trading.enable_trading:
            if not self.polymarket.api_key:
                errors.append("PM_API_KEY is missing")
            if not self.polymarket.api_secret:
                errors.append("PM_API_SECRET is missing")
            if not self.polymarket.passphrase:
                errors.append("PM_PASSPHRASE is missing")
            if not self.polymarket.wallet_key:
                errors.append("PM_WALLET_KEY is missing")

            if self.polymarket.taker_fee_bps < 0:
                errors.append(f"PM_TAKER_FEE_BPS must be >= 0 (current: {self.polymarket.taker_fee_bps})")

            # 3. Opinion Keys
            if not self.opinion.api_key:
                errors.append("OP_API_KEY is missing")
            if not self.opinion.wallet_key:
                errors.append("OP_WALLET_KEY is missing")
            if not self.opinion.ctf_exchange_addr:
                errors.append("OP_CTF_EXCHANGE_ADDR is missing")

            if self.opinion.taker_fee_bps < 0:
                errors.append(f"OP_TAKER_FEE_BPS must be >= 0 (current: {self.opinion.taker_fee_bps})")

        # 4. AI Config
        if self.ai.enabled:
            if self.ai.provider not in ("anthropic", "openrouter"):
                errors.append(f"AI_PROVIDER must be 'anthropic' or 'openrouter' (current: {self.ai.provider})")
            if self.ai.provider == "openrouter" and not self.ai.openrouter_api_key:
                errors.append("OPENROUTER_API_KEY is required when AI_PROVIDER=openrouter")
            if self.ai.api_timeout_ms <= 0:
                errors.append(f"AI_API_TIMEOUT_MS must be > 0 (current: {self.ai.api_timeout_ms})")
            if self.ai.cache_ttl_ms <= 0:
                errors.append(f"AI_CACHE_TTL_MS must be > 0 (current: {self.ai.cache_ttl_ms})")

        if errors:
            msg = "\n".join(f"  - {err}" for err in errors)
            raise ValueError(f"Configuration validation failed:\n{msg}")


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        s = Settings()
        markets_env = os.environ.get("MARKETS", "")
        if markets_env:
            s.trading.markets = [m.strip() for m in markets_env.split(",") if m.strip()]
        registry_env = os.environ.get("MARKET_REGISTRY_JSON", "").strip()
        if registry_env:
            try:
                s.trading.market_registry = json.loads(registry_env)
                if not isinstance(s.trading.market_registry, dict):
                    raise ValueError("MARKET_REGISTRY_JSON must be a JSON object")
                if not s.trading.markets:
                    s.trading.markets = list(s.trading.market_registry.keys())
            except Exception as exc:
                raise ValueError(f"Invalid MARKET_REGISTRY_JSON: {exc}") from exc
        _settings = s
    return _settings


def reload_settings() -> Settings:
    global _settings
    _settings = None
    return get_settings()
