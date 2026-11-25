import io
import contextlib
import unittest

from host_sandbox import (
    Perm,
    PermissionDeniedError,
    PolicyLayer,
    Rule,
    Sandbox,
    Node,
)
from host_sandbox.examples import handle_interleaving_example


class HandleInterleavingTests(unittest.TestCase):
    def test_existing_read_handle_keeps_read_after_grant_revocation(self):
        sandbox = Sandbox(
            Node.root(
                subtree_grants={"alice": [Perm.READ]},
                children=[Node.file("notes", b"persisted bytes\n")],
            )
        )
        handle = sandbox.open_read("/notes", "alice")
        self.assertEqual(b"persisted bytes\n", handle.read())

        sandbox.revoke_subtree("/", "alice", [Perm.READ])
        self.assertFalse(sandbox.can("/notes", "alice", Perm.READ))
        with self.assertRaises(PermissionDeniedError):
            sandbox.open_read("/notes", "alice")

        # The previously opened read-only handle retains its acquired read.
        self.assertEqual(b"persisted bytes\n", handle.read())

    def test_existing_read_handle_survives_layer_rule_removal(self):
        layer = PolicyLayer(
            "read-layer",
            managed_permissions=[Perm.READ],
            rules=[Rule("/", [Perm.READ], principal="alice")],
        )
        sandbox = Sandbox(
            Node.root(
                subtree_grants={"alice": [Perm.READ]},
                children=[Node.file("f", b"abc")],
            ),
            layers=[layer],
        )
        handle = sandbox.open_read("/f", "alice")
        layer.rules.clear()

        self.assertFalse(sandbox.can("/f", "alice", Perm.READ))
        with self.assertRaises(PermissionDeniedError):
            sandbox.open_read("/f", "alice")
        self.assertEqual(b"abc", handle.read())

    def test_old_directory_handle_relative_open_uses_current_location(self):
        sandbox = handle_interleaving_example()

        dir_handle = sandbox.open_directory("/work/dir", "alice")
        read_handle = dir_handle.open_read("note.txt")
        self.assertIn(b"hello", read_handle.read())

        # This move removes inherited read at the destination but adds no rwx,
        # so the no-escalation move rule permits it.
        plan = sandbox.move_subtree("/work/dir", "/archive", "alice")
        self.assertTrue(plan.allowed)
        self.assertEqual("/work/dir", dir_handle.opened_path)
        self.assertEqual("/archive/dir", dir_handle.current_path)

        # Existing read handle retains its old read grant.
        self.assertIn(b"hello", read_handle.read())

        # A new relative open through the old inode is evaluated at /archive.
        with self.assertRaises(PermissionDeniedError) as ctx:
            dir_handle.open_read("note.txt")
        self.assertIn("/archive/dir/note.txt", str(ctx.exception))

        # The relative parent reference also follows the current parent.  The
        # old /work/parent.txt would still be readable under /work's grant, so
        # this denial specifically proves resolution under /archive.
        with self.assertRaises(PermissionDeniedError) as parent_ctx:
            dir_handle.open_read("../parent.txt")
        self.assertIn("/archive/parent.txt", str(parent_ctx.exception))

        with self.assertRaises(PermissionDeniedError):
            sandbox.open_read("/archive/dir/note.txt", "alice")

    def test_closed_handles_cannot_be_used(self):
        sandbox = Sandbox(
            Node.root(
                subtree_grants={"alice": [Perm.READ]},
                children=[Node.file("f", b"x")],
            )
        )
        read_handle = sandbox.open_read("/f", "alice")
        read_handle.close()
        with self.assertRaises(Exception):
            read_handle.read()

        dir_handle = sandbox.open_directory("/", "alice")
        dir_handle.close()
        with self.assertRaises(Exception):
            dir_handle.open_read("f")

    def test_demo_runs_and_reports_intact_tree(self):
        from host_sandbox.demo import main

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main()
        text = output.getvalue()
        self.assertEqual(0, result)
        self.assertIn("Move rejected", text)
        self.assertIn("Original tree/policy snapshot intact: True", text)


if __name__ == "__main__":
    unittest.main()
