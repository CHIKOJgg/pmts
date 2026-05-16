"""infrastructure/alerting.py — Alert routing and notification channels."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class AlertSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertChannel(Enum):
    SLACK = "slack"
    EMAIL = "email"
    WEBHOOK = "webhook"


@dataclass
class Alert:
    severity: AlertSeverity
    title: str
    message: str
    source: str
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    metadata: Dict[str, Any] = field(default_factory=dict)
    alert_id: str = field(default_factory=lambda: str(time.time()))


@dataclass
class AlertConfig:
    slack_webhook_url: Optional[str] = None
    slack_channel: str = "#trading-alerts"
    email_smtp_host: str = "smtp.gmail.com"
    email_smtp_port: int = 587
    email_username: Optional[str] = None
    email_password: Optional[str] = None
    email_recipients: List[str] = field(default_factory=list)
    webhook_urls: List[str] = field(default_factory=list)
    rate_limit_per_minute: int = 10
    dedup_window_seconds: int = 300


class AlertRouter:
    """
    Routes alerts to configured channels with rate limiting and deduplication.
    """

    def __init__(self, config: AlertConfig) -> None:
        self._config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._alert_counts: Dict[str, int] = {}
        self._last_alert_times: Dict[str, int] = {}
        self._total_sent: int = 0
        self._total_suppressed: int = 0

    async def send(self, alert: Alert) -> bool:
        if not self._should_send(alert):
            self._total_suppressed += 1
            return False

        tasks = []
        if self._config.slack_webhook_url:
            tasks.append(self._send_slack(alert))
        if self._config.email_username and self._config.email_recipients:
            tasks.append(self._send_email(alert))
        for url in self._config.webhook_urls:
            tasks.append(self._send_webhook(url, alert))

        if not tasks:
            logger.info("Alert (no channels configured): [%s] %s", alert.severity.value, alert.title)
            return False

        results = await asyncio.gather(*tasks, return_exceptions=True)
        success = all(not isinstance(r, Exception) for r in results)
        if success:
            self._total_sent += 1
        return success

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _should_send(self, alert: Alert) -> bool:
        now = int(time.time())
        key = f"{alert.title}:{alert.source}"

        if key in self._last_alert_times:
            if now - self._last_alert_times[key] < self._config.dedup_window_seconds:
                return False

        minute_key = f"minute_{now // 60}"
        if self._alert_counts.get(minute_key, 0) >= self._config.rate_limit_per_minute:
            return False

        self._alert_counts[minute_key] = self._alert_counts.get(minute_key, 0) + 1
        self._last_alert_times[key] = now
        return True

    async def _send_slack(self, alert: Alert) -> None:
        session = await self._get_session()
        color = {"info": "#36a64f", "warning": "#ff9500", "critical": "#ff0000"}[alert.severity.value]

        payload = {
            "channel": self._config.slack_channel,
            "attachments": [{
                "color": color,
                "title": f"[{alert.severity.value.upper()}] {alert.title}",
                "text": alert.message,
                "fields": [
                    {"title": "Source", "value": alert.source, "short": True},
                    {"title": "Time", "value": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(alert.timestamp / 1000)), "short": True},
                ],
            }],
        }

        async with session.post(self._config.slack_webhook_url, json=payload) as resp:
            resp.raise_for_status()

    async def _send_email(self, alert: Alert) -> None:
        import smtplib
        from email.mime.text import MIMEText

        msg = MIMEText(f"{alert.message}\n\nSource: {alert.source}\nTime: {alert.timestamp}")
        msg["Subject"] = f"[PMTS {alert.severity.value.upper()}] {alert.title}"
        msg["From"] = self._config.email_username
        msg["To"] = ", ".join(self._config.email_recipients)

        with smtplib.SMTP(self._config.email_smtp_host, self._config.email_smtp_port) as server:
            server.starttls()
            server.login(self._config.email_username, self._config.email_password)
            server.sendmail(msg["From"], self._config.email_recipients, msg.as_string())

    async def _send_webhook(self, url: str, alert: Alert) -> None:
        session = await self._get_session()
        payload = {
            "severity": alert.severity.value,
            "title": alert.title,
            "message": alert.message,
            "source": alert.source,
            "timestamp": alert.timestamp,
            "metadata": alert.metadata,
        }
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
