import asyncio
import sys
import unittest
from unittest.mock import patch
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


class RequestBodySizeLimitTests(unittest.TestCase):
    def test_rejects_declared_oversized_body_before_reading_or_parsing(self):
        from app.request_body_limit import RequestBodySizeLimit

        reached_app = False
        sent = []

        async def downstream(scope, receive, send):
            nonlocal reached_app
            reached_app = True

        async def send(message):
            sent.append(message)

        guard = RequestBodySizeLimit(downstream, path="/upload", max_bytes=5, detail="too large")
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/upload",
            "headers": [(b"content-length", b"6")],
        }
        asyncio.run(guard(scope, lambda: None, send))

        start = next(message for message in sent if message["type"] == "http.response.start")
        self.assertEqual(start["status"], 413)
        self.assertFalse(reached_app)

    def test_rejects_chunked_body_after_crossing_limit(self):
        from app.request_body_limit import RequestBodySizeLimit

        reached_app = False
        sent = []
        chunks = iter([
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
        ])

        async def downstream(scope, receive, send):
            nonlocal reached_app
            reached_app = True

        async def receive():
            return next(chunks)

        async def send(message):
            sent.append(message)

        guard = RequestBodySizeLimit(downstream, path="/upload", max_bytes=5, detail="too large")
        scope = {"type": "http", "method": "POST", "path": "/upload", "headers": []}
        asyncio.run(guard(scope, receive, send))

        start = next(message for message in sent if message["type"] == "http.response.start")
        self.assertEqual(start["status"], 413)
        self.assertFalse(reached_app)

    def test_replays_accepted_body_to_the_route(self):
        from app.request_body_limit import RequestBodySizeLimit

        captured = []
        chunks = iter([
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        ])

        async def receive():
            return next(chunks)

        async def downstream(scope, receive, send):
            captured.append(await receive())

        async def send(message):
            return None

        guard = RequestBodySizeLimit(downstream, path="/upload", max_bytes=6, detail="too large")
        scope = {"type": "http", "method": "POST", "path": "/upload", "headers": []}
        asyncio.run(guard(scope, receive, send))

        self.assertEqual(captured, [{"type": "http.request", "body": b"abcdef", "more_body": False}])

    def test_spools_chunked_body_to_disk_and_replays_it_in_bounded_chunks(self):
        import app.request_body_limit as request_body_limit

        payload = bytes(range(256)) * 400
        incoming = [
            {
                "type": "http.request",
                "body": payload[start:start + 20_000],
                "more_body": start + 20_000 < len(payload),
            }
            for start in range(0, len(payload), 20_000)
        ]
        source = iter(incoming)
        created_spools = []
        captured_chunks = []
        was_rolled_to_disk = []
        create_spool = request_body_limit.tempfile.SpooledTemporaryFile

        def record_spool(**kwargs):
            spool = create_spool(**kwargs)
            created_spools.append(spool)
            return spool

        async def receive():
            return next(source)

        async def downstream(scope, receive, send):
            was_rolled_to_disk.append(created_spools[0]._rolled)
            while True:
                message = await receive()
                captured_chunks.append(message["body"])
                if not message["more_body"]:
                    break

        async def send(message):
            return None

        guard = request_body_limit.RequestBodySizeLimit(
            downstream,
            path="/upload",
            max_bytes=len(payload),
            detail="too large",
            spool_max_bytes=1024,
        )
        scope = {"type": "http", "method": "POST", "path": "/upload", "headers": []}
        with patch.object(request_body_limit.tempfile, "SpooledTemporaryFile", side_effect=record_spool):
            asyncio.run(guard(scope, receive, send))

        self.assertEqual(was_rolled_to_disk, [True])
        self.assertTrue(captured_chunks)
        self.assertLessEqual(max(map(len, captured_chunks)), 64 * 1024)
        self.assertEqual(b"".join(captured_chunks), payload)

    def test_streams_declared_in_limit_body_without_buffering(self):
        from app.request_body_limit import RequestBodySizeLimit

        captured = []

        async def receive():
            return {"type": "http.request", "body": b"abc", "more_body": False}

        async def downstream(scope, receive, send):
            captured.append(await receive())

        async def send(message):
            return None

        guard = RequestBodySizeLimit(downstream, path="/upload", max_bytes=5, detail="too large")
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/upload",
            "headers": [(b"content-length", b"3")],
        }
        asyncio.run(guard(scope, receive, send))

        self.assertEqual(captured, [{"type": "http.request", "body": b"abc", "more_body": False}])

    def test_rejects_body_that_exceeds_limit_despite_small_content_length(self):
        from app.request_body_limit import RequestBodySizeLimit

        reached_end = False
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"123456", "more_body": False}

        async def downstream(scope, receive, send):
            nonlocal reached_end
            while (message := await receive())["more_body"]:
                pass
            reached_end = True

        async def send(message):
            sent.append(message)

        guard = RequestBodySizeLimit(downstream, path="/upload", max_bytes=5, detail="too large")
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/upload",
            "headers": [(b"content-length", b"3")],
        }
        asyncio.run(guard(scope, receive, send))

        start = next(message for message in sent if message["type"] == "http.response.start")
        self.assertEqual(start["status"], 413)
        self.assertFalse(reached_end)

    def test_rejects_conflicting_content_length_headers(self):
        from app.request_body_limit import RequestBodySizeLimit

        reached_app = False
        sent = []

        async def downstream(scope, receive, send):
            nonlocal reached_app
            reached_app = True

        async def send(message):
            sent.append(message)

        guard = RequestBodySizeLimit(downstream, path="/upload", max_bytes=5, detail="too large")
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/upload",
            "headers": [(b"content-length", b"3"), (b"content-length", b"4")],
        }
        asyncio.run(guard(scope, lambda: None, send))

        start = next(message for message in sent if message["type"] == "http.response.start")
        self.assertEqual(start["status"], 400)
        self.assertFalse(reached_app)

    def test_other_routes_are_not_limited(self):
        from app.request_body_limit import RequestBodySizeLimit

        reached_app = False

        async def downstream(scope, receive, send):
            nonlocal reached_app
            reached_app = True

        async def send(message):
            return None

        guard = RequestBodySizeLimit(downstream, path="/upload", max_bytes=5, detail="too large")
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/assistant/chat",
            "headers": [(b"content-length", b"600")],
        }
        asyncio.run(guard(scope, lambda: None, send))

        self.assertTrue(reached_app)

    def test_limits_each_configured_path_and_bypasses_unlisted_paths(self):
        from app.request_body_limit import RequestBodySizeLimit

        reached_paths = []

        async def downstream(scope, receive, send):
            reached_paths.append(scope["path"])

        async def send(message):
            return None

        guard = RequestBodySizeLimit(
            downstream,
            paths=("/chat", "/export"),
            max_bytes=5,
            detail="too large",
        )
        for path in ("/chat", "/export", "/other"):
            scope = {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [(b"content-length", b"6")],
            }
            asyncio.run(guard(scope, lambda: None, send))

        self.assertEqual(reached_paths, ["/other"])


if __name__ == "__main__":
    unittest.main()
