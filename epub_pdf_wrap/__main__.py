"""Command line entry point."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .core import ConversionError, convert


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
        "-r", "--resolution", type=int,
        help="target render width in pixels for the output (default: input resolution)",
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
    try:
        out = convert(
            args.input,
            args.output,
            args.resolution,
            crop=crop,
            cover=not args.nocover,
            epub2=args.epub2,
            transparent_background=args.transparent_background,
            log=lambda line: print(line),
            progress=_progress,
        )
    except ConversionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
