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
  in the output file. By default normal pages are rendered at the size the PDF
  declares, while `--mrc-extract` retains each source image's native pixels.
  Supplying `-r` explicitly authorizes resampling extracted MRC resources.
- `--pages <selection>`: include only the selected PDF pages. Use 1-based page
  numbers and inclusive ranges separated by commas, such as
  `--pages 2,3,5-10,21`. Pages are included in the requested order.
- `--image-format <png|jpeg|auto>`: page image format. Normal rendering
  defaults to `png`. When omitted with `--mrc-extract`, compatible JPEG/PNG
  source resources are copied unchanged and unsupported codecs are losslessly
  adapted to PNG. Explicit `jpeg` or `auto` authorizes lossy recompression;
  `jpeg` usually makes scans and photographic pages much smaller.
- `--quality <1-100>`: JPEG quality used by `jpeg` and `auto` (default: `85`).
- `--mrc`: generate Mixed Raster Content for every rendered page. The render
  is separated into background and foreground color layers with a lossless
  full-resolution 1-bit selector. This can be combined with `--mrc-extract`
  to generate MRC for pages that cannot be extracted safely.
- `--mrc-color-scale <factor>`: downsample generated MRC background and
  foreground color planes by this integer factor while retaining the sharp
  full-resolution selector (default: `4`). Larger factors are faster and
  smaller but retain less color detail. Use `1` for full-resolution planes;
  with PNG this reconstructs the normal render exactly.
- `--mrc-extract`: extract compatible existing Mixed Raster Content (MRC)
  pages as source-preserving layered EPUB pages. Native image dimensions,
  compatible compressed bytes, selector depth, page geometry, and invisible
  OCR text are retained where EPUB permits. Pages that are not recognized as
  safe two-layer MRC use the normal renderer. Extraction is skipped when
  cropping, margins, or transparent page backgrounds are requested.
- `--margin <pixels>`: add a uniform margin to every side of each rendered
  page. The value must be an integer greater than or equal to zero.
- `--margins <top> <right> <bottom> <left>`: add a separate margin to each
  side of every rendered page. Values are output pixels and must be integers
  greater than or equal to zero.
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

`--margin` and `--margins` are mutually exclusive. Added margins are applied
after cropping and scaling, so their sizes are exact output pixel counts.

## Metadata

Document metadata (title, author, subject, keywords and creation date) is
taken from the PDF and written into the EPUB. Empty fields are omitted; the
title falls back to the input filename if the PDF has none.

## Examples

Convert a paper at a wider resolution and name the output explicitly:

```
epub-pdf-wrap paper.pdf -o paper.epub -r 1400
```

Convert only pages 2, 3, 5 through 10, and 21:

```
epub-pdf-wrap paper.pdf --pages 2,3,5-10,21
```

Make a scan substantially smaller with JPEG, or choose the smaller suitable
format separately for each page:

```
epub-pdf-wrap scan.pdf --image-format jpeg --quality 85
epub-pdf-wrap mixed.pdf --image-format auto --quality 85
epub-pdf-wrap paper.pdf --mrc --image-format auto
epub-pdf-wrap color-paper.pdf --mrc --mrc-color-scale 2 --image-format auto
epub-pdf-wrap lossless.pdf --mrc --mrc-color-scale 1
epub-pdf-wrap mrc-scan.pdf --mrc-extract
epub-pdf-wrap mixed-mrc.pdf --mrc-extract --mrc --image-format auto
epub-pdf-wrap mrc-scan.pdf --mrc-extract --image-format jpeg --quality 85
```

JPEG cannot preserve transparency. Use PNG or auto with
`--transparent-background`; auto will select PNG for every transparent page.
Generated MRC is opaque and therefore cannot be combined with
`--transparent-background`. It uses Otsu luminance segmentation, which is
well suited to dark text or line art on light paper and remains deterministic
for photographs and other page content. Pillow performs luminance conversion,
histogramming, mask creation, downsampling and diffusion using native image
operations. Pixels outside each color class are filled by expanding nearby
visible colors and smoothing the synthesized area at color-plane resolution.
This avoids encoding a sharp copy of the selector silhouette in each color
image and reduces compression artifacts at its edges. The default 1/4-size
color planes intentionally trade fine color detail for size and speed; use
`--mrc-color-scale 1` when pixel-exact PNG reconstruction is required.

In EPUB 3, layered MRC pages use an SVG wrapper and keep the selector as a
separate lossless mask. Extracted MRC uses PDF page coordinates; each image
keeps its native dimensions and the reader aligns it to the original full-page
placement. EPUB 2 uses a transparent PNG overlay instead because its older
content model does not reliably support inline SVG; this combination is an
unavoidable EPUB 2 adaptation.

MRC extraction currently recognizes the common PDF form with two full-page
image layers, where the second layer has a grayscale soft mask. JPEG and PNG
resources are reused byte-for-byte when their decoded pixels already match
the effective PDF image. Unsupported codecs are decoded once and adapted by
default: JPEG 2000 is already lossy, so it is re-encoded as JPEG targeting a
size a few times its original compressed bytes (bounded by a quality floor
to avoid heavy blocking) instead of a fixed high quality, since the source
PDF already chose an aggressive compression level for that layer; other
unsupported codecs are saved losslessly as PNG. Explicit `jpeg`, `png` or
`auto` overrides this per-image default for every extracted layer. Pages with
other visible content or geometry are rendered normally rather than silently
dropping content.

MRC extraction keeps each source image's native pixel dimensions unless `-r`
is given, which can leave embedded scan resolution far above what the PDF
page displays (e.g. a 720pt-wide page with 1500px-wide scans). Pass `-r`
matching the page's displayed width to shrink extracted MRC output closer to
the original PDF's size when native scan resolution isn't needed.

Source and destination bits-per-component are checked. Grayscale selector
PNGs use depths of 1, 2, 4, 8, or a directly reusable 16-bit source. A 1-bit
PDF mask is stored as a true 1-bit PNG. RGB PNG requires at least 8 bits per
component under the PNG format, so lower-depth PDF color samples are expanded
without inventing intermediate values. Greater-than-8-bit images are reused
when already EPUB-compatible; the converter reports an error instead of
silently reducing their depth when lossless transcoding or resizing is not
possible with PyMuPDF's 8-bit pixmap API.

Add 20 pixels around every side, or use different values for each side:

```
epub-pdf-wrap comic.pdf --margin 20
epub-pdf-wrap comic.pdf --margins 10 20 30 40
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
