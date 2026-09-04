from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from epub_pdf_wrap.core import (
    ConversionError,
    _encode_grayscale_png,
    _encode_pixmap,
    _split_mrc_pixmap,
    convert,
    format_size,
    parse_page_selection,
    page_clip_rect,
    render_page,
    slugify,
)


@pytest.fixture
def tiny_pdf(tmp_path: Path) -> Path:
    """Three-page PDF: text page, white-blank page is omitted; use text + image pages."""
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=260)
    page.insert_text((20, 40), "First page")
    page = doc.new_page(width=200, height=260)
    page.insert_text((20, 40), "Second page")
    out = tmp_path / "tiny.pdf"
    doc.save(str(out))
    doc.close()
    return out


def test_convert_produces_epub(tmp_path: Path, tiny_pdf: Path) -> None:
    out = convert(tiny_pdf, tmp_path / "out.epub")
    assert out.exists()
    import zipfile

    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert names[0] == "mimetype"
        assert z.read("mimetype") == b"application/epub+zip"
        assert "OEBPS/images/page-0001.png" in names
        assert "OEBPS/images/page-0002.png" in names


def test_page_xhtml_body_contains_only_the_page_image(
    tmp_path: Path, tiny_pdf: Path
) -> None:
    import xml.etree.ElementTree as ET
    import zipfile

    out = convert(tiny_pdf, tmp_path / "image-only.epub")
    with zipfile.ZipFile(out) as z:
        xhtml = z.read("OEBPS/page-0001.xhtml").decode("utf-8")

    root = ET.fromstring(xhtml)
    body = root.find("{http://www.w3.org/1999/xhtml}body")
    assert body is not None
    assert not (body.text or "").strip()
    assert [child.tag for child in body] == ["{http://www.w3.org/1999/xhtml}img"]
    assert not (body[0].tail or "").strip()


def test_epub_declares_image_pages_as_fixed_layout(
    tmp_path: Path, tiny_pdf: Path
) -> None:
    import zipfile

    out = convert(tiny_pdf, tmp_path / "fixed-layout.epub")
    with zipfile.ZipFile(out) as z:
        opf = z.read("OEBPS/content.opf").decode("utf-8")
        page = z.read("OEBPS/page-0001.xhtml").decode("utf-8")
        nav = z.read("OEBPS/nav.xhtml").decode("utf-8")

    assert 'version="3.0"' in opf
    assert 'unique-identifier="epubid"' in opf
    assert '<meta property="dcterms:modified">' in opf
    assert '<meta property="rendition:layout">pre-paginated</meta>' in opf
    assert '<meta property="rendition:spread">none</meta>' in opf
    assert 'properties="nav" href="nav.xhtml"' in opf
    assert '<meta name="viewport" content="width=200, height=260"/>' in page
    assert '<nav epub:type="page-list"' in nav
    assert nav.count('<a href="page-') == 4


def test_epub2_uses_legacy_package_and_navigation(
    tmp_path: Path, tiny_pdf: Path
) -> None:
    import ebooklib
    from ebooklib import epub
    import zipfile

    out = convert(tiny_pdf, tmp_path / "legacy.epub", epub2=True)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        opf = z.read("OEBPS/content.opf").decode("utf-8")
        page = z.read("OEBPS/page-0001.xhtml").decode("utf-8")

    assert 'version="2.0"' in opf
    assert 'unique-identifier="epubid"' in opf
    assert '<spine toc="ncx">' in opf
    assert '<meta name="cover" content="cover"/>' in opf
    assert 'properties="cover-image"' not in opf
    assert "rendition:" not in opf
    assert "dcterms:modified" not in opf
    assert "OEBPS/toc.ncx" in names
    assert "OEBPS/nav.xhtml" not in names
    assert "XHTML 1.1" in page
    assert "epub:type" not in page
    assert '<div class="page"><img ' in page

    book = epub.read_epub(str(out))
    assert [ref for ref, _ in book.spine] == ["sec-0000", "sec-0001"]
    assert {item.get_name() for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)} == {
        "page-0001.xhtml",
        "page-0002.xhtml",
    }


def test_epub_is_readable_by_ebooklib(tmp_path: Path, tiny_pdf: Path) -> None:
    """Reader-grade check: ebooklib must parse metadata, spine and content."""
    import ebooklib
    from ebooklib import epub

    out = convert(tiny_pdf, tmp_path / "reader.epub")
    book = epub.read_epub(str(out), options={"ignore_ncx": True})

    # Metadata
    titles = book.get_metadata("DC", "title")
    assert titles and titles[0][0] == "tiny"

    # Spine (reading order) must resolve to real items
    spine_refs = [ref for ref, _ in book.spine]
    assert spine_refs == ["sec-0000", "sec-0001"]
    for ref in spine_refs:
        item = book.get_item_with_id(ref)
        assert item is not None
        assert "<img" in item.get_content().decode("utf-8")

    # Cover: default is the first page's image, flagged in the manifest and
    # identical to the first page render.
    import zipfile

    with zipfile.ZipFile(out) as z:
        assert "OEBPS/images/cover.png" in z.namelist()
        assert z.read("OEBPS/images/cover.png") == z.read("OEBPS/images/page-0001.png")
        opf = z.read("OEBPS/content.opf").decode("utf-8")
        assert (
            '<item id="cover" media-type="image/png" '
            'properties="cover-image" href="images/cover.png"/>'
        ) in opf

    cover_items = list(book.get_items_of_type(ebooklib.ITEM_COVER))
    assert len(cover_items) == 1
    assert cover_items[0].get_name() == "images/cover.png"

    # Every spine image must be present
    images = {i.get_name() for i in book.get_items_of_type(ebooklib.ITEM_IMAGE)}
    assert images == {"images/page-0001.png", "images/page-0002.png"}

    # A default load must also succeed without the ignore_ncx bypass:
    # the NCX toc id has to resolve in the manifest (id="ncx").
    fresh = epub.read_epub(str(out))
    documents = list(fresh.get_items_of_type(ebooklib.ITEM_DOCUMENT))
    assert {item.get_name() for item in documents} == {
        "nav.xhtml",
        "page-0001.xhtml",
        "page-0002.xhtml",
    }
    assert fresh.get_item_with_id("nav") not in [
        fresh.get_item_with_id(ref) for ref, _ in fresh.spine
    ]
    # Navigation must provide one TOC entry per page, each resolving to a
    # real item (this is what readers do when a user opens the TOC).
    toc = list(fresh.toc)
    assert len(toc) == 2
    for link in toc:
        item = fresh.get_item_with_href(link.href)
        assert item is not None
        assert "<img" in item.get_content().decode("utf-8")


