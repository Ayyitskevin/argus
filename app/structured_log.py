"""JSON structured event logging for multi-tenant ops."""
from __future__ import annotations

import json
import logging
from typing import Any

from . import config

log = logging.getLogger("argus.event")


def event(name: str, **fields: Any) -> None:
    """Emit a single-line structured log entry."""
    payload = {"event": name}
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    if config.STRUCTURED_LOGS:
        log.info(json.dumps(payload, default=str))
    else:
        pairs = " ".join(f"{key}={value}" for key, value in payload.items() if key != "event")
        log.info("%s %s", name, pairs)