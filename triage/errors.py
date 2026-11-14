"""Exception hierarchy for the triage pipeline."""


class TriageError(Exception):
    """Fatal error that prevents any plan from being produced (exit code 2)."""


class ManifestError(TriageError):
    """A JSON index is malformed at a level where no batch can be derived."""


class UnsafePathError(TriageError):
    """An artifact path escapes its declared root or traverses a symlink."""
