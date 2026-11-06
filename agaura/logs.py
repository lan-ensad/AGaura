"""Application logs in production: one JSON object per line on standard output (SPEC §10),
collected and rotated by Docker (``compose.yaml``).

Uvicorn configures its own loggers before it imports the application; ``configure`` replaces
that configuration, so its lines are JSON too. The access log stays off (Caddy logs requests,
with the secret path tokens filtered out). What the application logs never holds ballot
content, passwords, codes, addresses or email bodies.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import logging.config
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


LOGGING: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": JsonFormatter}},
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "stream": "ext://sys.stdout",
        }
    },
    "loggers": {
        # INFO: the backups written, the retention purges (agaura.backups, agaura.tasks).
        "agaura": {"level": "INFO", "handlers": ["stdout"], "propagate": False},
        "uvicorn": {"level": "INFO", "handlers": ["stdout"], "propagate": False},
        "uvicorn.error": {"level": "INFO", "handlers": [], "propagate": True},
        "uvicorn.access": {"level": "WARNING", "handlers": [], "propagate": False},
    },
    "root": {"level": "WARNING", "handlers": ["stdout"]},
}


def configure() -> None:
    logging.config.dictConfig(LOGGING)
