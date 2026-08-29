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


def render_page(page: "pymupdf.Page", resolution: int | None = None,
                clip: "pymupdf.Rect | None" = None,
                transparent_background: bool = False,
                margins: "tuple[int, int, int, int] | None" = None) -> bytes:
    """Render one PDF page to a PNG byte string.

    *resolution*, when given, is the target width in pixels; the height is
    scaled proportionally. *clip*, when given, is a ``Rect`` in page
    coordinates to render only that region (e.g. with margins trimmed).
    *margins* is an optional ``(top, right, bottom, left)`` tuple specifying
    padding in output pixels, applied after rendering and scaling.
    Unpainted page areas are white by default; set *transparent_background*
    to preserve them, including added margins, as transparent PNG pixels.
    """
    margins = _validate_margins(margins)
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
    return pix.tobytes("png")


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


class _EpubWriter:
    """Minimal EPUB writer: one XHTML section per page, one PNG per page.

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
                f'<item id="img-{i:04d}" media-type="image/png" '
                f'href="images/{name}"/>'
                for i, name in enumerate(self.image_names, start=1)
            )
        )
        if self.cover is not None:
            properties = "" if self.epub2 else ' properties="cover-image"'
            manifest += (
                f'<item id="cover" media-type="image/png"{properties} '
                'href="images/cover.png"/>'
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
        if (
            len(data) < 24
            or data[:8] != b"\x89PNG\r\n\x1a\n"
            or data[12:16] != b"IHDR"
        ):
            raise ConversionError(f"page image is not a valid PNG: {name}")
        width, height = struct.unpack(">II", data[16:24])
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
                z.writestr("OEBPS/images/cover.png", self.cover[1])

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
            margins: "tuple[int, int, int, int] | None" = None) -> Path:
    """Convert *input_path* (a PDF) to an EPUB and return the output path.

    Every PDF page is rendered to a PNG and placed in its own EPUB section in
    document order. The output file defaults to the input name with the
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

    *log* (optional callable(str)) receives descriptive lines (input info,
    output result). *progress* (optional callable(done, total)) is called for
    every rendered page.
    """
    if crop is not None and crop not in ("global", "page"):
        raise ConversionError(f"crop must be 'global' or 'page', got {crop!r}")
    margins = _validate_margins(margins)

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
            png = render_page(
                page, resolution, clip,
                transparent_background=transparent_background,
                margins=margins,
            )
            rendered.append((f"page-{i + 1:04d}.png", png))
            _progress(i + 1, page_count)
    finally:
        doc.close()

    pages = rendered

    cover_data = ("cover.png", pages[0][1]) if cover else None

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
