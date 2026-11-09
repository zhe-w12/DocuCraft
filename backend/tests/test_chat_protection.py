import asyncio
import sys
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


class SlidingWindowRateLimiterTests(unittest.TestCase):
    def test_limits_requests_then_allows_them_after_the_window(self):
        from app.chat_protection import SlidingWindowRateLimiter

        limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60)
        self.assertEqual(limiter.allow("client-a", now=100), (True, 0))
        self.assertEqual(limiter.allow("client-a", now=101), (True, 0))
        self.assertEqual(limiter.allow("client-a", now=102), (False, 58))
        self.assertEqual(limiter.allow("client-a", now=160), (True, 0))

    def test_clients_have_independent_windows(self):
        from app.chat_protection import SlidingWindowRateLimiter

        limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60)
        self.assertEqual(limiter.allow("client-a", now=100), (True, 0))
        self.assertEqual(limiter.allow("client-a", now=101), (False, 59))
        self.assertEqual(limiter.allow("client-b", now=101), (True, 0))


class AssistantChatProtectionTests(unittest.TestCase):
    def test_limits_per_client_and_global_concurrency_and_releases(self):
        from app.chat_protection import EndpointProtection

        guard = EndpointProtection(
            None,
            paths=("/api/v1/assistant/chat",),
            requests_per_minute=10,
            max_concurrent=2,
            max_concurrent_per_client=1,
        )
        self.assertEqual(guard.try_acquire("client-a", now=100), (True, 200, 0))
        self.assertEqual(guard.try_acquire("client-a", now=101), (False, 429, 5))
        self.assertEqual(guard.try_acquire("client-b", now=102), (True, 200, 0))
        self.assertEqual(guard.try_acquire("client-c", now=103), (False, 503, 10))

        guard.release("client-a")
        self.assertEqual(guard.try_acquire("client-c", now=104), (True, 200, 0))
        guard.release("client-b")
        guard.release("client-c")
        self.assertEqual((guard.active_requests, dict(guard.active_by_client)), (0, {}))

    def test_guard_rejects_excess_chat_requests_with_retry_header(self):
        from app.chat_protection import EndpointProtection

        responses = []

        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        guard = EndpointProtection(downstream, paths=("/api/v1/assistant/chat",), requests_per_minute=1)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/assistant/chat",
            "client": ("client-a", 1234),
        }

        async def call():
            await guard(scope, lambda: None, lambda message: collect(message))

        async def collect(message):
            responses.append(message)

        asyncio.run(call())
        responses.clear()
        asyncio.run(call())

        start = next(message for message in responses if message["type"] == "http.response.start")
        self.assertEqual(start["status"], 429)
        self.assertIn((b"retry-after", b"60"), start["headers"])
        self.assertEqual(guard.active_requests, 0)

    def test_non_chat_routes_bypass_the_limit_and_cors_wraps_rejections(self):
        from app.chat_protection import EndpointProtection
        from app.main import app

        sent = []

        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        guard = EndpointProtection(downstream, paths=("/api/v1/assistant/chat",), requests_per_minute=1)
        scope = {"type": "http", "method": "POST", "path": "/api/v1/assistant/status", "client": ("client-a", 1234)}

        async def send(message):
            sent.append(message)

        async def call():
            await guard(scope, lambda: None, send)

        asyncio.run(call())
        asyncio.run(call())
        statuses = [message["status"] for message in sent if message["type"] == "http.response.start"]
        self.assertEqual(statuses, [200, 200])

        middleware_names = [middleware.cls.__name__ for middleware in app.user_middleware]
        self.assertLess(
            middleware_names.index("CORSMiddleware"),
            middleware_names.index("EndpointProtection"),
        )
        self.assertLess(
            middleware_names.index("EndpointProtection"),
            middleware_names.index("RequestBodySizeLimit"),
        )
        cors = next(item for item in app.user_middleware if item.cls.__name__ == "CORSMiddleware")
        self.assertIn("Retry-After", cors.kwargs["expose_headers"])
        self.assertIn("X-Error-Code", cors.kwargs["expose_headers"])

    def test_expensive_routes_have_independent_policies(self):
        from app.chat_protection import EndpointProtection

        ocr = EndpointProtection(None, paths=("/api/v1/documents/convert",), requests_per_minute=1)
        export = EndpointProtection(None, paths=("/api/v1/documents/export-edited",), requests_per_minute=1)
        refresh = EndpointProtection(
            None,
            path_prefix="/api/v1/documents/",
            path_suffix="/refresh",
            method="GET",
            requests_per_minute=1,
        )
        self.assertTrue(ocr.matches("POST", "/api/v1/documents/convert"))
        self.assertFalse(ocr.matches("GET", "/api/v1/documents/convert"))
        self.assertTrue(export.matches("POST", "/api/v1/documents/export-edited"))
        self.assertTrue(refresh.matches("GET", "/api/v1/documents/abc123/refresh"))
        self.assertFalse(refresh.matches("POST", "/api/v1/documents/abc123/refresh"))


if __name__ == "__main__":
    unittest.main()
