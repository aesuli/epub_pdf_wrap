"""Convert a PDF into an EPUB by rendering each page to an image."""

from __future__ import annotations

import re
import struct
import time
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pymupdf


IMAGE_FORMATS = ("png", "jpeg", "auto")
DEFAULT_JPEG_QUALITY = 85

# In auto mode, prefer lossless PNG when it costs no more than 10% over JPEG.
# This keeps crisp line art lossless without retaining very large PNGs for
# photographic or noisy scanned pages.
_AUTO_PNG_SIZE_RATIO = 1.10


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


def global_clip_rects(doc: "pymupdf.Document") -> list["pymupdf.Rect"]:
    """One common clip rect for every page, safe for all of them.

    For a common clip to never cut into any page's content it must fully
    contain each page's content region; the union of the per-page content
    boxes is the smallest box with that property, hence the most aggressive
    safe common trim (and it yields a uniform size for all pages). Blank
    pages fall back to their own full rect.
    """
    per_page = [(doc.load_page(i), content_bbox(doc.load_page(i)))
                for i in range(doc.page_count)]
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
    return _encode_pixmap(pix, image_format, quality)


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


class _EpubWriter:
    """Minimal EPUB writer: one XHTML section and one image per page.

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
        self.image_names: list[str] = []
        self.images: list[bytes] = []
        self.uuid = str(uuid.uuid4())
        self.modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._tmp = self.path.with_suffix(self.path.suffix + ".tmp")

    def add_image(self, name: str, data: bytes):
        self.image_names.append(name)
        self.images.append(data)

    @property
    def _section_ids(self) -> list[str]:
        return [f"sec-{i:04d}" for i in range(len(self.image_names))]

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
        manifest += (
            "".join(
                f'<item id="{sid}" media-type="application/xhtml+xml" '
                f'href="page-{i:04d}.xhtml"/>'
                for i, sid in enumerate(self._section_ids, start=1)
            )
            + "".join(
                f'<item id="img-{i:04d}" media-type="{_image_info(name, data)[1]}" '
                f'href="images/{name}"/>'
                for i, (name, data) in enumerate(
                    zip(self.image_names, self.images), start=1
                )
            )
        )
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
            for i in range(1, len(self.image_names) + 1)
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
            for i in range(1, len(self.image_names) + 1)
        )
        page_list = "".join(
            f'<li><a href="page-{i:04d}.xhtml">{i}</a></li>'
            for i in range(1, len(self.image_names) + 1)
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

    def _section_xhtml(self, index: int, name: str, data: bytes) -> str:
        _, _, width, height = _image_info(name, data)
        if self.epub2:
            css = (
                "html,body,.page{margin:0;padding:0;width:100%;}"
                "img{display:block;width:100%;height:auto;}"
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
                f'<img src="images/{escape(name)}" alt="Page {index}"/>'
                "</div></body></html>"
            )
        css = (
            "html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden;}"
            "img{display:block;width:100%;height:100%;}"
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE html>'
            '<html xmlns="http://www.w3.org/1999/xhtml" lang="en" '
            'xml:lang="en" xmlns:epub="http://www.idpf.org/2007/ops">'
            f"<head><title>Page {index}</title>"
            f'<meta name="viewport" content="width={width}, height={height}"/>'
            f'<style type="text/css">{css}</style></head>'
            '<body epub:type="bodymatter">'
            f'<img src="images/{escape(name)}" alt="Page {index}"/>'
            "</body></html>"
        )

    def build(self) -> None:
        assert len(self.image_names) == len(self.images)
        buf = BytesIO()
        with ZipFile(buf, "w", ZIP_DEFLATED) as z:
            z.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
            z.writestr("META-INF/container.xml", self._container_xml())
            z.writestr("OEBPS/content.opf", self._opf_xml())
            z.writestr("OEBPS/toc.ncx", self._ncx_xml())
            if not self.epub2:
                z.writestr("OEBPS/nav.xhtml", self._nav_xhtml())
            for i, (name, data) in enumerate(
                zip(self.image_names, self.images), start=1
            ):
                z.writestr(
                    f"OEBPS/page-{i:04d}.xhtml",
                    self._section_xhtml(i, name, data),
                )
            for name, data in zip(self.image_names, self.images):
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
            image_format: str = "png",
            quality: int = DEFAULT_JPEG_QUALITY) -> Path:
    """Convert *input_path* (a PDF) to an EPUB and return the output path.

    Every PDF page is rendered to an image and placed in its own EPUB section
    in document order. The output file defaults to the input name with the
    extension swapped to ``.epub``. *resolution*, when given, is the target
    render width in pixels. *crop*, when given, trims white margins:
    ``"global"`` uses one common inset for all pages, ``"page"`` trims each
    page to its own content.

    *margins*, when given, contains the top, right, bottom and left padding
    to add in output pixels after each page is rendered.

    When *cover* is true (the default) the first page also becomes the book's
    cover image. EPUB 3 fixed-layout output is the default; when *epub2* is
    true, the output uses EPUB 2 markup for compatibility with older readers.
    Unpainted page areas are rendered white unless *transparent_background*
    is true.

    *image_format* may be ``"png"`` (the lossless default), ``"jpeg"`` or
    ``"auto"``. Auto keeps PNG when it is no more than 10% larger than JPEG,
    otherwise it uses JPEG. *quality* controls JPEG encoding from 1 to 100.

    *log* (optional callable(str)) receives descriptive lines (input info,
    output result). *progress* (optional callable(done, total)) is called for
    every rendered page.
    """
    if crop is not None and crop not in ("global", "page"):
        raise ConversionError(f"crop must be 'global' or 'page', got {crop!r}")
    margins = _validate_margins(margins)
    image_format, quality = _validate_image_options(image_format, quality)
    if transparent_background and image_format == "jpeg":
        raise ConversionError("JPEG does not support a transparent background")

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
        page_count = doc.page_count
        if page_count == 0:
            raise ConversionError(f"PDF has no pages: {input_path}")
        metadata = epub_metadata(doc)
        metadata.setdefault("title", slugify(input_path.stem))
        pages_list = [doc.load_page(i) for i in range(page_count)]

        first = pages_list[0]
        w, h = first.rect.width, first.rect.height
        uniform = all(
            p.rect.width == w and p.rect.height == h for p in pages_list[1:]
        )
        native = f"{w:.0f} x {h:.0f} px" + ("" if uniform else " (varies)")
        _log(f"pages:   {page_count}  (native resolution: {native})")
        if crop == "global":
            clips = global_clip_rects(doc)
        elif crop == "page":
            clips = [page_clip_rect(p) for p in pages_list]
        else:
            clips = [None] * page_count
        if resolution is not None:
            _log(f"output:  {output_path}  ({resolution} px wide)")
        else:
            _log(f"output:  {output_path}  (native resolution)")
        _log(f"format:  EPUB {2 if epub2 else 3}")
        image_description = image_format.upper()
        if image_format in ("jpeg", "auto"):
            image_description += f" (JPEG quality {quality})"
        _log(f"images:  {image_description}")
        if any(margins):
            _log(f"margins: {margins[0]} {margins[1]} {margins[2]} {margins[3]} px")
        if crop is not None:
            trimmed = sum(
                1 for p, c in zip(pages_list, clips) if c is not None and c != p.rect
            )
            _log(f"crop:    {crop}  ({trimmed} of {page_count} pages trimmed)")

        rendered: list[tuple[str, bytes]] = []
        for i, (page, clip_raw) in enumerate(zip(pages_list, clips)):
            clip = None if (clip_raw is None or clip_raw == page.rect) else clip_raw
            data = render_page(
                page, resolution, clip,
                transparent_background=transparent_background,
                margins=margins,
                image_format=image_format,
                quality=quality,
            )
            extension = _image_info("", data)[0]
            rendered.append((f"page-{i + 1:04d}.{extension}", data))
            _progress(i + 1, page_count)
        if image_format == "auto":
            png_count = sum(name.endswith(".png") for name, _ in rendered)
            jpeg_count = page_count - png_count
            _log(f"selected: {png_count} PNG, {jpeg_count} JPEG")
    finally:
        doc.close()

    pages = rendered

    first_extension = Path(pages[0][0]).suffix
    cover_data = (f"cover{first_extension}", pages[0][1]) if cover else None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = _EpubWriter(output_path, metadata, cover=cover_data, epub2=epub2)
    for name, data in pages:
        writer.add_image(name, data)
    writer.build()

    _log(
        f"wrote:   {output_path}  "
        f"({format_size(output_path.stat().st_size)}, "
        f"{page_count} pages, {time.monotonic() - started:.1f}s)"
    )
    return output_path
