import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


class ImageSafetyTests(unittest.TestCase):
    def test_preserved_phone_photo_is_transposed_before_coordinate_scaling(self):
        from PIL import Image

        from app.main import preserve_source_image

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "phone.jpg"
            destination = root / "stored.png"
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (32, 20), "white").save(source, format="JPEG", exif=exif)
            storage = SimpleNamespace(path=lambda _name: destination)

            with patch("app.main.storage", storage):
                preserved, width, height = preserve_source_image(source, "a" * 32)

            self.assertEqual((width, height), (20, 32))
            with Image.open(preserved) as image:
                self.assertEqual(image.size, (20, 32))
                self.assertEqual(image.format, "PNG")

    def test_remote_png_dimension_probe_keeps_the_source_suffix_and_cleans_up(self):
        from PIL import Image

        from app import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_bytes = io.BytesIO()
            Image.new("RGB", (24, 18), "white").save(image_bytes, format="PNG")
            destinations = []

            class Storage:
                _remote = object()

                @staticmethod
                def path(name):
                    return root / name

                @staticmethod
                def download_published(name, destination):
                    destinations.append(destination)
                    destination.write_bytes(image_bytes.getvalue())

            with patch.object(main, "storage", Storage()):
                dimensions = main._stored_image_dimensions(
                    "a" * 32,
                    {"source_image_file": "source.png"},
                )

            self.assertEqual(dimensions, (24, 18))
            self.assertTrue(destinations[0].name.endswith("_dimension_probe.png"))
            self.assertFalse(destinations[0].exists(), "dimension probe should be removed")

    def test_normalizes_small_jpeg_after_using_draft_api(self):
        from PIL import Image

        from app.main import normalize_image

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "small.jpg"
            destination = Path(directory) / "normalized.jpg"
            Image.new("RGB", (32, 20), "white").save(source, format="JPEG")

            normalize_image(source, destination)

            with Image.open(destination) as normalized:
                self.assertEqual(normalized.size, (32, 20))

    def test_invalid_image_error_does_not_expose_decoder_details(self):
        from fastapi import HTTPException

        from app.main import normalize_image

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "private-upload-name.png"
            source.write_bytes(b"not an image")

            with self.assertRaises(HTTPException) as caught:
                normalize_image(source, root / "normalized.jpg")

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "无法读取图片，请检查文件后重试")
        self.assertNotIn("private-upload-name", caught.exception.detail)

    def test_transparent_palette_images_match_the_white_editor_page(self):
        from PIL import Image

        from app import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "transparent.gif"
            palette_image = Image.new("P", (20, 10))
            palette = [24, 36, 48, 220, 20, 60] + [0] * 762
            palette_image.putpalette(palette)
            palette_image.putdata([
                color
                for _ in range(10)
                for color in ([0] * 10 + [1] * 10)
            ])
            palette_image.save(source, format="GIF", transparency=0)
            normalized = root / "normalized.jpg"
            storage = SimpleNamespace(path=lambda name: root / name)

            main.normalize_image(source, normalized)
            with patch.object(main, "storage", storage):
                preserved, _, _ = main.preserve_source_image(source, "a" * 32)

            with Image.open(normalized) as image:
                self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
                red_pixel = image.getpixel((15, 0))
                self.assertGreater(red_pixel[0], 180)
                self.assertLess(red_pixel[1], 60)
                self.assertLess(red_pixel[2], 100)
            with Image.open(preserved) as image:
                self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
                self.assertEqual(image.getpixel((15, 0)), (220, 20, 60))

    def test_rejects_oversized_image_header_before_decoding_pixels(self):
        from app.main import normalize_image

        class ImageHeader:
            size = (10_000, 6_000)
            format = "PNG"

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def load(self):
                raise AssertionError("pixel data must not be decoded")

        with (
            patch.dict(os.environ, {"MAX_IMAGE_PIXELS": "50000000"}),
            patch("app.main.Image.open", return_value=ImageHeader()),
        ):
            with self.assertRaises(HTTPException) as caught:
                normalize_image(Path("oversized.png"), Path("unused.jpg"))

        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(caught.exception.detail, "图片分辨率过高，请缩小尺寸后重试")

    def test_maps_pillow_decompression_bomb_error_to_413(self):
        from PIL import Image

        from app.main import normalize_image

        with patch("app.main.Image.open", side_effect=Image.DecompressionBombError("too many pixels")):
            with self.assertRaises(HTTPException) as caught:
                normalize_image(Path("bomb.png"), Path("unused.jpg"))

        self.assertEqual(caught.exception.status_code, 413)


if __name__ == "__main__":
    unittest.main()
