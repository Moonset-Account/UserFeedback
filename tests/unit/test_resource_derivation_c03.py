"""C03 - representation invariance, reordering, unrelated resources, actions.

These tests pin the *contract* of the current implementation rather than any
IAM authorization semantics:

* Reordering Statement entries does not change which resources are allowed.
* A single-element JSON array and a JSON scalar express the same Action /
  Resource and derive the same results.
* Adding resources or statements unrelated to s3:GetObject does not change
  the s3:GetObject conclusions for the original S3 object resources.
* Explicit (s3:GetObject) and wildcard (s3:Get*, s3:*, "*") Action forms all
  reach the same object-resource derivation.
"""

import itertools
import unittest

from parliament import analyze_policy_string

from tests.support.resource_contracts import (
    ACTIONS_COVERING_GETOBJECT,
    DStmt,
    EXPLICIT_ACTION,
    OBJECTS,
    PRIVILEGE_NAME,
    PRIVILEGE_PREFIX,
    WILDCARD_EVERYTHING_ACTION,
    WILDCARD_GLOB_ACTION,
    WILDCARD_SERVICE_ACTION,
    oracle_allowed_resources,
    render_policy,
)


SQS_ARN = "arn:aws:sqs:us-east-1:123456789012:q-one"
S3_BUCKET_ARN = "arn:aws:s3:::b-alpha"


def _allowed_set(policy_str):
    policy = analyze_policy_string(policy_str)
    assert policy.is_valid, "synthetic policy must analyze cleanly"
    return policy, set(
        policy.get_allowed_resources(PRIVILEGE_PREFIX, PRIVILEGE_NAME)
    )


class TestStatementReordering(unittest.TestCase):
    def _base(self):
        target = OBJECTS[0]
        sibling = OBJECTS[2]
        return (
            target,
            sibling,
            [
                DStmt(
                    "AllowSibling",
                    "Allow",
                    EXPLICIT_ACTION,
                    [sibling],
                ),
                DStmt(
                    "AllowAndDenyTargetAllow",
                    "Allow",
                    EXPLICIT_ACTION,
                    [target],
                ),
                DStmt(
                    "HardDenyTarget",
                    "Deny",
                    EXPLICIT_ACTION,
                    [target],
                ),
                DStmt(
                    "AllowBothObjects",
                    "Allow",
                    EXPLICIT_ACTION,
                    [target, sibling],
                ),
            ],
        )

    def test_all_permutations_keep_the_same_allowed_set(self):
        target, sibling, dstmts = self._base()
        expected, star_deny = oracle_allowed_resources(dstmts)
        self.assertFalse(star_deny)
        self.assertEqual(expected, {sibling})

        observed_sets = set()
        for permutation in itertools.permutations(dstmts):
            _, observed = _allowed_set(render_policy(list(permutation)))
            observed_sets.add(tuple(sorted(observed)))
        self.assertEqual(observed_sets, {tuple(sorted(expected))})

    def test_grfp_results_are_attached_to_the_correct_statement(self):
        # Reordering must not make a resource "bleed" into another statement's
        # get_resources_for_privilege output.
        target, sibling, dstmts = self._base()
        original = analyze_policy_string(render_policy(dstmts))
        reversed_policy = analyze_policy_string(
            render_policy(list(reversed(dstmts)))
        )
        self.assertTrue(original.is_valid)
        self.assertTrue(reversed_policy.is_valid)

        by_sid_original = {
            stmt.sid: set(
                stmt.get_resources_for_privilege(
                    PRIVILEGE_PREFIX, PRIVILEGE_NAME
                )
            )
            for stmt in original.statements
        }
        by_sid_reversed = {
            stmt.sid: set(
                stmt.get_resources_for_privilege(
                    PRIVILEGE_PREFIX, PRIVILEGE_NAME
                )
            )
            for stmt in reversed_policy.statements
        }
        self.assertEqual(by_sid_original, by_sid_reversed)
        self.assertEqual(by_sid_original["HardDenyTarget"], {target})
        self.assertEqual(by_sid_original["AllowSibling"], {sibling})
        self.assertEqual(
            by_sid_original["AllowBothObjects"], {target, sibling}
        )


class TestScalarVsListRepresentation(unittest.TestCase):
    def test_scalar_and_list_resource_derive_identically(self):
        target = OBJECTS[0]
        list_form = [DStmt("AllowOne", "Allow", EXPLICIT_ACTION, [target])]
        expected, _ = oracle_allowed_resources(list_form)
        _, observed_list = _allowed_set(
            render_policy(list_form, scalar_resource=False)
        )
        _, observed_scalar = _allowed_set(
            render_policy(list_form, scalar_resource=True)
        )
        self.assertEqual(observed_list, expected)
        self.assertEqual(observed_scalar, expected)

    def test_scalar_and_list_action_derive_identically(self):
        target = OBJECTS[0]
        dstmts = [DStmt("AllowOne", "Allow", EXPLICIT_ACTION, [target])]
        _, observed_list = _allowed_set(
            render_policy(dstmts, scalar_action=False)
        )
        _, observed_scalar = _allowed_set(
            render_policy(dstmts, scalar_action=True)
        )
        self.assertEqual(observed_list, {target})
        self.assertEqual(observed_scalar, observed_list)

    def test_multi_resource_list_is_not_collapsed_to_one(self):
        dstmts = [DStmt("AllowMany", "Allow", EXPLICIT_ACTION, OBJECTS)]
        expected, _ = oracle_allowed_resources(dstmts)
        policy, observed = _allowed_set(render_policy(dstmts))
        self.assertEqual(observed, expected)
        self.assertEqual(len(observed), len(OBJECTS))
        grfp = set(
            policy.statements[0].get_resources_for_privilege(
                PRIVILEGE_PREFIX, PRIVILEGE_NAME
            )
        )
        self.assertEqual(grfp, set(OBJECTS))


