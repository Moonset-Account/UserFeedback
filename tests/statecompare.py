"""Canonicalization helpers for comparing recovered vs. reference state.

Object identities differ between two independent interpreter runs (allocation
order gives different oids), so equality is expressed structurally: every
mutable object is replaced by a canonical label, and fields become nested
labels.  Two graphs are equivalent when their label maps are equal, which
checks aliasing (same label reused), cycles (labels appear inside their own
fields) and field values in one comparison.
"""

from __future__ import annotations

from typing import Any

from deopt.interpreter import Frame, Interpreter
from deopt.ir import IRObject


def canonical_graph(roots: list[Any]) -> dict[str, dict[str, Any]]:
    """Return ``{label: {field: label-or-scalar}}`` reachable from ``roots``."""

    labels: dict[int, str] = {}
    graph: dict[str, dict[str, Any]] = {}

    def label_for(obj: IRObject) -> str:
        key = id(obj)
        if key not in labels:
            labels[key] = f"{obj.tag}#{len(labels)}"
        return labels[key]

    def walk(value: Any) -> Any:
        if isinstance(value, IRObject):
            label = label_for(value)
            if label not in graph:
                graph[label] = {}  # placeholder first: breaks cycles
                graph[label] = {
                    name: walk(field) for name, field in value.fields.items()
                }
            return label
        return value

    for root in roots:
        walk(root)
    return graph


def frame_roots(frames: list[Frame]) -> list[Any]:
    roots: list[Any] = []
    for frame in frames:
        roots.extend(frame.locals.values())
    return roots


def frame_signature(interp: Interpreter) -> list[tuple[str, int, list[str]]]:
    """Frame name/pc and assigned local names, excluding unreturned slots.

    The reference interpreter reserves a call's destination slot only when
    the call returns (see :attr:`Frame.pending_slots`); recovered frames list
    exactly their live locals.  Both views therefore exclude pending slots.
    """

    return [
        (f.function.name, f.pc,
         sorted(name for name in f.locals if name not in f.pending_slots))
        for f in interp.frames
    ]


def assert_states_equivalent(testcase: Any,
                             recovered: Interpreter,
                             reference: Interpreter,
                             in_flight_equal: bool = True) -> None:
    """Compare frame layout, object relationships and in-flight exception."""

    testcase.assertEqual(
        frame_signature(recovered), frame_signature(reference),
        "frame names/pcs/live-locals must match at the same logical position",
    )
    g1 = canonical_graph(frame_roots(recovered.frames))
    g2 = canonical_graph(frame_roots(reference.frames))
    testcase.assertEqual(
        g1, g2,
        "object graph (aliases, cycles, field values) must match",
    )
    # Aliasing is structural equality's strongest claim; assert it directly
    # on the recovered interpreter as well.
    _assert_aliasing(testcase, recovered)
    if in_flight_equal:
        testcase.assertEqual(recovered._in_flight, reference._in_flight)


def _assert_aliasing(testcase: Any, interp: Interpreter) -> None:
    """Assert surviving references of each tag share one object.

    The normal continuation retains the inlined frames with many aliasing
    locals; the exception-edge continuation may pop them, in which case only
    the surviving frame(s) are checked.  Cross-object aliasing (the cycle) is
    fully covered by the canonical-graph comparison either way.
    """

    by_name: dict[str, dict[str, IRObject]] = {}
    for frame in interp.frames:
        for name, value in frame.locals.items():
            if isinstance(value, IRObject):
                by_name.setdefault(value.tag, {})[name] = value

    for tag, refs in by_name.items():
        testcase.assertGreaterEqual(len(refs), 1, tag)
        testcase.assertEqual(
            len({id(o) for o in refs.values()}), 1,
            f"all surviving {tag} references must alias one object",
        )

    # When both objects survive as locals, the required cycle is direct.
    if "Box" in by_name and "Node" in by_name:
        box = next(iter(by_name["Box"].values()))
        node = next(iter(by_name["Node"].values()))
        testcase.assertIs(box.fields["next"], node)
        testcase.assertIs(node.fields["back"], box)


def user_effects(effects: list[str]) -> list[str]:
    """Drop allocation/constructor effects, keeping observable user effects.

    Generic interpretation performs constructor effects during real
    allocations; the optimized trace eliminated those allocations, and
    recovery must not replay them.  Comparing only user-level effects is the
    externally meaningful contract.
    """

    return [e for e in effects if not e.startswith("ctor:")]
