"""Tests for nesting, suspension, audit-domain exit and flag isolation.

These are the tests that pin down the security-relevant claims of the model:

* a suspended outer domain is invisible to the nested audit domain;
* an audit exit cannot consume the outer domain's pending traps;
* an audit domain cannot leak its own flags into the resumed outer domain;
* the state read after resume is exactly the state captured at suspension;
* nesting is strictly LIFO and illegal transitions are refused.
"""

from __future__ import annotations

import unittest
from fractions import Fraction as F

from fpiso.domain import Domain
from fpiso.errors import (
    DomainActiveError,
    DomainLifecycleError,
    DomainOwnershipError,
    IsolationError,
    NoActiveDomainError,
    SuspendedDomainError,
)
from fpiso.flags import Flag, MaskBits
from fpiso.format import SMALL, Format, RoundingMode
from fpiso.runtime import Runtime


AUDIT_FORMAT = Format("audit", 5, -3, 4)


def make_runtime() -> tuple[Runtime, Domain, Domain]:
    runtime = Runtime()
    outer = Domain(
        "outer",
        SMALL,
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
    return runtime, outer, audit


class EmptyStackTests(unittest.TestCase):
    def test_operations_without_domain(self) -> None:
        runtime = Runtime()
        with self.assertRaises(NoActiveDomainError):
            runtime.add(1, 1)
        with self.assertRaises(NoActiveDomainError):
            runtime.observe()

    def test_exit_without_entry(self) -> None:
        runtime = Runtime()
        with self.assertRaises(DomainLifecycleError):
            runtime.exit_domain()


class BasicEntryExitTests(unittest.TestCase):
    def test_enter_exit_returns_report(self) -> None:
        runtime = Runtime()
        domain = Domain("d", SMALL, RoundingMode.RNE, MaskBits.all_unmasked())
        runtime.register(domain)
        runtime.enter_domain(domain)
        self.assertIs(runtime.active, domain)
        self.assertTrue(domain.active)
        runtime.add(F(1, 3), F(1, 3))
        report = runtime.exit_domain()
        self.assertEqual(report.name, "d")
        self.assertEqual(report.sticky_delta, frozenset({Flag.INEXACT}))
        self.assertEqual(report.pending_generated, (Flag.INEXACT,))
        self.assertEqual(report.pending_remaining, (Flag.INEXACT,))
        self.assertIsNone(runtime.active)

    def test_consume_pending_on_exit(self) -> None:
        runtime = Runtime()
        domain = Domain("d", SMALL, RoundingMode.RNE, MaskBits.all_unmasked())
        runtime.register(domain)
        runtime.enter_domain(domain)
        runtime.add(F(1, 3), F(1, 3))
        report = runtime.exit_domain(consume_pending=True)
        self.assertEqual(report.consumed, (Flag.INEXACT,))
        self.assertEqual(report.pending_remaining, ())
        self.assertEqual(domain.pending, ())
        # Consuming the queue never clears the cumulative status.
        self.assertEqual(domain.sticky, frozenset({Flag.INEXACT}))

    def test_operations_counted(self) -> None:
        runtime = Runtime()
        domain = Domain("d", SMALL)
        runtime.register(domain)
        runtime.enter_domain(domain)
        runtime.add(1, 1)
        runtime.mul(2, 3)
        runtime.fma(1, 2, 3)
        report = runtime.exit_domain()
        self.assertEqual(report.ops, 3)

    def test_context_manager_exits_on_exception(self) -> None:
        runtime = Runtime()
        domain = Domain("d", SMALL)
        runtime.register(domain)
        with self.assertRaises(ValueError):
            with runtime.domain_scope(domain):
                raise ValueError("boom")
        self.assertIsNone(runtime.active)

    def test_exit_expected_name(self) -> None:
        runtime = Runtime()
        a = Domain("a", SMALL)
        b = Domain("b", SMALL)
        runtime.register(a)
        runtime.register(b)
        runtime.enter_domain(a)
        runtime.enter_domain(b)
        with self.assertRaises(DomainLifecycleError):
            runtime.exit_domain("a")
        self.assertEqual(runtime.stack_names(), ("a", "b"))
        runtime.exit_domain("b")
        runtime.exit_domain("a")
        self.assertIsNone(runtime.active)


class SuspensionTests(unittest.TestCase):
    def test_suspended_domain_cannot_execute(self) -> None:
        runtime, outer, audit = make_runtime()
        runtime.enter_domain(outer)
        outer.add(1, F(1, 16))
        runtime.enter_domain(audit, audit=True)
        self.assertTrue(outer.suspended)
        with self.assertRaises(SuspendedDomainError):
            outer.add(1, F(1, 16))
        with self.assertRaises(SuspendedDomainError):
            outer.mul(2, 3)
        with self.assertRaises(SuspendedDomainError):
            outer.fma(1, 2, 3)
        with self.assertRaises(SuspendedDomainError):
            outer.take_pending()
        with self.assertRaises(SuspendedDomainError):
            outer.clear_sticky()

    def test_suspended_state_is_frozen(self) -> None:
        runtime, outer, audit = make_runtime()
        runtime.enter_domain(outer)
        runtime.add(1, F(1, 16))
        before = runtime.inspect("outer")
        runtime.enter_domain(audit, audit=True)
        runtime.add(1, F(1, 3))  # audit does its own inexact work
        runtime.mul(F(1, 64), F(1, 64))
        self.assertEqual(runtime.inspect("outer"), before)
        runtime.exit_domain()
        self.assertEqual(runtime.observe(), before)

    def test_audit_state_visible_only_to_audit(self) -> None:
        runtime, outer, audit = make_runtime()
        runtime.enter_domain(outer)
        runtime.add(1, F(1, 16))
        outer_state = runtime.observe()
        runtime.enter_domain(audit, audit=True)
        self.assertEqual(runtime.observe().name, "audit")
        self.assertEqual(runtime.observe().sticky, frozenset())
        runtime.mul(F(1, 64), F(1, 64))  # underflow in the audit format
        self.assertEqual(runtime.observe().name, "audit")
        self.assertIn(Flag.UNDERFLOW, runtime.observe().sticky)
        self.assertNotIn(Flag.UNDERFLOW, outer_state.sticky)
        self.assertNotEqual(runtime.observe().sticky, outer_state.sticky)


class AuditExitTests(unittest.TestCase):
    def test_audit_exit_cannot_consume_outer_pending(self) -> None:
        runtime, outer, audit = make_runtime()
        runtime.enter_domain(outer)
        runtime.add(F(1, 3), F(1, 3))  # outer queues inexact (unmasked)
        runtime.add(15, 15)  # outer queues overflow + inexact
        outer_pending_before = outer.pending
        self.assertEqual(len(outer_pending_before), 3)

        runtime.enter_domain(audit, audit=True)
        runtime.add(1, F(1, 3))
        report = runtime.exit_domain(consume_pending=True)

        self.assertEqual(report.name, "audit")
        self.assertEqual(report.pending_remaining, ())
        self.assertEqual(outer.pending, outer_pending_before)
        self.assertEqual(runtime.observe().pending, outer_pending_before)

    def test_audit_flags_do_not_leak_into_outer(self) -> None:
        runtime, outer, audit = make_runtime()
        runtime.enter_domain(outer)
        runtime.add(F(1, 3), F(1, 3))
        outer_sticky_before = outer.sticky
        self.assertEqual(outer_sticky_before, frozenset({Flag.INEXACT}))

        runtime.enter_domain(audit, audit=True)
        runtime.mul(F(1, 64), F(1, 64))  # underflow in audit
        self.assertIn(Flag.UNDERFLOW, runtime.observe().sticky)
        report = runtime.exit_domain()

        self.assertIn(Flag.UNDERFLOW, report.sticky_delta)
        self.assertEqual(outer.sticky, outer_sticky_before)
        self.assertNotIn(Flag.UNDERFLOW, outer.sticky)

    def test_outer_flags_survive_audit(self) -> None:
        runtime, outer, audit = make_runtime()
        runtime.enter_domain(outer)
        runtime.add(15, 15)  # overflow in outer
        self.assertEqual(outer.sticky, frozenset({Flag.OVERFLOW, Flag.INEXACT}))
        runtime.enter_domain(audit, audit=True)
        runtime.add(1, 1)
        runtime.exit_domain()
        # Resume must not swallow the outer's pending overflow.
        self.assertEqual(outer.sticky, frozenset({Flag.OVERFLOW, Flag.INEXACT}))
        self.assertIn(Flag.OVERFLOW, outer.pending)
        self.assertIn(Flag.OVERFLOW, runtime.observe().pending)

    def test_nested_audit_cannot_drain_suspended_outer(self) -> None:
        runtime, outer, audit = make_runtime()
        runtime.enter_domain(outer)
        runtime.add(F(1, 3), F(1, 3))
        runtime.enter_domain(audit, audit=True)
        # The audit may drain its own queue...
        audit.take_pending()
        # ...but it cannot reach the suspended outer domain's state.
        with self.assertRaises(IsolationError):
            runtime.domain("outer").add(1, 1)
        with self.assertRaises(SuspendedDomainError):
            outer.take_pending()
        with self.assertRaises(SuspendedDomainError):
            outer.clear_flags()

    def test_dormant_neighbour_cannot_execute(self) -> None:
        runtime, outer, audit = make_runtime()
        other = Domain("other", SMALL)
        runtime.register(other)
        runtime.enter_domain(outer)
        # ``other`` is registered but not on the stack; it must not be able to
        # latch flags while the runtime believes ``outer`` is executing.
        with self.assertRaises(IsolationError):
            other.add(1, F(1, 3))
        self.assertEqual(other.sticky, frozenset())


class LifecycleTests(unittest.TestCase):
    def test_reentering_same_domain_refused(self) -> None:
        runtime, outer, audit = make_runtime()
        runtime.enter_domain(outer)
        with self.assertRaises(DomainActiveError):
            runtime.enter_domain(outer)

    def test_domain_from_another_runtime_refused(self) -> None:
        first = Runtime(name="first")
        second = Runtime(name="second")
        domain = Domain("d", SMALL)
        first.register(domain)
        with self.assertRaises(DomainOwnershipError):
            second.enter_domain(domain)

    def test_duplicate_name_with_different_object_refused(self) -> None:
        runtime = Runtime()
        runtime.register(Domain("d", SMALL))
        with self.assertRaises(DomainOwnershipError):
            runtime.register(Domain("d", SMALL))

    def test_register_same_object_twice_is_idempotent(self) -> None:
        runtime = Runtime()
        domain = Domain("d", SMALL)
        runtime.register(domain)
        runtime.register(domain)
        self.assertIs(runtime.domain("d"), domain)


class DeepNestingTests(unittest.TestCase):
    def test_three_levels_keep_all_state(self) -> None:
        runtime = Runtime()
        levels = [
            Domain("l0", SMALL, RoundingMode.RNE, MaskBits.all_masked()),
            Domain("l1", Format("mid", 5, -3, 4), RoundingMode.RD, MaskBits.all_unmasked()),
            Domain("l2", Format("tiny", 3, -1, 1), RoundingMode.RU, MaskBits.all_masked()),
        ]
        for domain in levels:
            runtime.register(domain)

        snapshots = []
        runtime.enter_domain(levels[0])
        runtime.add(F(1, 3), F(1, 3))
        snapshots.append(runtime.observe())

        runtime.enter_domain(levels[1])
        runtime.add(F(1, 3), F(1, 3))
        snapshots.append(runtime.observe())

        runtime.enter_domain(levels[2], audit=True)
        runtime.add(F(1, 3), F(1, 3))
        snapshots.append(runtime.observe())

        self.assertEqual(runtime.stack_names(), ("l0", "l1", "l2"))
        # The two outer domains were suspended untouched.
        self.assertEqual(runtime.inspect("l0"), snapshots[0])
        self.assertEqual(runtime.inspect("l1"), snapshots[1])

        runtime.exit_domain()
        self.assertEqual(runtime.observe(), snapshots[1])
        runtime.exit_domain()
        self.assertEqual(runtime.observe(), snapshots[0])
        runtime.exit_domain()
        self.assertIsNone(runtime.active)

    def test_interleaved_sibling_domains(self) -> None:
        runtime = Runtime()
        left = Domain("left", SMALL, RoundingMode.RD, MaskBits.all_unmasked())
        right = Domain("right", SMALL, RoundingMode.RU, MaskBits.all_unmasked())
        runtime.register(left)
        runtime.register(right)

        runtime.enter_domain(left)
        runtime.add(1, 1)  # exact and quiet
        left_state = runtime.observe()
        runtime.exit_domain()

        runtime.enter_domain(right)
        runtime.add(1, F(1, 16))  # inexact
        right_state = runtime.observe()
        runtime.exit_domain()

        self.assertEqual(left_state.sticky, frozenset())
        self.assertEqual(right_state.sticky, frozenset({Flag.INEXACT}))
        self.assertEqual(left_state.pending, ())
        self.assertEqual(right_state.pending, (Flag.INEXACT,))
        # Re-entering left shows no trace of right's traffic.
        runtime.enter_domain(left)
        self.assertEqual(runtime.observe().pending, ())
        self.assertEqual(runtime.observe().sticky, left_state.sticky)
        runtime.exit_domain()


class ExitReportTests(unittest.TestCase):
    def test_generated_uses_historical_log_after_inner_drain(self) -> None:
        runtime = Runtime()
        domain = Domain("d", SMALL, RoundingMode.RNE, MaskBits.all_unmasked())
        runtime.register(domain)
        runtime.enter_domain(domain)
        runtime.add(F(1, 3), F(1, 3))
        domain.take_pending()  # drain mid-tenure
        runtime.mul(F(1, 64), F(1, 64))
        report = runtime.exit_domain(consume_pending=True)
        # The report lists every trap queued in the tenure, in order, including
        # the one the domain already drained itself.
        self.assertEqual(
            report.pending_generated,
            (Flag.INEXACT, Flag.UNDERFLOW, Flag.INEXACT),
        )
        self.assertEqual(report.consumed, (Flag.UNDERFLOW, Flag.INEXACT))
        self.assertEqual(report.pending_remaining, ())
        self.assertEqual(report.sticky_delta, frozenset({Flag.UNDERFLOW, Flag.INEXACT}))

    def test_report_does_not_include_prior_pending(self) -> None:
        runtime = Runtime()
        domain = Domain("d", SMALL, RoundingMode.RNE, MaskBits.all_unmasked())
        runtime.register(domain)
        runtime.enter_domain(domain)
        runtime.add(F(1, 3), F(1, 3))
        runtime.exit_domain()

        runtime.enter_domain(domain)
        runtime.mul(F(1, 64), F(1, 64))
        report = runtime.exit_domain()
        self.assertEqual(report.pending_at_entry, (Flag.INEXACT,))
        self.assertEqual(report.pending_generated, (Flag.UNDERFLOW, Flag.INEXACT))
        # INEXACT was already sticky from the previous tenure, so the new
        # contribution to the cumulative status is only UNDERFLOW.
        self.assertEqual(report.sticky_delta, frozenset({Flag.UNDERFLOW}))


if __name__ == "__main__":
    unittest.main()
