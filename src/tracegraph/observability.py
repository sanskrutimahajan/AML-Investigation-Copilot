"""Structured logs for the API: one JSON object per line, a request ID on every line, and an access
log that never contains names or IDs.

- **Request IDs:** taken from a well-formed `X-Request-ID` header or generated, returned in the
  response header, and attached to every log line written while the request runs.
- **PII-safe access log:** each request is logged by its *route template* (`/subjects/{subject_id}`,
  `/screen`), never the raw path or query string, so screened names, customer and account IDs and
  questions stay out of the logs. Uvicorn's own access log is turned off (`make api`).
- **Redaction:** a filter masks query strings and values that look like customer, account, alert or
  case IDs in any message, as a second line of defence for log calls elsewhere.

`LOG_FORMAT=text` gives plain lines for local reading; the default is `json`.
"""

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
# Customer, account, alert and case IDs, and anything after `?` in a URL.
_SENSITIVE = re.compile(
    r"\bcust-[0-9A-F]+\b|\b\d+:[0-9A-F]{6,}\b|\b(?:alert|case)-[0-9a-f]{16}\b|\?\S+"
)
# Fields a log call may add through `extra=`; everything else is ignored.
_EXTRA_FIELDS = ("route", "method", "status", "duration_ms", "llm_calls", "input_tokens",
                 "output_tokens", "llm_seconds", "outcome")  # fmt: skip


def redact(text: str) -> str:
    return _SENSITIVE.sub("[redacted]", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        record.request_id = request_id.get()
        return True


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        entry.update({k: getattr(record, k) for k in _EXTRA_FIELDS if hasattr(record, k)})
        if record.exc_info:
            entry["error"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, default=str)


def configure_logging(log_format: str = "json", level: str = "INFO") -> None:
    """Route the `tracegraph` loggers through one redacting handler (idempotent)."""
    root = logging.getLogger("tracegraph")
    root.handlers.clear()
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    handler.setFormatter(
        JSONFormatter()
        if log_format == "json"
        else logging.Formatter("%(levelname)s %(name)s [%(request_id)s] %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False


access_log = logging.getLogger("tracegraph.access")


class RequestContextMiddleware:
    """Assigns the request ID and writes one access line per request (route template only)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        rid = incoming if _REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id.set(rid)
        started = time.monotonic()
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", rid.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            access_log.info(
                "%s %s %s", scope["method"], template, status,
                extra={"route": template, "method": scope["method"], "status": status,
                       "duration_ms": round((time.monotonic() - started) * 1000, 1)},
            )  # fmt: skip
            request_id.reset(token)
