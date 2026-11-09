from __future__ import annotations

import tempfile

from starlette.responses import JSONResponse


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodySizeLimit:
    """Bound a request body before FastAPI parses multipart uploads."""

    def __init__(
        self,
        app,
        *,
        path: str | None = None,
        paths: tuple[str, ...] = (),
        method: str = "POST",
        max_bytes: int,
        detail: str,
        spool_max_bytes: int = 1024 * 1024,
    ):
        self.app = app
        self.paths = frozenset(paths + ((path,) if path else ()))
        if not self.paths:
            raise ValueError("at least one request path is required")
        self.method = method.upper()
        self.max_bytes = max(1, max_bytes)
        self.detail = detail
        self.spool_max_bytes = max(1, spool_max_bytes)

    async def reject(self, scope, receive, send, status_code: int, detail: str) -> None:
        response = JSONResponse(status_code=status_code, content={"detail": detail})
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") != "http"
            or scope.get("method", "").upper() != self.method
            or scope.get("path") not in self.paths
        ):
            await self.app(scope, receive, send)
            return

        lengths = [value for key, value in scope.get("headers", []) if key.lower() == b"content-length"]
        if lengths:
            try:
                if any(not value.isdigit() for value in lengths):
                    raise ValueError
                parsed_lengths = [int(value) for value in lengths]
            except (ValueError, OverflowError):
                await self.reject(scope, receive, send, 400, "请求长度无效")
                return
            if any(value != parsed_lengths[0] for value in parsed_lengths[1:]):
                await self.reject(scope, receive, send, 400, "请求长度无效")
                return
            declared_length = parsed_lengths[0]
            if declared_length > self.max_bytes:
                await self.reject(scope, receive, send, 413, self.detail)
                return
            body_size = 0

            async def limited_receive():
                nonlocal body_size
                message = await receive()
                if message["type"] == "http.request":
                    body_size += len(message.get("body", b""))
                    if body_size > self.max_bytes:
                        raise _RequestBodyTooLarge
                return message

            try:
                await self.app(scope, limited_receive, send)
            except _RequestBodyTooLarge:
                await self.reject(scope, receive, send, 413, self.detail)
            return

        with tempfile.SpooledTemporaryFile(max_size=self.spool_max_bytes, mode="w+b") as body:
            body_size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    continue
                chunk = message.get("body", b"")
                body_size += len(chunk)
                if body_size > self.max_bytes:
                    await self.reject(scope, receive, send, 413, self.detail)
                    return
                if chunk:
                    body.write(chunk)
                if not message.get("more_body", False):
                    break

            body.seek(0)
            remaining = body_size
            final_message_sent = False

            async def replay_receive():
                nonlocal remaining, final_message_sent
                if final_message_sent:
                    return await receive()

                chunk = body.read(min(64 * 1024, remaining)) if remaining else b""
                remaining -= len(chunk)
                final_message_sent = remaining == 0
                return {"type": "http.request", "body": chunk, "more_body": not final_message_sent}

            await self.app(scope, replay_receive, send)
