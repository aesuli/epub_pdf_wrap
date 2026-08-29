from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from epub_pdf_wrap.core import ConversionError, convert, format_size, page_clip_rect, render_page, slugify


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
    assert args.nocover is False
    assert args.epub2 is False
    assert args.transparent_background is False
    args = p.parse_args(["in.pdf", "--nocover"])
    assert args.nocover is True
    args = p.parse_args(["in.pdf", "--epub2"])
    assert args.epub2 is True
    args = p.parse_args(["in.pdf", "--transparent-background"])
    assert args.transparent_background is True


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
