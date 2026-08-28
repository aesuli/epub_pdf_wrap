from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from epub_pdf_wrap.core import ConversionError, convert, format_size, render_page, slugify


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

    # Every spine image must be present
    images = {i.get_name() for i in book.get_items_of_type(ebooklib.ITEM_IMAGE)}
    assert images == {"images/page-0001.png", "images/page-0002.png"}

    # A default load must also succeed without the ignore_ncx bypass:
    # the NCX toc id has to resolve in the manifest (id="ncx").
    fresh = epub.read_epub(str(out))
    assert len(list(fresh.get_items_of_type(ebooklib.ITEM_DOCUMENT))) == 2
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
    assert format_size(5 * 1024**3) == "5.0 GB"
