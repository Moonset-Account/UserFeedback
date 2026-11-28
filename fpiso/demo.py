"""Scripted demonstrations of the isolation semantics.

Each scenario is a plain function that drives a :class:`~fpiso.runtime.Runtime`
with synthetic exact operands and returns a list of transcript lines.  The
scenarios are deterministic, so tests can assert on both the transcript and the
underlying state objects.

The four scenarios map to the four claims of the model:

``scenario_midpoints``
    One exact rounding midpoint evaluated by domains that differ only in their
    rounding attribute.  Every domain reports its own result and flags.
``scenario_masks``
    The same inexact event under different mask bits, showing that masking
    changes the pending queue but never the sticky state and never another
    domain's view.
``scenario_nested``
    An outer protection domain is suspended, a nested audit domain runs and
    exits.  The audit's traps are reported, not merged; the outer domain's
    sticky flags and pending queue are byte-for-byte unchanged.
``scenario_fma``
    A multiply-add whose fused single rounding differs from a two-step
    multiply-then-add, proving the product is never rounded first.
"""

from __future__ import annotations

from fractions import Fraction

from .domain import Domain
from .flags import Flag, MaskBits, flags_text
from .format import SMALL, Format, RoundingMode
from .runtime import Runtime

__all__ = [
    "AUDIT_FORMAT",
    "SMALL",
    "scenario_fma",
    "scenario_masks",
    "scenario_midpoints",
    "scenario_nested",
    "scenario_probe",
    "scenario_contrast",
]


