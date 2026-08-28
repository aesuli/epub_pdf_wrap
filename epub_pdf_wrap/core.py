"""Convert a PDF into an EPUB by rendering each page to an image."""

from __future__ import annotations

import re
import time
import uuid
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


def render_page(page: "pymupdf.Page", resolution: int | None = None) -> bytes:
    """Render one PDF page to a PNG byte string.

    *resolution*, when given, is the target width in pixels; the height is
    scaled proportionally.
    """
    if resolution is not None:
        if resolution <= 0:
            raise ConversionError(f"resolution must be positive, got {resolution}")
        scale = resolution / page.rect.width
    else:
        scale = 1.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=True, colorspace=pymupdf.csRGB)
    return pix.tobytes("png")


class _EpubWriter:
    """Minimal EPUB 2 writer: one XHTML section per page, one PNG per page.

    The spine is built from the XHTML sections (never directly from images),
    which strict readers require.
    """

    def __init__(self, path: Path, title: str):
        self.path = Path(path)
        self.title = title
        self.image_names: list[str] = []
        self.images: list[bytes] = []
        self.uuid = str(uuid.uuid4())
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

    def _opf_xml(self) -> str:
        manifest = (
            '<item id="ncx" media-type="application/x-dtbncx+xml" href="toc.ncx"/>'
            + "".join(
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
        spine = "".join(
            f'<itemref idref="{sid}"/>' for sid in self._section_ids
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:identifier id="epubid">{self.uuid}</dc:identifier>'
            f"<dc:title>{escape(self.title)}</dc:title>"
            "<dc:language>en</dc:language>"
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
            f"<docTitle><text>{escape(self.title)}</text></docTitle>"
            f"<navMap>{nav}</navMap></ncx>"
        )

    def _section_xhtml(self, index: int, name: str) -> str:
        css = "html,body{margin:0;padding:0;}img{display:block;max-width:100vw;margin:auto;}"
        return (
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
            '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" lang="en" '
            'xmlns:epub="http://www.idpf.org/2007/ops">'
            f"<head><title>Page {index}</title>"
            f'<style type="text/css">{css}</style></head>'
            '<body epub:type="bodymatter">'
            f'<p style="margin:0;padding:0;border:none;">\u200b</p>'
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
            for i, name in enumerate(self.image_names, start=1):
                z.writestr(f"OEBPS/page-{i:04d}.xhtml", self._section_xhtml(i, name))
            for name, data in zip(self.image_names, self.images):
                z.writestr(f"OEBPS/images/{name}", data)

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
            resolution: int | None = None, log=None, progress=None) -> Path:
    """Convert *input_path* (a PDF) to an EPUB and return the output path.

    Every PDF page is rendered to a PNG and placed in its own EPUB section in
    document order. The output file defaults to the input name with the
    extension swapped to ``.epub``. *resolution*, when given, is the target
    render width in pixels.

    *log* (optional callable(str)) receives descriptive lines (input info,
    output result). *progress* (optional callable(done, total)) is called for
    every rendered page.
    """
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
        title = doc.metadata.get("title") or slugify(input_path.stem)

        first = doc.load_page(0)
        w, h = first.rect.width, first.rect.height
        uniform = all(
            doc.load_page(i).rect.width == w and doc.load_page(i).rect.height == h
            for i in range(1, page_count)
        )
        native = f"{w:.0f} x {h:.0f} px" + ("" if uniform else " (varies)")
        if resolution is not None:
            _log(f"pages:   {page_count}  (native resolution: {native})")
            _log(f"output:  {output_path}  ({resolution} px wide)")
        else:
            _log(f"pages:   {page_count}  (native resolution: {native})")
            _log(f"output:  {output_path}  (native resolution)")

        pages: list[tuple[str, bytes]] = []
        for i in range(page_count):
            png = render_page(doc.load_page(i), resolution)
            pages.append((f"page-{i + 1:04d}.png", png))
            _progress(i + 1, page_count)
    finally:
        doc.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = _EpubWriter(output_path, title)
    for name, data in pages:
        writer.add_image(name, data)
    writer.build()

    _log(
        f"wrote:   {output_path}  "
        f"({format_size(output_path.stat().st_size)}, "
        f"{page_count} pages, {time.monotonic() - started:.1f}s)"
    )
    return output_path
