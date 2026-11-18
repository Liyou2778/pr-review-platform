"""Logging setup.

Default is human-readable for local work; ``LOG_JSON=true`` switches to one
JSON object per line for log shippers. The important part is not the format but
that every request/run line carries an id you can grep.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("run_id", "request_id", "repo", "status", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(json_logs: bool = False) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else logging.Formatter(_TEXT_FORMAT))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
