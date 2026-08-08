"""Structured logging.

Secrets never reach a log here: settings are only ever emitted through
:meth:`Settings.safe_dict`, and provider request records strip anything whose
key contains "key".
"""

from __future__ import annotations

import logging
import sys

import structlog

from app.config.settings import Settings, get_settings

_SECRET_HINTS = ("api_key", "apikey", "token", "secret", "password", "authorization")


def _redact(_logger, _name, event_dict):
    for key in list(event_dict):
        if any(hint in key.lower() for hint in _SECRET_HINTS):
            event_dict[key] = "***redacted***"
    return event_dict


def configure_logging(settings: Settings | None = None) -> None:
    s = settings or get_settings()
    level = getattr(logging, s.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if s.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str):
    return structlog.get_logger(name)


__all__ = ["configure_logging", "get_logger"]
