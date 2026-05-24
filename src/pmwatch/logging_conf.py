"""Centralised logging configuration.

We deliberately avoid ``print`` anywhere in the codebase. Every operational
message goes through the standard :mod:`logging` module so that:

* log level is controlled from ``config.yaml`` (``logging.level``),
* output can be switched to single-line JSON for log shippers
  (``logging.json: true``), which is convenient on a long-running server, and
* the long-lived watcher loops emit a consistent, timestamped, structured trail.

Call :func:`configure_logging` exactly once, as early as possible in each
process entry point (see :mod:`pmwatch.cli`).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime


class _JsonFormatter(logging.Formatter):
    """Render each log record as one compact JSON object per line.

    Keeping every record on a single line is what makes the output trivially
    ingestible by line-oriented log collectors (journald, Loki, CloudWatch...).
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            # ISO-8601 UTC timestamp; explicit timezone avoids ambiguity.
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Preserve exception tracebacks if present.
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Promote any caller-supplied structured "extra" fields.
        for key, value in record.__dict__.items():
            if key == "extra_fields" and isinstance(value, dict):
                payload.update(value)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", *, as_json: bool = False) -> None:
    """Configure the root logger for this process.

    Parameters
    ----------
    level:
        A standard logging level name (``"DEBUG"``, ``"INFO"`` ...). Invalid
        names fall back to ``INFO`` rather than crashing the process.
    as_json:
        When ``True`` emit single-line JSON records; otherwise a human-readable
        text format suited to interactive use and ``journalctl``.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    handler = logging.StreamHandler(stream=sys.stdout)
    if as_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )

    root = logging.getLogger()
    # Reset handlers so repeated calls (e.g. in tests) don't double-log.
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # The requests/urllib3 stack is noisy at INFO; quiet it unless debugging.
    logging.getLogger("urllib3").setLevel(max(numeric_level, logging.WARNING))


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger. Thin wrapper for a consistent call-site."""
    return logging.getLogger(name)
