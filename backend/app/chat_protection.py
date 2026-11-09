from __future__ import annotations

import math
import os
import time
from collections import defaultdict, deque

from starlette.responses import JSONResponse


class SlidingWindowRateLimiter:
    def __init__(self, limit: int, window_seconds: int = 60, max_clients: int = 10_000):
        self.limit = max(1, limit)
        self.window_seconds = max(1, window_seconds)
        self.max_clients = max(1, max_clients)
        self._requests: dict[str, deque[float]] = {}
        self._checks = 0

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        for client, events in list(self._requests.items()):
            while events and events[0] <= cutoff:
                events.popleft()
            if not events:
                self._requests.pop(client, None)

    def allow(self, client: str, now: float | None = None) -> tuple[bool, int]:
        current = time.monotonic() if now is None else now
        self._checks += 1
        if self._checks % 256 == 0 or len(self._requests) >= self.max_clients:
            self._prune(current)

        events = self._requests.get(client)
        if events is None:
            if len(self._requests) >= self.max_clients:
                oldest = min(
                    self._requests,
                    key=lambda key: self._requests[key][-1] if self._requests[key] else current,
                )
                self._requests.pop(oldest, None)
            events = self._requests.setdefault(client, deque())

        cutoff = current - self.window_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= self.limit:
            retry_after = max(1, math.ceil(events[0] + self.window_seconds - current))
            return False, retry_after

        events.append(current)
        return True, 0


class EndpointProtection:
    """In-process rate and concurrency guard for a billable/heavy endpoint."""

    def __init__(
        self,
        app,
        paths: tuple[str, ...] = (),
        path_prefix: str = "",
        path_suffix: str = "",
        method: str = "POST",
        requests_per_minute: int = 10,
        max_concurrent: int = 4,
        max_concurrent_per_client: int = 2,
        rate_limit_detail: str = "请求过于频繁，请稍后再试",
        busy_detail: str = "服务当前较忙，请稍后重试",
    ):
        self.app = app
        self.paths = frozenset(paths)
        self.path_prefix = path_prefix
        self.path_suffix = path_suffix
        self.method = method.upper()
        self.rate_limiter = SlidingWindowRateLimiter(requests_per_minute)
        self.max_concurrent = max(1, max_concurrent)
        self.max_concurrent_per_client = max(1, max_concurrent_per_client)
        self.rate_limit_detail = rate_limit_detail
        self.busy_detail = busy_detail
        self.active_requests = 0
        self.active_by_client: dict[str, int] = defaultdict(int)

    def matches(self, method: str, path: str) -> bool:
        if method.upper() != self.method:
            return False
        if path in self.paths:
            return True
        return bool(
            self.path_prefix
            and path.startswith(self.path_prefix)
            and (not self.path_suffix or path.endswith(self.path_suffix))
        )

    def try_acquire(self, client: str, now: float | None = None) -> tuple[bool, int, int]:
        allowed, retry_after = self.rate_limiter.allow(client, now)
        if not allowed:
            return False, 429, retry_after
        if self.active_by_client.get(client, 0) >= self.max_concurrent_per_client:
            return False, 429, 5
        if self.active_requests >= self.max_concurrent:
            return False, 503, 10

        self.active_requests += 1
        self.active_by_client[client] += 1
        return True, 200, 0

    def release(self, client: str) -> None:
        self.active_requests = max(0, self.active_requests - 1)
        active = self.active_by_client.get(client, 0)
        if active <= 1:
            self.active_by_client.pop(client, None)
        else:
            self.active_by_client[client] = active - 1

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.matches(scope.get("method", ""), scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        client_info = scope.get("client")
        client = str(client_info[0]) if client_info else "unknown"
        allowed, status_code, retry_after = self.try_acquire(client)
        if not allowed:
            detail = self.rate_limit_detail if status_code == 429 else self.busy_detail
            response = JSONResponse(
                status_code=status_code,
                content={"detail": detail},
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        finally:
            self.release(client)


def bounded_positive_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(maximum, max(1, value))
