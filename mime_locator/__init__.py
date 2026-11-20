"""Read-only MIME evidence locator.

Public entry points:

* :func:`parse_message` -- parse a raw RFC 2045/2046 message into a tree.
* :class:`MimeArchive` -- parse a message and locate decoded selections.
* :class:`Location` -- raw encoded-unit ranges plus the hierarchy path.
"""

from .archive import MimeArchive
from .model import (
    CorruptedPartError,
    InvalidSelectionError,
    Location,
    MimeParseError,
    NotSelectableError,
    PartNode,
    PathEntry,
    Unit,
)
from .parser import parse_message

__all__ = [
    "MimeArchive",
    "Location",
    "PartNode",
    "PathEntry",
    "Unit",
    "MimeParseError",
    "InvalidSelectionError",
    "NotSelectableError",
    "CorruptedPartError",
    "parse_message",
]

__version__ = "1.0.0"
