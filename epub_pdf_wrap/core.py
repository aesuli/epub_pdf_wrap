"""Convert a PDF into an EPUB by rendering each page to an image."""

from __future__ import annotations

import re
import struct
import time
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pymupdf
from PIL import Image, ImageFilter, ImageOps, ImageStat


IMAGE_FORMATS = ("png", "jpeg", "auto")
DEFAULT_JPEG_QUALITY = 85
DEFAULT_MRC_COLOR_SCALE = 4
ImageData = tuple[str, bytes]
PageLayers = tuple[ImageData, ...]
OcrWord = tuple[float, float, float, float, str]


@dataclass(frozen=True)
class PageContent:
    """Image resources and optional source-page geometry/text for one page."""

    layers: PageLayers
    viewport: tuple[float, float] | None = None
    ocr_words: tuple[OcrWord, ...] = ()

# In auto mode, prefer lossless PNG when it costs no more than 10% over JPEG.
# This keeps crisp line art lossless without retaining very large PNGs for
# photographic or noisy scanned pages.
_AUTO_PNG_SIZE_RATIO = 1.10

_PNG_GRAYSCALE_DEPTHS = (1, 2, 4, 8)

# Hidden MRC pixels are blurred after the visible pixels have been expanded
# into them. A small radius removes mask-shaped edges without making the
# synthesis unnecessarily expensive on large rendered pages.
_MRC_HIDDEN_BLUR_RADIUS = 2
_MRC_DIFFUSION_PASSES = 2

# JPEG 2000 sources are typically compressed far more aggressively than a
# generic JPEG quality would choose by default (MRC layers are meant to be
# masked/approximate, not high fidelity). When re-encoding one without an
# explicit format request, target a size a few times the original instead of
# blindly using the default quality, without dropping below a quality floor
# that would introduce heavy blocking artifacts.
_JPX_SIZE_TARGET_FACTOR = 6
_JPX_SIZE_TARGET_FLOOR = 20_000
_JPX_MIN_JPEG_QUALITY = 35


class ConversionError(RuntimeError):
    """Raised when the PDF cannot be read or rendered."""


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name.strip())
    return slug.strip("-") or "document"


# An edge inset counts as a real margin only if it is at least this fraction
# of the page dimension; anything smaller is treated as a sliver
# (anti-aliasing, registration marks) and the page is left untrimmed.
_MARGINS_FRACTION = 0.01


def content_bbox(page: "pymupdf.Page"):
    """Union of the bounding boxes of all content on the page, or None if
    the page is blank.

    ``get_bboxlog`` returns a list of ``(kind, bbox)`` tuples; the union of
    those boxes is the extent of the page's content.
    """
    log = page.get_bboxlog()
    if not log:
        return None
    bbox = pymupdf.Rect(*log[0][1])
    for _, b in log[1:]:
        bbox |= pymupdf.Rect(*b)
    if not bbox.is_valid or bbox.is_empty or bbox.is_infinite:
        return None
    return bbox


def margin_insets(bbox: "pymupdf.Rect", rect: "pymupdf.Rect") -> tuple[int, int, int, int]:
    """Return (left, right, top, bottom) booleans flagging real margins.

    An inset counts as a real margin when it is at least
    ``_MARGINS_FRACTION`` of the corresponding page dimension.
    """
    left = rect.x0 < bbox.x0 - _MARGINS_FRACTION * rect.width
    right = rect.x1 > bbox.x1 + _MARGINS_FRACTION * rect.width
    top = rect.y0 < bbox.y0 - _MARGINS_FRACTION * rect.height
    bottom = rect.y1 > bbox.y1 + _MARGINS_FRACTION * rect.height
    return (1 if left else 0, 1 if right else 0, 1 if top else 0, 1 if bottom else 0)


def page_clip_rect(page: "pymupdf.Page"):
    """Per-page clip: the content bbox clamped to the page, or the page rect
    if the page is blank or has no real margins on every side."""
    rect = page.rect
    bbox = content_bbox(page)
    if bbox is None or not any(margin_insets(bbox, rect)):
        return rect
    clip = bbox & rect  # intersection, clamps the bbox within the page
    if clip.is_empty:
        return rect
    return clip


def global_clip_rects(
    doc: "pymupdf.Document", page_indices: "list[int] | None" = None,
) -> list["pymupdf.Rect"]:
    """One common clip rect for every page, safe for all of them.

    For a common clip to never cut into any page's content it must fully
    contain each page's content region; the union of the per-page content
    boxes is the smallest box with that property, hence the most aggressive
    safe common trim (and it yields a uniform size for all pages). Blank
    pages fall back to their own full rect.
    """
    indices = (
        page_indices if page_indices is not None else list(range(doc.page_count))
    )
    pages = [doc.load_page(i) for i in indices]
    per_page = [(page, content_bbox(page)) for page in pages]
    nonblank = [bbox for _, bbox in per_page if bbox is not None]
    if not nonblank:
        return [page.rect for page, _ in per_page]

    common = pymupdf.Rect(
        min(b.x0 for b in nonblank),
        min(b.y0 for b in nonblank),
        max(b.x1 for b in nonblank),
        max(b.y1 for b in nonblank),
    )
    clips = []
    for page, bbox in per_page:
        if bbox is None:
            clips.append(page.rect)
            continue
        clip = common & page.rect  # clamp the common box inside this page
        clips.append(clip if clip.width > 0 and clip.height > 0 else page.rect)
    return clips


def parse_page_selection(selection: str, page_count: int) -> list[int]:
    """Expand a print-style, 1-based page selection into PDF page indices.

    Items are comma-separated page numbers or inclusive ranges, for example
    ``"2,3,5-10,21"``. The requested order is preserved.
    """
    if not isinstance(selection, str) or not selection.strip():
        raise ConversionError(
            "pages must be a comma-separated list of page numbers or ranges"
        )

    indices: list[int] = []
    for item in selection.split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", item)
        if match is None:
            raise ConversionError(
                f"invalid page selection {selection!r}; "
                "use a format such as 2,3,5-10,21"
            )
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) is not None else first
        if first < 1 or last < 1:
            raise ConversionError("page numbers must be greater than or equal to 1")
        if last < first:
            raise ConversionError(f"page range must be ascending: {item.strip()!r}")
        if last > page_count:
            raise ConversionError(
                f"page {last} is out of range; PDF has {page_count} pages"
            )
        indices.extend(range(first - 1, last))
    return indices


def _clean(value: str) -> str:
    return " ".join(value.split()) if value else ""


def _w3cdtf(value: str) -> str:
    """Convert a PDF creation date (``D:YYYYMMDDHHmmSSz``) to W3CDTF, or
    ``""`` if it does not look parseable."""
    s = value.strip()
    if not s:
        return ""
    if s.startswith("D:"):
        s = s[2:]
    digits = s[:14]
    if len(digits) < 8 or not digits[:8].isdigit():
        return ""
    if len(digits) == 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) == 14 and digits[8:14].isdigit():
        return (
            f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
            f"T{digits[8:10]}:{digits[10:12]}:{digits[12:14]}"
        )
    return ""


