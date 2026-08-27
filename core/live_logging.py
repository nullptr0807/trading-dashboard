"""Local structured logs with aggressive secret redaction."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOG_DIR = Path(__file__).resolve().parents[1] / "logs" / "live_account"
_SECRET_KEY = re.compile(r"(password|token|secret|credential|authorization|account.?id)", re.I)
_SECRET_TEXT = re.compile(r"(?i)(password|token|secret|authorization)\s*[:=]\s*[^\s,;]+")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if _SECRET_KEY.search(str(k)) else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v) for v in value)
    if isinstance(value, str):
        return _SECRET_TEXT.sub(lambda m: m.group(1) + "=[REDACTED]", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        data = getattr(record, "structured", None)
        if data:
            payload["data"] = redact(data)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def get_live_logger(name: str, filename: str = "live-system.jsonl") -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(LOG_DIR, 0o700)
    except OSError:
        pass
    logger = logging.getLogger(name)
    if not logger.handlers:
        path = LOG_DIR / filename
        handler = RotatingFileHandler(path, maxBytes=20 * 1024 * 1024, backupCount=20)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return logger


def log_event(logger: logging.Logger, level: str, message: str, **data: Any) -> None:
    logger.log(getattr(logging, level.upper(), logging.INFO), message,
               extra={"structured": data})
