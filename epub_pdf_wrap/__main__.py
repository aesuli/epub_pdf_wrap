"""Command line entry point."""

from __future__ import annotations

import argparse
import sys

from .core import ConversionError, convert


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="epub-pdf-wrap",
        description=(
            "Convert a PDF into an EPUB by rendering and wrapping each page "
            "of the PDF in an EPUB page. Aims at PDFs whose layout would be "
            "corrupted by text-based conversion (comics, scientific papers)."
        ),
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
    try:
        out = convert(
            args.input,
            args.output,
            args.resolution,
            log=lambda line: print(line),
            progress=_progress,
        )
    except ConversionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