def epub_metadata(doc: "pymupdf.Document") -> dict:
    """Transfer the PDF document metadata into an EPUB-friendly mapping.

    Only non-empty fields are included: ``title``, ``creator`` (from the PDF
    ``author``), ``subject``, ``keywords`` and ``date`` (from the PDF
    ``creationDate``, W3CDTF). Tooling fields (creator/producer) are
    deliberately skipped.
    """
    meta = doc.metadata or {}
    out: dict[str, str] = {}
    for key, field in (
        ("title", "title"),
        ("creator", "author"),
        ("subject", "subject"),
        ("keywords", "keywords"),
    ):
        if value := _clean(meta.get(field) or ""):
            out[key] = value
    for field in ("creationDate", "modDate"):
        if value := _w3cdtf(meta.get(field) or ""):
            out["date"] = value
            break
    return out


def _validate_image_options(image_format: str, quality: int) -> tuple[str, int]:
    """Return validated image format and JPEG quality options."""
    if image_format not in IMAGE_FORMATS:
        expected = ", ".join(repr(value) for value in IMAGE_FORMATS)
        raise ConversionError(
            f"image_format must be one of {expected}, got {image_format!r}"
        )
    if (
        isinstance(quality, bool)
        or not isinstance(quality, int)
        or not 1 <= quality <= 100
    ):
        raise ConversionError(f"quality must be an integer from 1 to 100, got {quality!r}")
    return image_format, quality


def _validate_mrc_color_scale(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConversionError(
            f"mrc_color_scale must be a positive integer, got {value!r}"
        )
    return value


def _jpeg_at_target_size(
    pixmap: "pymupdf.Pixmap", target_bytes: int, max_quality: int
) -> bytes:
    """Binary-search the highest JPEG quality up to *max_quality* fitting
    *target_bytes*, without going below ``_JPX_MIN_JPEG_QUALITY``."""
    floor_quality = min(_JPX_MIN_JPEG_QUALITY, max_quality)
    best: bytes | None = None
    low, high = floor_quality, max_quality
    while low <= high:
        mid = (low + high) // 2
        data = pixmap.tobytes("jpeg", jpg_quality=mid)
        if len(data) <= target_bytes:
            best = data
            low = mid + 1
        else:
            high = mid - 1
    return best if best is not None else pixmap.tobytes("jpeg", jpg_quality=floor_quality)


def _encode_pixmap(pix: "pymupdf.Pixmap", image_format: str, quality: int) -> bytes:
    """Encode *pix* according to the already validated image options."""
    if image_format == "png":
        return pix.tobytes("png")
    if image_format == "jpeg":
        return pix.tobytes("jpeg", jpg_quality=quality)

    png = pix.tobytes("png")
    if pix.alpha:
        return png
    jpeg = pix.tobytes("jpeg", jpg_quality=quality)
    return png if len(png) <= len(jpeg) * _AUTO_PNG_SIZE_RATIO else jpeg


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    """Return one length-prefixed, checksummed PNG chunk."""
    checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", checksum)
    )


def _png_grayscale_depth(source_bpc: int) -> int:
    """Map PDF bits-per-component to the smallest supported PNG depth."""
    for depth in _PNG_GRAYSCALE_DEPTHS:
        if source_bpc <= depth:
            return depth
    # MuPDF exposes 8-bit samples, so a higher-depth source cannot be written
    # losslessly at 16 bits from this Pixmap. Retain all available samples.
    return 8


def _encode_grayscale_png(
    pixmap: "pymupdf.Pixmap", source_bpc: int
) -> bytes:
    """Encode a grayscale Pixmap using the source PDF's minimal PNG depth.

    PyMuPDF's PNG encoder always writes an 8-bit grayscale PNG. PDF selector
    images commonly use one bit per component, so pack scanlines ourselves at
    PNG depths 1, 2, or 4. Resampling gray values are quantized to the number
    of levels available in the source; an 8-bit source is preserved verbatim.
    """
    if pixmap.colorspace is None or pixmap.colorspace.n != 1 or pixmap.alpha:
        raise ConversionError("MRC selector must be an opaque grayscale image")

    bit_depth = _png_grayscale_depth(source_bpc)
    raw = bytearray()
    samples = pixmap.samples
    if bit_depth == 8:
        for y in range(pixmap.height):
            start = y * pixmap.stride
            raw.append(0)  # PNG filter type: None
            raw.extend(samples[start:start + pixmap.width])
    else:
        levels = (1 << bit_depth) - 1
        alphabet = b"0123456789abcdef"
        quantized_digits = bytes(
            alphabet[(value * levels + 127) // 255]
            for value in range(256)
        )
        pixels_per_byte = 8 // bit_depth
        row_bytes = (pixmap.width + pixels_per_byte - 1) // pixels_per_byte
        padding = (-pixmap.width) % pixels_per_byte
        zero_padding = b"0" * padding
        base = 1 << bit_depth
        for y in range(pixmap.height):
            start = y * pixmap.stride
            row = samples[start:start + pixmap.width]
            digits = row.translate(quantized_digits) + zero_padding
            packed = int(digits, base).to_bytes(row_bytes, "big")
            raw.append(0)  # PNG filter type: None
            raw.extend(packed)

    header = struct.pack(
        ">IIBBBBB",
        pixmap.width,
        pixmap.height,
        bit_depth,
        0,  # grayscale color type
        0,  # compression method
        0,  # filter method
        0,  # no interlace
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), level=9))
        + _png_chunk(b"IEND", b"")
    )


def _pdf_image_bpc(doc: "pymupdf.Document", xref: int) -> int:
    """Return a PDF image's declared bits-per-component, conservatively."""
    try:
        kind, value = doc.xref_get_key(xref, "BitsPerComponent")
        if kind == "int":
            return max(1, int(value))
        image_mask_kind, image_mask = doc.xref_get_key(xref, "ImageMask")
        if image_mask_kind == "bool" and image_mask == "true":
            return 1
    except (TypeError, ValueError, RuntimeError):
        pass
    return 8


def _render_page_pixmap(
    page: "pymupdf.Page",
    resolution: int | None,
    clip: "pymupdf.Rect | None",
    transparent_background: bool,
    margins: tuple[int, int, int, int],
) -> "pymupdf.Pixmap":
    """Render *page* to an RGB(A) Pixmap, applying scale, clip and margins."""
    rect = clip if clip is not None else page.rect
    if resolution is not None:
        if resolution <= 0:
            raise ConversionError(f"resolution must be positive, got {resolution}")
        scale = resolution / rect.width
    else:
        scale = 1.0
    kwargs = {"alpha": transparent_background, "colorspace": pymupdf.csRGB}
    if clip is not None:
        kwargs["clip"] = clip
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), **kwargs)
    if any(margins):
        top, right, bottom, left = margins
        padded = pymupdf.Pixmap(
            pix.colorspace,
            pymupdf.IRect(0, 0, pix.width + left + right, pix.height + top + bottom),
            pix.alpha,
        )
        if transparent_background:
            padded.clear_with()
        else:
            padded.clear_with(255)
        pix.set_origin(left, top)
        padded.copy(pix, pix.irect)
        pix = padded
    return pix


