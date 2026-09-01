import json
import logging
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    """Small JSON formatter with a stable operational field set."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "service": self.service,
            "event": getattr(record, "event", record.getMessage()),
            "message": record.getMessage(),
        }
        for field in (
            "path",
            "method",
            "status_code",
            "duration_ms",
            "dependency",
            "error_type",
            "event_id",
            "outbox_id",
            "outbox_ids",
            "consumer",
            "attempt",
            "attempts",
            "worker_id",
            "redis_message_id",
        ):
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str, service: str = "forge-api") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service))

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level.upper())
