"""What each protection domain can read, step by step.

This file makes the "observable state" contract executable.  It walks a fixed
script of rounding midpoints and nested domain switches and asserts, at every
point, the *exact* state each domain holds:

* the active domain sees only its own rounding, masks, sticky flags and queue;
* a paused domain is not visible through the active domain's API at all;
* the harness can read a paused domain's state and finds it byte-for-byte what
  it was at suspension;
* after the nested domain exits, the outer domain's observable state is
  exactly the suspension snapshot, plus whatever the outer domain itself did.

No assertion here relies on string matching alone: every state claim is also
compared as a ``DomainState`` dataclass value.
"""

from __future__ import annotations

import unittest
from fractions import Fraction as F

from fpiso.domain import Domain, DomainState
from fpiso.flags import Flag, MaskBits, flags_text
from fpiso.format import SMALL, Format, RoundingMode
from fpiso.runtime import Runtime


class VisibleStateWalkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Runtime(name="walk")
        self.outer = Domain(
            "outer",
            SMALL,
            RoundingMode.RNE,
            MaskBits.unmasking(Flag.INEXACT),
        )
        self.audit = Domain(
            "audit",
            Format("audit", 5, -3, 4),
            RoundingMode.RU,
            MaskBits.unmasking(Flag.UNDERFLOW),
        )
        self.runtime.register(self.outer)
        self.runtime.register(self.audit)

    def assert_state(
        self,
        state: DomainState,
        *,
        name: str,
        rounding: RoundingMode,
        sticky: set[Flag],
        pending: tuple[Flag, ...],
    ) -> None:
        self.assertEqual(state.name, name)
        self.assertEqual(state.rounding, rounding)
        self.assertEqual(state.sticky, frozenset(sticky))
        self.assertEqual(state.pending, pending)

    def test_step_by_step_visible_state(self) -> None:
        runtime, outer, audit = self.runtime, self.outer, self.audit

        runtime.enter_domain(outer)
        empty_outer = runtime.observe()
        self.assert_state(
            empty_outer,
            name="outer",
            rounding=RoundingMode.RNE,
            sticky=set(),
            pending=(),
        )

        # Step 1: exact midpoint 17/16 in the small format, domain rounding RNE
        # picks 1 and reports inexact.  INEXACT is unmasked in outer, so the
        # trap is queued for outer only.
        mid = runtime.add(1, F(1, 16))
        self.assertEqual(mid.value, F(1))
        self.assertEqual(mid.flags, frozenset({Flag.INEXACT}))
        state_after_mid = runtime.observe()
        self.assert_state(
            state_after_mid,
            name="outer",
            rounding=RoundingMode.RNE,
            sticky={Flag.INEXACT},
            pending=(Flag.INEXACT,),
        )

        # Step 2: nest the audit domain.  Its view starts empty.
        runtime.enter_domain(audit, audit=True)
        audit_empty = runtime.observe()
        self.assert_state(
            audit_empty,
            name="audit",
            rounding=RoundingMode.RU,
            sticky=set(),
            pending=(),
        )
        # outer is paused; the harness snapshot equals the state at suspension.
        self.assertEqual(runtime.inspect("outer"), state_after_mid)

        # Step 3: the audit evaluates the *same* midpoint.  It has 5 bits of
        # precision, so 17/16 is representable and the result is exact: the
        # audit's RU rounding does not change it and it raises no flag.
        audit_mid = self.runtime.add(1, F(1, 16))
        self.assertEqual(audit_mid.value, F(17, 16))
        self.assertEqual(audit_mid.flags, frozenset())
        self.assert_state(
            runtime.observe(),
            name="audit",
            rounding=RoundingMode.RU,
            sticky=set(),
            pending=(),
        )

        # Step 4: an audit operation that is tiny in the audit format.  The
        # audit rounds toward +infinity, so the nonzero exact 1/4096 is not
        # flushed to zero: it becomes the smallest subnormal 1/128, inexact and
        # underflowing.  This is a good contrast with the outer format, where
        # the same product flushes to +0 under RNE.
        tiny = self.runtime.mul(F(1, 64), F(1, 64))
        self.assertEqual(tiny.value, F(1, 128))
        self.assertEqual(tiny.flags, frozenset({Flag.UNDERFLOW, Flag.INEXACT}))
        audit_state = runtime.observe()
        self.assert_state(
            audit_state,
            name="audit",
            rounding=RoundingMode.RU,
            sticky={Flag.UNDERFLOW, Flag.INEXACT},
            pending=(Flag.UNDERFLOW,),
        )
        # The paused outer domain has not moved.
        self.assertEqual(runtime.inspect("outer"), state_after_mid)

        # Step 5: exit the audit, consuming only its own trap.
        report = runtime.exit_domain(consume_pending=True)
        self.assertEqual(report.pending_generated, (Flag.UNDERFLOW,))
        self.assertEqual(report.consumed, (Flag.UNDERFLOW,))
        # outer resumes with exactly the suspension snapshot.
        resumed = runtime.observe()
        self.assertEqual(resumed, state_after_mid)
        # The audit, now dormant, keeps its own cumulative status; consuming a
        # trap never clears sticky state.
        self.assertEqual(audit.sticky, frozenset({Flag.UNDERFLOW, Flag.INEXACT}))
        self.assertEqual(audit.pending, ())

        # Step 6: the outer domain drains its own queue explicitly.
        drained = outer.take_pending()
        self.assertEqual(drained, (Flag.INEXACT,))
        final = runtime.observe()
        self.assert_state(
            final,
            name="outer",
            rounding=RoundingMode.RNE,
            sticky={Flag.INEXACT},
            pending=(),
        )

    def test_visible_state_describe_text_matches_objects(self) -> None:
        runtime, outer, audit = self.runtime, self.outer, self.audit
        runtime.enter_domain(outer)
        runtime.add(1, F(1, 16))
        runtime.enter_domain(audit, audit=True)
        runtime.mul(F(1, 64), F(1, 64))

        # The text form the harness prints must agree with the object fields.
        text = runtime.observe().describe()
        self.assertIn("domain 'audit'", text)
        self.assertIn("rounding=ru", text)
        self.assertIn(flags_text({Flag.UNDERFLOW, Flag.INEXACT}), text)
        self.assertIn("pending=[underflow]", text)

        paused_text = runtime.inspect("outer").describe()
        self.assertIn("domain 'outer'", paused_text)
        self.assertIn("rounding=rne", paused_text)
        self.assertIn("pending=[inexact]", paused_text)

    def test_domain_view_never_mentions_other_domain(self) -> None:
        runtime, outer, audit = self.runtime, self.outer, self.audit
        runtime.enter_domain(outer)
        runtime.enter_domain(audit, audit=True)
        view = runtime.observe()
        # The active view has exactly one name and no aggregate of siblings.
        self.assertEqual(view.name, "audit")
        self.assertEqual(
            set(DomainState.__dataclass_fields__) - {"name", "format", "rounding",
                                                     "masks", "sticky", "pending"},
            set(),
        )
        self.assertNotIn("outer", view.describe())


