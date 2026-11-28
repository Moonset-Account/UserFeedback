"""Exception types raised by the font subsetting module."""


class FontSubsetError(Exception):
    """Base class for all errors raised by this package."""


class UnknownGlyphError(FontSubsetError):
    """A glyph name referenced in a spec does not exist in the glyph table."""

    def __init__(self, name, context=""):
        self.name = name
        self.context = context
        where = f" ({context})" if context else ""
        super().__init__(f"unknown glyph {name!r}{where}")


class DuplicateGlyphError(FontSubsetError):
    """Two glyphs share the same name."""

    def __init__(self, name):
        self.name = name
        super().__init__(f"duplicate glyph name {name!r}")


class MissingMappingError(FontSubsetError):
    """A public text contains a character that has no cmap entry."""

    def __init__(self, codepoint):
        self.codepoint = codepoint
        super().__init__(
            f"no cmap mapping for U+{codepoint:04X} ({chr(codepoint)!r})"
        )


class CompositeCycleError(FontSubsetError):
    """Composite glyph references form a cycle.

    ``cycle`` lists the involved component glyph names in reference order,
    with the first name repeated at the end to close the loop.
    """

    def __init__(self, cycle):
        self.cycle = list(cycle)
        super().__init__(
            "composite reference cycle: " + " -> ".join(self.cycle)
        )


class SubsetVerificationError(FontSubsetError):
    """Playback verification of a subset font failed."""

    def __init__(self, report):
        self.report = report
        failed = [r for r in report.records if not r.ok]
        lines = ["subset playback verification failed:"]
        for rec in failed:
            lines.append(f"  text {rec.text!r}: {rec.detail}")
        super().__init__("\n".join(lines))
