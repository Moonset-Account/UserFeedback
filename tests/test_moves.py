import json
import unittest

from host_sandbox import (
    MoveEndpointDeniedError,
    MoveError,
    MoveEscalationError,
    Node,
    Perm,
    Sandbox,
    STRANGER_PRINCIPAL,
)
from host_sandbox.examples import escalation_example


class MoveTests(unittest.TestCase):
    def test_canonical_descendant_escalation_is_rejected_and_rolls_back(self):
        sandbox = escalation_example()
        before = json.dumps(sandbox.snapshot(), sort_keys=True, ensure_ascii=False)

        # The operator passes both endpoint checks and already has rwx on the
        # movable directory itself.
        plan = sandbox.preview_move("/src/movable", "/drop", "operator")
        self.assertTrue(plan.source_decision.allowed)
        self.assertTrue(plan.destination_decision.allowed)
        self.assertTrue(sandbox.can("/src/movable", "operator", Perm.WRITE))
        # The moved directory itself inherits the source's move-out approval.
        self.assertTrue(sandbox.can("/src/movable", "operator", Perm.MOVE_OUT))
        self.assertTrue(sandbox.can("/drop", "operator", Perm.MOVE_IN))

        with self.assertRaises(MoveEscalationError) as ctx:
            sandbox.move_subtree("/src/movable", "/drop", "operator")

        gained_nodes = {(g.path_after, tuple(sorted(g.added))) for g in ctx.exception.gains}
        self.assertIn(
            ("/drop/movable/fragment", ("write",)),
            gained_nodes,
        )
        self.assertIn(
            ("/drop/movable/fragment/report.sh", ("write",)),
            gained_nodes,
        )
        # Neither the movable root nor the already-writable sibling gains.
        self.assertFalse(
            any(g.path_after == "/drop/movable" for g in ctx.exception.gains)
        )
        self.assertFalse(
            any(g.path_after == "/drop/movable/notes.txt" for g in ctx.exception.gains)
        )

        after = json.dumps(sandbox.snapshot(), sort_keys=True, ensure_ascii=False)
        self.assertEqual(before, after)
        self.assertIsNotNone(sandbox.resolve_path("/src/movable/fragment/report.sh"))
        with self.assertRaises(Exception):
            sandbox.resolve_path("/drop/movable")

    def test_successful_cross_directory_move_requires_no_new_rights(self):
        moved_file = Node.file("child.txt", "data", inode="child")
        source = Node.directory(
            "src",
            children=[moved_file],
            grants={"bob": [Perm.READ, Perm.MOVE_OUT]},
            inode="src",
        )
        destination = Node.directory(
            "dst",
            # Move-in only; this intentionally does not add rwx.
            grants={"bob": [Perm.MOVE_IN]},
            inode="dst",
        )
        root = Node.root(children=[source, destination])
        sandbox = Sandbox(root)

        plan = sandbox.move_subtree("/src/child.txt", "/dst", "bob")
        self.assertTrue(plan.allowed)
        self.assertEqual("/dst/child.txt", plan.final_path)
        self.assertEqual("child", sandbox.resolve_path("/dst/child.txt").inode)
        with self.assertRaises(Exception):
            sandbox.resolve_path("/src/child.txt")

    def test_both_endpoint_permissions_are_required(self):
        child = Node.file("child", inode="child")
        source = Node.directory(
            "src", children=[child], grants={"bob": [Perm.MOVE_OUT]}, inode="src"
        )
        destination = Node.directory("dst", inode="dst")  # no move_in
        root = Node.root(children=[source, destination])
        sandbox = Sandbox(root)
        before = sandbox.root.to_dict()

        with self.assertRaises(MoveEndpointDeniedError):
            sandbox.move_subtree("/src/child", "/dst", "bob")
        self.assertEqual(before, sandbox.root.to_dict())

        destination.subtree_grants["bob"] = frozenset({Perm.MOVE_IN})
        source.subtree_grants.pop("bob")
        before_second = sandbox.root.to_dict()
        with self.assertRaises(MoveEndpointDeniedError):
            sandbox.move_subtree("/src/child", "/dst", "bob")
        self.assertEqual(before_second, sandbox.root.to_dict())

    def test_wildcard_destination_grant_escalates_an_anonymous_principal(self):
        child = Node.file("child", inode="child")
        source = Node.directory(
            "src", children=[child], grants={"mover": [Perm.MOVE_OUT]}, inode="src"
        )
        destination = Node.directory(
            "dst",
            grants={"mover": [Perm.MOVE_IN], "*": [Perm.READ]},
            inode="dst",
        )
        root = Node.root(children=[source, destination])
        sandbox = Sandbox(root)

        plan = sandbox.preview_move("/src/child", "/dst", "mover")
        self.assertFalse(plan.allowed)
        stranger_gains = [g for g in plan.gains if g.principal == STRANGER_PRINCIPAL]
        self.assertEqual(1, len(stranger_gains))
        self.assertEqual(frozenset({Perm.READ}), stranger_gains[0].added)

    def test_structural_move_boundaries_leave_tree_intact(self):
        ordinary = Node.directory("ordinary", children=[Node.file("f")])
        nested = Node.directory("nested")
        source = Node.directory("src", children=[ordinary, nested])
        existing = Node.file("exists")
        destination = Node.directory("dst", children=[existing])
        source.add(Node.file("exists"))
        root = Node.root(children=[source, destination])
        sandbox = Sandbox(root)
        before = sandbox.root.to_dict()

        with self.assertRaises(MoveError):
            sandbox.move_subtree("/", "/dst", "alice")
        with self.assertRaises(MoveError):
            sandbox.move_subtree("/src", "/src/nested", "alice")
        with self.assertRaises(MoveError):
            sandbox.move_subtree("/src/exists", "/dst", "alice")
        self.assertEqual(before, sandbox.root.to_dict())

    def test_preview_does_not_commit_even_when_move_is_allowed(self):
        child = Node.file("child")
        source = Node.directory("src", children=[child], grants={"a": [Perm.MOVE_OUT]})
        destination = Node.directory("dst", grants={"a": [Perm.MOVE_IN]})
        root = Node.root(children=[source, destination])
        sandbox = Sandbox(root)

        plan = sandbox.preview_move("/src/child", "/dst", "a")
        self.assertTrue(plan.allowed)
        self.assertIsNotNone(sandbox.resolve_path("/src/child"))
        with self.assertRaises(Exception):
            sandbox.resolve_path("/dst/child")

    def test_mv_like_wrapper_supports_final_path_rename(self):
        child = Node.file("child", inode="child")
        source = Node.directory("src", children=[child], grants={"a": [Perm.MOVE_OUT]})
        destination = Node.directory("dst", grants={"a": [Perm.MOVE_IN]})
        root = Node.root(children=[source, destination])
        sandbox = Sandbox(root)

        plan = sandbox.move("/src/child", "/dst/renamed", "a")
        self.assertEqual("/dst/renamed", plan.final_path)
        self.assertEqual("child", sandbox.resolve_path("/dst/renamed").inode)


if __name__ == "__main__":
    unittest.main()
