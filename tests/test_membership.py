import unittest

from acl_diag.membership import expand_members, is_privileged
from tests._fixtures import example_inputs, subjects_v2


class MembershipTest(unittest.TestCase):
    def test_direct_and_nested_group_expansion(self):
        _, subjects, _ = example_inputs()
        # carol is directly in grp_oncall and grp_legacy_share; grp_oncall
        # is nested inside grp_ops_staff, so carol transitively matches it.
        ids = expand_members("acc_carol", subjects)
        self.assertIn("acc_carol", ids)
        self.assertIn("grp_oncall", ids)
        self.assertIn("grp_ops_staff", ids)
        self.assertIn("grp_legacy_share", ids)

    def test_membership_change_removes_group_match(self):
        """Required scenario: group membership changes between snapshots."""
        _, subjects_v1, _ = example_inputs()
        before = expand_members("acc_bob", subjects_v1)
        after = expand_members("acc_bob", subjects_v2())
        # v1: bob sits in both writer groups.
        self.assertIn("grp_legacy_share", before)
        self.assertIn("grp_ops_staff", before)
        # v2: bob was removed from both groups.
        self.assertNotIn("grp_legacy_share", after)
        self.assertNotIn("grp_ops_staff", after)
        self.assertEqual(after, frozenset({"acc_bob"}))

    def test_group_ace_does_not_match_non_members(self):
        _, subjects, _ = example_inputs()
        # rootops is only in grp_admins; it must never match ops groups.
        ids = expand_members("acc_rootops", subjects)
        self.assertNotIn("grp_ops_staff", ids)
        self.assertNotIn("grp_legacy_share", ids)
        self.assertTrue(is_privileged("acc_rootops", subjects))
        self.assertFalse(is_privileged("acc_bob", subjects))

    def test_cyclic_group_definitions_terminate(self):
        from acl_diag.models import SubjectDirectory

        directory = SubjectDirectory(
            accounts={"acc_x": {"name": "X", "privileged": False}},
            groups={
                "g_a": {"name": "A", "members": ["g_b", "acc_x"]},
                "g_b": {"name": "B", "members": ["g_a"]},
            },
        )
        ids = expand_members("acc_x", directory)
        self.assertEqual(ids, frozenset({"acc_x", "g_a", "g_b"}))


if __name__ == "__main__":
    unittest.main()
