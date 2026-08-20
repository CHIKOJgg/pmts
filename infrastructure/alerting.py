"""infrastructure/alerting.py — Alert routing and notification channels."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
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
        self._history: deque[Alert] = deque(maxlen=500)

    def get_recent(self, limit: int = 50) -> List[Alert]:
        return list(self._history)[-limit:]

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
        else:
            active_channels = []
            if self._config.slack_webhook_url:
                active_channels.append(f"slack:{self._config.slack_channel}")
            if self._config.email_username and self._config.email_recipients:
                active_channels.append(f"email:{','.join(self._config.email_recipients)}")
            for url in self._config.webhook_urls:
                active_channels.append(f"webhook:{url}")
            for r in results:
                if isinstance(r, Exception):
                    logger.error(
                        "Alert delivery failed (severity=%s, title=%s, source=%s, channels=%s): %s",
                        alert.severity.value,
                        alert.title,
                        alert.source,
                        ",".join(active_channels) or "none",
                        r,
                    )
        self._history.append(alert)
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
        webhook_url = self._config.slack_webhook_url
        if webhook_url is None:
            return

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

        async with session.post(webhook_url, json=payload) as resp:
            resp.raise_for_status()

    async def _send_email(self, alert: Alert) -> None:
        from email.mime.text import MIMEText

        import aiosmtplib

        email_username = self._config.email_username
        email_password = self._config.email_password
        if email_username is None or email_password is None:
            return

        msg = MIMEText(f"{alert.message}\n\nSource: {alert.source}\nTime: {alert.timestamp}")
        msg["Subject"] = f"[PMTS {alert.severity.value.upper()}] {alert.title}"
        msg["From"] = email_username
        msg["To"] = ", ".join(self._config.email_recipients)

        await aiosmtplib.send(
            msg,
            hostname=self._config.email_smtp_host,
            port=self._config.email_smtp_port,
            username=email_username,
            password=email_password,
            use_tls=False,
            start_tls=True,
        )

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
