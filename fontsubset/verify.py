"""Playback verification: re-shape public texts with the subset font and
compare glyph sequences and positions against the original font."""

from __future__ import annotations

from dataclasses import dataclass, field

from .shaping import shape, signature


@dataclass
class PlaybackRecord:
    text: str
    expected: tuple  # (name, x, y) tuples from the original font
    actual: tuple  # (name, x, y) tuples from the subset font
    ok: bool
    detail: str = ""


@dataclass
class VerificationReport:
    records: list = field(default_factory=list)

    @property
    def ok(self):
        return all(r.ok for r in self.records)

    def __str__(self):
        lines = []
        for rec in self.records:
            status = "OK  " if rec.ok else "FAIL"
            lines.append(f"[{status}] {rec.text!r} ({len(rec.expected)} glyphs)")
            if not rec.ok:
                lines.append(f"       {rec.detail}")
        verdict = "all texts verified" if self.ok else "verification FAILED"
        lines.append(verdict)
        return "\n".join(lines)


def _diff_detail(expected, actual):
    if len(expected) != len(actual):
        return (f"glyph count differs: expected {len(expected)}, "
                f"got {len(actual)}; expected={list(expected)!r} "
                f"actual={list(actual)!r}")
    for i, (e, a) in enumerate(zip(expected, actual)):
        if e != a:
            return (f"glyph {i} differs: expected {e!r}, got {a!r}")
    return "unknown difference"


def verify_subset(original, subset, public_texts):
    """Shape every public text with both fonts and compare results.

    Returns a :class:`VerificationReport`.  Glyph *names* and pen
    positions must match exactly; glyph ids may differ because the
    subset is renumbered.
    """
    report = VerificationReport()
    for text in public_texts:
        expected = signature(shape(original, text))
        actual = signature(shape(subset, text))
        ok = expected == actual
        report.records.append(PlaybackRecord(
            text=text,
            expected=expected,
            actual=actual,
            ok=ok,
            detail="" if ok else _diff_detail(expected, actual),
        ))
    return report
