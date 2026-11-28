"""Font resource subsetting with shaping playback verification.

Public API:

* :func:`fontsubset.build_font` -- build a :class:`fontsubset.Font`
  from name-based glyph/cmap/rule specifications.
* :func:`fontsubset.subset_font` -- subset a font to a list of public
  texts; returns :class:`fontsubset.SubsetResult`.
* :func:`fontsubset.shape` -- shape text into positioned glyphs.
* :func:`fontsubset.verify_subset` -- replay public texts through a
  subset font and compare glyph sequences/positions with the original.

Errors: :class:`FontSubsetError` and subclasses
(:class:`MissingMappingError`, :class:`CompositeCycleError`,
:class:`SubsetVerificationError`, :class:`UnknownGlyphError`,
:class:`DuplicateGlyphError`).
"""

from .errors import (
    CompositeCycleError,
    DuplicateGlyphError,
    FontSubsetError,
    MissingMappingError,
    SubsetVerificationError,
    UnknownGlyphError,
)
from .model import Component, Font, Glyph, Rule, build_font
from .shaping import PositionedGlyph, apply_rules, map_text, shape
from .subset import SubsetResult, subset_font
from .verify import PlaybackRecord, VerificationReport, verify_subset

__all__ = [
    "Component",
    "CompositeCycleError",
    "DuplicateGlyphError",
    "Font",
    "FontSubsetError",
    "Glyph",
    "MissingMappingError",
    "PlaybackRecord",
    "PositionedGlyph",
    "Rule",
    "SubsetResult",
    "SubsetVerificationError",
    "UnknownGlyphError",
    "VerificationReport",
    "apply_rules",
    "build_font",
    "map_text",
    "shape",
    "subset_font",
    "verify_subset",
]

__version__ = "0.1.0"
