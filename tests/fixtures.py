"""Shared synthetic font fixtures for the test suite.

All data is synthetic and local; nothing is loaded from disk or network.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fontsubset import build_font  # noqa: E402

FULLWIDTH_A = 0xFF21  # shares the *same glyph* as public "A"
CYRILLIC_A = 0x0410  # distinct glyph, *identical outline* to public "A"


def build_latin_font():
    """Latin-ish font exercising ligatures, contextual rules, shared
    outlines, duplicate outlines and an unused glyph/rule."""
    return build_font(
        glyphs=[
            {"name": ".notdef", "advance": 600},
            {"name": "space", "advance": 240},
            {"name": "f", "advance": 300, "outline": "f"},
            {"name": "i", "advance": 240, "outline": "i"},
            # component-only duplicate of "i" (same outline data)
            {"name": "i.copy", "advance": 240, "outline": "i"},
            # component-only glyph, never reachable from any cmap entry
            {"name": "dotlessi", "advance": 240, "outline": "dotlessi"},
            {"name": "fi", "advance": 560,
             "components": [("f", 0, 0), ("i.copy", 300, 0)]},
            {"name": "a", "advance": 500, "outline": "a"},
            {"name": "b", "advance": 520, "outline": "b"},
            {"name": "b.alt", "advance": 520, "outline": "b.alt"},
            {"name": "x", "advance": 480, "outline": "x"},
            {"name": "A", "advance": 640, "outline": "A"},
            # removed character's glyph: outline identical to "A"
            {"name": "A.cyr", "advance": 640, "outline": "A"},
            {"name": "unused", "advance": 100, "outline": "unused"},
        ],
        cmap={
            ord(" "): "space",
            ord("f"): "f",
            ord("i"): "i",
            ord("a"): "a",
            ord("b"): "b",
            ord("x"): "x",
            ord("A"): "A",
            FULLWIDTH_A: "A",      # removed char, same glyph as public A
            CYRILLIC_A: "A.cyr",   # removed char, same outline as public A
        },
        rules=[
            (["f", "i"], ["fi"]),            # ligature
            (["a", "b"], ["a", "b.alt"]),    # contextual: b after a
            (["unused", "x"], ["x"]),        # references a dropped glyph
        ],
    )


LATIN_PUBLIC_TEXTS = ["fi", "ab xA", "fif", "A", ""]


def build_ffi_font(rule_order=0):
    """Font with interacting ligatures (f/f/i) in two rule orders."""
    rulesets = [
        [(["f", "f"], ["ff"]), (["f", "i"], ["fi"]), (["ff", "i"], ["ffi"])],
        [(["f", "i"], ["fi"]), (["f", "f"], ["ff"]), (["ff", "i"], ["ffi"])],
    ]
    return build_font(
        glyphs=[
            {"name": ".notdef", "advance": 0},
            {"name": "f", "advance": 300, "outline": "f"},
            {"name": "i", "advance": 240, "outline": "i"},
            {"name": "ff", "advance": 580,
             "components": [("f", 0, 0), ("f", 290, 0)]},
            {"name": "fi", "advance": 540,
             "components": [("f", 0, 0), ("i", 300, 0)]},
            {"name": "ffi", "advance": 840,
             "components": [("ff", 0, 0), ("i", 590, 0)]},
        ],
        cmap={ord("f"): "f", ord("i"): "i"},
        rules=rulesets[rule_order],
    )


def build_cycle_font():
    """c1 -> c2 -> c3 -> c1 composite reference cycle."""
    return build_font(
        glyphs=[
            {"name": ".notdef", "advance": 0},
            {"name": "c1", "advance": 100, "components": [("c2", 0, 0)]},
            {"name": "c2", "advance": 100, "components": [("c3", 0, 0)]},
            {"name": "c3", "advance": 100, "components": [("c1", 0, 0)]},
        ],
        cmap={ord("z"): "c1"},
    )


def build_self_cycle_font():
    return build_font(
        glyphs=[
            {"name": ".notdef", "advance": 0},
            {"name": "s", "advance": 100, "components": [("s", 0, 0)]},
        ],
        cmap={ord("s"): "s"},
    )


def build_unreachable_cycle_font():
    """A well-formed public part plus an unreachable reference cycle."""
    return build_font(
        glyphs=[
            {"name": ".notdef", "advance": 0},
            {"name": "a", "advance": 500, "outline": "a"},
            {"name": "c1", "advance": 100, "components": [("c2", 0, 0)]},
            {"name": "c2", "advance": 100, "components": [("c1", 0, 0)]},
        ],
        cmap={ord("a"): "a", ord("z"): "c1"},
    )
