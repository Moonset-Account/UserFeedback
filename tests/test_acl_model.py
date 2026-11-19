"""Acceptance tests for the ACL creation/access/chmod model.

Run from the workspace root:

    .venv/bin/python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from acl_model import (  # noqa: E402
    Acl,
    AclError,
    AclFile,
    FileMode,
    Perm,
    Subject,
    create_acl,
    create_file,
    parse_acl,
)

RX = Perm.READ | Perm.EXEC
RW = Perm.READ | Perm.WRITE
RWX = Perm.READ | Perm.WRITE | Perm.EXEC

EXTENDED_DEFAULT = (
    "user::rwx,user:alice:rwx,user:bob:rw-,group::r-x,group:devs:rwx,"
    "mask::rwx,other::---"
)


def extended_default() -> Acl:
    return parse_acl(EXTENDED_DEFAULT)


def file_with(acl: Acl, *, owner: str = "root", group: str = "staff") -> AclFile:
    return AclFile(owner=owner, group=group, acl=acl)


class PermAndModeTests(unittest.TestCase):
    def test_permission_text_round_trip(self):
        self.assertEqual(Perm.from_text("r-x"), RX)
        self.assertEqual(Perm.from_text("7"), RWX)
        self.assertEqual(RWX.to_text(), "rwx")
        self.assertEqual(Perm.NONE.to_text(), "---")

    def test_mode_octal_round_trip(self):
        mode = FileMode.from_octal("0750")
        self.assertEqual((mode.owner, mode.group, mode.other), (RWX, RX, Perm.NONE))
        self.assertEqual(mode.to_octal(), "750")
        self.assertEqual(FileMode.from_octal(0o640).to_octal(), "640")

    def test_mode_accepts_prefixed_octal_and_rejects_bad_input(self):
        self.assertEqual(FileMode.from_octal("0o644").to_octal(), "644")
        for bad in ("", "8", "abc", "0o", -1, 0o1000, True, None, "0o8"):
            with self.subTest(bad=bad):
                with self.assertRaises(AclError):
                    FileMode.from_octal(bad)

    def test_permission_rejects_out_of_range_bits(self):
        with self.assertRaises(AclError):
            Perm.from_text("rwxr")
        with self.assertRaises(AclError):
            Perm.from_text("q-x")
        with self.assertRaises(AclError):
            FileMode(owner=8, group=0, other=0)


class ParseAclTests(unittest.TestCase):
    def test_parses_named_entries_and_mask(self):
        acl = extended_default()
        self.assertEqual(acl.user_obj, RWX)
        self.assertEqual(acl.group_obj, RX)
        self.assertEqual(acl.mask, RWX)
        self.assertEqual(acl.other, Perm.NONE)
        self.assertEqual(acl.named_users, {"alice": RWX, "bob": RW})
        self.assertEqual(acl.named_groups, {"devs": RWX})
        self.assertTrue(acl.is_extended)

    def test_round_trips_its_own_formatting(self):
        acl = extended_default()
        self.assertEqual(parse_acl(acl.format()), acl)
        self.assertEqual(parse_acl(acl.format(default=True)), acl)

    def test_accepts_short_tags_and_whitespace(self):
        acl = parse_acl("u::rwx g::r-x g:staff:r-x m::r-x o::---")
        self.assertEqual((acl.user_obj, acl.group_obj, acl.other), (RWX, RX, Perm.NONE))
        self.assertEqual(acl.named_groups, {"staff": RX})
        self.assertEqual(acl.mask, RX)

    def test_rejects_malformed_text(self):
        for bad in (
            "",
            "user::rwx",
            "user::rwx,group::r-x",
            "user::rwx,group::r-x,other::---,user::rwx",
            "user::rwx,group::r-x,other::---,user::rwx,extra",
            "user::rwx,group::r-x,other::---,mark::---",
            "user::rwx,group::r-x,other::---,user::rwx,,,,",
            "user::rwx,group::r-x,other::---,other::r--",
            "user::rwx,group::r-x,other::---,user::---",
            "user::rwx,group::r-x,other::---,user::r",
            "user::rwx,group::r-x,other::---,user:alice:rwx",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(AclError):
                    parse_acl(bad)

    def test_rejects_mixing_default_and_access_prefixes(self):
        with self.assertRaises(AclError):
            parse_acl(
                "default:user::rwx,default:group::r-x,default:other::---,"
                "user:alice:rw-,mask::rw-"
            )

    def test_acl_is_immutable_and_hashable(self):
        acl = extended_default()
        with self.assertRaises(TypeError):
            acl.named_users["mallory"] = RWX  # type: ignore[index]
        self.assertEqual(hash(acl), hash(extended_default()))
        self.assertEqual(len({acl, extended_default()}), 1)

    def test_rejects_named_entry_without_mask(self):
        with self.assertRaises(AclError):
            Acl(
                user_obj=RWX,
                group_obj=RX,
                other=Perm.NONE,
                named_users={"alice": RW},
            )

    def test_rejects_empty_qualifier_in_constructed_acl(self):
        with self.assertRaises(AclError):
            Acl(
                user_obj=RWX,
                group_obj=RX,
                other=Perm.NONE,
                mask=RWX,
                named_groups={"": RW},
            )


class CreationTests(unittest.TestCase):
    def test_default_acl_clips_owner_mask_and_other_only(self):
        created = create_acl(extended_default(), "740")
        self.assertEqual(created.user_obj, RWX)  # 7 & 7
        self.assertEqual(created.group_obj, RX)  # untouched, not clipped
        self.assertEqual(created.other, Perm.NONE)  # 0 & 0
        self.assertEqual(created.mask, Perm.READ)  # requested group 4
        self.assertEqual(created.named_users, {"alice": RWX, "bob": RW})
        self.assertEqual(created.named_groups, {"devs": RWX})

    def test_requested_zero_group_strips_group_class_but_keeps_named_entries(self):
        created = create_acl(extended_default(), "700")
        self.assertEqual(created.user_obj, RWX)
        self.assertEqual(created.mask, Perm.NONE)
        self.assertEqual(created.named_users["alice"], RWX)
        self.assertTrue(created.is_extended)

    def test_minimal_default_acl_clips_group_entry(self):
        parent = parse_acl("user::rwx,group::rwx,other::rwx")
        created = create_acl(parent, "750")
        self.assertEqual(created.to_mode(), 0o750)
        self.assertEqual(created.group_obj, RX)
        self.assertFalse(created.is_extended)

    def test_no_default_acl_uses_process_umask(self):
        created = create_acl(None, "777", umask="022")
        self.assertEqual(created.to_mode(), 0o755)
        self.assertFalse(created.is_extended)

    def test_no_default_acl_and_no_umask_keeps_request(self):
        self.assertEqual(create_acl(None, "640").to_mode(), 0o640)

    def test_minimal_default_acl_keeps_no_mask_entry(self):
        parent = parse_acl("user::rwx,group::rwx,other::rwx")
        created = create_acl(parent, "777")
        self.assertIsNone(created.mask)
        self.assertEqual(created.group_class, RWX)

    def test_zero_request_strips_everything_from_empty_parent_default(self):
        parent = parse_acl("user::---,group::---,other::---")
        self.assertEqual(create_acl(parent, "777").to_mode(), 0o000)

    def test_created_acl_rejects_negative_mode(self):
        with self.assertRaises(AclError):
            create_acl(extended_default(), -1)

    def test_umask_bits_only_strip_requested_bits(self):
        created = create_acl(None, "000", umask="777")
        self.assertEqual(created.to_mode(), 0o000)

    def test_parent_default_ignores_umask(self):
        clipped = create_acl(extended_default(), "777")
        clipped_with_umask = create_acl(extended_default(), "777", umask="777")
        self.assertEqual(clipped, clipped_with_umask)

    def test_group_class_reflects_mask_for_extended_acl(self):
        created = create_acl(extended_default(), "740")
        self.assertEqual(created.group_class, Perm.READ)
        self.assertEqual(created.to_mode(), 0o740)

    def test_rejects_non_acl_default(self):
        with self.assertRaises(AclError):
            create_acl("user::rwx,group::r-x,other::---", "750")

    def test_created_file_keeps_owner_and_group(self):
        created = create_file(extended_default(), "750", owner="u1", group="g1")
        self.assertEqual((created.owner, created.group), ("u1", "g1"))


class AccessPrecedenceTests(unittest.TestCase):
    def test_owner_beats_named_user_entry(self):
        acl = create_acl(extended_default(), "750")
        # The owner is also explicitly named with broader permissions.
        subject = Subject("root")
        named = file_with(acl, owner="root")
        self.assertEqual(named.effective_permissions(subject), acl.user_obj)
        self.assertEqual(named.evaluate(subject).rule, "owner")

    def test_owner_unaffected_by_restrictive_mask(self):
        acl = create_acl(extended_default(), "700")
        self.assertEqual(acl.mask, Perm.NONE)
        self.assertEqual(file_with(acl).effective_permissions(Subject("root")), RWX)

    def test_named_user_is_limited_by_mask(self):
        acl = create_acl(extended_default(), "740")
        alice = Subject("alice")
        evaluation = file_with(acl).evaluate(alice)
        self.assertEqual(evaluation.rule, "named_user")
        self.assertEqual(evaluation.raw, RWX)
        self.assertEqual(evaluation.effective, Perm.READ)
        self.assertEqual(evaluation.mask, Perm.READ)

    def test_named_user_beats_matching_group(self):
        acl = parse_acl(
            "user::rwx,user:alice:r--,group::rwx,group:devs:rwx,mask::rwx,other::rwx"
        )
        alice = Subject("alice", groups=["devs", "staff"])
        evaluation = file_with(acl).evaluate(alice)
        self.assertEqual(evaluation.rule, "named_user")
        self.assertEqual(evaluation.effective, Perm.READ)

    def test_matching_groups_are_unioned(self):
        acl = parse_acl(
            "user::rwx,group::r--,group:devs:-w-,group:ops:--x,mask::rwx,other::---"
        )
        subject = Subject("dave", groups=["staff", "devs", "ops"])
        evaluation = file_with(acl).evaluate(subject)
        self.assertEqual(evaluation.rule, "group")
        self.assertEqual(evaluation.raw, RWX)
        self.assertEqual(evaluation.effective, RWX)
        self.assertEqual(
            evaluation.matched_groups, frozenset({"staff", "devs", "ops"})
        )

    def test_group_union_is_limited_by_mask(self):
        acl = parse_acl(
            "user::rwx,group::r--,group:devs:-w-,group:ops:--x,mask::r--,other::---"
        )
        subject = Subject("dave", groups=["staff", "devs", "ops"])
        evaluation = file_with(acl).evaluate(subject)
        self.assertEqual(evaluation.effective, Perm.READ)
        self.assertFalse(file_with(acl).check_access(subject, "w"))

    def test_owning_group_only_subject_uses_group_entry(self):
        acl = create_acl(extended_default(), "750")
        evaluation = file_with(acl).evaluate(Subject("carol", groups=["staff"]))
        self.assertEqual(evaluation.rule, "group")
        self.assertEqual(evaluation.effective, RX)

    def test_non_matching_subject_falls_back_to_other(self):
        acl = parse_acl("user::rwx,group::rwx,group:devs:rwx,mask::rwx,other::r--")
        evaluation = file_with(acl).evaluate(Subject("eve", groups=["visitors"]))
        self.assertEqual(evaluation.rule, "other")
        self.assertEqual(evaluation.effective, Perm.READ)

    def test_other_is_not_limited_by_mask(self):
        acl = parse_acl("user::rwx,group::rwx,group:devs:rwx,mask::---,other::rwx")
        evaluation = file_with(acl).evaluate(Subject("eve", groups=["visitors"]))
        self.assertEqual(evaluation.effective, RWX)

    def test_mask_zero_blocks_named_user_and_groups_but_not_owner_or_other(self):
        acl = parse_acl(
            "user::rwx,user:alice:rwx,group::rwx,group:devs:rwx,mask::---,other::r--"
        )
        file = file_with(acl)
        self.assertEqual(file.effective_permissions(Subject("root")), RWX)
        self.assertEqual(file.effective_permissions(Subject("alice")), Perm.NONE)
        self.assertEqual(
            file.effective_permissions(Subject("dave", groups=["devs"])), Perm.NONE
        )
        self.assertEqual(
            file.effective_permissions(Subject("eve", groups=["visitors"])), Perm.READ
        )

    def test_primary_group_counts_as_group_match(self):
        acl = parse_acl("user::rwx,group::r--,other::---")
        self.assertEqual(
            file_with(acl).effective_permissions(Subject("z", groups=["staff"])),
            Perm.READ,
        )

    def test_check_access_requires_full_requested_bits(self):
        acl = create_acl(extended_default(), "760")  # mask rw- for alice
        alice = Subject("alice")
        self.assertTrue(file_with(acl).check_access(alice, "r"))
        self.assertTrue(file_with(acl).check_access(alice, "rw"))
        self.assertFalse(file_with(acl).check_access(alice, "x"))
        self.assertFalse(file_with(acl).check_access(alice, "rwx"))

    def test_check_access_rejects_bad_requests_and_subjects(self):
        file = file_with(create_acl(extended_default(), "750"))
        with self.assertRaises(AclError):
            file.check_access(Subject("alice"), "rrr")
        with self.assertRaises(AclError):
            file.check_access(Subject("alice"), 8)
        with self.assertRaises(AclError):
            file.effective_permissions("alice")  # not a Subject

    def test_subject_groups_are_deduplicated(self):
        subject = Subject("dave", groups=["devs", "devs", "staff"])
        self.assertEqual(subject.groups, frozenset({"devs", "staff"}))

    def test_primary_group_is_folded_into_group_membership(self):
        subject = Subject("carol", primary_group="staff")
        self.assertEqual(subject.groups, frozenset({"staff"}))
        acl = parse_acl("user::rwx,group::r-x,other::---")
        self.assertEqual(file_with(acl).effective_permissions(subject), RX)

    def test_subject_label_defaults_to_uid(self):
        self.assertEqual(Subject("alice").label, "alice")
        self.assertEqual(Subject("alice", label="Alice").label, "Alice")


class ChmodTests(unittest.TestCase):
    def test_extended_acl_chmod_only_touches_mask(self):
        acl = create_acl(extended_default(), "750")
        file = file_with(acl)
        file.chmod("640")
        self.assertEqual(file.acl.mask, Perm.READ)
        self.assertEqual(file.acl.group_obj, RX)  # preserved
        self.assertEqual(file.acl.named_users, {"alice": RWX, "bob": RW})
        self.assertEqual(file.acl.named_groups, {"devs": RWX})
        self.assertEqual(file.acl.user_obj, RW)
        self.assertEqual(file.acl.other, Perm.NONE)

    def test_minimal_acl_chmod_moves_group_bits(self):
        file = file_with(create_acl(None, "640"))
        file.chmod("750")
        self.assertEqual(file.acl.group_obj, RX)
        self.assertIsNone(file.acl.mask)
        self.assertEqual(file.acl.to_mode(), 0o750)

    def test_chmod_updates_named_and_group_effective_permissions_together(self):
        file = file_with(create_acl(extended_default(), "777"))
        before = file.observe(
            {
                "owner": Subject("root"),
                "alice": Subject("alice"),
                "carol": Subject("carol", groups=["staff"]),
                "dave": Subject("dave", groups=["devs"]),
                "eve": Subject("eve", groups=["visitors"]),
            }
        )
        file.chmod("700")
        after = file.observe(
            {
                "owner": Subject("root"),
                "alice": Subject("alice"),
                "carol": Subject("carol", groups=["staff"]),
                "dave": Subject("dave", groups=["devs"]),
                "eve": Subject("eve", groups=["visitors"]),
            }
        )
        self.assertEqual(before["owner"], RWX)
        self.assertEqual(after["owner"], RWX)  # owner untouched by group-class change
        self.assertEqual(before["alice"], RWX)
        self.assertEqual(after["alice"], Perm.NONE)  # mask went to ---
        self.assertEqual(before["carol"], RX)
        self.assertEqual(after["carol"], Perm.NONE)
        self.assertEqual(before["dave"], RWX)
        self.assertEqual(after["dave"], Perm.NONE)
        self.assertEqual(before["eve"], Perm.NONE)
        self.assertEqual(after["eve"], Perm.NONE)

    def test_chmod_raising_group_bits_restores_masked_access(self):
        file = file_with(create_acl(extended_default(), "700"))
        alice = Subject("alice")
        self.assertEqual(file.effective_permissions(alice), Perm.NONE)
        file.chmod("740")
        self.assertEqual(file.effective_permissions(alice), Perm.READ)
        self.assertEqual(file.acl.group_obj, RX)  # still the default entry

    def test_chmod_rejects_invalid_mode_without_mutating(self):
        file = file_with(create_acl(extended_default(), "750"))
        snapshot = file.acl
        with self.assertRaises(AclError):
            file.chmod("999")
        self.assertEqual(file.acl, snapshot)


class ObserveTests(unittest.TestCase):
    def test_observe_maps_labels_to_effective_permissions(self):
        file = file_with(create_acl(extended_default(), "740"))
        seen = file.observe(
            [
                Subject("root", label="owner"),
                Subject("alice"),
                Subject("dave", groups=["devs"], label="dave"),
                Subject("eve", groups=["visitors"], label="eve"),
            ]
        )
        self.assertEqual(
            seen,
            {
                "owner": RWX,
                "alice": Perm.READ,  # rwx & mask r--
                "dave": Perm.READ,  # named group rwx & mask r--
                "eve": Perm.NONE,
            },
        )

    def test_observe_rejects_duplicate_labels(self):
        file = file_with(create_acl(extended_default(), "750"))
        with self.assertRaises(AclError):
            file.observe([Subject("alice", label="x"), Subject("bob", label="x")])

    def test_observe_rejects_non_subject_values(self):
        file = file_with(create_acl(extended_default(), "750"))
        with self.assertRaises(AclError):
            file.observe({"alice": "alice"})

    def test_observe_returns_evaluation_consistent_with_evaluate(self):
        file = file_with(create_acl(extended_default(), "740"))
        subjects = [Subject("alice"), Subject("dave", groups=["devs"])]
        seen = file.observe(subjects)
        for subject in subjects:
            self.assertEqual(seen[subject.label], file.effective_permissions(subject))


class CrossConstraintScenarioTests(unittest.TestCase):
    """The creation request, access precedence, and chmod must agree."""

    def test_creation_request_narrows_later_named_user_access(self):
        broad = create_file(extended_default(), "777", owner="root", group="staff")
        narrow = create_file(extended_default(), "770", owner="root", group="staff")
        alice = Subject("alice")
        # Named entry identical in both files, mask differs by creation request.
        self.assertEqual(broad.acl.named_users, narrow.acl.named_users)
        self.assertEqual(broad.effective_permissions(alice), RWX)
        self.assertEqual(narrow.effective_permissions(alice), RWX)
        # Execute comes from the requested group bits, which were stripped here.
        very_narrow = create_file(extended_default(), "744", owner="root", group="staff")
        self.assertEqual(very_narrow.effective_permissions(alice), Perm.READ)

    def test_owner_who_is_named_user_and_group_member_uses_owner_entry(self):
        acl = parse_acl(
            "user::r--,user:root:rwx,group::rwx,group:devs:rwx,mask::rwx,other::---"
        )
        root = Subject("root", groups=["staff", "devs"])
        evaluation = file_with(acl).evaluate(root)
        self.assertEqual(evaluation.rule, "owner")
        self.assertEqual(evaluation.effective, Perm.READ)

    def test_mixed_subjects_shift_together_across_chmod(self):
        file = create_file(extended_default(), "750", owner="root", group="staff")
        subjects = {
            "owner": Subject("root"),
            "alice": Subject("alice"),  # named user rwx
            "bob": Subject("bob"),  # named user rw-
            "carol": Subject("carol", groups=["staff"]),  # owning group r-x
            "dave": Subject("dave", groups=["devs"]),  # named group rwx
            "eve": Subject("eve", groups=["visitors"]),  # other ---
        }
        initial = file.observe(subjects)
        self.assertEqual(
            initial,
            {
                "owner": RWX,
                "alice": RX,  # rwx & mask r-x
                "bob": Perm.READ,  # rw- & mask r-x
                "carol": RX,  # group:: r-x & mask r-x
                "dave": RX,  # group:devs rwx & mask r-x
                "eve": Perm.NONE,
            },
        )

        file.chmod("755")
        self.assertEqual(
            file.observe(subjects),
            {
                "owner": RWX,
                "alice": RX,  # rwx & r-x
                "bob": Perm.READ,  # rw- & r-x
                "carol": RX,
                "dave": RX,
                "eve": RX,  # chmod also rewrites the other class
            },
        )

        file.chmod("770")
        widened = file.observe(subjects)
        self.assertEqual(widened["alice"], RWX)
        self.assertEqual(widened["bob"], RW)
        self.assertEqual(widened["carol"], RX)  # group:: stays r-x, only mask moves
        self.assertEqual(widened["dave"], RWX)
        self.assertEqual(widened["eve"], Perm.NONE)  # other bits back to ---

    def test_named_user_never_inherits_group_union(self):
        acl = parse_acl(
            "user::rwx,user:alice:r--,group::---,group:devs:rwx,mask::rwx,other::---"
        )
        alice = Subject("alice", groups=["devs", "staff"])
        self.assertEqual(file_with(acl).effective_permissions(alice), Perm.READ)

    def test_subject_matching_owning_group_and_named_group_gets_union_then_mask(self):
        acl = parse_acl(
            "user::rwx,group::r--,group:devs:-wx,mask::-wx,other::---"
        )
        file = file_with(acl)
        evaluation = file.evaluate(Subject("dave", groups=["staff", "devs"]))
        self.assertEqual(evaluation.raw, RWX)
        self.assertEqual(evaluation.effective, Perm.WRITE | Perm.EXEC)
        self.assertTrue(file.check_access(Subject("dave", groups=["staff", "devs"]), "wx"))
        self.assertFalse(file.check_access(Subject("dave", groups=["staff", "devs"]), "r"))


class DemoEntryPointTests(unittest.TestCase):
    def test_main_returns_zero_for_demo(self):
        import io
        from contextlib import redirect_stdout

        from acl_model import main

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(main([]), 0)
        self.assertIn("Effective permissions after creation:", buffer.getvalue())

    def test_main_reports_bad_acl(self):
        import io
        from contextlib import redirect_stderr

        from acl_model import main

        buffer = io.StringIO()
        with redirect_stderr(buffer):
            self.assertEqual(main(["--default-acl", "garbage"]), 2)
        self.assertIn("error:", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