def test_convert_default_output_name(tiny_pdf: Path) -> None:
    out = convert(tiny_pdf)
    assert out.name == "tiny.epub"
    out.unlink()


def test_resolution_flag(tmp_path: Path, tiny_pdf: Path) -> None:
    out = convert(tiny_pdf, tmp_path / "res.epub", resolution=600)
    import zipfile

    with zipfile.ZipFile(out) as z:
        png = z.read("OEBPS/images/page-0001.png")
    import struct

    # IHDR: width at bytes 16..20
    width = struct.unpack(">I", png[16:20])[0]
    assert width == 600


def test_jpeg_images_cover_and_manifest(tmp_path: Path, tiny_pdf: Path) -> None:
    import zipfile

    out = convert(
        tiny_pdf,
        tmp_path / "jpeg.epub",
        image_format="jpeg",
        quality=72,
    )
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        page = z.read("OEBPS/images/page-0001.jpg")
        cover = z.read("OEBPS/images/cover.jpg")
        opf = z.read("OEBPS/content.opf").decode("utf-8")
        xhtml = z.read("OEBPS/page-0001.xhtml").decode("utf-8")

    assert page[:2] == b"\xff\xd8" and page[-2:] == b"\xff\xd9"
    assert cover == page
    assert "OEBPS/images/page-0001.png" not in names
    assert (
        '<item id="img-0001" media-type="image/jpeg" '
        'href="images/page-0001.jpg"/>'
    ) in opf
    assert (
        '<item id="cover" media-type="image/jpeg" '
        'properties="cover-image" href="images/cover.jpg"/>'
    ) in opf
    assert '<meta name="viewport" content="width=200, height=260"/>' in xhtml
    assert '<img src="images/page-0001.jpg"' in xhtml


def test_epub2_accepts_jpeg_pages_and_cover(tmp_path: Path, tiny_pdf: Path) -> None:
    import ebooklib
    from ebooklib import epub
    import zipfile

    out = convert(
        tiny_pdf,
        tmp_path / "jpeg-epub2.epub",
        image_format="jpeg",
        epub2=True,
    )
    with zipfile.ZipFile(out) as z:
        opf = z.read("OEBPS/content.opf").decode("utf-8")
        assert '<meta name="cover" content="cover"/>' in opf
        assert (
            '<item id="cover" media-type="image/jpeg" '
            'href="images/cover.jpg"/>'
        ) in opf

    book = epub.read_epub(str(out))
    images = {item.get_name() for item in book.get_items_of_type(ebooklib.ITEM_IMAGE)}
    assert images == {
        "images/cover.jpg",
        "images/page-0001.jpg",
        "images/page-0002.jpg",
    }


def test_jpeg_quality_changes_encoded_size() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=160, height=120)
    for x in range(0, 160, 4):
        color = (x / 160, ((x * 7) % 160) / 160, ((x * 13) % 160) / 160)
        page.draw_rect(
            pymupdf.Rect(x, 0, x + 4, 120),
            color=color,
            fill=color,
            width=0,
        )
    try:
        low = render_page(page, image_format="jpeg", quality=20)
        high = render_page(page, image_format="jpeg", quality=95)
    finally:
        doc.close()

    assert low[:2] == high[:2] == b"\xff\xd8"
    assert len(low) < len(high)


@pytest.mark.parametrize(
    ("source_bpc", "expected_png_depth"),
    ((1, 1), (2, 2), (3, 4), (4, 4), (8, 8), (16, 8)),
)
def test_grayscale_png_uses_minimal_supported_source_depth(
    source_bpc: int, expected_png_depth: int
) -> None:
    samples = bytes((index * 37) % 256 for index in range(13 * 3))
    pixmap = pymupdf.Pixmap(pymupdf.csGRAY, 13, 3, samples, False)

    png = _encode_grayscale_png(pixmap, source_bpc)
    decoded = pymupdf.Pixmap(png)

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert png[24] == expected_png_depth
    assert png[25] == 0  # grayscale
    assert (decoded.width, decoded.height) == (13, 3)


def test_auto_prefers_png_when_close_in_size_and_jpeg_when_clearly_smaller() -> None:
    class FakePixmap:
        alpha = 0

        def __init__(self, png_size: int, jpeg_size: int):
            self.png_size = png_size
            self.jpeg_size = jpeg_size

        def tobytes(self, output: str, jpg_quality: int = 0) -> bytes:
            if output == "png":
                return b"p" * self.png_size
            assert jpg_quality == 85
            return b"j" * self.jpeg_size

    assert _encode_pixmap(FakePixmap(109, 100), "auto", 85).startswith(b"p")
    assert _encode_pixmap(FakePixmap(111, 100), "auto", 85).startswith(b"j")


