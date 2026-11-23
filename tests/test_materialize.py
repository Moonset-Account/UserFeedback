"""Boundary and failure-recovery tests for materialization.

These cover the acceptance conditions that a happy-path run cannot exercise:

* missing provenance reports frame and value and publishes no half state;
* cyclic graphs are rebuilt without infinite recursion;
* aliasing is preserved across separate descriptors and across frames;
* overwritten fields coalesce to the last write;
* pending stores targeting unknown objects are rejected;
* mutable values cannot travel through ConstSource.
"""

from __future__ import annotations

import unittest

from deopt import (
    MISSING,
    ConstSource,
    IRObject,
    Materializer,
    PendingStore,
    RecoveryError,
    ReconstructSource,
    RegisterSource,
    Runtime,
)


class MissingProvenanceTest(unittest.TestCase):
    def test_missing_local_reports_frame_and_value(self):
        mat = Materializer(Runtime(), {})
        with self.assertRaises(RecoveryError) as ctx:
            mat.materialize(MISSING, "middle", "b2")
        self.assertEqual(ctx.exception.frame, "middle")
        self.assertEqual(ctx.exception.value, "b2")
        self.assertIn("no provenance", ctx.exception.reason)

    def test_none_source_reports_frame_and_value(self):
        mat = Materializer(Runtime(), {})
        with self.assertRaises(RecoveryError) as ctx:
            mat.materialize(None, "inner", "box")
        self.assertEqual(ctx.exception.frame, "inner")
        self.assertEqual(ctx.exception.value, "box")

    def test_missing_register_reports_registers_name(self):
        mat = Materializer(Runtime(), {})  # empty bank
        with self.assertRaises(RecoveryError) as ctx:
            mat.materialize(RegisterSource("r9"), "outer", "x")
        self.assertEqual(ctx.exception.frame, "outer")
        self.assertEqual(ctx.exception.value, "x")
        self.assertIn("r9", ctx.exception.reason)

    def test_nested_path_is_included_in_error(self):
        mat = Materializer(Runtime(), {})
        src = ReconstructSource(
            "v1", "Box",
            {"next": ReconstructSource(
                "v2", "Node", {"back": RegisterSource("missing")})},
        )
        with self.assertRaises(RecoveryError) as ctx:
            mat.materialize(src, "middle", "b1")
        self.assertIn("v2.back", " -> ".join(ctx.exception.path))

    def test_unknown_source_kind_rejected(self):
        mat = Materializer(Runtime(), {})
        with self.assertRaises(RecoveryError):
            mat.materialize(object(), "middle", "b1")


class HalfStateNotPublishedTest(unittest.TestCase):
    def test_failed_materialization_leaves_runtime_without_objects(self):
        runtime = Runtime()
        mat = Materializer(runtime, {})
        good = ReconstructSource("v1", "Box", {})
        bad = ReconstructSource(
            "v2", "Node", {"back": RegisterSource("gone")},
        )
        # The good object gets allocated internally before the bad one fails.
        with self.assertRaises(RecoveryError):
            mat.materialize(good, "f", "a")
            mat.materialize(bad, "f", "b")
        # The externally visible effect log stays empty: no constructor side
        # effect accompanied the internal allocation.
        self.assertEqual(runtime.effects, [])

    def test_pending_store_to_unknown_object_rejected(self):
        runtime = Runtime()
        mat = Materializer(runtime, {"r1": 7})
        with self.assertRaises(RecoveryError) as ctx:
            mat.apply_pending(
                [PendingStore("ghost", "f", ConstSource(7))], "middle",
            )
        self.assertEqual(ctx.exception.value, "ghost")


class AliasingAndCycleTest(unittest.TestCase):
    def test_separate_descriptors_same_vobj_share_identity(self):
        mat = Materializer(Runtime(), {})
        a = mat.materialize(ReconstructSource("v1", "Box", {}), "f", "a")
        b = mat.materialize(ReconstructSource("v1", "Box", {}), "f", "b")
        self.assertIs(a, b)

    def test_two_register_aliases_share_identity(self):
        obj = IRObject("Box", 0)
        mat = Materializer(Runtime(), {"r1": obj})
        a = mat.materialize(RegisterSource("r1"), "f", "a")
        b = mat.materialize(RegisterSource("r1"), "f", "b")
        self.assertIs(a, obj)
        self.assertIs(b, obj)

    def test_self_cycle_rebuilds(self):
        src = ReconstructSource("v1", "Node", {})
        src.fields["self"] = src  # direct back-edge
        mat = Materializer(Runtime(), {})
        obj = mat.materialize(src, "f", "n")
        self.assertIs(obj.fields["self"], obj)

    def test_mutual_cycle_rebuilds(self):
        box = ReconstructSource("vbox", "Box", {})
        node = ReconstructSource("vnode", "Node", {})
        box.fields["next"] = node
        node.fields["back"] = box
        mat = Materializer(Runtime(), {})
        rebuilt_box = mat.materialize(box, "f", "b")
        rebuilt_node = rebuilt_box.fields["next"]
        self.assertIs(rebuilt_node.fields["back"], rebuilt_box)

    def test_pending_stores_complete_cycle(self):
        # Objects exist without the back-edge first; the pending store closes
        # the cycle afterwards, mirroring optimizer-eliminated stores.
        box = ReconstructSource("vbox", "Box", {})
        node = ReconstructSource("vnode", "Node", {})
        box.fields["next"] = node
        mat = Materializer(Runtime(), {})
        rebuilt_box = mat.materialize(box, "f", "b")
        mat.apply_pending(
            [PendingStore("vnode", "back",
                          ReconstructSource("vbox", "Box", {}))],
            "f",
        )
        self.assertIs(rebuilt_box.fields["next"].fields["back"], rebuilt_box)

    def test_overwritten_field_keeps_last_write(self):
        mat = Materializer(Runtime(), {})
        obj_src = ReconstructSource("v1", "Box", {})
        mat.materialize(obj_src, "f", "o")
        stores = [
            PendingStore("v1", "f", ConstSource("first")),
            PendingStore("v1", "f", ConstSource("second")),
        ]
        mat.apply_pending(stores, "f")
        self.assertEqual(mat._object_memo["v1"].fields["f"], "second")


class ConstSourceGuardTest(unittest.TestCase):
    def test_mutable_const_source_rejected_at_construction(self):
        with self.assertRaises(TypeError):
            ConstSource(IRObject("Box", 0))
        with self.assertRaises(TypeError):
            ConstSource((1, 2))

    def test_immutable_scalars_accepted(self):
        for value in (None, True, 1, 1.5, "s"):
            self.assertEqual(ConstSource(value).value, value)


if __name__ == "__main__":
    unittest.main()
