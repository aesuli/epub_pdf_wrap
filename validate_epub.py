from __future__ import annotations

"""Validate a generated EPUB with reader-grade libraries and show what breaks."""
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

target = Path(sys.argv[1]) if len(sys.argv) > 1 else next(Path("samples").glob("*.epub"))
print("== target:", target)

errors = 0

print("\n== ebooklib read ==")
try:
    import ebooklib
    from ebooklib import epub

    book = epub.read_epub(str(target), options={"ignore_ncx": True})
    print("spine items:", [(item_id, ref) for item_id, ref in book.spine])
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        print("doc item:", item.get_id(), item.get_name())
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        print("image item:", item.get_id(), item.get_name())
    for item in book.items:
        print("item:", item.get_id(), item.get_name())
    meta = book.get_metadata("DC", "title")
    print("title:", meta)
except Exception as exc:
    errors += 1
    print("FAILED:", type(exc).__name__, exc)

print("\n== raw zip / XML sanity ==")
try:
    with zipfile.ZipFile(target) as z:
        entries = z.infolist()
        print("first entry:", entries[0].filename, "compress_type:", entries[0].compress_type)
        with target.open("rb") as f:
            head = f.read(30)
        print("first 30 bytes:", head)
        opf = ET.fromstring(z.read("OEBPS/content.opf"))
        opf_ns = {"opf": "http://www.idpf.org/2007/opf"}
        version = opf.get("version")
        if version not in ("2.0", "3.0"):
            errors += 1
            print("PACKAGE ERROR: expected EPUB version 2.0 or 3.0")
        print("package version:", version)
        ncx = opf.find("opf:manifest/opf:item[@id='ncx']", opf_ns)
        spine = opf.find("opf:spine", opf_ns)
        ncx_path = f"OEBPS/{ncx.get('href')}" if ncx is not None else None
        if ncx_path not in z.namelist() or spine is None or spine.get("toc") != "ncx":
            errors += 1
            print("PACKAGE ERROR: NCX manifest item and spine reference are required")
        if version == "3.0":
            layout = opf.find(
                "opf:metadata/opf:meta[@property='rendition:layout']", opf_ns
            )
            if layout is None or layout.text != "pre-paginated":
                errors += 1
                print("PACKAGE ERROR: missing pre-paginated fixed-layout metadata")
            nav = opf.find("opf:manifest/opf:item[@properties='nav']", opf_ns)
            nav_path = f"OEBPS/{nav.get('href')}" if nav is not None else None
            if nav_path not in z.namelist():
                errors += 1
                print("PACKAGE ERROR: navigation document is missing")
        elif "OEBPS/nav.xhtml" in z.namelist():
            errors += 1
            print("PACKAGE ERROR: EPUB 2 unexpectedly contains an EPUB 3 nav")
        for image in opf.findall("opf:manifest/opf:item", opf_ns):
            media_type = image.get("media-type")
            if media_type not in ("image/png", "image/jpeg"):
                continue
            image_path = f"OEBPS/{image.get('href')}"
            if image_path not in z.namelist():
                errors += 1
                print("PACKAGE ERROR: manifest image is missing:", image_path)
                continue
            data = z.read(image_path)
            expected = (
                data.startswith(b"\x89PNG\r\n\x1a\n")
                if media_type == "image/png"
                else data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")
            )
            if not expected:
                errors += 1
                print("PACKAGE ERROR: image data does not match media type:", image_path)
        for name in z.namelist():
            if name.endswith((".opf", ".xhtml", ".ncx", "container.xml")):
                try:
                    ET.fromstring(z.read(name))
                    print("xml well-formed:", name)
                except ET.ParseError as e:
                    errors += 1
                    print("PARSE ERROR in", name, ":", e)
except Exception as exc:
    errors += 1
    print("FAILED:", type(exc).__name__, exc)

print("\nRESULT:", "FAIL" if errors else "OK")
sys.exit(1 if errors else 0)