def test_auto_preserves_transparency_as_png() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=20, height=20)
    try:
        data = render_page(
            page,
            transparent_background=True,
            image_format="auto",
        )
    finally:
        doc.close()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize(
    ("image_format", "quality"),
    [("webp", 85), ("jpeg", 0), ("jpeg", 101), ("jpeg", 1.5), ("jpeg", True)],
)
def test_invalid_image_options_raise(
    tiny_pdf: Path, image_format, quality
) -> None:
    with pytest.raises(ConversionError):
        convert(tiny_pdf, image_format=image_format, quality=quality)


def test_jpeg_rejects_transparent_background() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=20, height=20)
    try:
        with pytest.raises(ConversionError, match="transparent"):
            render_page(
                page,
                image_format="jpeg",
                transparent_background=True,
            )
    finally:
        doc.close()


def test_mrc_extract_writes_layered_page_and_flattened_cover(tmp_path: Path) -> None:
    import zipfile
    from random import Random

    doc = pymupdf.open()
    page = doc.new_page(width=120, height=80)
    background = pymupdf.Pixmap(
        pymupdf.csRGB,
        120,
        80,
        bytes([240, 240, 240]) * (120 * 80),
        False,
    )
    foreground_samples = Random(0).randbytes(120 * 80 * 3)
    foreground = pymupdf.Pixmap(
        pymupdf.csRGB,
        120,
        80,
        foreground_samples,
        False,
    )
    mask = pymupdf.Pixmap(
        pymupdf.csGRAY,
        120,
        80,
        bytes([0]) * (120 * 80),
        False,
    )
    mask.set_rect((50, 30, 70, 50), (255,))
    page.insert_image(page.rect, pixmap=background)
    page.insert_image(
        page.rect,
        stream=foreground.tobytes("png"),
        mask=_encode_grayscale_png(mask, 1),
    )
    source = tmp_path / "mrc.pdf"
    doc.save(str(source))
    doc.close()

    out = convert(
        source,
        tmp_path / "mrc.epub",
        resolution=120,
        image_format="jpeg",
        mrc_extract=True,
    )
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        opf = z.read("OEBPS/content.opf").decode("utf-8")
        xhtml = z.read("OEBPS/page-0001.xhtml").decode("utf-8")
        background_data = z.read("OEBPS/images/page-0001-background.jpg")
        foreground_data = z.read("OEBPS/images/page-0001-foreground.jpg")
        mask_data = z.read("OEBPS/images/page-0001-mask.png")
        cover_data = z.read("OEBPS/images/cover.jpg")

    assert "OEBPS/images/page-0001-background.jpg" in names
    assert "OEBPS/images/page-0001-foreground.jpg" in names
    assert "OEBPS/images/page-0001-mask.png" in names
    assert "OEBPS/images/page-0001.jpg" not in names
    assert '<item id="img-0001-01" media-type="image/jpeg"' in opf
    assert '<item id="img-0001-02" media-type="image/jpeg"' in opf
    assert '<item id="img-0001-03" media-type="image/png"' in opf
    assert '<item id="cover" media-type="image/jpeg"' in opf
    assert xhtml.count("<image ") == 3
    assert '<mask id="mrc-selector"' in xhtml
    assert 'mask="url(#mrc-selector)"' in xhtml
    assert '<meta name="viewport" content="width=120, height=80"/>' in xhtml
    assert background_data[:2] == b"\xff\xd8"
    assert foreground_data[:2] == b"\xff\xd8"
    assert mask_data[:8] == b"\x89PNG\r\n\x1a\n"
    assert mask_data[24] == 1
    assert mask_data[25] == 0
    assert set(pymupdf.Pixmap(mask_data).samples) <= {0, 255}
    assert len(foreground_data) < len(foreground_samples)
    assert cover_data[:2] == b"\xff\xd8"

    epub2_out = convert(
        source,
        tmp_path / "mrc-epub2.epub",
        resolution=120,
        image_format="jpeg",
        mrc_extract=True,
        epub2=True,
        cover=False,
    )
    with zipfile.ZipFile(epub2_out) as z:
        epub2_names = z.namelist()
        epub2_xhtml = z.read("OEBPS/page-0001.xhtml").decode("utf-8")
    assert "OEBPS/images/page-0001-background.jpg" in epub2_names
    assert "OEBPS/images/page-0001-foreground.png" in epub2_names
    assert "OEBPS/images/page-0001-mask.png" not in epub2_names
    assert epub2_xhtml.count("<img ") == 2
    assert "<svg " not in epub2_xhtml


def test_mrc_extract_falls_back_for_non_mrc_pages(
    tmp_path: Path, tiny_pdf: Path
) -> None:
    logs: list[str] = []
    out = convert(
        tiny_pdf,
        tmp_path / "fallback.epub",
        mrc_extract=True,
        log=logs.append,
    )
    import zipfile

    with zipfile.ZipFile(out) as z:
        assert "OEBPS/images/page-0001.png" in z.namelist()
        assert "OEBPS/images/foreground.png" not in z.namelist()
    assert "mrc:     extracted 0 of 2 pages" in logs


