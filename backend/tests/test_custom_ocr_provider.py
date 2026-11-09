import asyncio
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app import main
from app.services.ocr import GenericHttpOcrProvider, get_ocr_provider


class CustomOcrProviderTests(unittest.TestCase):
    def tearDown(self):
        get_ocr_provider.cache_clear()

    def test_self_hosted_http_provider_is_selected_by_configuration(self):
        with patch.dict(
            os.environ,
            {
                "OCR_PROVIDER": "generic_http",
                "OCR_ENDPOINT": "https://ocr.example/recognize",
            },
        ):
            get_ocr_provider.cache_clear()
            self.assertIsInstance(get_ocr_provider(), GenericHttpOcrProvider)

    def test_third_party_and_local_providers_are_not_selectable(self):
        for provider in ("baidu", "tesseract"):
            with self.subTest(provider=provider), patch.dict(
                os.environ,
                {"OCR_PROVIDER": provider, "OCR_ENDPOINT": "https://ocr.example/recognize"},
            ):
                get_ocr_provider.cache_clear()
                with self.assertRaisesRegex(RuntimeError, "仅支持 mock.*generic_http"):
                    get_ocr_provider()

    def test_self_hosted_provider_failure_does_not_fall_back_to_local_ocr(self):
        class OfflineProvider(GenericHttpOcrProvider):
            def __init__(self):
                super().__init__("https://ocr.example/recognize", None)
                self.calls = 0

            async def recognize(self, _image_path):
                self.calls += 1
                request = httpx.Request("POST", self.endpoint)
                raise httpx.ConnectError("OCR service unavailable", request=request)

        provider = OfflineProvider()
        with (
            patch.object(main.asyncio, "sleep", new=AsyncMock()),
            patch.object(main, "recognize_locally") as local_recognizer,
        ):
            with self.assertRaises(httpx.ConnectError):
                asyncio.run(
                    main._recognize_with_transient_ocr_retry(
                        provider,
                        Path("unused.png"),
                    )
                )

        self.assertEqual(provider.calls, 2)
        local_recognizer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
