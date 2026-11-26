"""Exceptions for dictionary-coded grouped reductions."""

from __future__ import annotations


class DictionaryReductionError(ValueError):
    """Base class for all validation and reduction failures."""


class DictionaryStructureError(DictionaryReductionError):
    """A local dictionary or its codes are inconsistent."""


class CodeOutOfRangeError(DictionaryStructureError):
    """A non-null row references a missing local dictionary code."""


class NullMarkerError(DictionaryStructureError):
    """A null marker sequence or bitmap is invalid."""


class MeasureError(DictionaryReductionError):
    """An integer measure column is invalid."""


class RowIdentifierError(DictionaryReductionError):
    """Global row identifiers are missing or invalid."""


class DuplicateGlobalRowError(RowIdentifierError):
    """The same global row number was used by two source positions."""


class OrderDeclarationError(DictionaryReductionError):
    """Declared row or value ordering does not match the data."""


class DomainValidationError(DictionaryReductionError):
    """A value is outside its comparable domain or cannot be grouped."""


class DomainMismatchError(DictionaryReductionError):
    """Segments were supplied with incompatible comparable domains."""


class CodeCollisionError(DictionaryReductionError):
    """A stable dictionary code is already bound to another value."""