def test_mrc_extract_reuses_native_resources_geometry_depth_and_ocr(
    tmp_path: Path,
) -> None:
    import zipfile

    doc = pymupdf.open()
    page = doc.new_page(width=120, height=80)
    background = pymupdf.Pixmap(
        pymupdf.csRGB,
        60,
        40,
        bytes([220, 210, 200]) * (60 * 40),
        False,
    )
    foreground = pymupdf.Pixmap(
        pymupdf.csRGB,
        120,
        80,
        bytes([30, 20, 10]) * (120 * 80),
        False,
    )
    mask = pymupdf.Pixmap(
        pymupdf.csGRAY,
        120,
        80,
        bytes([0]) * (120 * 80),
        False,
    )
    mask.set_rect((15, 20, 105, 35), (255,))
    page.insert_image(page.rect, stream=background.tobytes("jpeg", jpg_quality=83))
    page.insert_image(
        page.rect,
        stream=foreground.tobytes("jpeg", jpg_quality=79),
        mask=_encode_grayscale_png(mask, 1),
    )
    page.insert_text((20, 30), "SEARCHABLE", render_mode=3)
    source_path = tmp_path / "native-mrc.pdf"
    doc.save(str(source_path))
    doc.close()

    source_doc = pymupdf.open(source_path)
    refs = source_doc[0].get_images(full=True)
    expected_background = source_doc.extract_image(refs[0][0])["image"]
    expected_foreground = source_doc.extract_image(refs[1][0])["image"]
    expected_mask_samples = pymupdf.Pixmap(source_doc, refs[1][1]).samples
    source_doc.close()

    out = convert(
        source_path,
        tmp_path / "native-mrc.epub",
        mrc_extract=True,
        cover=False,
    )
    with zipfile.ZipFile(out) as z:
        background_data = z.read("OEBPS/images/page-0001-background.jpg")
        foreground_data = z.read("OEBPS/images/page-0001-foreground.jpg")
        mask_data = z.read("OEBPS/images/page-0001-mask.png")
        xhtml = z.read("OEBPS/page-0001.xhtml").decode("utf-8")

    assert background_data == expected_background
    assert foreground_data == expected_foreground
    assert pymupdf.Pixmap(mask_data).samples == expected_mask_samples
    assert (pymupdf.Pixmap(background_data).width, pymupdf.Pixmap(background_data).height) == (60, 40)
    assert (pymupdf.Pixmap(foreground_data).width, pymupdf.Pixmap(foreground_data).height) == (120, 80)
    assert mask_data[24:26] == bytes((1, 0))
    assert 'viewBox="0 0 120 80"' in xhtml
    assert '<meta name="viewport" content="width=120, height=80"/>' in xhtml
    assert "SEARCHABLE" in xhtml

    resized = convert(
        source_path,
        tmp_path / "resized-mrc.epub",
        resolution=90,
        image_format="png",
        mrc_extract=True,
        cover=False,
    )
    with zipfile.ZipFile(resized) as z:
        resized_background = z.read("OEBPS/images/page-0001-background.png")
        resized_foreground = z.read("OEBPS/images/page-0001-foreground.png")
        resized_mask = z.read("OEBPS/images/page-0001-mask.png")
    assert _png_size(resized_background) == (90, 60)
    assert _png_size(resized_foreground) == (90, 60)
    assert _png_size(resized_mask) == (90, 60)
    assert resized_mask[24:26] == bytes((1, 0))


def test_mrc_generates_layers_for_rendered_page_and_reconstructs_png(
    tmp_path: Path,
) -> None:
    import zipfile

    doc = pymupdf.open()
    page = doc.new_page(width=40, height=30)
    page.draw_rect(
        pymupdf.Rect(5, 6, 22, 17),
        color=(0, 0, 0),
        fill=(0, 0, 0),
        width=0,
    )
    page.draw_rect(
        pymupdf.Rect(24, 8, 35, 24),
        color=(0.8, 0.2, 0.1),
        fill=(0.8, 0.2, 0.1),
        width=0,
    )
    source = tmp_path / "rendered.pdf"
    expected = pymupdf.Pixmap(render_page(page, resolution=40))
    doc.save(str(source))
    doc.close()

    logs: list[str] = []
    out = convert(
        source,
        tmp_path / "rendered-mrc.epub",
        resolution=40,
        mrc=True,
        mrc_color_scale=1,
        cover=False,
        log=logs.append,
    )
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        background = pymupdf.Pixmap(
            z.read("OEBPS/images/page-0001-background.png")
        )
        foreground = pymupdf.Pixmap(
            z.read("OEBPS/images/page-0001-foreground.png")
        )
        mask_data = z.read("OEBPS/images/page-0001-mask.png")
        selector = pymupdf.Pixmap(mask_data)
        xhtml = z.read("OEBPS/page-0001.xhtml").decode("utf-8")

    assert "OEBPS/images/page-0001.png" not in names
    assert mask_data[24:26] == bytes((1, 0))
    assert (background.width, background.height) == (40, 30)
    assert (foreground.width, foreground.height) == (40, 30)
    assert (selector.width, selector.height) == (40, 30)
    assert set(selector.samples) == {0, 255}
    reconstructed = bytearray()
    for position, selected in enumerate(selector.samples):
        source_samples = foreground.samples if selected else background.samples
        start = position * 3
        reconstructed.extend(source_samples[start:start + 3])
    assert bytes(reconstructed) == expected.samples
    assert '<mask id="mrc-selector"' in xhtml
    assert '<meta name="viewport" content="width=40, height=30"/>' in xhtml
    assert "mrc:     generated 1 of 1 pages" in logs