def render_page(page: "pymupdf.Page", resolution: int | None = None,
                clip: "pymupdf.Rect | None" = None,
                transparent_background: bool = False,
                margins: "tuple[int, int, int, int] | None" = None,
                image_format: str = "png",
                quality: int = DEFAULT_JPEG_QUALITY) -> bytes:
    """Render one PDF page to a PNG or JPEG byte string.

    *resolution*, when given, is the target width in pixels; the height is
    scaled proportionally. *clip*, when given, is a ``Rect`` in page
    coordinates to render only that region (e.g. with margins trimmed).
    *margins* is an optional ``(top, right, bottom, left)`` tuple specifying
    padding in output pixels, applied after rendering and scaling.
    Unpainted page areas are white by default; set *transparent_background*
    to preserve them, including added margins, as transparent PNG pixels.
    *image_format* may be ``"png"``, ``"jpeg"`` or ``"auto"``. Auto encodes
    both formats and keeps lossless PNG when it is no more than 10% larger;
    otherwise it uses JPEG. *quality* is the JPEG quality from 1 to 100.
    """
    margins = _validate_margins(margins)
    image_format, quality = _validate_image_options(image_format, quality)
    if transparent_background and image_format == "jpeg":
        raise ConversionError("JPEG does not support a transparent background")
    pix = _render_page_pixmap(
        page, resolution, clip, transparent_background, margins
    )
    return _encode_pixmap(pix, image_format, quality)


def _otsu_threshold(histogram: list[int], pixel_count: int) -> int:
    """Choose a luminance threshold that best separates two pixel classes.

    A page with only one luminance has no foreground class. Returning one less
    than that value makes the generated selector empty instead of pointlessly
    selecting the whole page.
    """
    occupied = [value for value, count in enumerate(histogram) if count]
    if not occupied:
        return -1
    if len(occupied) == 1:
        return occupied[0] - 1

    weighted_total = sum(value * count for value, count in enumerate(histogram))
    lower_count = 0
    lower_sum = 0
    best_threshold = occupied[0]
    best_variance = -1.0
    for threshold, count in enumerate(histogram):
        lower_count += count
        if lower_count == 0:
            continue
        upper_count = pixel_count - lower_count
        if upper_count == 0:
            break
        lower_sum += threshold * count
        lower_mean = lower_sum / lower_count
        upper_mean = (weighted_total - lower_sum) / upper_count
        between_variance = (
            lower_count * upper_count * (lower_mean - upper_mean) ** 2
        )
        if between_variance > best_variance:
            best_variance = between_variance
            best_threshold = threshold
    return best_threshold


def _expanded_mrc_layer(
    source: Image.Image,
    mask: Image.Image,
    size: tuple[int, int],
    empty_fill: tuple[int, int, int],
) -> Image.Image:
    """Expand and smooth one MRC class using Pillow's native operations.

    Masked native downsampling produces the requested color-plane resolution.
    Repeated native blur-and-paste passes diffuse visible colors through the
    class mean. At full resolution, the final paste restores visible pixels
    verbatim; at lower resolutions it restores their area-weighted proxy.
    """
    if mask.getbbox() is None:
        return Image.new("RGB", size, empty_fill)

    mean = tuple(round(value) for value in ImageStat.Stat(source, mask).mean)
    visible = Image.new("RGBA", source.size)
    visible.paste(source, (0, 0), mask)
    if visible.size != size:
        visible = visible.resize(size, Image.Resampling.BOX)

    visible_rgb = visible.convert("RGB")
    visible_mask = visible.getchannel("A")
    expanded = Image.new("RGB", size, mean)
    expanded.paste(visible_rgb, (0, 0), visible_mask)
    blur = ImageFilter.BoxBlur(_MRC_HIDDEN_BLUR_RADIUS)
    for _ in range(_MRC_DIFFUSION_PASSES):
        expanded = expanded.filter(blur)
        expanded.paste(visible_rgb, (0, 0), visible_mask)
    return expanded


def _pillow_rgb_image(pixmap: "pymupdf.Pixmap") -> Image.Image:
    """Expose an opaque RGB Pixmap as a Pillow image, respecting its stride."""
    return Image.frombytes(
        "RGB",
        (pixmap.width, pixmap.height),
        pixmap.samples,
        "raw",
        "RGB",
        pixmap.stride,
        1,
    )


