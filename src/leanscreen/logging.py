"""Structured logging.

Decision: stdlib ``logging`` with a JSON formatter rather than a third-party
structured-logging dependency. It is fully typed, has zero runtime cost when
disabled, and emits one JSON object per line so logs are grep- and
ingest-friendly in any environment.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

# LogRecord's own attribute names: a caller-supplied extra field with one of
# these names would make logging raise ("Attempt to overwrite ...").
_RESERVED_FIELDS = frozenset(
    logging.makeLogRecord({}).__dict__.keys() | {"message", "asctime", "taskName"}
)


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON objects."""

    _RESERVED = _RESERVED_FIELDS

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Promote any structured `extra=` fields the caller attached.
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info is not None:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Configure the root logger once, idempotently."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    # httpx logs one INFO line per request — a 250-draft batch emits ~800 of
    # them, burying the pipeline's own WARNINGs (the 2026-07 billing failures
    # scrolled past unseen under them). Real HTTP problems still surface: the
    # client raises, and our handlers log the failure with context.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Thin wrapper to centralize the import surface."""
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, event: str, **fields: object) -> None:
    """Emit a structured event with arbitrary typed fields.

    Example: ``log_event(log, logging.INFO, "pair.scored", pair_id=pid)``.

    Field names colliding with LogRecord attributes (``name``, ``msg``, ...)
    are prefixed with ``field_`` instead of crashing the logging call.
    """
    extra: Mapping[str, object] = {
        (f"field_{k}" if k in _RESERVED_FIELDS else k): v for k, v in fields.items()
    }
    logger.log(level, event, extra=dict(extra))