def test_mrc_hidden_pixels_are_expanded_and_smoothed() -> None:
    width, height = 11, 5
    samples = bytearray()
    for y in range(height):
        for x in range(width):
            color = (190 + x * 4, 205 + y * 3, 225 - x * 2)
            if y == 2 and x == 2:
                color = (8, 18, 28)
            elif y == 2 and x == 8:
                color = (72, 12, 22)
            samples.extend(color)
    source = pymupdf.Pixmap(
        pymupdf.csRGB, width, height, bytes(samples), False
    )

    background, foreground, selector = _split_mrc_pixmap(source, color_scale=1)
    mask = selector.samples

    assert sum(value == 255 for value in mask) == 2
    foreground_hidden = {
        foreground.samples[position * 3:position * 3 + 3]
        for position, selected in enumerate(mask)
        if not selected
    }
    background_hidden = [
        background.samples[position * 3:position * 3 + 3]
        for position, selected in enumerate(mask)
        if selected
    ]

    # A flat fill produces one repeated hidden color and preserves the mask's
    # silhouette as a hard edge. Expansion plus blur produces a smooth field
    # derived from nearby visible pixels on both layers.
    assert len(foreground_hidden) > 2
    assert background_hidden[0] != background_hidden[1]

    # Synthesis may only touch pixels hidden by the selector.
    for position, selected in enumerate(mask):
        start = position * 3
        visible = foreground.samples if selected else background.samples
        assert visible[start:start + 3] == source.samples[start:start + 3]


def test_mrc_epub2_uses_transparent_foreground_overlay(
    tmp_path: Path, tiny_pdf: Path
) -> None:
    import zipfile

    out = convert(
        tiny_pdf,
        tmp_path / "rendered-mrc-epub2.epub",
        resolution=80,
        image_format="jpeg",
        mrc=True,
        epub2=True,
        cover=False,
    )
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        xhtml = z.read("OEBPS/page-0001.xhtml").decode("utf-8")
        foreground = z.read("OEBPS/images/page-0001-foreground.png")

    assert "OEBPS/images/page-0001-background.jpg" in names
    assert "OEBPS/images/page-0001-mask.png" not in names
    assert foreground[25] == 6  # RGBA PNG
    assert xhtml.count("<img ") == 2
    assert "<svg " not in xhtml


def test_mrc_default_uses_low_resolution_color_planes(
    tmp_path: Path, tiny_pdf: Path
) -> None:
    import zipfile

    out = convert(
        tiny_pdf,
        tmp_path / "scaled-mrc.epub",
        resolution=80,
        mrc=True,
        cover=False,
    )
    with zipfile.ZipFile(out) as z:
        background = pymupdf.Pixmap(
            z.read("OEBPS/images/page-0001-background.png")
        )
        foreground = pymupdf.Pixmap(
            z.read("OEBPS/images/page-0001-foreground.png")
        )
        selector = pymupdf.Pixmap(
            z.read("OEBPS/images/page-0001-mask.png")
        )
        xhtml = z.read("OEBPS/page-0001.xhtml").decode("utf-8")

    assert (background.width, background.height) == (20, 26)
    assert (foreground.width, foreground.height) == (20, 26)
    assert (selector.width, selector.height) == (80, 104)
    assert '<meta name="viewport" content="width=80, height=104"/>' in xhtml


def test_mrc_and_extract_generate_fallback_pages(
    tmp_path: Path, tiny_pdf: Path
) -> None:
    import zipfile

    logs: list[str] = []
    out = convert(
        tiny_pdf,
        tmp_path / "mrc-fallback.epub",
        mrc=True,
        mrc_extract=True,
        cover=False,
        log=logs.append,
    )
    with zipfile.ZipFile(out) as z:
        names = z.namelist()

    assert "OEBPS/images/page-0001-background.png" in names
    assert "OEBPS/images/page-0002-background.png" in names
    assert "mrc:     extracted 0 of 2 pages" in logs
    assert "mrc:     generated 2 of 2 pages" in logs


def test_mrc_rejects_transparent_background(tiny_pdf: Path) -> None:
    with pytest.raises(ConversionError, match="transparent"):
        convert(tiny_pdf, mrc=True, transparent_background=True)


@pytest.mark.parametrize("scale", [0, -1, 1.5, True])
def test_mrc_rejects_invalid_color_scale(tiny_pdf: Path, scale) -> None:
    with pytest.raises(ConversionError, match="mrc_color_scale"):
        convert(tiny_pdf, mrc=True, mrc_color_scale=scale)


def test_margins_add_exact_output_pixels(tmp_path: Path, tiny_pdf: Path) -> None:
    out = convert(
        tiny_pdf,
        tmp_path / "margins.epub",
        resolution=100,
        margins=(10, 20, 30, 40),
    )
    import zipfile

    with zipfile.ZipFile(out) as z:
        png = z.read("OEBPS/images/page-0001.png")

    # The 200 x 260 page renders to 100 x 130 before padding.
    assert _png_size(png) == (160, 170)


@pytest.mark.parametrize(
    "margins",
    [(-1, 0, 0, 0), (0, 0, 0), (0, 0, 0, 1.5), 1],
)
def test_invalid_margins_raise(tiny_pdf: Path, margins) -> None:
    with pytest.raises(ConversionError):
        convert(tiny_pdf, tiny_pdf.with_suffix(".epub"), margins=margins)


def test_blank_only_pdf_still_produces_epub(tmp_path: Path) -> None:
    doc = pymupdf.open()
    doc.new_page(width=200, height=260)
    blank = tmp_path / "blank.pdf"
    doc.save(str(blank))
    doc.close()

    out = convert(blank, tmp_path / "blank.epub")
    assert out.exists()
    import zipfile

    with zipfile.ZipFile(out) as z:
        assert "OEBPS/images/page-0001.png" in z.namelist()
    out.unlink()
    blank.unlink()


def test_missing_input_raises(tmp_path: Path) -> None:
    with pytest.raises(ConversionError):
        convert(tmp_path / "nope.pdf", tmp_path / "nope.epub")


def test_bad_resolution_raises(tiny_pdf: Path) -> None:
    doc = pymupdf.open(str(tiny_pdf))
    page = doc.load_page(0)
    try:
        with pytest.raises(ConversionError):
            render_page(page, resolution=0)
        with pytest.raises(ConversionError):
            render_page(page, resolution=-5)
    finally:
        doc.close()


