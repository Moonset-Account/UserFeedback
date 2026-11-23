"""Tests for the simulated optimized inlined trace."""

from __future__ import annotations

import unittest

from deopt.trace import (
    GUARD_FAILED,
    OK,
    RAISED,
    TConst,
    TConstException,
    TEmitConst,
    TGuardTag,
    TMove,
    TNew,
    TRaise,
    TSetField,
    VirtualObject,
    run_trace,
)


class TraceExecutionTest(unittest.TestCase):
    def test_virtual_allocation_emits_no_constructor_effect(self):
        result = run_trace([
            TNew("b", "vbox", "Box", "ctor:Box"),
            TNew("n", "vnode", "Node", "ctor:Node"),
        ])
        self.assertEqual(result.outcome, OK)
        self.assertEqual(result.effects, [])
        self.assertIsInstance(result.registers["b"], VirtualObject)
        self.assertEqual(set(result.reconstruct), {"vbox", "vnode"})

    def test_guard_deopt_mode_returns_guard_failed(self):
        result = run_trace([
            TNew("b", "vbox", "Box", "ctor:Box"),
            TGuardTag("b", "Node", on_fail="deopt"),
        ])
        self.assertEqual(result.outcome, GUARD_FAILED)
        self.assertEqual(result.trigger_register, "b")
        self.assertEqual(result.expected_tag, "Node")

    def test_guard_raise_mode_falls_through_to_exception(self):
        result = run_trace([
            TNew("n", "vnode", "Node", "ctor:Node"),
            TGuardTag("n", "Box", on_fail="raise"),
            TConstException("e", "guard-failed:Box"),
            TRaise("e"),
        ])
        self.assertEqual(result.outcome, RAISED)
        self.assertEqual(result.exception_marker, "guard-failed:Box")
        self.assertEqual(result.trigger_register, "e")

    def test_passing_guard_continues(self):
        result = run_trace([
            TNew("n", "vnode", "Node", "ctor:Node"),
            TGuardTag("n", "Node"),
            TEmitConst("after-guard"),
        ])
        self.assertEqual(result.outcome, OK)
        self.assertEqual(result.effects, ["after-guard"])

    def test_pending_store_records_field_provenance(self):
        result = run_trace([
            TNew("b", "vbox", "Box", "ctor:Box"),
            TNew("n", "vnode", "Node", "ctor:Node"),
            TMove("alias", "b"),
            TSetField("alias", "next", "n"),
        ])
        self.assertEqual(len(result.pending), 1)
        store = result.pending[0]
        self.assertEqual((store.vobj, store.field), ("vbox", "next"))
        # The stored provenance references the same virtual node identity.
        self.assertEqual(result.reconstruct["vbox"].fields["next"].vobj,
                         "vnode")

    def test_undefined_register_is_a_programming_error(self):
        with self.assertRaises(KeyError):
            run_trace([TMove("dst", "missing")])

    def test_setfield_on_scalar_register_rejected(self):
        with self.assertRaises(TypeError):
            run_trace([
                TConst("s", "scalar"),
                TSetField("s", "f", "s"),
            ])


if __name__ == "__main__":
    unittest.main()