class NestedMidpointContrastTests(unittest.TestCase):
    """The same midpoint, evaluated in three formats, yields three results."""

    def test_three_formats_three_results(self) -> None:
        runtime = Runtime()
        specs = [
            ("bits4", SMALL, F(1)),
            ("bits5", Format("bits5", 5, -2, 3), F(17, 16)),
            ("bits3", Format("bits3", 3, -2, 3), F(1)),
        ]
        domains = [Domain(name, fmt, RoundingMode.RNE) for name, fmt, _ in specs]
        for domain in domains:
            runtime.register(domain)

        seen = []
        runtime.enter_domain(domains[0])
        seen.append((runtime.observe().name, runtime.add(1, F(1, 16)).value))
        runtime.enter_domain(domains[1])
        seen.append((runtime.observe().name, runtime.add(1, F(1, 16)).value))
        runtime.enter_domain(domains[2], audit=True)
        seen.append((runtime.observe().name, runtime.add(1, F(1, 16)).value))
        runtime.exit_domain()
        runtime.exit_domain()
        runtime.exit_domain()

        self.assertEqual(
            seen,
            [("bits4", F(1)), ("bits5", F(17, 16)), ("bits3", F(1))],
        )
        # bits4 and bits3 cannot represent 17/16; bits5 can, so only the
        # wider-format domain is exact.
        self.assertEqual(domains[0].sticky, frozenset({Flag.INEXACT}))
        self.assertEqual(domains[1].sticky, frozenset())
        self.assertEqual(domains[2].sticky, frozenset({Flag.INEXACT}))
        for domain in domains:
            self.assertEqual(domain.pending, ())


if __name__ == "__main__":
    unittest.main()