def test_page_background_is_opaque_by_default_and_transparency_is_optional() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=20, height=20)
    try:
        opaque = render_page(page)
        transparent = render_page(page, transparent_background=True)
    finally:
        doc.close()

    # PNG IHDR color type: 2 is RGB, 6 is RGBA.
    assert opaque[25] == 2
    assert transparent[25] == 6


def test_added_margin_uses_the_requested_background_and_offsets() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=10, height=10)
    page.draw_rect(page.rect, color=(0, 0, 0), fill=(0, 0, 0), width=0)
    try:
        opaque = pymupdf.Pixmap(render_page(page, margins=(2, 3, 4, 5)))
        transparent = pymupdf.Pixmap(
            render_page(
                page,
                transparent_background=True,
                margins=(2, 3, 4, 5),
            )
        )
    finally:
        doc.close()

    assert opaque.pixel(0, 0) == (255, 255, 255)
    assert opaque.pixel(5, 2) == (0, 0, 0)
    assert opaque.pixel(14, 11) == (0, 0, 0)
    assert opaque.pixel(15, 11) == (255, 255, 255)
    assert transparent.pixel(0, 0)[-1] == 0
    assert transparent.pixel(5, 2) == (0, 0, 0, 255)
    assert transparent.pixel(14, 11) == (0, 0, 0, 255)


def test_convert_passes_background_setting_to_page_images(
    tmp_path: Path, tiny_pdf: Path
) -> None:
    import zipfile

    opaque_epub = convert(tiny_pdf, tmp_path / "opaque.epub")
    transparent_epub = convert(
        tiny_pdf,
        tmp_path / "transparent.epub",
        transparent_background=True,
    )

    with zipfile.ZipFile(opaque_epub) as z:
        opaque = z.read("OEBPS/images/page-0001.png")
    with zipfile.ZipFile(transparent_epub) as z:
        transparent = z.read("OEBPS/images/page-0001.png")

    assert opaque[25] == 2
    assert transparent[25] == 6


def test_convert_reports_input_info_and_progress(tmp_path: Path, tiny_pdf: Path) -> None:
    logged: list[str] = []
    steps: list[tuple[int, int]] = []
    out = convert(
        tiny_pdf,
        tmp_path / "report.epub",
        log=logged.append,
        progress=lambda done, total: steps.append((done, total)),
    )
    joined = "\n".join(logged)
    assert str(tiny_pdf) in joined
    assert "pages:   2" in joined
    assert str(out) in joined
    assert "2 pages" in joined
    assert steps[-1] == (2, 2)
    assert len(steps) == 2


def test_parse_page_selection() -> None:
    assert parse_page_selection("2, 3, 5-7, 1", 7) == [1, 2, 4, 5, 6, 0]


@pytest.mark.parametrize(
    "selection", ["", "1,", "a", "1--2", "0", "3-2", "1-4"]
)
def test_invalid_page_selection_raises(selection: str) -> None:
    with pytest.raises(ConversionError):
        parse_page_selection(selection, 3)


def test_convert_selected_pages(tmp_path: Path, tiny_pdf: Path) -> None:
    import zipfile

    selected_steps: list[tuple[int, int]] = []
    selected = convert(
        tiny_pdf,
        tmp_path / "selected.epub",
        pages="2",
        progress=lambda done, total: selected_steps.append((done, total)),
    )
    full = convert(tiny_pdf, tmp_path / "full.epub")

    with zipfile.ZipFile(selected) as selected_zip, zipfile.ZipFile(full) as full_zip:
        assert "OEBPS/images/page-0002.png" not in selected_zip.namelist()
        assert (
            selected_zip.read("OEBPS/images/page-0001.png")
            == full_zip.read("OEBPS/images/page-0002.png")
        )
        assert (
            selected_zip.read("OEBPS/images/cover.png")
            == selected_zip.read("OEBPS/images/page-0001.png")
        )
    assert selected_steps == [(1, 1)]


def test_slugify() -> None:
    assert slugify("Some Comic 42") == "Some-Comic-42"
    assert slugify("2406.12128v2") == "2406-12128v2"
    assert slugify("   ") == "document"


def test_format_size() -> None:
    assert format_size(0) == "0 B"
    assert format_size(999) == "999 B"
    assert format_size(1024) == "1.0 KB"
    assert format_size(2048) == "2.0 KB"
    assert format_size(1536) == "1.5 KB"
    assert format_size(1536 * 1024) == "1.5 MB"


def _png_size(png: bytes) -> tuple[int, int]:
    import struct

    return struct.unpack(">II", png[16:24])


@pytest.fixture
def inset_pdf(tmp_path: Path) -> Path:
    """Two pages with content inset in the middle (real margins around it).

    Page 1: content from (50, 50) to (150, 210).
    Page 2: content from (30, 30) to (170, 230) — wider than page 1, so the
    global (union) clip keeps the wider extent on that side.
    """
    doc = pymupdf.open()
    p = doc.new_page(width=200, height=260)
    p.insert_text((50, 80), "page one")
    p.draw_rect(pymupdf.Rect(50, 100, 150, 200))
    p = doc.new_page(width=200, height=260)
    p.insert_text((30, 60), "page two")
    p.draw_rect(pymupdf.Rect(30, 80, 170, 220))
    out = tmp_path / "inset.pdf"
    doc.save(str(out))
    doc.close()
    return out


