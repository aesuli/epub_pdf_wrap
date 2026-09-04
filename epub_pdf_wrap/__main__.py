"""Command line entry point."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .core import (
    DEFAULT_JPEG_QUALITY,
    DEFAULT_MRC_COLOR_SCALE,
    IMAGE_FORMATS,
    ConversionError,
    convert,
)


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be an integer greater than or equal to 0")
    return number


def _jpeg_quality(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 100")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be an integer greater than 0")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="epub-pdf-wrap",
        description=(
            "Convert a PDF into an EPUB by rendering and wrapping each page "
            "of the PDF in an EPUB page. Aims at PDFs whose layout would be "
            "corrupted by text-based conversion (comics, scientific papers)."
        ),
        epilog="Copyright (c) 2026 Andrea Esuli — BSD-3-Clause license",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"epub-pdf-wrap {__version__}",
    )
    parser.add_argument("input", help="input PDF file")
    parser.add_argument(
        "-o", "--output",
        help="output EPUB file (default: input filename with .epub extension)",
    )
    parser.add_argument(
        "--title",
        help="override the EPUB title extracted from the PDF",
    )
    parser.add_argument(
        "--author",
        help="override the EPUB author extracted from the PDF",
    )
    parser.add_argument(
        "-r", "--resolution", type=int,
        help=(
            "target output width in pixels; explicitly resamples extracted "
            "MRC images (default: preserve their native pixels)"
        ),
    )
    parser.add_argument(
        "--pages", metavar="SELECTION",
        help=(
            "pages to include, as numbers and inclusive ranges "
            "(for example: 2,3,5-10,21)"
        ),
    )
    parser.add_argument(
        "--image-format", choices=IMAGE_FORMATS,
        help=(
            "page image format: png, jpeg, or auto (default: PNG for rendered "
            "pages; --mrc-extract preserves compatible source images and "
            "losslessly converts unsupported codecs to PNG)"
        ),
    )
    parser.add_argument(
        "--quality", type=_jpeg_quality, default=DEFAULT_JPEG_QUALITY,
        metavar="1-100",
        help=(
            f"JPEG quality for --image-format jpeg or auto "
            f"(default: {DEFAULT_JPEG_QUALITY})"
        ),
    )
    parser.add_argument(
        "--mrc", action="store_true",
        help=(
            "split every rendered page into Mixed Raster Content background, "
            "foreground and selector layers"
        ),
    )
    parser.add_argument(
        "--mrc-color-scale", type=_positive_int,
        default=DEFAULT_MRC_COLOR_SCALE, metavar="FACTOR",
        help=(
            "downsample generated MRC color planes by this factor while "
            f"keeping the mask full-size (default: {DEFAULT_MRC_COLOR_SCALE}; "
            "use 1 for full resolution)"
        ),
    )
    parser.add_argument(
        "--mrc-extract", action="store_true",
        help=(
            "extract compatible existing PDF MRC image layers into layered "
            "EPUB pages; other pages are rendered and honor --mrc"
        ),
    )
    margin = parser.add_mutually_exclusive_group()
    margin.add_argument(
        "--margin", type=_non_negative_int, metavar="PIXELS",
        help="add the same margin in pixels to every side of each page",
    )
    margin.add_argument(
        "--margins", type=_non_negative_int, nargs=4,
        metavar=("TOP", "RIGHT", "BOTTOM", "LEFT"),
        help="add top, right, bottom and left margins in pixels",
    )
    crop = parser.add_mutually_exclusive_group()
    crop.add_argument(
        "-c", "--crop-global", action="store_true",
        help="trim white margins using one common inset for all pages",
    )
    crop.add_argument(
        "--crop-page", action="store_true",
        help="trim white margins per page, to each page's own content",
    )
    parser.add_argument(
        "--transparent-background", action="store_true",
        help="preserve unpainted page areas as transparent (default: white)",
    )
    parser.add_argument(
        "--nocover", action="store_true",
        help="do not use the first page as the book's cover (default: cover on)",
    )
    parser.add_argument(
        "--epub2", action="store_true",
        help="write EPUB 2 instead of EPUB 3 for compatibility with older readers",
    )
    return parser


def _progress(done: int, total: int) -> None:
    # Redraw a single-line progress meter over the conversion.
    bar_width = 30
    filled = int(bar_width * done / total) if total else bar_width
    bar = "#" * filled + "-" * (bar_width - filled)
    sys.stdout.write(f"\rrendering {bar} {done}/{total}")
    if done != total:
        sys.stdout.flush()
    else:
        sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    crop = "global" if args.crop_global else ("page" if args.crop_page else None)
    margins = ((args.margin,) * 4 if args.margin is not None
               else tuple(args.margins) if args.margins is not None else None)
    try:
        out = convert(
            args.input,
            args.output,
            args.resolution,
            crop=crop,
            cover=not args.nocover,
            epub2=args.epub2,
            transparent_background=args.transparent_background,
            margins=margins,
            image_format=args.image_format,
            quality=args.quality,
            mrc=args.mrc,
            mrc_color_scale=args.mrc_color_scale,
            mrc_extract=args.mrc_extract,
            pages=args.pages,
            title=args.title,
            author=args.author,
            log=lambda line: print(line),
            progress=_progress,
        )
    except ConversionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
