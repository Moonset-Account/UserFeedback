"""Font subsetting.

Given a font and a list of public texts, produce a compact subset font
that reproduces the shaping of exactly those texts:

* only glyphs reachable from the shaped output (including transitive
  composite components) are kept;
* composite reference cycles are detected and reported with the names
  of the involved components (:class:`CompositeCycleError`);
* the subset cmap contains *only* the code points occurring in the
  public texts -- component glyphs never gain external mappings, and
  removed characters cannot be looked up again even when they shared
  an outline (or a whole glyph) with a public character;
* kept glyphs are renumbered compactly (``.notdef`` stays at gid 0);
* duplicate outlines are merged (dedup never creates cmap entries);
* every public text is replayed through the subset font and the glyph
  sequence and positions must match the original exactly, otherwise
  :class:`SubsetVerificationError` is raised.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import (
    CompositeCycleError,
    MissingMappingError,
    SubsetVerificationError,
)
from .model import Component, Font, Glyph, Rule
from .shaping import shape
from .verify import verify_subset


@dataclass
class SubsetResult:
    font: Font  # the subset font (compactly renumbered)
    public_codepoints: frozenset  # code points exposed in the subset cmap
    kept_glyph_names: tuple  # names in subset glyph-id order
    old_to_new: dict  # original gid -> subset gid (kept glyphs only)
    dropped_glyph_names: tuple  # names of glyphs that were removed
    report: object  # playback VerificationReport


def _public_codepoints(font, public_texts):
    """Collect code points of the public texts; all must be mapped."""
    cps = set()
    for text in public_texts:
        for ch in text:
            cp = ord(ch)
            if cp not in font.cmap:
                raise MissingMappingError(cp)
            cps.add(cp)
    return cps


def _expand_components(font, roots):
    """Transitive closure of composite references starting at ``roots``.

    Raises :class:`CompositeCycleError` naming the involved components
    when a reference cycle is encountered.
    """
    kept = set()
    state = {}  # gid -> 1 (on stack) / 2 (done)
    stack = []

    def visit(gid):
        st = state.get(gid, 0)
        if st == 2:
            return
        if st == 1:
            start = stack.index(gid)
            cycle = [font.glyphs[g].name for g in stack[start:]]
            cycle.append(font.glyphs[gid].name)
            raise CompositeCycleError(cycle)
        state[gid] = 1
        stack.append(gid)
        kept.add(gid)
        for comp in font.glyphs[gid].components:
            visit(comp.glyph)
        stack.pop()
        state[gid] = 2

    for root in roots:
        visit(root)
    return kept


def _remap_glyph(glyph, gid_of):
    """Copy a glyph, remapping component references through ``gid_of``."""
    if not glyph.components:
        return Glyph(glyph.name, glyph.advance, glyph.outline, ())
    comps = tuple(
        Component(gid_of[c.glyph], c.dx, c.dy) for c in glyph.components
    )
    return Glyph(glyph.name, glyph.advance, glyph.outline, comps)


def _deduplicate(glyphs, direct_gids):
    """Merge glyphs with identical content.

    ``direct_gids`` are gids produced by shaping public texts (as
    opposed to component-only glyphs); they are preferred as
    representatives so that playback keeps the original glyph names.
    Glyph 0 (.notdef) is never merged.

    Returns ``rep_of``: old index -> representative index.
    """
    rep_of = {}
    key_to_rep = {}
    order = sorted(
        range(len(glyphs)),
        key=lambda g: (g not in direct_gids, g),
    )
    for gid in order:
        if gid == 0:
            rep_of[gid] = gid
            continue
        g = glyphs[gid]
        key = (g.advance, g.outline, g.components)
        if key in key_to_rep:
            rep_of[gid] = key_to_rep[key]
        else:
            key_to_rep[key] = gid
            rep_of[gid] = gid
    return rep_of


def subset_font(font, public_texts, deduplicate=True):
    """Subset ``font`` to the glyphs needed to shape ``public_texts``.

    Returns a :class:`SubsetResult`.  Raises
    :class:`MissingMappingError` if a public character is unmapped,
    :class:`CompositeCycleError` if reachable composite references form
    a cycle, and :class:`SubsetVerificationError` if playback of any
    public text through the subset does not reproduce the original
    glyph sequence and positions.
    """
    public_texts = list(public_texts)
    cps = _public_codepoints(font, public_texts)

    # 1. Shape every public text with the original font.  The "direct"
    #    glyph set is the union of:
    #    * the cmap targets of the public code points (base glyphs --
    #      needed so the subset cmap can map the characters at all,
    #      even when a substitution later replaces them), and
    #    * the glyphs appearing in the shaped (post-substitution)
    #      output.
    base = {font.cmap[cp] for cp in cps}
    direct = set(base)
    for text in public_texts:
        for pg in shape(font, text):
            direct.add(pg.gid)
    direct.add(0)  # always keep .notdef at gid 0

    # 2. Pull in transitive composite components (cycle check happens
    #    here and reports the involved component names).
    kept = _expand_components(font, direct)

    # 3. Compact renumbering: .notdef first, remaining glyphs keep
    #    their original relative order.
    ordered = sorted(kept)
    new_gid_of_old = {old: new for new, old in enumerate(ordered)}
    stage1 = [_remap_glyph(font.glyphs[old], new_gid_of_old) for old in ordered]

    # 4. Optional outline dedup (never touches the cmap, so removed
    #    characters stay unrecoverable even on shared outlines).
    if deduplicate:
        direct_stage1 = {new_gid_of_old[g] for g in direct}
        rep_of = _deduplicate(stage1, direct_stage1)
    else:
        rep_of = {i: i for i in range(len(stage1))}

    reps = sorted(set(rep_of.values()))
    final_gid_of_stage1 = {}
    final_glyphs = []
    for rep in reps:
        final_gid_of_stage1[rep] = len(final_glyphs)
        final_glyphs.append(stage1[rep])
    # Remap component references through the dedup merge.
    for i, g in enumerate(final_glyphs):
        if g.components:
            final_glyphs[i] = Glyph(
                g.name, g.advance, g.outline,
                tuple(Component(final_gid_of_stage1[rep_of[c.glyph]], c.dx, c.dy)
                      for c in g.components),
            )

    def final_gid(old_gid):
        return final_gid_of_stage1[rep_of[new_gid_of_old[old_gid]]]

    # 5. Subset cmap: exactly the public code points.  Internal
    #    component glyphs get no mapping; removed characters are absent
    #    even if they shared a glyph or outline with a public one.
    new_cmap = {cp: final_gid(font.cmap[cp]) for cp in sorted(cps)}

    # 6. Keep only rules whose every glyph survived; remap and dedupe.
    new_rules = []
    seen_rules = set()
    for rule in font.rules:
        if all(g in kept for g in rule.input + rule.output):
            mapped = Rule(
                tuple(final_gid(g) for g in rule.input),
                tuple(final_gid(g) for g in rule.output),
            )
            if mapped not in seen_rules:
                seen_rules.add(mapped)
                new_rules.append(mapped)

    subset = Font(final_glyphs, new_cmap, new_rules)

    # 7. Playback verification: every public text must reproduce the
    #    original glyph sequence and positions exactly.
    report = verify_subset(font, subset, public_texts)
    if not report.ok:
        raise SubsetVerificationError(report)

    kept_names = tuple(g.name for g in final_glyphs)
    dropped = tuple(
        g.name for i, g in enumerate(font.glyphs) if i not in kept
    )
    return SubsetResult(
        font=subset,
        public_codepoints=frozenset(cps),
        kept_glyph_names=kept_names,
        old_to_new={old: final_gid(old) for old in ordered},
        dropped_glyph_names=dropped,
        report=report,
    )