def test_crop_page_trims_to_content(tmp_path: Path, inset_pdf: Path) -> None:
    out = convert(inset_pdf, tmp_path / "crop.epub", crop="page")

    # Compare against the no-crop render of the same PDF
    nocrop = convert(inset_pdf, tmp_path / "nocrop.epub")
    import zipfile

    with zipfile.ZipFile(out) as z:
        cropped = _png_size(z.read("OEBPS/images/page-0001.png"))
    with zipfile.ZipFile(nocrop) as z:
        full = _png_size(z.read("OEBPS/images/page-0001.png"))
    assert full == (200, 260)
    # Page 1 content spans roughly 50..150 wide, 70..200 tall: the crop must
    # clearly remove margins on all four sides.
    assert cropped[0] < full[0] * 0.7
    assert cropped[1] < full[1] * 0.7


def test_crop_global_uniform_but_safe(tmp_path: Path, inset_pdf: Path) -> None:
    out = convert(inset_pdf, tmp_path / "g.epub", crop="global")
    import zipfile

    with zipfile.ZipFile(out) as z:
        s1 = _png_size(z.read("OEBPS/images/page-0001.png"))
        s2 = _png_size(z.read("OEBPS/images/page-0002.png"))
    # Union of content boxes is the same size for both pages (each clipped
    # to the same global box), so both page images have equal dimensions.
    assert s1 == s2
    # The global box must never clip content: page 2, which extends
    # furthest (30..170 wide), must not have been cut.
    assert s1[0] >= 170 - 30


def test_full_bleed_crop_unchanged(tmp_path: Path) -> None:
    doc = pymupdf.open()
    p = doc.new_page(width=200, height=260)
    p.draw_rect(pymupdf.Rect(0, 0, 200, 260))  # content touches all edges
    full = tmp_path / "full.pdf"
    doc.save(str(full))
    doc.close()

    a = convert(full, tmp_path / "a.epub")
    b = convert(full, tmp_path / "b.epub", crop="global")
    c = convert(full, tmp_path / "c.epub", crop="page")
    import zipfile

    with zipfile.ZipFile(a) as z:
        base = z.read("OEBPS/images/page-0001.png")
    with zipfile.ZipFile(b) as z:
        assert z.read("OEBPS/images/page-0001.png") == base
    with zipfile.ZipFile(c) as z:
        assert z.read("OEBPS/images/page-0001.png") == base
    full.unlink()


def test_blank_page_crop_does_not_crash(tmp_path: Path) -> None:
    doc = pymupdf.open()
    doc.new_page(width=200, height=260)
    blank = tmp_path / "blank.pdf"
    doc.save(str(blank))
    doc.close()

    a = convert(blank, tmp_path / "gb.epub", crop="global")
    b = convert(blank, tmp_path / "pb.epub", crop="page")
    import zipfile

    for f in (a, b):
        with zipfile.ZipFile(f) as z:
            assert _png_size(z.read("OEBPS/images/page-0001.png")) == (200, 260)
    blank.unlink()


def test_crop_invalid_value_raises(inset_pdf: Path) -> None:
    with pytest.raises(ConversionError):
        convert(inset_pdf, inset_pdf.with_suffix(".epub"), crop="bogus")


def test_nocover_flag(tmp_path: Path, tiny_pdf: Path) -> None:
    import zipfile

    out = convert(tiny_pdf, tmp_path / "nocover.epub", cover=False)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert "OEBPS/images/cover.png" not in names
        opf = z.read("OEBPS/content.opf").decode("utf-8")
        assert 'id="cover"' not in opf
        assert "cover-image" not in opf
        assert '<itemref idref="sec-0000"/>' in opf

    # The rest of the book is unchanged apart from the cover image
    base = convert(tiny_pdf, tmp_path / "base.epub", cover=True)
    with zipfile.ZipFile(out) as z:
        without = set(z.namelist())
    with zipfile.ZipFile(base) as z:
        with_cover = set(z.namelist())
    assert with_cover - without == {"OEBPS/images/cover.png"}


def test_cli_parser_crop_flags() -> None:
    from epub_pdf_wrap.__main__ import build_parser

    p = build_parser()
    args = p.parse_args(["in.pdf", "-c"])
    assert args.crop_global is True and args.crop_page is False
    args = p.parse_args(["in.pdf", "--crop-global"])
    assert args.crop_global is True
    args = p.parse_args(["in.pdf", "--crop-page"])
    assert args.crop_global is False and args.crop_page is True
    with pytest.raises(SystemExit) as exc:
        p.parse_args(["in.pdf", "-c", "--crop-page"])
    assert exc.value.code == 2
    args = p.parse_args(["in.pdf"])
    assert args.title is None
    assert args.author is None
    assert args.nocover is False
    assert args.epub2 is False
    assert args.transparent_background is False
    assert args.image_format is None
    assert args.quality == 85
    assert args.mrc is False
    assert args.mrc_color_scale == 4
    assert args.mrc_extract is False
    assert args.pages is None
    args = p.parse_args(["in.pdf", "--nocover"])
    assert args.nocover is True
    args = p.parse_args(["in.pdf", "--epub2"])
    assert args.epub2 is True
    args = p.parse_args(["in.pdf", "--transparent-background"])
    assert args.transparent_background is True
    args = p.parse_args(["in.pdf", "--mrc-extract"])
    assert args.mrc_extract is True
    args = p.parse_args(["in.pdf", "--mrc"])
    assert args.mrc is True
    args = p.parse_args(["in.pdf", "--mrc", "--mrc-color-scale", "2"])
    assert args.mrc_color_scale == 2
    with pytest.raises(SystemExit) as exc:
        p.parse_args(["in.pdf", "--mrc-color-scale", "0"])
    assert exc.value.code == 2
    args = p.parse_args(["in.pdf", "--pages", "2,3,5-10,21"])
    assert args.pages == "2,3,5-10,21"
    args = p.parse_args(["in.pdf", "--title", "Override", "--author", "New Author"])
    assert args.title == "Override"
    assert args.author == "New Author"


