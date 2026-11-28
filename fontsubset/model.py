"""Simplified font data model used by the subsetter.

A font consists of:

* a glyph table (ordered, glyph id == index; glyph 0 is ``.notdef``),
* a character map ``cmap`` mapping Unicode code points to glyph ids,
* an ordered list of substitution rules.  A rule fires when its input
  glyph sequence appears as *adjacent* glyphs in the shaped stream and
  replaces it with its output sequence (ligature and contextual
  substitutions are both expressible).

Glyphs are either simple (they carry opaque, hashable ``outline`` data)
or composite (they reference other glyphs as components with offsets).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import DuplicateGlyphError, UnknownGlyphError

NOTDEF = ".notdef"


@dataclass(frozen=True)
class Component:
    """A reference to another glyph inside a composite glyph."""

    glyph: int  # glyph id of the referenced component
    dx: int = 0
    dy: int = 0


@dataclass
class Glyph:
    name: str
    advance: int
    outline: tuple = ()  # opaque hashable data for simple glyphs
    components: tuple = ()  # tuple[Component, ...] for composite glyphs

    @property
    def is_composite(self) -> bool:
        return bool(self.components)


@dataclass(frozen=True)
class Rule:
    """Adjacency-triggered substitution: ``input`` -> ``output``.

    Both are tuples of glyph ids.  ``input`` must be non-empty; the rule
    fires wherever the exact input sequence occurs consecutively.
    """

    input: tuple
    output: tuple

    def __post_init__(self):
        if not self.input:
            raise ValueError("rule input sequence must not be empty")
        if not self.output:
            raise ValueError("rule output sequence must not be empty")


class Font:
    """An in-memory font: glyph table + cmap + substitution rules."""

    def __init__(self, glyphs, cmap, rules=()):
        self.glyphs = list(glyphs)
        self.cmap = dict(cmap)  # codepoint -> glyph id
        self.rules = list(rules)
        self.name_to_gid = {}
        for gid, glyph in enumerate(self.glyphs):
            if glyph.name in self.name_to_gid:
                raise DuplicateGlyphError(glyph.name)
            self.name_to_gid[glyph.name] = gid

    def gid(self, name):
        return self.name_to_gid[name]

    def glyph_name(self, gid):
        return self.glyphs[gid].name

    def __len__(self):
        return len(self.glyphs)


def build_font(glyphs, cmap, rules=()):
    """Build a :class:`Font` from name-based specifications.

    ``glyphs``: list of dicts with keys ``name`` (str), ``advance`` (int),
    optional ``outline`` (hashable, simple glyphs) and optional
    ``components`` (list of ``(glyph_name, dx, dy)`` tuples).

    ``cmap``: dict mapping code points to glyph *names*.

    ``rules``: list of ``(input_names, output_names)`` pairs where each
    element is a list/tuple of glyph names.

    A ``.notdef`` glyph is prepended automatically when not supplied.
    """
    specs = [dict(g) for g in glyphs]
    if not specs or specs[0]["name"] != NOTDEF:
        specs.insert(0, {"name": NOTDEF, "advance": 0})

    names = [s["name"] for s in specs]
    if len(set(names)) != len(names):
        for n in names:
            if names.count(n) > 1:
                raise DuplicateGlyphError(n)
    name_to_gid = {n: i for i, n in enumerate(names)}

    def resolve(name, context):
        if name not in name_to_gid:
            raise UnknownGlyphError(name, context)
        return name_to_gid[name]

    table = []
    for spec in specs:
        comps = tuple(
            Component(resolve(c[0], f"component of {spec['name']!r}"),
                      int(c[1]) if len(c) > 1 else 0,
                      int(c[2]) if len(c) > 2 else 0)
            for c in spec.get("components", ())
        )
        outline = spec.get("outline", ())
        if not isinstance(outline, tuple):
            outline = (outline,)
        table.append(Glyph(
            name=spec["name"],
            advance=int(spec.get("advance", 0)),
            outline=outline,
            components=comps,
        ))

    resolved_cmap = {
        int(cp): resolve(name, f"cmap U+{int(cp):04X}")
        for cp, name in cmap.items()
    }
    resolved_rules = [
        Rule(
            tuple(resolve(n, "rule input") for n in inp),
            tuple(resolve(n, "rule output") for n in out),
        )
        for inp, out in rules
    ]
    return Font(table, resolved_cmap, resolved_rules)
