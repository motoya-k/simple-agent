"""Logging for hosts: readable in a terminal, one JSON object per line in a container.

``SIMPLE_AGENT_LOG_FORMAT=json`` makes every record a single line that
CloudWatch Logs Insights (or any log store) can query by field.
"""

from __future__ import annotations

import json
import logging
import os
import time


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure() -> None:
    handler = logging.StreamHandler()
    if os.environ.get("SIMPLE_AGENT_LOG_FORMAT", "").lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("SIMPLE_AGENT_LOG_LEVEL", "INFO").upper())
