# EPUB PDF wrap

Converts a PDF into an EPUB by rendering and wrapping each page of the PDF in
an EPUB page. This is specifically aimed at PDF files that cannot be converted
into EPUB any other way without corrupting their visual rendering (comics,
scientific papers...).

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
epub-pdf-wrap <pdf-filename> [-o <epub-filename>]
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

`-c/--crop-global` and `--crop-page` are mutually exclusive; with neither
flag the margins are left as-is.

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