class TestUnrelatedResourcesAndStatements(unittest.TestCase):
    def test_unrelated_sqs_statement_does_not_change_s3_conclusion(self):
        target = OBJECTS[0]
        base = [DStmt("AllowObject", "Allow", EXPLICIT_ACTION, [target])]
        extended = base + [
            DStmt(
                "QueueStatement",
                "Allow",
                "sqs:SendMessage",
                [SQS_ARN],
            )
        ]
        expected, _ = oracle_allowed_resources(base)
        _, observed_base = _allowed_set(render_policy(base))
        _, observed_extended = _allowed_set(render_policy(extended))
        self.assertEqual(observed_base, expected)
        self.assertEqual(observed_extended, expected)

        extended_policy = analyze_policy_string(render_policy(extended))
        queue_stmt = next(
            s for s in extended_policy.statements if s.sid == "QueueStatement"
        )
        # The unrelated statement genuinely contributes no S3 object
        # resources, rather than silently matching "*".
        self.assertEqual(
            queue_stmt.get_resources_for_privilege(
                PRIVILEGE_PREFIX, PRIVILEGE_NAME
            ),
            [],
        )

    def test_bucket_arn_is_unrelated_to_object_privilege(self):
        # s3:GetObject only applies to object* resources; a bucket ARN in the
        # same statement must not show up in its object-resource derivation.
        target = OBJECTS[0]
        dstmts = [
            DStmt(
                "MixedResources",
                "Allow",
                EXPLICIT_ACTION,
                [S3_BUCKET_ARN, target],
            )
        ]
        policy = analyze_policy_string(render_policy(dstmts))
        self.assertTrue(policy.is_valid)
        grfp = set(
            policy.statements[0].get_resources_for_privilege(
                PRIVILEGE_PREFIX, PRIVILEGE_NAME
            )
        )
        self.assertEqual(grfp, {target})
        self.assertNotIn(S3_BUCKET_ARN, grfp)
        observed = set(
            policy.get_allowed_resources(PRIVILEGE_PREFIX, PRIVILEGE_NAME)
        )
        self.assertEqual(observed, {target})

    def test_adding_unrelated_resource_keeps_deny_relationship(self):
        target = OBJECTS[0]
        sibling = OBJECTS[1]
        base = [
            DStmt("AllowBoth", "Allow", EXPLICIT_ACTION, [target, sibling]),
            DStmt("HardDeny", "Deny", EXPLICIT_ACTION, [target]),
        ]
        with_extra = [
            DStmt(
                "AllowBoth",
                "Allow",
                EXPLICIT_ACTION,
                [S3_BUCKET_ARN, target, sibling],
            ),
            DStmt("HardDeny", "Deny", EXPLICIT_ACTION, [target]),
        ]
        expected, _ = oracle_allowed_resources(base)
        self.assertEqual(expected, {sibling})
        _, observed_base = _allowed_set(render_policy(base))
        _, observed_extra = _allowed_set(render_policy(with_extra))
        self.assertEqual(observed_base, {sibling})
        self.assertEqual(observed_extra, observed_base)


class TestActionForms(unittest.TestCase):
    def test_all_action_forms_cover_getobject(self):
        self.assertEqual(
            set(ACTIONS_COVERING_GETOBJECT),
            {
                EXPLICIT_ACTION,
                WILDCARD_GLOB_ACTION,
                WILDCARD_SERVICE_ACTION,
                WILDCARD_EVERYTHING_ACTION,
            },
        )

    def test_explicit_and_wildcard_actions_derive_same_object_resources(self):
        target = OBJECTS[0]
        results = {}
        for action in ACTIONS_COVERING_GETOBJECT:
            dstmts = [DStmt("Stmt", "Allow", action, [target])]
            policy = analyze_policy_string(render_policy(dstmts))
            self.assertTrue(
                policy.is_valid,
                "action {} produced an invalid policy".format(action),
            )
            results[action] = set(
                policy.get_allowed_resources(
                    PRIVILEGE_PREFIX, PRIVILEGE_NAME
                )
            )
        for action, observed in results.items():
            self.assertEqual(
                observed,
                {target},
                "action {} derived {} instead of {{target}}".format(
                    action, observed
                ),
            )

    def test_wildcard_action_still_respects_object_resource_filter(self):
        # s3:* names many privileges, but for s3:GetObject specifically only
        # the object ARN is returned; the bucket ARN without a slash is not.
        dstmts = [
            DStmt(
                "StarAction",
                "Allow",
                WILDCARD_SERVICE_ACTION,
                [S3_BUCKET_ARN, OBJECTS[0]],
            )
        ]
        policy = analyze_policy_string(render_policy(dstmts))
        self.assertTrue(policy.is_valid)
        grfp = set(
            policy.statements[0].get_resources_for_privilege(
                PRIVILEGE_PREFIX, PRIVILEGE_NAME
            )
        )
        self.assertEqual(grfp, {OBJECTS[0]})

    def test_non_matching_action_yields_no_resources(self):
        dstmts = [
            DStmt(
                "ListOnly",
                "Allow",
                "s3:ListBucket",
                [S3_BUCKET_ARN],
            )
        ]
        policy = analyze_policy_string(render_policy(dstmts))
        self.assertTrue(policy.is_valid)
        self.assertEqual(
            policy.statements[0].get_resources_for_privilege(
                PRIVILEGE_PREFIX, PRIVILEGE_NAME
            ),
            [],
        )
        self.assertEqual(
            policy.get_allowed_resources(PRIVILEGE_PREFIX, PRIVILEGE_NAME),
            [],
        )


if __name__ == "__main__":
    unittest.main()
