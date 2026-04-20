"""config/logging_setup.py — Structured logging configuration."""
from __future__ import annotations
import json, logging, logging.handlers, sys, time
from typing import Optional


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts":      int(time.time() * 1000),
            "level":   record.levelname,
            "logger":  record.name,
            "message": record.getMessage(),
            "module":  record.module,
            "func":    record.funcName,
            "line":    record.lineno,
        }
        if record.exc_info:
            doc["exception"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self):
        super().__init__(
            fmt="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


def configure_logging(
    level:     str           = "INFO",
    fmt:       str           = "text",
    file_path: Optional[str] = None,
) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    formatter = JsonFormatter() if fmt.lower() == "json" else TextFormatter()

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    if file_path:
        fh = logging.handlers.RotatingFileHandler(
            file_path,
            maxBytes=100 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)

    for noisy in ("aiohttp", "websockets", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)