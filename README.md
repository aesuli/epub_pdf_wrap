# EPUB PDF wrap

Converts a PDF into an EPUB by rendering and wrapping each page of the PDF in
an EPUB page. This is specifically aimed at PDF files that cannot be converted
into EPUB any other way without corrupting their visual rendering (comics,
scientific papers...).

The generated EPUB uses EPUB 3 fixed-layout metadata by default: each PDF page
is one pre-paginated spine item.

## Installation

```
pip install epub-pdf-wrap
```

From source (with dev dependencies for testing):

```
pip install -e ".[dev]"
```

## Usage

```
epub-pdf-wrap <pdf-filename> [options]
```

By default the output filename is the input filename with the `pdf` extension
replaced by the `epub` extension. Running without `pip install` also works via
`python -m epub_pdf_wrap`.

### Options

- `-r <num>, --resolution <num>`: target render width in pixels for the pages
  in the output file. By default the pages are rendered at the resolution the
  PDF itself declares.
- `-c, --crop-global`: trim the white margins around the page content using
  one common inset for all pages (safe: never clips content on any page, all
  pages keep the same size).
- `--crop-page`: trim the white margins around each page's own content, page
  by page (trims more aggressively but page sizes may vary).
- `--transparent-background`: preserve unpainted PDF page areas as transparent
  PNG pixels. By default they are rendered white for consistent display across
  reader themes.
- `--nocover`: skip using the first page as the book's cover image. By
  default the first page of the PDF is also set as the EPUB cover.
- `--epub2`: generate EPUB 2 output for older readers. The default is EPUB 3
  fixed-layout output.

`-c/--crop-global` and `--crop-page` are mutually exclusive; with neither
flag the margins are left as-is.

## Metadata

Document metadata (title, author, subject, keywords and creation date) is
taken from the PDF and written into the EPUB. Empty fields are omitted; the
title falls back to the input filename if the PDF has none.

## Examples

Convert a paper at a wider resolution and name the output explicitly:

```
epub-pdf-wrap paper.pdf -o paper.epub -r 1400
```

## Development

```
python -m venv .venv
.venv\Scripts\activate        # Windows  (or `source .venv/bin/activate` elsewhere)
pip install -e ".[dev]"
pytest
```

`samples/` contains real input PDFs for manually verifying the output.

## License

Distributed under the BSD 3-Clause License; see `LICENSE`.
Copyright (c) 2026, Andrea Esuli (andrea@esuli.it).
