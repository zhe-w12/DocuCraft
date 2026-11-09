import sys
import unittest
from pathlib import Path

import httpx
from pydantic import ValidationError


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.models import PositionedWordRequest
from app import main


class PositionedWordRequestTests(unittest.TestCase):
    def test_rejects_non_finite_coordinates(self):
        for field in ("left", "top", "width", "height"):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValidationError):
                        PositionedWordRequest.model_validate({field: value})

    def test_rejects_non_finite_coordinates_in_json_payloads(self):
        for field in ("left", "top", "width", "height"):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    PositionedWordRequest.model_validate_json(
                        f'{{"{field}":NaN}}'
                    )

    def test_accepts_finite_source_coordinates(self):
        word = PositionedWordRequest.model_validate({
            "text": "标题",
            "left": 10.5,
            "top": 20.25,
            "width": 80,
            "height": 18,
        })

        self.assertEqual(word.left, 10.5)
        self.assertEqual(word.top, 20.25)
        self.assertEqual(word.width, 80)
        self.assertEqual(word.height, 18)

    def test_rejects_negative_or_unbounded_geometry_and_oversized_text(self):
        invalid_values = (
            {"left": -1},
            {"top": -1},
            {"width": -1},
            {"height": -1},
            {"left": 100_000_001},
            {"text": "字" * 10_001},
        )
        for value in invalid_values:
            with self.subTest(value=list(value)):
                with self.assertRaises(ValidationError):
                    PositionedWordRequest.model_validate(value)


class EditValidationEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_finite_coordinates_return_422_instead_of_500(self):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/documents/export-edited",
                content=b'{"positioned_words":[{"left":NaN}]}',
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn("finite number", response.text.lower())
        self.assertNotIn("NaN", response.text)


class PositionBoundsTests(unittest.TestCase):
    def test_accepts_boxes_within_the_source_page_and_small_rounding_tolerance(self):
        word = PositionedWordRequest(
            text="文字",
            left=98,
            top=48,
            width=5,
            height=5,
        )

        main._validate_positioned_word_bounds([word], 100, 50)

    def test_rejects_boxes_outside_the_source_page(self):
        word = PositionedWordRequest(
            text="越界",
            left=98,
            top=10,
            width=6,
            height=8,
        )

        with self.assertRaises(main.HTTPException) as context:
            main._validate_positioned_word_bounds([word], 100, 50)

        self.assertEqual(context.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
