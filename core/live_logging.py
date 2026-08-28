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
_SECRET_KEY = re.compile(
    r"(password|token|secret|credential|authorization|reference|"
    r"(?:account|order|deal|broker)[ _-]?(?:id|reference|ref|number))", re.I,
)
_AUTHORIZATION_LABEL = re.compile(r"(?i)\bauthorization\b")
_AUTHORIZATION_JSON_FIELD = re.compile(
    r'''(?ix)
    (?:"authorization"\s*:\s*"(?:\\.|[^"\\])*"
      |'authorization'\s*:\s*'(?:\\.|[^'\\])*')
    ''',
)
_QUALIFIED_SECRET = re.compile(
    r"(?i)\b(password|token|secret|credential|broker\s+order|"
    r"(?:account|order|deal|broker)[ _-]?(?:id|reference|ref|number))"
    r"\s*(?:[:=]\s*|\s+)(?:bearer\s+)?[^\s,;]+"
)
_PLAIN_IDENTIFIER = re.compile(
    r"(?i)\b(order|deal|broker|account)\b\s*(?:[:=]\s*|\s+)"
    r"([A-Za-z0-9][A-Za-z0-9._=-]{1,})"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_KNOWN_TOKEN = re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9]{12,}|sk-[A-Za-z0-9_-]{12,})\b")
_LONG_NUMBER = re.compile(r"\b\d{6,}\b")
_LONG_OPAQUE = re.compile(r"\b(?=[A-Za-z0-9_-]{24,}\b)(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+\b")
_MAX_TEXT_LENGTH = 4096
_SAFE_PLAIN_WORDS = frozenset({
    "accepted", "available", "cancelled", "canceled", "closed", "completed",
    "connected", "created", "disabled", "failed", "filled", "invalid", "missing",
    "offline", "online", "open", "pending", "placed", "ready", "rejected",
    "required", "stale", "submitted", "succeeded", "unavailable", "unknown",
})
_SENSITIVE_STRUCTURED_KEYS = frozenset({
    "authorization", "account", "account_id", "account_ref", "account_reference",
    "broker", "broker_id", "broker_ref", "broker_reference", "credential", "credentials",
    "deal", "deal_id", "deal_ref", "deal_reference", "order", "order_id", "order_ref",
    "order_reference", "password", "ref", "reference", "secret", "token",
})


def _redact_plain_identifier(match: re.Match[str]) -> str:
    token = match.group(2)
    return (match.group(0) if token.casefold() in _SAFE_PLAIN_WORDS
            else f"{match.group(1)}=[REDACTED_ID]")


def _inside_unescaped_quote(value: str, position: int, quote: str) -> bool:
    escaped = False
    inside = False
    for char in value[:position]:
        if char == quote and not escaped:
            inside = not inside
        escaped = char == "\\" and not escaped
        if char != "\\":
            escaped = False
    return inside


def _closing_quote(value: str, start: int, quote: str) -> int:
    escaped = False
    for index in range(start, len(value)):
        char = value[index]
        if char == quote and not escaped:
            return index
        escaped = char == "\\" and not escaped
        if char != "\\":
            escaped = False
    return len(value)


def _redact_authorization(value: str) -> str:
    # Preserve the boundary of a quoted JSON field.  All other header-shaped
    # occurrences consume the rest of their physical line: commas and
    # semicolons are legal inside Digest/AWS4/custom authorization values.
    sentinel = '"__redacted_auth_field__":"[REDACTED]"'
    value = _AUTHORIZATION_JSON_FIELD.sub(sentinel, value)
    output: list[str] = []
    position = 0
    while match := _AUTHORIZATION_LABEL.search(value, position):
        output.append(value[position:match.start()])
        end = len(value)
        # JSON strings use double quotes. Treating arbitrary apostrophes as
        # boundaries could stop redaction early in ordinary prose.
        for quote in ('"',):
            if _inside_unescaped_quote(value, match.start(), quote):
                end = min(end, _closing_quote(value, match.end(), quote))
        line_ends = [index for index in (
            value.find("\r", match.end()), value.find("\n", match.end()),
            value.find("\\n", match.end()),
        )
                     if index >= 0]
        if line_ends:
            end = min(end, min(line_ends))
        output.append("Authorization=[REDACTED]")
        position = end
    output.append(value[position:])
    return "".join(output).replace('"__redacted_auth_field__"', '"Authorization"')


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[\s-]+", "_", key.strip().casefold())
    return (normalized in _SENSITIVE_STRUCTURED_KEYS or bool(_SECRET_KEY.search(key))
            or _redact_text(key) != key)


def _redact_text(value: str) -> str:
    # Authorization is handled first so a scheme (Basic, Bearer, Digest, or
    # custom) cannot survive after a narrower credential substitution.
    value = _redact_authorization(value)
    value = _QUALIFIED_SECRET.sub(lambda match: match.group(1) + "=[REDACTED]", value)
    value = _PLAIN_IDENTIFIER.sub(_redact_plain_identifier, value)
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _KNOWN_TOKEN.sub("[REDACTED]", value)
    value = _LONG_NUMBER.sub("[REDACTED_ID]", value)
    value = _LONG_OPAQUE.sub("[REDACTED_ID]", value)
    value = value.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")
    if len(value) > _MAX_TEXT_LENGTH:
        value = value[:_MAX_TEXT_LENGTH] + "…[TRUNCATED]"
    return value


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {_redact_text(str(k)): ("[REDACTED]" if _is_sensitive_key(str(k)) else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v) for v in value)
    if isinstance(value, str):
        return _redact_text(value)
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
