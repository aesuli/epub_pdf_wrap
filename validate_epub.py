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
