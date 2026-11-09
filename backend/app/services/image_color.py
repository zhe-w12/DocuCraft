from __future__ import annotations

from io import BytesIO

from PIL import Image


def _thumbnail_ocr_input(
    image: Image.Image,
    max_size: tuple[int, int],
) -> tuple[Image.Image, bool]:
    """Bound OCR pixels before expanding color channels or converting ICC data."""
    thumbnail_image = image
    owns_image = False
    if image.mode in {"1", "P"}:
        has_transparency = "transparency" in image.info
        if image.mode == "1":
            target_mode = "RGBA" if has_transparency else "L"
        else:
            target_mode = "RGBA" if has_transparency else "RGB"
        thumbnail_image = image.convert(target_mode)
        owns_image = True

    try:
        thumbnail_image.thumbnail(max_size, Image.Resampling.LANCZOS)
    except Exception:
        if owns_image:
            thumbnail_image.close()
        raise
    return thumbnail_image, owns_image


def _image_on_white(image: Image.Image) -> Image.Image:
    """Flatten transparency onto the white page used by the editor."""
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        try:
            alpha = rgba.getchannel("A")
            try:
                flattened = Image.new("RGB", rgba.size, "white")
                try:
                    flattened.paste(rgba, mask=alpha)
                except Exception:
                    flattened.close()
                    raise
                return flattened
            finally:
                alpha.close()
        finally:
            rgba.close()
    return image.convert("RGB")


def _image_to_srgb_on_white(
    image: Image.Image,
    *,
    force_srgb: bool = False,
) -> tuple[Image.Image, bytes | None]:
    """Convert profiled scans to sRGB, optionally converting RGB profiles too."""
    profile_bytes = image.info.get("icc_profile")
    if not isinstance(profile_bytes, bytes):
        return _image_on_white(image), None
    if image.mode in {"RGB", "RGBA"} and not force_srgb:
        # Source exports can keep their RGB samples and profile together.
        return _image_on_white(image), profile_bytes

    try:
        from PIL import ImageCms
    except ImportError:
        return _image_on_white(image), None

    has_alpha = (
        any(channel.upper() == "A" for channel in image.getbands())
        or "transparency" in image.info
    )
    if image.mode in {"RGB", "CMYK", "L"}:
        color_source = image.copy()
    elif image.mode == "LA":
        color_source = image.getchannel("L")
    elif image.mode == "1":
        color_source = image.convert("L")
    else:
        color_source = image.convert("RGB")

    converted: Image.Image | None = None
    try:
        input_profile = ImageCms.ImageCmsProfile(BytesIO(profile_bytes))
        output_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
        converted = ImageCms.profileToProfile(
            color_source,
            input_profile,
            output_profile,
            outputMode="RGB",
        )
        output_profile_bytes = output_profile.tobytes()
        if not has_alpha:
            return converted, output_profile_bytes

        rgba = image.convert("RGBA")
        try:
            alpha = rgba.getchannel("A")
            flattened = Image.new("RGB", image.size, "white")
            try:
                flattened.paste(converted, mask=alpha)
            except Exception:
                flattened.close()
                raise
            finally:
                alpha.close()
        finally:
            rgba.close()
        converted.close()
        converted = None
        return flattened, output_profile_bytes
    except (ImageCms.PyCMSError, OSError, TypeError, ValueError):
        if converted is not None:
            converted.close()
        return _image_on_white(image), None
    finally:
        color_source.close()