def test_cli_preserves_unspecified_image_format(monkeypatch, tmp_path: Path) -> None:
    import epub_pdf_wrap.__main__ as cli

    formats: list[str] = []

    def fake_convert(*args, **kwargs):
        formats.append(kwargs["image_format"])
        return tmp_path / "unused.epub"

    monkeypatch.setattr(cli, "convert", fake_convert)
    assert cli.main(["in.pdf"]) == 0
    assert cli.main(["in.pdf", "--mrc-extract"]) == 0
    assert cli.main(["in.pdf", "--mrc-extract", "--image-format", "png"]) == 0
    assert formats == [None, None, "png"]


def test_cli_forwards_metadata_overrides(monkeypatch, tmp_path: Path) -> None:
    import epub_pdf_wrap.__main__ as cli

    metadata = []

    def fake_convert(*args, **kwargs):
        metadata.append((kwargs["title"], kwargs["author"]))
        return tmp_path / "unused.epub"

    monkeypatch.setattr(cli, "convert", fake_convert)
    assert cli.main(["in.pdf", "--title", "New Title", "--author", "New Author"]) == 0
    assert metadata == [("New Title", "New Author")]


def test_cli_parser_image_options() -> None:
    from epub_pdf_wrap.__main__ import build_parser

    p = build_parser()
    args = p.parse_args(
        ["in.pdf", "--image-format", "auto", "--quality", "73"]
    )
    assert args.image_format == "auto"
    assert args.quality == 73
    for argv in (
        ["in.pdf", "--image-format", "webp"],
        ["in.pdf", "--quality", "0"],
        ["in.pdf", "--quality", "101"],
    ):
        with pytest.raises(SystemExit) as exc:
            p.parse_args(argv)
        assert exc.value.code == 2


def test_cli_parser_margin_flags() -> None:
    from epub_pdf_wrap.__main__ import build_parser

    p = build_parser()
    args = p.parse_args(["in.pdf", "--margin", "12"])
    assert args.margin == 12 and args.margins is None
    args = p.parse_args(["in.pdf", "--margins", "1", "2", "3", "4"])
    assert args.margin is None and args.margins == [1, 2, 3, 4]
    for argv in (
        ["in.pdf", "--margin", "-1"],
        ["in.pdf", "--margins", "1", "2", "3", "-4"],
        ["in.pdf", "--margin", "1", "--margins", "1", "2", "3", "4"],
    ):
        with pytest.raises(SystemExit) as exc:
            p.parse_args(argv)
        assert exc.value.code == 2


@pytest.fixture
def metadata_pdf(tmp_path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=260)
    page.insert_text((20, 40), "content")
    doc.set_metadata(
        {
            "title": "A <Great> Book & Co",
            "author": "Jane Doe, John Roe",
            "subject": "Typesetting with pypdf",
            "keywords": "epub, pdf, wrap",
            "creationDate": "D:20041212120000+01'00'",
        }
    )
    out = tmp_path / "meta.pdf"
    doc.save(str(out))
    doc.close()
    return out


def test_metadata_is_transferred_to_epub(tmp_path: Path, metadata_pdf: Path) -> None:
    import ebooklib
    from ebooklib import epub

    out = convert(metadata_pdf, tmp_path / "meta.epub")
    book = epub.read_epub(str(out), options={"ignore_ncx": True})

    assert book.get_metadata("DC", "title")[0][0] == "A <Great> Book & Co"
    assert book.get_metadata("DC", "creator")[0][0] == "Jane Doe, John Roe"
    assert book.get_metadata("DC", "subject")[0][0] == "Typesetting with pypdf"
    assert book.get_metadata("DC", "date")[0][0] == "2004-12-12T12:00:00"
    # Tooling fields are not transferred
    assert not book.get_metadata("DC", "source")

    # The OPF XML itself: identifier still present, values escaped in title,
    # keywords present as an EPUB-3 meta element.
    from zipfile import ZipFile

    with ZipFile(out) as z:
        opf = z.read("OEBPS/content.opf").decode("utf-8")
    assert "A &lt;Great&gt; Book &amp; Co" in opf
    assert '<dc:identifier id="epubid"' in opf
    assert '<meta name="keywords" content="epub, pdf, wrap"/>' in opf


def test_metadata_fallback_title_when_empty(tmp_path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page(width=200, height=260)
    blank = tmp_path / "no-meta.pdf"
    doc.save(str(blank))
    doc.close()

    out = convert(blank, tmp_path / "no-meta.epub")
    import zipfile

    with zipfile.ZipFile(out) as z:
        opf = z.read("OEBPS/content.opf").decode("utf-8")
    assert "<dc:title>no-meta</dc:title>" in opf
    # Only empty fields are omitted, not the required title
    assert "<dc:creator>" not in opf
    assert "<dc:subject>" not in opf
    assert "<dc:date>" not in opf
    blank.unlink()


def test_metadata_overrides_are_independent(
    tmp_path: Path, metadata_pdf: Path
) -> None:
    from ebooklib import epub

    title_out = convert(metadata_pdf, tmp_path / "title.epub", title="New Title")
    author_out = convert(metadata_pdf, tmp_path / "author.epub", author="New Author")

    title_book = epub.read_epub(str(title_out), options={"ignore_ncx": True})
    assert title_book.get_metadata("DC", "title")[0][0] == "New Title"
    assert title_book.get_metadata("DC", "creator")[0][0] == "Jane Doe, John Roe"

    author_book = epub.read_epub(str(author_out), options={"ignore_ncx": True})
    assert author_book.get_metadata("DC", "title")[0][0] == "A <Great> Book & Co"
    assert author_book.get_metadata("DC", "creator")[0][0] == "New Author"
