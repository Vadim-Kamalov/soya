import json
import logging
import time
from datetime import UTC, datetime
from uuid import uuid4

from app.errors import ApiError, response

logger = logging.getLogger("marketplace.api")
SENSITIVE = {"password", "password_hash", "access_token", "refresh_token", "authorization", "secret", "token"}


def redact(value):
    if isinstance(value, dict):
        return {k: "***" if k.lower() in SENSITIVE else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class RequestLoggingMiddleware:
    """Pure ASGI middleware: preserves streaming and logs even unhandled failures."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        start, status, started = time.perf_counter(), 500, False
        timestamp = datetime.now(UTC).isoformat()
        body = bytearray()
        truncated = False

        async def capture_receive():
            nonlocal truncated
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                if len(body) + len(chunk) <= 65536:
                    body.extend(chunk)
                else:
                    truncated = True
            return message

        async def capture_send(message):
            nonlocal status, started
            if message["type"] == "http.response.start":
                started, status = True, message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, capture_receive, capture_send)
        except Exception:
            # Never log exception locals or raw bodies; they can contain secrets.
            logger.error(json.dumps({"request_id": request_id, "event": "unhandled_error"}))
            if not started:
                await response(ApiError("INTERNAL_ERROR", 500, "Internal server error"))(
                    scope, receive, capture_send
                )
            else:
                raise
        finally:
            event = {
                "request_id": request_id,
                "method": scope["method"],
                "endpoint": scope["path"],
                "status_code": status,
                "duration_ms": round((time.perf_counter() - start) * 1000, 3),
                "user_id": scope["state"].get("user_id"),
                "timestamp": timestamp,
            }
            if scope["method"] in {"POST", "PUT", "DELETE", "PATCH"}:
                try:
                    event["request_body"] = redact(json.loads(body)) if body and not truncated else None
                except (ValueError, UnicodeDecodeError):
                    event["request_body"] = "[unparseable body omitted]"
                if truncated:
                    event["request_body"] = "[large body omitted]"
            logger.info(json.dumps(event, ensure_ascii=False))
