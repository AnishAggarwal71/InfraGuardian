"""Opt-in, local JSONL tracing for real-model investigation runs.

Set ``INFRAGUARDIAN_TRACE_DIR`` to a local directory to capture an ``events.jsonl`` file.
The file can contain metric values, prompts, and raw provider responses, so it is deliberately
disabled by default and the repository ignores the recommended ``evaluation/real-runs/`` path.
It never records environment variables or API keys.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_TRACE_DIR_ENV = "INFRAGUARDIAN_TRACE_DIR"
_WRITE_LOCK = threading.Lock()


def tracing_enabled() -> bool:
    """Return whether this process was explicitly configured to write local trace artifacts."""
    return bool(os.environ.get(_TRACE_DIR_ENV))


def new_trace_id() -> str:
    """Create a unique identifier that ties all events from one incident run together."""
    return str(uuid.uuid4())


def _json_safe(value: Any) -> Any:
    """Convert provider/Pydantic values to JSON-safe data without serializing process state."""
    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump(mode="json"))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    return str(value)


def record_trace_event(trace_id: str | None, event_type: str, **payload: Any) -> None:
    """
    Append one investigation event to ``events.jsonl`` when tracing is enabled.

    Trace I/O must never change incident routing or turn a healthy run into a failure, so an
    unavailable trace path is intentionally ignored. The returned artifact is diagnostic data,
    not a production dependency.
    """
    trace_dir = os.environ.get(_TRACE_DIR_ENV)
    if not trace_dir:
        return

    event = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "trace_id": trace_id,
        "event_type": event_type,
        **_json_safe(payload),
    }
    try:
        path = Path(trace_dir)
        path.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK, (path / "events.jsonl").open("a", encoding="utf-8") as trace_file:
            trace_file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        # A diagnostic artifact must not compromise the fail-closed incident path.
        return
