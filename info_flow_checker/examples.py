"""Small synthetic circuits used by tests and documentation."""

from __future__ import annotations

from .circuit import Circuit, CircuitBuilder


def cancellation_circuit() -> Circuit:
    """Secret reaches internal branches but their OR reconverges to ``p``.

    Circuit::

        ns    = NOT s
        left  = s AND p
        right = ns AND p
        out   = left OR right

    For every public ``p`` and secret ``s``, ``out == p``.  When ``p == 1``,
    the two branches visibly swap which one is high as ``s`` changes.
    """

    builder = CircuitBuilder("secret_cancellation")
    p = builder.input("p")
    s = builder.input("s")
    not_s = builder.not_(s, node_id="not_s")
    left = builder.and_(s, p, node_id="left")
    right = builder.and_(not_s, p, node_id="right")
    out = builder.or_(left, right, node_id="decision")
    builder.output("decision", out)
    builder.output("left", left)
    builder.output("right", right)
    builder.output("not_s", not_s)
    return builder.build()


def mux_equal_branches_circuit() -> Circuit:
    """A secret drives a MUX selector while both data branches equal ``p``."""

    builder = CircuitBuilder("mux_equal_branches")
    p = builder.input("p")
    s = builder.input("s")
    out = builder.mux(s, p, p, node_id="mux_out")
    builder.output("decision", out)
    return builder.build()


def xor_summary_circuit() -> Circuit:
    """Two secrets with an approved XOR summary and an over-revealing output.

    Outputs:

    * ``summary = a XOR b`` (approved)
    * ``leak_a = a`` (actual external bit in negative tests)
    """

    builder = CircuitBuilder("xor_summary")
    a = builder.input("a")
    b = builder.input("b")
    either = builder.or_(a, b, node_id="either")
    both = builder.and_(a, b, node_id="both")
    not_both = builder.not_(both, node_id="not_both")
    xor = builder.and_(either, not_both, node_id="summary_xor")
    builder.output("summary", xor)
    builder.output("leak_a", a)
    return builder.build()


def constant_heavy_circuit() -> Circuit:
    """Circuit containing foldable constants and a shared secret subgraph."""

    builder = CircuitBuilder("constant_heavy")
    p = builder.input("p")
    s = builder.input("s")
    true = builder.const(True, node_id="tt")
    false = builder.const(False, node_id="ff")

    shared = builder.and_(s, p, node_id="shared")
    out_and_true = builder.and_(shared, true, node_id="out_and_true")
    out_or_false = builder.or_(shared, false, node_id="out_or_false")
    dead_and_false = builder.and_(shared, false, node_id="dead_and_false")
    double_not = builder.not_(builder.not_(s, node_id="not_s"), node_id="double_not")
    mux_const_selector = builder.mux(false, s, p, node_id="mux_const_selector")
    mux_same_branches = builder.mux(s, p, p, node_id="mux_same_branches")

    builder.output("and_true", out_and_true)
    builder.output("or_false", out_or_false)
    builder.output("dead_zero", dead_and_false)
    builder.output("double_not", double_not)
    builder.output("mux_const", mux_const_selector)
    builder.output("mux_same", mux_same_branches)
    return builder.build()