def _split_mrc_pixmap(
    pixmap: "pymupdf.Pixmap",
    color_scale: int = DEFAULT_MRC_COLOR_SCALE,
) -> tuple["pymupdf.Pixmap", "pymupdf.Pixmap", "pymupdf.Pixmap"]:
    """Split an opaque RGB render into background, foreground and selector.

    Otsu luminance segmentation is intentionally global and deterministic: it
    handles the common dark-text/light-paper scan without adding another image
    processing dependency, while remaining valid for every opaque rendered
    page. Each color class is expanded and blurred through the other class's
    hidden pixels, removing the selector-shaped edge that would otherwise be
    expensive to compress. Scale 1 PNG color planes reconstruct the render
    exactly; downsampled planes and JPEG trade fidelity for size and speed.
    """
    if (
        pixmap.colorspace is None
        or pixmap.colorspace.n != 3
        or pixmap.alpha
    ):
        raise ConversionError("generated MRC requires an opaque RGB page render")
    color_scale = _validate_mrc_color_scale(color_scale)

    width, height = pixmap.width, pixmap.height
    source = _pillow_rgb_image(pixmap)
    grayscale = source.convert("L")
    threshold = _otsu_threshold(grayscale.histogram(), width * height)
    selector_image = grayscale.point(
        [255 if value <= threshold else 0 for value in range(256)], "L"
    )
    plane_size = (
        max(1, (width + color_scale - 1) // color_scale),
        max(1, (height + color_scale - 1) // color_scale),
    )
    foreground_image = _expanded_mrc_layer(
        source, selector_image, plane_size, (0, 0, 0)
    )
    background_image = _expanded_mrc_layer(
        source, ImageOps.invert(selector_image), plane_size, (255, 255, 255)
    )

    background = pymupdf.Pixmap(
        pymupdf.csRGB, plane_size[0], plane_size[1], background_image.tobytes(), False
    )
    foreground = pymupdf.Pixmap(
        pymupdf.csRGB, plane_size[0], plane_size[1], foreground_image.tobytes(), False
    )
    selector = pymupdf.Pixmap(
        pymupdf.csGRAY, width, height, selector_image.tobytes(), False
    )
    return background, foreground, selector


def _rendered_mrc_page(
    pixmap: "pymupdf.Pixmap",
    image_format: str,
    quality: int,
    svg_mask: bool = True,
    color_scale: int = DEFAULT_MRC_COLOR_SCALE,
) -> PageContent:
    """Encode one rendered Pixmap as a two-color-layer MRC page."""
    background, foreground, selector = _split_mrc_pixmap(pixmap, color_scale)
    background_data = _encode_pixmap(background, image_format, quality)
    background_extension = _image_info("", background_data)[0]

    if svg_mask:
        foreground_data = _encode_pixmap(foreground, image_format, quality)
        foreground_extension = _image_info("", foreground_data)[0]
        selector_data = _encode_grayscale_png(selector, 1)
        return PageContent(
            (
                (f"background.{background_extension}", background_data),
                (f"foreground.{foreground_extension}", foreground_data),
                ("mask.png", selector_data),
            ),
            viewport=(float(pixmap.width), float(pixmap.height)),
        )

    # EPUB 2 cannot apply a separate SVG mask reliably. Store the foreground
    # as a transparent PNG overlay, matching extracted MRC compatibility mode.
    overlay_selector = selector
    if (selector.width, selector.height) != (foreground.width, foreground.height):
        selector_image = Image.frombytes(
            "L", (selector.width, selector.height), selector.samples
        ).resize((foreground.width, foreground.height), Image.Resampling.BOX)
        overlay_selector = pymupdf.Pixmap(
            pymupdf.csGRAY,
            foreground.width,
            foreground.height,
            selector_image.tobytes(),
            False,
        )
    foreground_alpha = pymupdf.Pixmap(foreground, overlay_selector)
    foreground_alpha.set_alpha(overlay_selector.samples, premultiply=1)
    foreground_data = foreground_alpha.tobytes("png")
    return PageContent(
        (
            (f"background.{background_extension}", background_data),
            ("foreground.png", foreground_data),
        ),
        viewport=(float(pixmap.width), float(pixmap.height)),
    )


def _validate_margins(
    margins: "tuple[int, int, int, int] | None",
) -> tuple[int, int, int, int]:
    """Return validated ``(top, right, bottom, left)`` pixel margins."""
    if margins is None:
        return (0, 0, 0, 0)
    try:
        values = tuple(margins)
    except TypeError as exc:
        raise ConversionError(
            "margins must contain top, right, bottom and left values"
        ) from exc
    if len(values) != 4:
        raise ConversionError("margins must contain top, right, bottom and left values")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in values):
        raise ConversionError("margins must be integers greater than or equal to 0")
    return values


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Read JPEG dimensions from its start-of-frame segment."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ValueError("missing JPEG start marker")
    start_of_frame = {
        0xC0, 0xC1, 0xC2, 0xC3,
        0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB,
        0xCD, 0xCE, 0xCF,
    }
    pos = 2
    while pos < len(data):
        while pos < len(data) and data[pos] != 0xFF:
            pos += 1
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker in (0x01, 0xD8, 0xD9):
            continue
        if marker == 0xDA or pos + 2 > len(data):
            break
        segment_length = struct.unpack(">H", data[pos:pos + 2])[0]
        if segment_length < 2 or pos + segment_length > len(data):
            break
        if marker in start_of_frame:
            if segment_length < 7:
                break
            height, width = struct.unpack(">HH", data[pos + 3:pos + 7])
            if width and height:
                return width, height
            break
        pos += segment_length
    raise ValueError("missing JPEG dimensions")


def _image_info(name: str, data: bytes) -> tuple[str, str, int, int]:
    """Return extension, media type, width and height for PNG/JPEG data."""
    if (
        len(data) >= 24
        and data[:8] == b"\x89PNG\r\n\x1a\n"
        and data[12:16] == b"IHDR"
    ):
        extension = "png"
        media_type = "image/png"
        width, height = struct.unpack(">II", data[16:24])
    elif data[:2] == b"\xff\xd8":
        extension = "jpg"
        media_type = "image/jpeg"
        try:
            width, height = _jpeg_dimensions(data)
        except ValueError as exc:
            raise ConversionError(f"page image is not a valid JPEG: {name}") from exc
    else:
        raise ConversionError(f"page image is not a valid PNG or JPEG: {name}")

    suffix = Path(name).suffix.lower()
    expected_suffixes = {".png"} if extension == "png" else {".jpg", ".jpeg"}
    if suffix and suffix not in expected_suffixes:
        raise ConversionError(f"page image extension does not match its data: {name}")
    return extension, media_type, width, height


def _normalized_image_extension(extension: str) -> str | None:
    extension = extension.lower()
    if extension in ("jpg", "jpeg"):
        return "jpg"
    if extension == "png":
        return "png"
    return None


def _lossless_png(pixmap: "pymupdf.Pixmap", source_bpc: int) -> bytes:
    """Encode all samples available from *pixmap* without lossy compression."""
    if pixmap.colorspace is not None and pixmap.colorspace.n == 1 and not pixmap.alpha:
        if source_bpc > 8:
            raise ConversionError(
                "cannot losslessly transcode a greater-than-8-bit grayscale PDF "
                "image after decoding"
            )
        return _encode_grayscale_png(pixmap, source_bpc)
    if pixmap.colorspace is None or pixmap.colorspace.n not in (1, 3):
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
    if source_bpc > 8:
        raise ConversionError(
            "cannot losslessly transcode a greater-than-8-bit color PDF image "
            "with PyMuPDF's 8-bit Pixmap API"
        )
    return pixmap.tobytes("png")


def _decoded_pdf_image(
    doc: "pymupdf.Document",
    xref: int,
    target_size: tuple[int, int] | None,
) -> "pymupdf.Pixmap":
    pixmap = pymupdf.Pixmap(doc, xref)
    if target_size is not None and (pixmap.width, pixmap.height) != target_size:
        pixmap = pymupdf.Pixmap(pixmap, target_size[0], target_size[1])
    return pixmap


def _extracted_pixels_match_pdf(
    doc: "pymupdf.Document", xref: int, data: bytes
) -> bool:
    """Whether an extracted image already includes the PDF image semantics."""
    try:
        extracted = pymupdf.Pixmap(data)
        effective = pymupdf.Pixmap(doc, xref)
    except Exception:
        return False
    return (
        extracted.width == effective.width
        and extracted.height == effective.height
        and extracted.n == effective.n
        and extracted.samples == effective.samples
    )


def _pdf_image_for_epub(
    doc: "pymupdf.Document",
    xref: int,
    target_size: tuple[int, int] | None,
    image_format: str | None,
    quality: int,
) -> ImageData:
    """Reuse a PDF image when possible, otherwise minimally adapt it.

    JPEG and PNG resources are copied byte-for-byte when no resize or explicit
    format conversion is required. Unsupported PDF codecs are decoded once and
    encoded losslessly unless the caller explicitly selected JPEG or auto.
    JPEG 2000 (``/JPXDecode``) is the one exception: it is already lossy, so
    losslessly preserving its decoded samples as PNG only inflates the output
    without any fidelity benefit. It is re-encoded as JPEG instead, targeting
    a size a few times the original compressed bytes (bounded by a quality
    floor) rather than a fixed high quality, since MRC layers already accept
    the source PDF's own lossy compression level.
    """
    try:
        source = doc.extract_image(xref)
        source_data = source["image"]
        source_ext = source.get("ext", "")
        source_extension = _normalized_image_extension(source_ext)
        source_size = (int(source["width"]), int(source["height"]))
        source_bpc = int(source.get("bpc") or _pdf_image_bpc(doc, xref))
    except Exception as exc:
        raise ConversionError(f"could not extract PDF image xref {xref}: {exc}") from exc

    resize_required = target_size is not None and target_size != source_size
    requested_extension = "jpg" if image_format == "jpeg" else image_format
    reuse_source = (
        not resize_required
        and source_extension is not None
        and (
            image_format in (None, "auto")
            or requested_extension == source_extension
        )
        and _extracted_pixels_match_pdf(doc, xref, source_data)
    )
    if reuse_source:
        _image_info(f"source.{source_extension}", source_data)
        return f"image.{source_extension}", source_data

    pixmap = _decoded_pdf_image(doc, xref, target_size)
    source_is_lossy = source_ext.lower() in ("jpx", "jp2")
    if image_format == "jpeg":
        if pixmap.colorspace is None or pixmap.colorspace.n not in (1, 3):
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
        data = pixmap.tobytes("jpeg", jpg_quality=quality)
    elif image_format is None and source_is_lossy:
        if pixmap.colorspace is None or pixmap.colorspace.n not in (1, 3):
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
        target_bytes = max(len(source_data) * _JPX_SIZE_TARGET_FACTOR, _JPX_SIZE_TARGET_FLOOR)
        data = _jpeg_at_target_size(pixmap, target_bytes, quality)
    elif image_format == "auto":
        if pixmap.colorspace is None or pixmap.colorspace.n not in (1, 3):
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
        data = _encode_pixmap(pixmap, "auto", quality)
    else:
        data = _lossless_png(pixmap, source_bpc)
    extension = _image_info("", data)[0]
    return f"image.{extension}", data


def _pdf_mask_for_epub(
    doc: "pymupdf.Document",
    xref: int,
    target_size: tuple[int, int] | None,
) -> bytes:
    """Reuse a PDF selector PNG, or resize it at its source bit depth."""
    try:
        source = doc.extract_image(xref)
        source_data = source["image"]
        source_size = (int(source["width"]), int(source["height"]))
        source_bpc = int(source.get("bpc") or _pdf_image_bpc(doc, xref))
    except Exception as exc:
        raise ConversionError(f"could not extract PDF mask xref {xref}: {exc}") from exc

    resize_required = target_size is not None and target_size != source_size
    if not resize_required and _normalized_image_extension(source.get("ext", "")) == "png":
        extension, _, width, height = _image_info("mask.png", source_data)
        if extension == "png" and (width, height) == source_size:
            # Verify both storage depth and effective PDF samples. A /Decode
            # array or color-space mapping can make raw extracted pixels differ.
            saved_depth = source_data[24]
            expected_depth = source_bpc if source_bpc in (1, 2, 4, 8, 16) else 8
            if (
                saved_depth == expected_depth
                and source_data[25] == 0
                and _extracted_pixels_match_pdf(doc, xref, source_data)
            ):
                return source_data

    if source_bpc > 8:
        raise ConversionError(
            "cannot resize a greater-than-8-bit PDF selector without reducing depth"
        )
    mask = _decoded_pdf_image(doc, xref, target_size)
    return _encode_grayscale_png(mask, source_bpc)


def _rect_matches_page(rect: "pymupdf.Rect", page_rect: "pymupdf.Rect") -> bool:
    """Whether *rect* covers a page, allowing tiny PDF coordinate rounding."""
    tolerance = max(page_rect.width, page_rect.height) * 1e-4
    return (
        abs(rect.x0 - page_rect.x0) <= tolerance
        and abs(rect.y0 - page_rect.y0) <= tolerance
        and abs(rect.x1 - page_rect.x1) <= tolerance
        and abs(rect.y1 - page_rect.y1) <= tolerance
    )


def _mrc_page_layers(
    page: "pymupdf.Page",
    doc: "pymupdf.Document",
    resolution: int | None,
    image_format: str | None,
    quality: int,
    svg_mask: bool = True,
) -> PageContent | None:
    """Extract a conservative two-layer MRC page, or return ``None``.

    The supported form is a full-page background followed by a full-page RGB
    foreground image with a grayscale soft mask. Other page content is only
    accepted when it is invisible OCR text (``ignore-text`` in MuPDF's bbox
    log). This prevents extraction from silently dropping visible vectors,
    annotations, clipping, or additional images.
    """
    refs = page.get_images(full=True)
    if len(refs) != 2:
        return None
    background_ref, foreground_ref = refs
    if background_ref[1] != 0 or foreground_ref[1] <= 0:
        return None

    visual_kinds = {
        kind for kind, *_ in page.get_bboxlog() if kind != "ignore-text"
    }
    if visual_kinds != {"fill-image"}:
        return None

    page_rect = page.rect
    for ref in refs:
        try:
            placements = page.get_image_rects(ref[0], transform=True)
        except Exception:
            return None
        if len(placements) != 1:
            return None
        rect, matrix = placements[0]
        if not _rect_matches_page(rect, page_rect):
            return None
        # Layer extraction below preserves the pixel orientation, so reject
        # rotations, shears, and mirrored placements for now.
        tolerance = 1e-5
        if abs(matrix.b) > tolerance or abs(matrix.c) > tolerance:
            return None
        if matrix.a <= 0 or matrix.d <= 0:
            return None

    target_size = None
    if resolution is not None:
        target_size = (
            resolution,
            max(1, round(resolution * page_rect.height / page_rect.width)),
        )

    background_name, background_data = _pdf_image_for_epub(
        doc, background_ref[0], target_size, image_format, quality
    )
    foreground_name, foreground_data = _pdf_image_for_epub(
        doc, foreground_ref[0], target_size, image_format, quality
    )
    background_extension = Path(background_name).suffix.lstrip(".")
    foreground_extension = Path(foreground_name).suffix.lstrip(".")

    words = tuple(
        (float(x0), float(y0), float(x1), float(y1), str(text))
        for x0, y0, x1, y1, text, *_ in page.get_text("words")
        if str(text)
    )
    viewport = (float(page_rect.width), float(page_rect.height))

    if svg_mask:
        mask_data = _pdf_mask_for_epub(doc, foreground_ref[1], target_size)
        return PageContent(
            (
                (f"background.{background_extension}", background_data),
                (f"foreground.{foreground_extension}", foreground_data),
                ("mask.png", mask_data),
            ),
            viewport=viewport,
            ocr_words=words,
        )

    # EPUB 2 cannot reliably apply a separate SVG selector, so combining the
    # native foreground and mask into one transparent PNG is the only required
    # reconstruction in this compatibility mode.
    foreground = _decoded_pdf_image(doc, foreground_ref[0], target_size)
    mask = _decoded_pdf_image(doc, foreground_ref[1], target_size)
    if (foreground.width, foreground.height) != (mask.width, mask.height):
        raise ConversionError(
            "EPUB 2 MRC extraction requires matching foreground and mask dimensions"
        )
    foreground_alpha = pymupdf.Pixmap(foreground, mask)
    foreground_alpha.set_alpha(mask.samples, premultiply=1)
    foreground_png = foreground_alpha.tobytes("png")
    _image_info("", foreground_png)
    return PageContent(
        (
            (f"background.{background_extension}", background_data),
            ("foreground.png", foreground_png),
        ),
        viewport=viewport,
        ocr_words=words,
    )


class _EpubWriter:
    """Minimal EPUB writer: one XHTML section and image resources per page.

    The spine is built from the XHTML sections (never directly from images),
    which strict readers require.
    """

    def __init__(self, path: Path, metadata: dict,
                 cover: "tuple[str, bytes] | None" = None,
                 epub2: bool = False):
        self.path = Path(path)
        self.metadata = metadata
        self.cover = cover
        self.epub2 = epub2
        self.pages: list[PageContent] = []
        self.uuid = str(uuid.uuid4())
        self.modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._tmp = self.path.with_suffix(self.path.suffix + ".tmp")

    def add_image(self, name: str, data: bytes):
        self.add_layered_page(((name, data),))

    def add_layered_page(
        self,
        layers: PageLayers,
        viewport: tuple[float, float] | None = None,
        ocr_words: tuple[OcrWord, ...] = (),
    ):
        if not layers:
            raise ConversionError("a page must contain at least one image layer")
        names = [name for page in self.pages for name, _ in page.layers]
        if any(name in names for name, _ in layers):
            raise ConversionError("image layer names must be unique in an EPUB")
        self.pages.append(PageContent(tuple(layers), viewport, tuple(ocr_words)))

    @property
    def page_layers(self) -> list[PageLayers]:
        return [page.layers for page in self.pages]

    @property
    def image_names(self) -> list[str]:
        """Compatibility view of the first image name on every page."""
        return [layers[0][0] for layers in self.page_layers]

    @property
    def images(self) -> list[bytes]:
        """Compatibility view of the first image data on every page."""
        return [layers[0][1] for layers in self.page_layers]

    @property
    def _section_ids(self) -> list[str]:
        return [f"sec-{i:04d}" for i in range(len(self.pages))]

    @staticmethod
    def _image_id(page_index: int, layer_index: int, layer_count: int) -> str:
        if layer_count == 1:
            return f"img-{page_index:04d}"
        return f"img-{page_index:04d}-{layer_index:02d}"

    def _container_xml(self) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            "<rootfiles>"
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>"
        )

    def _metadata_xml(self) -> str:
        # Order follows the OPF/DC convention; only fields present are emitted.
        parts = [f'<dc:identifier id="epubid">{self.uuid}</dc:identifier>']
        if "title" in self.metadata:
            parts.append(f"<dc:title>{escape(self.metadata['title'])}</dc:title>")
        if "creator" in self.metadata:
            parts.append(f"<dc:creator>{escape(self.metadata['creator'])}</dc:creator>")
        if "subject" in self.metadata:
            parts.append(f"<dc:subject>{escape(self.metadata['subject'])}</dc:subject>")
        if "date" in self.metadata:
            parts.append(f"<dc:date>{escape(self.metadata['date'])}</dc:date>")
        parts.append("<dc:language>en</dc:language>")
        if self.epub2:
            if self.cover is not None:
                parts.append('<meta name="cover" content="cover"/>')
        else:
            parts.extend(
                (
                    f'<meta property="dcterms:modified">{self.modified}</meta>',
                    '<meta property="rendition:layout">pre-paginated</meta>',
                    '<meta property="rendition:orientation">auto</meta>',
                    '<meta property="rendition:spread">none</meta>',
                )
            )
        if "keywords" in self.metadata:
            parts.append(
                f'<meta name="keywords" content="{escape(self.metadata["keywords"])}"/>'
            )
        return "".join(parts)

    def _opf_xml(self) -> str:
        manifest = '<item id="ncx" media-type="application/x-dtbncx+xml" href="toc.ncx"/>'
        if not self.epub2:
            manifest += (
                '<item id="nav" media-type="application/xhtml+xml" '
                'properties="nav" href="nav.xhtml"/>'
            )
        page_items = "".join(
            f'<item id="{sid}" media-type="application/xhtml+xml" '
            f'href="page-{i:04d}.xhtml"/>'
            for i, sid in enumerate(self._section_ids, start=1)
        )
        image_items = []
        for page_index, layers in enumerate(self.page_layers, start=1):
            for layer_index, (name, data) in enumerate(layers, start=1):
                image_items.append(
                    f'<item id="{self._image_id(page_index, layer_index, len(layers))}" '
                    f'media-type="{_image_info(name, data)[1]}" '
                    f'href="images/{name}"/>'
                )
        manifest += page_items + "".join(image_items)
        if self.cover is not None:
            cover_name, cover_data = self.cover
            cover_media_type = _image_info(cover_name, cover_data)[1]
            properties = "" if self.epub2 else ' properties="cover-image"'
            manifest += (
                f'<item id="cover" media-type="{cover_media_type}"{properties} '
                f'href="images/{cover_name}"/>'
            )
        spine = "".join(
            f'<itemref idref="{sid}"/>' for sid in self._section_ids
        )
        version = "2.0" if self.epub2 else "3.0"
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<package xmlns="http://www.idpf.org/2007/opf" version="{version}" '
            'unique-identifier="epubid">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"{self._metadata_xml()}"
            "</metadata>"
            f"<manifest>{manifest}</manifest>"
            f'<spine toc="ncx">{spine}</spine></package>'
        )

    def _ncx_xml(self) -> str:
        nav = "".join(
            f'<navPoint id="nav-{i:04d}" playOrder="{i}">'
            f'<navLabel><text>{i}. Page {i}</text></navLabel>'
            f'<content src="page-{i:04d}.xhtml"/>'
            "</navPoint>"
            for i in range(1, len(self.page_layers) + 1)
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
            f'<head><meta name="dtb:uid" content="{self.uuid}"/></head>'
            f"<docTitle><text>{escape(self.metadata.get('title', 'Document'))}</text></docTitle>"
            f"<navMap>{nav}</navMap></ncx>"
        )

    def _nav_xhtml(self) -> str:
        toc = "".join(
            f'<li><a href="page-{i:04d}.xhtml">Page {i}</a></li>'
            for i in range(1, len(self.page_layers) + 1)
        )
        page_list = "".join(
            f'<li><a href="page-{i:04d}.xhtml">{i}</a></li>'
            for i in range(1, len(self.page_layers) + 1)
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE html>'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">'
            '<head><title>Navigation</title></head><body>'
            '<nav epub:type="toc" id="toc"><h1>Contents</h1>'
            f'<ol>{toc}</ol></nav>'
            '<nav epub:type="page-list" id="page-list"><h2>Pages</h2>'
            f'<ol>{page_list}</ol></nav>'
            '</body></html>'
        )

    def _section_xhtml(
        self,
        index: int,
        layers: PageLayers,
        viewport: tuple[float, float] | None = None,
        ocr_words: tuple[OcrWord, ...] = (),
    ) -> str:
        names = [name for name, _ in layers]
        image_info = [_image_info(name, data) for name, data in layers]
        masked_mrc = (
            len(names) == 3
            and Path(names[0]).stem.endswith("-background")
            and Path(names[1]).stem.endswith("-foreground")
            and Path(names[2]).stem.endswith("-mask")
        )
        mrc_page = (
            len(names) in (2, 3)
            and Path(names[0]).stem.endswith("-background")
            and Path(names[1]).stem.endswith("-foreground")
        )
        if viewport is None:
            width, height = image_info[0][2], image_info[0][3]
        else:
            width, height = viewport
        if not mrc_page and any(
            info[2:] != image_info[0][2:] for info in image_info[1:]
        ):
            raise ConversionError(f"image layers have mismatched dimensions on page {index}")
        if masked_mrc and self.epub2:
            raise ConversionError("SVG-masked MRC pages require EPUB 3")

        def number(value: float) -> str:
            return f"{value:.4f}".rstrip("0").rstrip(".")

        width_text, height_text = number(width), number(height)
        image_tags = []
        for layer_index, name in enumerate(names):
            alt = f"Page {index}" if layer_index == 0 else ""
            image_tags.append(
                f'<img src="images/{escape(name)}" alt="{alt}"/>'
            )
        images = "".join(image_tags)
        if self.epub2:
            ocr_spans = "".join(
                '<span class="ocr" '
                f'style="left:{number(100 * x0 / width)}%;'
                f'top:{number(100 * y0 / height)}%;'
                f'width:{number(100 * max(0.0, x1 - x0) / width)}%;'
                f'height:{number(100 * max(0.0, y1 - y0) / height)}%;">'
                f'{escape(text)}</span>'
                for x0, y0, x1, y1, text in ocr_words
            )
            css = (
                "html,body,.page{margin:0;padding:0;width:100%;height:100%;overflow:hidden;}"
                ".page{position:relative;}"
                "img{position:absolute;left:0;top:0;display:block;width:100%;height:100%;}"
                ".ocr{position:absolute;color:transparent;white-space:pre;overflow:hidden;}"
            )
            return (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
                '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" '
                'xml:lang="en">'
                f"<head><title>Page {index}</title>"
                f'<style type="text/css">{css}</style></head><body>'
                '<div class="page">'
                f"{images}{ocr_spans}"
                "</div></body></html>"
            )
        if masked_mrc:
            background_name, foreground_name, mask_name = names
            ocr_text = "".join(
                f'<text x="{number(x0)}" y="{number(y1)}" '
                f'font-size="{number(max(0.1, y1 - y0))}" '
                f'textLength="{number(max(0.1, x1 - x0))}" '
                'lengthAdjust="spacingAndGlyphs">'
                f'{escape(text)}</text>'
                for x0, y0, x1, y1, text in ocr_words
            )
            ocr_group = (
                f'<g class="ocr" fill-opacity="0">{ocr_text}</g>'
                if ocr_text else ""
            )
            css = (
                "html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden;}"
                "svg{display:block;width:100%;height:100%;}"
            )
            page_content = (
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'xmlns:xlink="http://www.w3.org/1999/xlink" '
                f'viewBox="0 0 {width_text} {height_text}" preserveAspectRatio="none" '
                f'role="img" aria-label="Page {index}">'
                '<defs><mask id="mrc-selector" maskUnits="userSpaceOnUse" '
                f'x="0" y="0" width="{width_text}" height="{height_text}">'
                f'<image x="0" y="0" width="{width_text}" height="{height_text}" '
                f'xlink:href="images/{escape(mask_name)}"/>'
                '</mask></defs>'
                f'<image x="0" y="0" width="{width_text}" height="{height_text}" '
                f'xlink:href="images/{escape(background_name)}"/>'
                f'<image x="0" y="0" width="{width_text}" height="{height_text}" '
                'mask="url(#mrc-selector)" '
                f'xlink:href="images/{escape(foreground_name)}"/>'
                f'{ocr_group}'
                '</svg>'
            )
        elif len(layers) == 1:
            css = (
                "html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden;}"
                "img{display:block;width:100%;height:100%;}"
            )
            page_content = images
        else:
            css = (
                "html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden;}"
                ".page{position:relative;width:100%;height:100%;}"
                "img{position:absolute;left:0;top:0;display:block;width:100%;height:100%;}"
            )
            page_content = f'<div class="page">{images}</div>'
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE html>'
            '<html xmlns="http://www.w3.org/1999/xhtml" lang="en" '
            'xml:lang="en" xmlns:epub="http://www.idpf.org/2007/ops">'
            f"<head><title>Page {index}</title>"
            f'<meta name="viewport" content="width={width_text}, height={height_text}"/>'
            f'<style type="text/css">{css}</style></head>'
            '<body epub:type="bodymatter">'
            f'{page_content}'
            "</body></html>"
        )

    def build(self) -> None:
        buf = BytesIO()
        with ZipFile(buf, "w", ZIP_DEFLATED) as z:
            z.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
            z.writestr("META-INF/container.xml", self._container_xml())
            z.writestr("OEBPS/content.opf", self._opf_xml())
            z.writestr("OEBPS/toc.ncx", self._ncx_xml())
            if not self.epub2:
                z.writestr("OEBPS/nav.xhtml", self._nav_xhtml())
            for i, page in enumerate(self.pages, start=1):
                z.writestr(
                    f"OEBPS/page-{i:04d}.xhtml",
                    self._section_xhtml(
                        i, page.layers, page.viewport, page.ocr_words
                    ),
                )
            for page in self.pages:
                for name, data in page.layers:
                    z.writestr(f"OEBPS/images/{name}", data)
            if self.cover is not None:
                z.writestr(f"OEBPS/images/{self.cover[0]}", self.cover[1])

        self._tmp.write_bytes(buf.getvalue())
        self._tmp.replace(self.path)


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024


