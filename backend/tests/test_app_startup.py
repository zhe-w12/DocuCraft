import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


class ApplicationStartupTests(unittest.TestCase):
    def test_health_endpoint_is_reachable(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with patch.dict("os.environ", {"MAX_UPLOAD_MB": "7", "MAX_SOURCE_IMAGE_MB": "12"}):
            with TestClient(app) as client:
                response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertRegex(payload["source_fingerprint"], r"^[a-f0-9]{16}$")
        self.assertIsInstance(payload["max_upload_bytes"], int)
        self.assertEqual(payload["max_upload_bytes"], 7 * 1024 * 1024)
        self.assertEqual(payload["max_source_image_bytes"], 12 * 1024 * 1024)

    def test_source_upload_limit_never_falls_below_ocr_upload_limit(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with patch.dict("os.environ", {"MAX_UPLOAD_MB": "30", "MAX_SOURCE_IMAGE_MB": "12"}):
            with TestClient(app) as client:
                response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["max_upload_bytes"], 30 * 1024 * 1024)
        self.assertEqual(payload["max_source_image_bytes"], 30 * 1024 * 1024)

    def test_document_response_keeps_heading_styles(self):
        from app.models import DocumentResponse

        response = DocumentResponse(
            id="demo",
            paragraphs=["标题"],
            paragraph_styles=["title"],
        )

        self.assertEqual(response.paragraph_styles, ["title"])


if __name__ == "__main__":
    unittest.main()
