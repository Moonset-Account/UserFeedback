import unittest

from host_sandbox import (
    Perm,
    PolicyLayer,
    Rule,
    Sandbox,
    Node,
)


class LayerPermissionTests(unittest.TestCase):
    def test_permits_accumulate_along_ancestor_path_in_one_layer(self):
        tree = Node.root(
            subtree_grants={"alice": [Perm.READ, Perm.WRITE, Perm.EXECUTE]},
            children=[
                Node.directory(
                    "a",
                    children=[
                        Node.directory(
                            "b",
                            children=[Node.file("c", inode="c")],
                        )
                    ],
                )
            ],
        )
        layer = PolicyLayer(
            "layer",
            managed_permissions=[Perm.READ, Perm.WRITE, Perm.EXECUTE],
            rules=[
                Rule("/a", [Perm.READ], principal="alice"),
                Rule("/a/b", [Perm.WRITE], principal="alice"),
            ],
        )
        sandbox = Sandbox(tree, [layer])

        rights = sandbox.effective_permissions("/a/b/c", "alice")
        self.assertIn(Perm.READ, rights)
        self.assertIn(Perm.WRITE, rights)
        self.assertNotIn(Perm.EXECUTE, rights)

        # A deeper exact-path rule adds execute without removing earlier allows.
        layer.add_rule(Rule("/a/b/c", [Perm.EXECUTE], principal="alice"))
        rights = sandbox.effective_permissions("/a/b/c", "alice")
        self.assertEqual(
            {Perm.READ, Perm.WRITE, Perm.EXECUTE},
            rights & {Perm.READ, Perm.WRITE, Perm.EXECUTE},
        )

    def test_every_layer_managing_a_permission_must_approve_it(self):
        tree = Node.root(
            subtree_grants={"alice": [Perm.READ, Perm.WRITE]},
            children=[Node.file("f", inode="f")],
        )
        approving = PolicyLayer(
            "approving-read",
            managed_permissions=[Perm.READ],
            rules=[Rule("/", [Perm.READ], principal="alice")],
        )
        blocking = PolicyLayer(
            "blocking-read",
            managed_permissions=[Perm.READ],
            rules=[],
        )
        write_only = PolicyLayer(
            "write-layer",
            managed_permissions=[Perm.WRITE],
            rules=[Rule("/", [Perm.WRITE], principal="alice")],
        )
        sandbox = Sandbox(tree, [approving, blocking, write_only])

        decision = sandbox.evaluate("/f", "alice", Perm.READ)
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.directory_granted)
        layer_by_name = {d.layer: d for d in decision.layer_decisions}
        self.assertTrue(layer_by_name["approving-read"].allowed)
        self.assertFalse(layer_by_name["blocking-read"].allowed)
        # This layer does not manage read and therefore cannot veto it.
        self.assertTrue(layer_by_name["write-layer"].allowed)

        blocking.add_rule(Rule("/f", [Perm.READ], principal="alice"))
        self.assertTrue(sandbox.can("/f", "alice", Perm.READ))

    def test_directory_subtree_grants_are_distinct_from_self_acl(self):
        child = Node.file("child")
        parent = Node.directory(
            "parent",
            children=[child],
            # Only the directory inode itself gets execute here.
            acl={"alice": [Perm.EXECUTE]},
            # Every descendant gets read.
            grants={"alice": [Perm.READ]},
        )
        root = Node.root(children=[parent])
        sandbox = Sandbox(root)

        self.assertTrue(sandbox.can("/parent", "alice", Perm.EXECUTE))
        self.assertTrue(sandbox.can("/parent/child", "alice", Perm.READ))
        self.assertFalse(sandbox.can("/parent/child", "alice", Perm.EXECUTE))

    def test_global_permission_set_is_treated_as_wildcard_subtree_grant(self):
        sandbox = Sandbox(
            Node.root(
                children=[
                    Node.directory(
                        "d",
                        grants=[Perm.READ, "w"],
                        children=[Node.file("f")],
                    )
                ]
            )
        )
        self.assertTrue(sandbox.can("/d/f", "any-subject", Perm.READ))
        self.assertTrue(sandbox.can("/d/f", "any-subject", Perm.WRITE))

    def test_config_loader_constructs_layers_and_tree(self):
        config = {
            "tree": {
                "name": "",
                "type": "directory",
                "subtree_grants": {"*": ["read"]},
                "children": [{"name": "f", "type": "file", "content": "x"}],
            },
            "layers": [
                {
                    "name": "l",
                    "managed_permissions": ["read"],
                    "rules": [{"path": "/", "permissions": ["r"]}],
                }
            ],
        }
        sandbox = Sandbox.from_config(config)
        self.assertTrue(sandbox.can("/f", "@anonymous-stranger", Perm.READ))


if __name__ == "__main__":
    unittest.main()
