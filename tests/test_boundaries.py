import unittest

from host_sandbox import (
    MoveEndpointDeniedError,
    Node,
    Perm,
    PermissionDeniedError,
    Sandbox,
)
from host_sandbox.paths import normalize_absolute, resolve


class BoundaryTests(unittest.TestCase):
    def test_default_is_deny_without_directory_grant(self):
        sandbox = Sandbox(Node.root(children=[Node.file("secret")]))
        self.assertFalse(sandbox.can("/secret", "anyone", Perm.READ))
        with self.assertRaises(PermissionDeniedError):
            sandbox.open_read("/secret", "anyone")

    def test_no_managing_layer_leaves_directory_grant_as_decider(self):
        sandbox = Sandbox(
            Node.root(
                subtree_grants={"alice": [Perm.EXECUTE]},
                children=[Node.file("tool")],
            )
        )
        self.assertTrue(sandbox.can("/tool", "alice", Perm.EXECUTE))

    def test_parent_traversal_cannot_escape_sandbox_root(self):
        root = Node.root(children=[Node.file("f", inode="f")])
        sandbox = Sandbox(root)
        node = sandbox.resolve_path("/../../f")
        self.assertEqual("f", node.inode)
        self.assertEqual("/f", sandbox.path_of(node))

        directory = Node.directory("d")
        root.add(directory)
        node = resolve(root, "../../f", start=directory)
        self.assertEqual("f", node.inode)

    def test_normalize_absolute_paths(self):
        self.assertEqual("/", normalize_absolute("/"))
        self.assertEqual("/a/c", normalize_absolute("/a/b/../c/"))
        self.assertEqual("/c", normalize_absolute("/a/../../c"))

    def test_same_directory_rename_requires_both_name_removal_and_creation(self):
        child = Node.file("old", inode="child")
        directory = Node.directory(
            "d",
            children=[child],
            grants={"alice": [Perm.MOVE_OUT, Perm.MOVE_IN]},
        )
        sandbox = Sandbox(Node.root(children=[directory]))
        plan = sandbox.move_subtree("/d/old", "/d", "alice", new_name="new")
        self.assertTrue(plan.allowed)
        self.assertEqual("child", sandbox.resolve_path("/d/new").inode)

        other = Node.file("other")
        directory.add(other)
        directory.subtree_grants["alice"] = frozenset({Perm.MOVE_OUT})
        with self.assertRaises(MoveEndpointDeniedError):
            sandbox.move_subtree("/d/other", "/d", "alice", new_name="renamed")


if __name__ == "__main__":
    unittest.main()
