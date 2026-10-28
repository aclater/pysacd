"""Pure-Python reader for SACD (Scarletbook) ISO images."""

from .toc import (
    Area,
    Disc,
    Genre,
    Locale,
    SACDError,
    SACDImage,
    Timecode,
    Track,
    read_toc,
)

__all__ = [
    "Area", "Disc", "Genre", "Locale", "SACDError", "SACDImage",
    "Timecode", "Track", "read_toc",
]
__version__ = "0.1.0"
