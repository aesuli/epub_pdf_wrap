"""Convert PDFs to EPUBs by rendering each page as an EPUB page."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .core import ConversionError, convert

try:
    __version__ = version("epub-pdf-wrap")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0.0.0"

__all__ = ["ConversionError", "convert", "__version__"]