def convert(input_path: Path, output_path: Path | None = None,
            resolution: int | None = None, crop: str | None = None,
            cover: bool = True, log=None, progress=None,
            epub2: bool = False,
            transparent_background: bool = False,
            margins: "tuple[int, int, int, int] | None" = None,
            image_format: str | None = None,
            quality: int = DEFAULT_JPEG_QUALITY,
            mrc_extract: bool = False,
            pages: str | None = None,
            mrc: bool = False,
            mrc_color_scale: int = DEFAULT_MRC_COLOR_SCALE,
            title: str | None = None,
            author: str | None = None) -> Path:
    """Convert *input_path* (a PDF) to an EPUB and return the output path.

    Every PDF page is rendered to an image and placed in its own EPUB section
    in document order. The output file defaults to the input name with the
    extension swapped to ``.epub``. *resolution*, when given, is the target
    render width in pixels. *crop*, when given, trims white margins:
    ``"global"`` uses one common inset for all pages, ``"page"`` trims each
    page to its own content.

    *pages*, when given, is a print-style, 1-based page selection such as
    ``"2,3,5-10,21"``. Ranges are inclusive and requested order is preserved.

    *margins*, when given, contains the top, right, bottom and left padding
    to add in output pixels after each page is rendered.

    When *cover* is true (the default) the first page also becomes the book's
    cover image. EPUB 3 fixed-layout output is the default; when *epub2* is
    true, the output uses EPUB 2 markup for compatibility with older readers.
    Unpainted page areas are rendered white unless *transparent_background*
    is true.

    *image_format* may be ``"png"``, ``"jpeg"`` or ``"auto"``. When omitted,
    normal pages use PNG and MRC extraction preserves EPUB-compatible source
    resources while losslessly adapting unsupported codecs to PNG. Auto keeps
    PNG when it is no more than 10% larger than JPEG, otherwise it uses JPEG.
    *quality* controls JPEG encoding from 1 to 100.
    When *mrc* is true, every normally rendered page is split into background
    and foreground color layers selected by a lossless 1-bit mask. This also
    applies to pages where *mrc_extract* cannot safely reuse source layers.
    *mrc_color_scale* downsamples those color layers by that integer factor;
    the selector remains at full resolution. Use 1 for full-resolution planes.
    When *mrc_extract* is true, canonical two-layer MRC pages are extracted
    into background and foreground images selected by a lossless mask. Pages
    that do not match the safe extraction pattern use the normal renderer.

    When *title* or *author* is given, it overrides the corresponding PDF
    metadata field independently. *author* is written as the EPUB creator.

    *log* (optional callable(str)) receives descriptive lines (input info,
    output result). *progress* (optional callable(done, total)) is called for
    every rendered page.
    """
    if crop is not None and crop not in ("global", "page"):
        raise ConversionError(f"crop must be 'global' or 'page', got {crop!r}")
    margins = _validate_margins(margins)
    requested_image_format = image_format
    effective_image_format, quality = _validate_image_options(
        image_format or "png", quality
    )
    mrc_color_scale = _validate_mrc_color_scale(mrc_color_scale)
    if transparent_background and effective_image_format == "jpeg":
        raise ConversionError("JPEG does not support a transparent background")
    if mrc and transparent_background:
        raise ConversionError("generated MRC does not support a transparent background")

    def _log(message: str) -> None:
        if log is not None:
            log(message)

    def _progress(done: int, total: int) -> None:
        if progress is not None:
            progress(done, total)

    started = time.monotonic()

    input_path = Path(input_path)
    if not input_path.exists():
        raise ConversionError(f"input file not found: {input_path}")
    if output_path is None:
        output_path = input_path.with_suffix(".epub")
    output_path = Path(output_path)

    input_size = input_path.stat().st_size
    _log(f"input:   {input_path}  ({format_size(input_size)})")

    try:
        doc = pymupdf.open(str(input_path))
    except Exception as exc:
        raise ConversionError(f"could not open PDF: {exc}") from exc

    try:
        total_page_count = doc.page_count
        if total_page_count == 0:
            raise ConversionError(f"PDF has no pages: {input_path}")
        page_indices = (
            parse_page_selection(pages, total_page_count)
            if pages is not None else list(range(total_page_count))
        )
        page_count = len(page_indices)
        metadata = epub_metadata(doc)
        metadata.setdefault("title", slugify(input_path.stem))
        if title is not None:
            metadata["title"] = title
        if author is not None:
            metadata["creator"] = author
        pages_list = [doc.load_page(i) for i in page_indices]

        first = pages_list[0]
        w, h = first.rect.width, first.rect.height
        uniform = all(
            p.rect.width == w and p.rect.height == h for p in pages_list[1:]
        )
        native = f"{w:.0f} x {h:.0f} px" + ("" if uniform else " (varies)")
        selection_description = (
            f"{page_count} selected of {total_page_count}"
            if pages is not None else str(page_count)
        )
        _log(f"pages:   {selection_description}  (native resolution: {native})")
        if crop == "global":
            clips = global_clip_rects(doc, page_indices)
        elif crop == "page":
            clips = [page_clip_rect(p) for p in pages_list]
        else:
            clips = [None] * page_count
        if resolution is not None:
            _log(f"output:  {output_path}  ({resolution} px wide)")
        else:
            _log(f"output:  {output_path}  (native resolution)")
        _log(f"format:  EPUB {2 if epub2 else 3}")
        if requested_image_format is None and mrc_extract:
            image_description = "SOURCE-PRESERVING (lossless PNG/lossy JPEG fallback)"
        else:
            image_description = effective_image_format.upper()
        if effective_image_format in ("jpeg", "auto"):
            image_description += f" (JPEG quality {quality})"
        _log(f"images:  {image_description}")
        if mrc_extract:
            if crop is None and not any(margins) and not transparent_background:
                _log("mrc:     extraction enabled (non-MRC pages use rendering)")
            else:
                _log("mrc:     extraction skipped with crop, margins, or transparency")
        if mrc:
            _log("mrc:     generation enabled for rendered pages")
            _log(f"mrc:     color-plane scale 1/{mrc_color_scale}")
        if any(margins):
            _log(f"margins: {margins[0]} {margins[1]} {margins[2]} {margins[3]} px")
        if crop is not None:
            trimmed = sum(
                1 for p, c in zip(pages_list, clips) if c is not None and c != p.rect
            )
            _log(f"crop:    {crop}  ({trimmed} of {page_count} pages trimmed)")

        rendered: list[PageContent] = []
        extracted_count = 0
        generated_count = 0
        cover_image_for_layers: bytes | None = None
        extraction_allowed = (
            mrc_extract
            and crop is None
            and not any(margins)
            and not transparent_background
        )
        for i, (page, clip_raw) in enumerate(zip(pages_list, clips)):
            clip = None if (clip_raw is None or clip_raw == page.rect) else clip_raw
            extracted_page = (
                _mrc_page_layers(
                    page, doc, resolution, requested_image_format, quality,
                    svg_mask=not epub2,
                )
                if extraction_allowed else None
            )
            if extracted_page is not None:
                layers = tuple(
                    (f"page-{i + 1:04d}-{name}", data)
                    for name, data in extracted_page.layers
                )
                rendered.append(
                    PageContent(
                        layers,
                        extracted_page.viewport,
                        extracted_page.ocr_words,
                    )
                )
                extracted_count += 1
                _progress(i + 1, page_count)
                continue
            if mrc:
                pixmap = _render_page_pixmap(
                    page, resolution, clip, False, margins
                )
                generated_page = _rendered_mrc_page(
                    pixmap, effective_image_format, quality,
                    svg_mask=not epub2,
                    color_scale=mrc_color_scale,
                )
                layers = tuple(
                    (f"page-{i + 1:04d}-{name}", data)
                    for name, data in generated_page.layers
                )
                rendered.append(PageContent(layers, generated_page.viewport))
                if cover and i == 0:
                    # Keep the already rendered pixels for the single-image
                    # cover. Re-rendering would be wasteful and, with native
                    # resolution plus added margins, could scale the page a
                    # second time before applying the padding again.
                    cover_image_for_layers = _encode_pixmap(
                        pixmap, effective_image_format, quality
                    )
                generated_count += 1
            else:
                data = render_page(
                    page, resolution, clip,
                    transparent_background=transparent_background,
                    margins=margins,
                    image_format=effective_image_format,
                    quality=quality,
                )
                extension = _image_info("", data)[0]
                rendered.append(
                    PageContent(((f"page-{i + 1:04d}.{extension}", data),))
                )
            _progress(i + 1, page_count)
        if effective_image_format == "auto":
            color_images = [
                name
                for page in rendered
                for name, _ in page.layers
                if not Path(name).stem.endswith("-mask")
            ]
            png_count = sum(name.endswith(".png") for name in color_images)
            jpeg_count = sum(name.endswith(".jpg") for name in color_images)
            _log(f"selected: {png_count} PNG, {jpeg_count} JPEG color images")
        if mrc_extract:
            _log(f"mrc:     extracted {extracted_count} of {page_count} pages")
        if mrc:
            _log(f"mrc:     generated {generated_count} of {page_count} pages")
        if (
            cover
            and rendered
            and len(rendered[0].layers) > 1
            and cover_image_for_layers is None
        ):
            # Flatten the first page while the PDF is still open. EPUB cover
            # metadata points to one image, whereas the page itself is layered.
            cover_image_for_layers = render_page(
                pages_list[0],
                resolution or max(
                    _image_info(name, data)[2]
                    for name, data in rendered[0].layers
                ),
                None if clips[0] is None or clips[0] == pages_list[0].rect else clips[0],
                transparent_background=transparent_background,
                margins=margins,
                image_format=effective_image_format,
                quality=quality,
            )
    finally:
        doc.close()

    pages = rendered

    cover_data = None
    if cover:
        if len(pages[0].layers) == 1:
            first_name, first_data = pages[0].layers[0]
            cover_data = (f"cover{Path(first_name).suffix}", first_data)
        else:
            # A layered page needs a flattened cover image because EPUB cover
            # metadata points to one image resource, not an XHTML section.
            assert cover_image_for_layers is not None
            cover_image = cover_image_for_layers
            cover_data = (
                f"cover.{_image_info('', cover_image)[0]}",
                cover_image,
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = _EpubWriter(output_path, metadata, cover=cover_data, epub2=epub2)
    for page in pages:
        writer.add_layered_page(page.layers, page.viewport, page.ocr_words)
    writer.build()

    _log(
        f"wrote:   {output_path}  "
        f"({format_size(output_path.stat().st_size)}, "
        f"{page_count} pages, {time.monotonic() - started:.1f}s)"
    )
    return output_path
