"""Logging setup: one JSON object per line on Cloud Run, plain text on a terminal."""

import json
import logging
import os
import sys

# Attributes every LogRecord has; anything else was passed through `extra`.
_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Cloud Logging reads `severity` and `message`; the other keys become searchable fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {"severity": record.levelname, "message": record.getMessage()}
        entry |= {key: value for key, value in vars(record).items() if key not in _STANDARD}
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging() -> None:
    on_cloud_run = "CLOUD_RUN_JOB" in os.environ or "K_SERVICE" in os.environ
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if on_cloud_run else logging.Formatter("%(levelname)s %(message)s")
    )
    # Libraries stay at WARNING: at INFO the HTTP client logs every request.
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    logging.getLogger("wobot").setLevel(logging.INFO)
