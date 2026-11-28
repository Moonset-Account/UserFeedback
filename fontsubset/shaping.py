"""Shaping: map text to positioned glyph sequences using cmap + rules.

Rules are applied in declaration order.  Each rule scans the glyph
stream left to right and rewrites every non-overlapping occurrence of
its input sequence; scanning resumes after the replacement, so a rule
never re-fires on its own output.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import MissingMappingError


@dataclass(frozen=True)
class PositionedGlyph:
    gid: int
    name: str
    x: int
    y: int


def apply_rules(rules, gids):
    """Apply substitution rules (in order) to a glyph id sequence."""
    stream = list(gids)
    for rule in rules:
        stream = _apply_rule(rule, stream)
    return stream


def _apply_rule(rule, gids):
    n = len(rule.input)
    out = []
    i = 0
    while i < len(gids):
        if tuple(gids[i:i + n]) == rule.input:
            out.extend(rule.output)
            i += n
        else:
            out.append(gids[i])
            i += 1
    return out


def map_text(font, text):
    """Map a string to glyph ids via the font's cmap."""
    gids = []
    for ch in text:
        cp = ord(ch)
        if cp not in font.cmap:
            raise MissingMappingError(cp)
        gids.append(font.cmap[cp])
    return gids


def shape(font, text):
    """Shape ``text`` into a list of :class:`PositionedGlyph`.

    Positions come from cumulative advances (pen starts at x=0, y=0).
    """
    gids = apply_rules(font.rules, map_text(font, text))
    result = []
    x = 0
    for gid in gids:
        glyph = font.glyphs[gid]
        result.append(PositionedGlyph(gid=gid, name=glyph.name, x=x, y=0))
        x += glyph.advance
    return result


def signature(shaped):
    """Hashable (name, x, y) view of a shaped run, used for comparison."""
    return tuple((g.name, g.x, g.y) for g in shaped)
