# AGENTS.md

- Python project: converts a PDF to EPUB by rendering each page as a PNG image page (`epub_pdf_wrap/core.py`), packaged as `epub-pdf-wrap` CLI (`epub_pdf_wrap/__main__.py`, entry point `main`).
- CLI per README: `epub-pdf-wrap <input.pdf> [-o <output.epub>] [-r <num>] [-c|--crop-global | --crop-page]`; `-r` is target render width in pixels (default = PDF's own resolution); default output = input name with `.epub` extension. `-c` is the shortcut for `--crop-global` (both flags mutually exclusive).
- Setup (Windows, PowerShell): `python -m venv .venv`; `.\.venv\Scripts\pip install -e ".[dev]"`.
- Packaging: license is **BSD-3-Clause** (`LICENSE`, PEP 639 `license-files` in `pyproject.toml`). Version is a single source of truth in `pyproject.toml` (imported via `importlib.metadata` in `__init__.py`).
- Run tests: `.\.venv\Scripts\pytest -q` (single test: add `-k <name>`). No lint/typecheck config; plain Python, deps are `pymupdf` only (`ebooklib` is a dev dep used to validate output).
- Always verify generated EPUBs with `.\.venv\Scripts\python.exe validate_epub.py <file.epub>` (checks zip order, XML well-formedness, and an ebooklib reader-grade load). Keep it in sync when changing the EPUB writer.
- Use `import pymupdf`, never `import fitz` (the `fitz` alias is deprecated in v1.28 and warns on import).
- Margin detection in `epub_pdf_wrap/core.py` works via `page.get_bboxlog()` (a list of *(kind, bbox)* tuples — union them into one `Rect`; it is NOT a `Rect` itself). Clipping is done with `get_pixmap(clip=Rect)`; when cropping, `-r` scales against the clip width, not the page width.
- EPUB structure gotcha: the spine **must** reference XHTML sections (one per page), never raw images, and `spine toc="ncx"` must match a manifest item id or readers/ebooklib fail to open the file. This was the actual bug that made generated EPUBs unopenable.
- `samples/` contains real test input PDFs; expected outputs are `*.epub` files next to them (git-ignored). When running manual conversions or `validate_epub.py`, use any PDF in `samples/`.
- Gotcha: rendering many/large PDF pages at high resolution produces big files fast — keep test renders small.
- Gotcha: on a Windows machine, `.venv\Scripts\python.exe -c "<code>"` hangs forever in pyrepl interactive reader — always run code via a script file (`python.exe script.py`) or through pytest.