def _frac(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _value_text(value) -> str:
    return str(value)


def _flags(flags) -> str:
    return flags_text(flags)


#: Audit format: one more significand bit and one wider exponent range than
#: :data:`SMALL`, so the audit domain's arithmetic genuinely differs.
AUDIT_FORMAT = Format(name="audit", precision=5, emin=-3, emax=4)


def scenario_midpoints() -> list[str]:
    """One exact midpoint, five rounding attributes, five isolated domains."""

    lines: list[str] = []
    lines.append("=== scenario 1: one exact midpoint, five domains ===")
    lines.append(
        "format small: p=4 emin=-2 emax=3 -> representable grid at 1 is "
        "1, 9/8, 5/4, ..."
    )
    lines.append(
        "exact operand: 1 + 1/16 = 17/16, exactly halfway between 1 and 9/8"
    )
    lines.append("")

    runtime = Runtime(name="midpoint")
    expected = {
        RoundingMode.RNE: "1",
        RoundingMode.RNA: "9/8",
        RoundingMode.RZ: "1",
        RoundingMode.RD: "1",
        RoundingMode.RU: "9/8",
    }
    for mode in RoundingMode:
        domain = Domain(f"mid-{mode.value}", SMALL, mode)
        runtime.register(domain)
        runtime.enter_domain(domain)
        result = runtime.add(1, Fraction(1, 16))
        state = runtime.observe()
        lines.append(
            f"  {mode.value:>3}: value {_value_text(result.value):<5} "
            f"(expected {expected[mode]:<5}) flags {_flags(result.flags):<20} "
            f"sticky {_flags(state.sticky):<20} pending "
            f"{'[' + ', '.join(f.value for f in state.pending) + ']' if state.pending else '[]'}"
        )
        runtime.exit_domain()
    lines.append("")
    lines.append(
        "same exact operand, same {inexact} event, five different values; "
        "each domain's sticky state holds only its own flag."
    )
    return lines


def scenario_masks() -> list[str]:
    """Identical arithmetic under different mask bits."""

    lines: list[str] = []
    lines.append("=== scenario 2: mask bits decide the pending queue, not the value ===")
    lines.append("exact operand: 1/3 in small, which rounds to 11/32 in every mode")
    lines.append("")

    runtime = Runtime(name="masks")
    variants = {
        "all-masked": MaskBits.all_masked(),
        "inexact-unmasked": MaskBits.unmasking(Flag.INEXACT),
        "underflow-unmasked": MaskBits.unmasking(Flag.UNDERFLOW),
        "all-unmasked": MaskBits.all_unmasked(),
    }
    for label, masks in variants.items():
        domain = Domain(label, SMALL, RoundingMode.RNE, masks)
        runtime.register(domain)
        runtime.enter_domain(domain)
        result = runtime.mul(Fraction(1, 3), 1)
        state = runtime.observe()
        lines.append(
            f"  {label:<20} [{masks.describe()}]"
        )
        lines.append(
            f"  {'':<20} value {result.value} flags {_flags(result.flags)} "
            f"sticky {_flags(state.sticky)} "
            f"pending {[flag.value for flag in state.pending]}"
        )
        report = runtime.exit_domain()
        lines.append(f"  {'':<20} {report.describe()}")
        lines.append("")
    lines.append(
        "masking never changes the value or the sticky set; it only decides "
        "whether a trap is also queued for the owning domain."
    )
    return lines


def scenario_nested() -> list[str]:
    """Suspend an outer domain, run a nested audit domain, resume."""

    lines: list[str] = []
    lines.append("=== scenario 3: nested audit domain over a suspended outer domain ===")

    runtime = Runtime(name="nested")
    outer = Domain(
        "protect",
        Format("small", 4, -2, 3),
        RoundingMode.RNE,
        MaskBits.unmasking(Flag.OVERFLOW, Flag.INEXACT),
    )
    audit = Domain(
        "audit",
        AUDIT_FORMAT,
        RoundingMode.RU,
        MaskBits.unmasking(Flag.UNDERFLOW),
    )
    runtime.register(outer)
    runtime.register(audit)

    runtime.enter_domain(outer, label="observation")
    first = runtime.add(1, Fraction(1, 16))
    second = runtime.fma(16, 1, 0)
    outer_state = runtime.observe()
    lines.append(
        "outer 'protect' does two operations:"
    )
    lines.append(
        f"  add(1, 1/16)  -> {first.value} flags {_flags(first.flags)} "
        f"pending {[flag.value for flag in first.pending]}"
    )
    lines.append(
        f"  fma(16, 1, 0) -> {second.result.value} flags "
        f"{_flags(second.result.flags)} "
        f"pending {[flag.value for flag in second.result.pending]}"
    )
    lines.append(
        f"  outer before nesting: sticky {_flags(outer_state.sticky)} "
        f"pending {[flag.value for flag in outer_state.pending]}"
    )
    lines.append("")

    runtime.enter_domain(audit, audit=True)
    audit_first = runtime.add(1, Fraction(1, 32))
    audit_second = runtime.mul(Fraction(1, 64), Fraction(1, 64))
    audit_state = runtime.observe()
    paused_outer = runtime.inspect("protect")
    lines.append("nested audit 'audit' is now the active domain:")
    lines.append(
        f"  add(1, 1/32)        -> {audit_first.value} flags "
        f"{_flags(audit_first.flags)} pending "
        f"{[flag.value for flag in audit_first.pending]}"
    )
    lines.append(
        f"  mul(1/64, 1/64)     -> {audit_second.value} flags "
        f"{_flags(audit_second.flags)} pending "
        f"{[flag.value for flag in audit_second.pending]}"
    )
    lines.append(
        f"  audit reads itself : sticky {_flags(audit_state.sticky)} "
        f"pending {[flag.value for flag in audit_state.pending]}"
    )
    lines.append(
        f"  harness reads outer: sticky {_flags(paused_outer.sticky)} "
        f"pending {[flag.value for flag in paused_outer.pending]}  (unchanged)"
    )

    blocked = None
    try:
        outer.take_pending()
    except Exception as exc:  # noqa: BLE001 - demonstration of the guard
        blocked = type(exc).__name__
    lines.append(
        f"  audit tries to drain outer pending -> blocked with {blocked}"
    )

    report = runtime.exit_domain(consume_pending=True)
    resumed = runtime.observe()
    lines.append("")
    lines.append(f"  {report.describe()}")
    lines.append(
        f"  after resume      : sticky {_flags(resumed.sticky)} "
        f"pending {[flag.value for flag in resumed.pending]}"
    )
    lines.append(
        f"  audit after exit  : sticky {_flags(audit.sticky)} "
        f"pending {[flag.value for flag in audit.pending]}"
    )
    lines.append(
        "  audit re-entered  : its own sticky "
        f"{_flags(audit.sticky)} is intact for its own next run"
    )

    drained = runtime.require_active().take_pending()
    lines.append(
        f"  outer drains own  : {[flag.value for flag in drained]}"
    )
    lines.append("")
    lines.append(
        "the audit's underflow never entered the outer domain, and the outer "
        "pending overflow/inexact were never consumed by the audit."
    )
    return lines


def scenario_fma() -> list[str]:
    """Fused multiply-add versus multiply-then-add."""

    lines: list[str] = []
    lines.append("=== scenario 4: multiply-add rounds exactly once ===")

    runtime = Runtime(name="fma")
    fused = Domain(
        "fused",
        Format("small", 4, -2, 3),
        RoundingMode.RNE,
        MaskBits.all_unmasked(),
    )
    split = Domain(
        "split",
        Format("small", 4, -2, 3),
        RoundingMode.RNE,
        MaskBits.all_unmasked(),
    )
    runtime.register(fused)
    runtime.register(split)

    a = Fraction(9, 8)
    b = Fraction(9, 8)
    c = Fraction(-5, 4)

    runtime.enter_domain(fused)
    trace = runtime.fma(a, b, c)
    lines.append(
        f"  fused fma({_frac(a)}, {_frac(b)}, {_frac(c)}):"
    )
    lines.append(
        f"    exact product {_frac(trace.product)}, exact sum "
        f"{_frac(trace.exact)}, one rounding -> {trace.result.value} "
        f"flags {_flags(trace.result.flags)}"
    )
    runtime.exit_domain()

    runtime.enter_domain(split)
    product = runtime.mul(a, b)
    total = runtime.add(product.value, c)
    lines.append(
        f"  split mul then add:"
    )
    lines.append(
        f"    mul -> {product.value} flags {_flags(product.flags)}"
    )
    lines.append(
        f"    add -> {total.value} flags {_flags(total.flags)}"
    )
    lines.append(
        f"    split sticky {_flags(split.sticky)}"
    )
    runtime.exit_domain()

    lines.append("")
    lines.append(
        "the two-step path rounds 81/64 to 5/4 (inexact) and then subtracts "
        "5/4 exactly, so its final add raises nothing and the exact 1/64 "
        "difference is lost; the fused path keeps 81/64 exact, subtracts, and "
        "its single rounding of 1/64 is tiny and inexact -- the correct "
        "verdict for the true result."
    )
    return lines


def scenario_probe() -> list[str]:
    """Show which state each domain can read while a sibling is paused."""

    lines: list[str] = []
    lines.append("=== scenario 5: exact state visible to each domain ===")

    runtime = Runtime(name="probe")
    left = Domain(
        "left",
        Format("small", 4, -2, 3),
        RoundingMode.RD,
        MaskBits.unmasking(Flag.INEXACT),
    )
    right = Domain(
        "right",
        Format("small", 4, -2, 3),
        RoundingMode.RU,
        MaskBits.unmasking(Flag.INEXACT),
    )
    runtime.register(left)
    runtime.register(right)

    runtime.enter_domain(left)
    runtime.add(-1, Fraction(-1, 16))
    left_view = runtime.observe()
    lines.append(f"  left active, reads itself: {left_view.describe()}")

    runtime.enter_domain(right)
    right_view = runtime.observe()
    left_paused = runtime.inspect("left")
    lines.append(f"  right active, reads itself: {right_view.describe()}")
    lines.append(f"  harness reads paused left : {left_paused.describe()}")
    runtime.add(1, Fraction(1, 16))
    lines.append(
        f"  right after add(1, 1/16)  : {runtime.observe().describe()}"
    )
    lines.append(
        f"  paused left is unchanged  : "
        f"{runtime.inspect('left') == left_view}"
    )
    runtime.exit_domain()
    lines.append(f"  left resumed              : {runtime.observe().describe()}")
    runtime.exit_domain()
    return lines


def scenario_contrast() -> list[str]:
    """Contrast with a deliberately leaky shared-state runtime.

    This exists only to demonstrate the failure mode the model prevents; it is
    labelled and never used by :class:`~fpiso.runtime.Runtime`.
    """

    lines: list[str] = []
    lines.append("=== scenario 6: the failure mode, for contrast ===")

    class LeakyRuntime:
        """A single global flag word shared by every 'domain'."""

        shared_sticky: set[Flag] = set()
        shared_pending: list[Flag] = []

        def __init__(self) -> None:
            self.own_rounding = RoundingMode.RNE
            self.own_masks = MaskBits.all_masked()

        def switch(self, rounding: RoundingMode, masks: MaskBits) -> None:
            # A naive context switch saves the rounding attribute and mask bits
            # but leaves the status word and handler queue global.
            self.own_rounding = rounding
            self.own_masks = masks

        def latch(self, flags) -> None:
            for flag in flags:
                LeakyRuntime.shared_sticky.add(flag)
                if self.own_masks.is_unmasked(flag):
                    LeakyRuntime.shared_pending.append(flag)

    leaky = LeakyRuntime()
    leaky.latch({Flag.INEXACT})
    outer_after = set(LeakyRuntime.shared_sticky)
    leaky.switch(RoundingMode.RU, MaskBits.unmasking(Flag.UNDERFLOW))
    leaky.latch({Flag.UNDERFLOW, Flag.INEXACT})
    inner_view = set(LeakyRuntime.shared_sticky)
    leaky.switch(RoundingMode.RNE, MaskBits.all_masked())
    back_view = set(LeakyRuntime.shared_sticky)

    lines.append(f"  leaky shared state after outer op : {_flags(outer_after)}")
    lines.append(f"  leaky shared state seen by inner  : {_flags(inner_view)}")
    lines.append(f"  leaky shared state seen on resume : {_flags(back_view)}")
    lines.append(
        f"  leaky shared pending queue        : "
        f"{[flag.value for flag in LeakyRuntime.shared_pending]}"
    )
    lines.append("")
    lines.append(
        "the inner underflow survives into the resumed outer domain and the "
        "inner handler queue is visible to everyone; fpiso's runtime reports "
        "the inner traps only in the inner exit report."
    )
    return lines


def all_scenarios():
    return [
        ("midpoints", scenario_midpoints),
        ("masks", scenario_masks),
        ("nested", scenario_nested),
        ("fma", scenario_fma),
        ("probe", scenario_probe),
        ("contrast", scenario_contrast),
    ]
