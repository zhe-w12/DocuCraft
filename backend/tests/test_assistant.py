import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


class AssistantWorkflowTests(unittest.TestCase):
    def test_document_output_limit_is_configurable_and_chat_stays_bounded(self):
        from app.services.assistant import output_token_limit

        with patch.dict(os.environ, {"AI_MAX_OUTPUT_TOKENS": "8192"}):
            self.assertEqual(output_token_limit("write"), 8192)
            self.assertEqual(output_token_limit("chat"), 4096)

        with patch.dict(os.environ, {"AI_MAX_OUTPUT_TOKENS": "999999"}):
            self.assertEqual(output_token_limit("revise"), 131_072)

    def test_status_never_exposes_provider_secret(self):
        from app.services.assistant import assistant_status

        secret = "server-only-test-key"
        with patch.dict(os.environ, {
            "AI_PROVIDER": "bailian",
            "AI_API_KEY": secret,
            "AI_MODEL": "test-endpoint",
        }):
            status = assistant_status()

        self.assertTrue(status["configured"])
        self.assertEqual(status["provider"], "阿里云百炼")
        self.assertNotIn(secret, str(status))

    def test_bailian_defaults_to_beijing_openai_compatible_endpoint(self):
        from app.services.assistant import get_assistant_config

        with patch.dict(os.environ, {
            "AI_PROVIDER": "bailian",
            "AI_API_BASE_URL": "",
            "AI_API_KEY": "server-only-test-key",
            "AI_MODEL": "qwen3.8-max",
        }):
            config = get_assistant_config()

        self.assertEqual(config.base_url, "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.assertEqual(config.model, "qwen3.8-max")

    def test_revision_prompt_protects_original_facts(self):
        from app.services.assistant import build_system_prompt

        prompt = build_system_prompt("revise")

        self.assertIn("保留事实", prompt)
        self.assertIn("只改指定部分", prompt)

    def test_prompt_uses_standard_markdown_math_delimiters(self):
        from app.services.assistant import build_system_prompt

        prompt = build_system_prompt("write")

        self.assertIn("行内公式用 `$...$`", prompt)
        self.assertIn("独立公式用单独成行的 `$$...$$`", prompt)
        self.assertIn("不要用裸方括号包裹公式", prompt)

    def test_document_prompt_omits_fake_workspace_and_save_metadata(self):
        from app.services.assistant import build_system_prompt

        prompt = build_system_prompt("write")

        self.assertIn("不添加头像、作者/工作区", prompt)
        self.assertIn("自动保存状态", prompt)

    def test_chat_context_accepts_a_document_up_to_the_export_limit(self):
        from app.models import AssistantChatRequest

        content = "文" * 100_000
        request = AssistantChatRequest(
            mode="revise",
            messages=[
                {"role": "user", "content": "请润色这份文档"},
                {"role": "assistant", "content": content},
            ],
        )

        self.assertEqual(len(request.messages[1].content), 100_000)

    def test_casual_chat_prompt_does_not_force_document_output(self):
        from app.models import AssistantChatRequest
        from app.services.assistant import build_system_prompt

        request = AssistantChatRequest(mode="chat", messages=[{"role": "user", "content": "最近有点累"}])
        prompt = build_system_prompt(request.mode)

        self.assertEqual(request.mode, "chat")
        self.assertIn("轻松自然的日常闲聊", prompt)
        self.assertIn("不强行生成正式文档", prompt)
        self.assertIn("不输出文档标题", prompt)

    def test_unconfigured_chat_returns_clear_service_error(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with patch.dict(os.environ, {"AI_API_KEY": "", "AI_MODEL": ""}):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/assistant/chat",
                    json={"mode": "write", "messages": [{"role": "user", "content": "写一段欢迎词"}]},
                )

        self.assertEqual(response.status_code, 503)
        self.assertIn("后端环境", response.json()["detail"])

    def test_provider_request_uses_server_key_and_returns_text(self):
        from app.services.assistant import generate_reply

        captured = {}

        class FakeResponse:
            status_code = 200
            is_error = False

            def json(self):
                return {"choices": [{"message": {"content": "修改后的正文"}}]}

        class FakeClient:
            def __init__(self, **kwargs):
                captured["timeout"] = kwargs["timeout"]

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def post(self, url, *, headers, json):
                captured.update(url=url, headers=headers, request=json)
                return FakeResponse()

        environment = {
            "AI_PROVIDER": "bailian",
            "AI_API_BASE_URL": "https://example.test/v1/",
            "AI_API_KEY": "server-only-test-key",
            "AI_MODEL": "test-endpoint",
            "AI_MAX_OUTPUT_TOKENS": "16384",
        }
        with patch.dict(os.environ, environment), patch("app.services.assistant.httpx.AsyncClient", FakeClient):
            reply = asyncio.run(generate_reply("polish", [{"role": "user", "content": "润色这段话"}]))

        self.assertEqual(reply, "修改后的正文")
        self.assertEqual(captured["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer server-only-test-key")
        self.assertEqual(captured["request"]["model"], "test-endpoint")
        self.assertEqual(captured["request"]["max_tokens"], 16_384)
        self.assertEqual(captured["request"]["messages"][-1]["role"], "user")

    def test_provider_stream_yields_incremental_text(self):
        from app.services.assistant import stream_reply

        captured = {}

        class FakeResponse:
            status_code = 200
            is_error = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def aiter_lines(self):
                yield 'data: {"choices":[],"usage":{"total_tokens":4}}'
                yield 'data: {"choices":[{"delta":{"content":"你好"}}]}'
                yield 'data: {"choices":[{"delta":{"content":"，世界"}}]}'
                yield "data: [DONE]"

        class FakeClient:
            def __init__(self, **kwargs):
                captured["timeout"] = kwargs["timeout"]

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def stream(self, method, url, *, headers, json):
                captured.update(method=method, url=url, headers=headers, request=json)
                return FakeResponse()

        environment = {
            "AI_PROVIDER": "bailian",
            "AI_API_BASE_URL": "https://example.test/v1/",
            "AI_API_KEY": "server-only-test-key",
            "AI_MODEL": "test-endpoint",
            "AI_MAX_OUTPUT_TOKENS": "16384",
        }

        async def collect():
            return [part async for part in stream_reply("chat", [{"role": "user", "content": "嗨"}])]

        with patch.dict(os.environ, environment), patch("app.services.assistant.httpx.AsyncClient", FakeClient):
            parts = asyncio.run(collect())

        self.assertEqual(parts, ["你好", "，世界"])
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], "https://example.test/v1/chat/completions")
        self.assertTrue(captured["request"]["stream"])
        self.assertEqual(captured["request"]["max_tokens"], 4_096)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer server-only-test-key")

    def test_provider_stream_reports_output_truncation(self):
        from app.services.assistant import AssistantProviderError, stream_reply

        class FakeResponse:
            status_code = 200
            is_error = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def aiter_lines(self):
                yield 'data: {"choices":[{"delta":{"content":"文档前半段"}}]}'
                yield 'data: {"choices":[{"delta":{},"finish_reason":"length"}]}'
                yield "data: [DONE]"

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def stream(self, *args, **kwargs):
                return FakeResponse()

        async def collect():
            return [part async for part in stream_reply("write", [{"role": "user", "content": "写长文"}])]

        with patch.dict(os.environ, {
            "AI_PROVIDER": "bailian",
            "AI_API_KEY": "server-only-test-key",
            "AI_MODEL": "test-endpoint",
        }), patch("app.services.assistant.httpx.AsyncClient", FakeClient):
            with self.assertRaises(AssistantProviderError) as caught:
                asyncio.run(collect())

        self.assertIn("文档可能不完整", str(caught.exception))

    def test_provider_non_stream_reports_output_truncation(self):
        from app.services.assistant import AssistantProviderError, generate_reply

        class FakeResponse:
            status_code = 200
            is_error = False

            def json(self):
                return {"choices": [{
                    "message": {"content": "文档前半段"},
                    "finish_reason": "length",
                }]}

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def post(self, *args, **kwargs):
                return FakeResponse()

        with patch.dict(os.environ, {
            "AI_PROVIDER": "bailian",
            "AI_API_KEY": "server-only-test-key",
            "AI_MODEL": "test-endpoint",
        }), patch("app.services.assistant.httpx.AsyncClient", FakeClient):
            with self.assertRaises(AssistantProviderError) as caught:
                asyncio.run(generate_reply("write", [{"role": "user", "content": "写长文"}]))

        self.assertIn("文档可能不完整", str(caught.exception))

    def test_chat_endpoint_returns_incremental_ndjson(self):
        from fastapi.testclient import TestClient

        from app.main import app

        async def fake_stream_reply(mode, messages):
            self.assertEqual(mode, "chat")
            self.assertEqual(messages[-1]["content"], "嗨")
            yield "你好"
            yield "，朋友。"

        with patch.dict(os.environ, {
            "AI_PROVIDER": "bailian",
            "AI_API_KEY": "server-only-test-key",
            "AI_MODEL": "test-endpoint",
        }), patch("app.main.stream_reply", fake_stream_reply), TestClient(app) as client:
            response = client.post(
                "/api/v1/assistant/chat",
                json={"mode": "chat", "stream": True, "messages": [{"role": "user", "content": "嗨"}]},
            )

        self.assertEqual(response.status_code, 200)
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual(events, [
            {"type": "delta", "content": "你好"},
            {"type": "delta", "content": "，朋友。"},
            {"type": "done"},
        ])

    def test_chat_endpoint_sends_each_delta_before_requesting_the_next(self):
        from app.main import app

        trace = []

        async def fake_stream_reply(mode, messages):
            trace.append("yield:你好")
            yield "你好"
            trace.append("yield:，朋友。")
            yield "，朋友。"

        async def invoke_app():
            request_body = json.dumps({
                "mode": "chat",
                "stream": True,
                "messages": [{"role": "user", "content": "嗨"}],
            }, ensure_ascii=False).encode("utf-8")
            received_request = False
            messages = []

            async def receive():
                nonlocal received_request
                if not received_request:
                    received_request = True
                    return {"type": "http.request", "body": request_body, "more_body": False}
                await asyncio.Event().wait()

            async def send(message):
                messages.append(message)
                if message["type"] == "http.response.body" and message.get("body"):
                    trace.append("send:" + message["body"].decode("utf-8"))

            await app({
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/v1/assistant/chat",
                "raw_path": b"/api/v1/assistant/chat",
                "query_string": b"",
                "headers": [
                    (b"host", b"testserver"),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(request_body)).encode("ascii")),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "root_path": "",
            }, receive, send)
            return messages

        with patch.dict(os.environ, {
            "AI_PROVIDER": "bailian",
            "AI_API_KEY": "server-only-test-key",
            "AI_MODEL": "test-endpoint",
        }), patch("app.main.stream_reply", fake_stream_reply):
            messages = asyncio.run(invoke_app())

        response_start = next(message for message in messages if message["type"] == "http.response.start")
        headers = {key.lower(): value for key, value in response_start["headers"]}
        self.assertEqual(response_start["status"], 200)
        self.assertEqual(headers[b"content-type"], b"application/x-ndjson")
        self.assertEqual(headers[b"cache-control"], b"no-cache")
        self.assertEqual(headers[b"x-accel-buffering"], b"no")
        self.assertEqual(
            [message["body"].decode("utf-8") for message in messages
             if message["type"] == "http.response.body" and message.get("body")],
            [
                '{"type": "delta", "content": "你好"}\n',
                '{"type": "delta", "content": "，朋友。"}\n',
                '{"type": "done"}\n',
            ],
        )
        self.assertEqual(
            [entry for entry in trace if entry.startswith(("yield:", "send:"))],
            [
                "yield:你好",
                'send:{"type": "delta", "content": "你好"}\n',
                "yield:，朋友。",
                'send:{"type": "delta", "content": "，朋友。"}\n',
                'send:{"type": "done"}\n',
            ],
        )

    def test_chat_disconnect_closes_the_provider_stream(self):
        from app.chat_protection import EndpointProtection
        from app.main import app

        first_delta_sent = None
        response_closed = None
        client_closed = None
        partial_content = "保留的部分内容".encode("utf-8")
        partial_text = partial_content.decode("utf-8")

        class FakeResponse:
            status_code = 200
            is_error = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                response_closed.set()
                return False

            async def aiter_lines(self):
                yield "data: " + json.dumps({"choices": [{"delta": {"content": partial_text}}]}, ensure_ascii=False)
                await asyncio.Event().wait()

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                client_closed.set()
                return False

            def stream(self, *args, **kwargs):
                return FakeResponse()

        async def invoke_app():
            nonlocal first_delta_sent, response_closed, client_closed
            first_delta_sent = asyncio.Event()
            response_closed = asyncio.Event()
            client_closed = asyncio.Event()
            request_body = json.dumps({
                "mode": "write",
                "stream": True,
                "messages": [{"role": "user", "content": "写一份文档"}],
            }, ensure_ascii=False).encode("utf-8")
            received_request = False
            messages = []

            async def receive():
                nonlocal received_request
                if not received_request:
                    received_request = True
                    return {"type": "http.request", "body": request_body, "more_body": False}
                await first_delta_sent.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)
                if message["type"] == "http.response.body" and partial_content in message.get("body", b""):
                    first_delta_sent.set()

            await asyncio.wait_for(app({
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/v1/assistant/chat",
                "raw_path": b"/api/v1/assistant/chat",
                "query_string": b"",
                "headers": [
                    (b"host", b"testserver"),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(request_body)).encode("ascii")),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "root_path": "",
            }, receive, send), timeout=2)
            return messages

        with patch.dict(os.environ, {
            "AI_PROVIDER": "bailian",
            "AI_API_KEY": "server-only-test-key",
            "AI_MODEL": "test-endpoint",
        }), patch("app.services.assistant.httpx.AsyncClient", FakeClient):
            messages = asyncio.run(invoke_app())

        self.assertTrue(response_closed.is_set(), "disconnect should close the model response stream")
        self.assertTrue(client_closed.is_set(), "disconnect should close the model HTTP client")
        response_bodies = [message.get("body", b"") for message in messages if message["type"] == "http.response.body"]
        self.assertTrue(any(partial_content in body for body in response_bodies))
        self.assertFalse(any(b'"type": "done"' in body for body in response_bodies))

        middleware = app.middleware_stack
        while middleware is not None and not isinstance(middleware, EndpointProtection):
            middleware = getattr(middleware, "app", None)
        self.assertIsNotNone(middleware, "the chat concurrency guard should be installed")
        self.assertEqual(middleware.active_requests, 0, "disconnect should release the chat concurrency slot")

    def test_chat_endpoint_marks_truncated_stream_as_error_not_done(self):
        from fastapi.testclient import TestClient

        from app.main import app
        from app.services.assistant import AssistantProviderError

        async def fake_stream_reply(mode, messages):
            yield "文档前半段"
            raise AssistantProviderError("模型输出达到当前长度上限，文档可能不完整。")

        with patch.dict(os.environ, {
            "AI_PROVIDER": "bailian",
            "AI_API_KEY": "server-only-test-key",
            "AI_MODEL": "test-endpoint",
        }), patch("app.main.stream_reply", fake_stream_reply), TestClient(app) as client:
            response = client.post(
                "/api/v1/assistant/chat",
                json={"mode": "write", "stream": True, "messages": [{"role": "user", "content": "写长文"}]},
            )

        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual(events, [
            {"type": "delta", "content": "文档前半段"},
            {"type": "error", "detail": "模型输出达到当前长度上限，文档可能不完整。"},
        ])

    def test_document_export_returns_an_editable_docx(self):
        from io import BytesIO
        from fastapi.testclient import TestClient
        from docx import Document

        from app.main import app

        with TestClient(app) as client:
            response = client.post(
                "/api/v1/assistant/export/docx",
                json={
                    "title": "测试文档",
                    "content": (
                        "## 第一部分\n\n这是**正文**。\n\n"
                        "| 现象 | 时间 |\n\n| --- | :---: |\n\n"
                        "| 脏读 | T1\n\n"
                        "| 可重复读 | `T2 | T3` |\n"
                    ),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["content-type"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertTrue(response.content.startswith(b"PK"))
        exported = Document(BytesIO(response.content))
        self.assertEqual(len(exported.tables), 1)
        self.assertEqual(len(exported.tables[0].rows), 3)
        self.assertEqual([cell.text for cell in exported.tables[0].rows[0].cells], ["现象", "时间"])
        self.assertEqual([cell.text for cell in exported.tables[0].rows[2].cells], ["可重复读", "T2 | T3"])
        body = next(paragraph for paragraph in exported.paragraphs if "正文" in paragraph.text)
        self.assertTrue(any(run.bold for run in body.runs), "inline Markdown bold should be preserved in Word")

    def test_document_export_rejects_unknown_format(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            response = client.post(
                "/api/v1/assistant/export/rtf",
                json={"title": "测试文档", "content": "这是正文。"},
            )

        self.assertEqual(response.status_code, 404)

    def test_pdf_export_returns_generated_pdf(self):
        from fastapi.testclient import TestClient
        from docx import Document

        from app.main import app

        def fake_convert_docx_to_pdf(docx_path, output_dir):
            generated_docx = Document(docx_path)
            assert len(generated_docx.tables) == 1
            assert generated_docx.tables[0].rows[1].cells[0].text == "T1"
            pdf_path = output_dir / "iscan-document.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 test")
            return pdf_path, False

        with patch("app.main.convert_docx_to_pdf", fake_convert_docx_to_pdf), TestClient(app) as client:
            response = client.post(
                "/api/v1/assistant/export/pdf",
                json={
                    "title": "测试文档",
                    "content": "| 时间 | 事务 A |\n| --- | --- |\n| T1 | 修改余额 |\n",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
