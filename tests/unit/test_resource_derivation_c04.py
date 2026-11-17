"""C04 - failure-sample reduction, statement positions, ARN-pattern boundary.

The reducer (tests/support/resource_contracts.ddmin_statements) shrinks a
synthetic policy that exhibits an unconditional object-level Deny while
sibling objects remain allowed.  Invariants checked on every reduced output:

* the surviving resources keep the same S3 *object* resource type (no bucket
  ARN or bare "*" may replace the object ARN);
* the same unconditional Deny relationship on the target object survives;
* surviving statements keep their original relative positions/Sids, so a
  finding location cannot be attributed to a different Statement;
* the result is 1-minimal (the reducer asserts this itself).

A separate, explicitly-labeled boundary test documents that Parliament does
NOT promise arbitrary ARN-pattern intersection semantics: resources are grouped
by their exact string, and a glob-pattern Allow and a concrete Deny pass each
other without interacting.  This characterizes current behavior only and must
not be read as a complete IAM authorization decision.
"""

import unittest

from parliament import analyze_policy_string

from tests.support.resource_contracts import (
    DStmt,
    EXPLICIT_ACTION,
    OBJECTS,
    PRIVILEGE_NAME,
    PRIVILEGE_PREFIX,
    build_reduction_input,
    ddmin_statements,
    render_policy,
)


TARGET = OBJECTS[0]


class TestFailureSampleReduction(unittest.TestCase):
    def setUp(self):
        self.original = build_reduction_input(TARGET)
        self.original_sids = [d.sid for d in self.original]
        self.reduced = ddmin_statements(self.original, TARGET)

    def test_reduction_shrinks_the_policy(self):
        self.assertLess(
            len(self.reduced),
            len(self.original),
            "reducer must remove at least one statement",
        )
        self.assertGreaterEqual(len(self.reduced), 2)

    def test_reduced_resources_keep_object_resource_type(self):
        for d in self.reduced:
            for resource in d.resources:
                if resource.startswith("arn:aws:s3:::"):
                    self.assertIn(
                        "/",
                        resource,
                        "reduction changed an object ARN into a bucket ARN: "
                        "{}".format(resource),
                    )
                else:
                    self.fail(
                        "reduction introduced a non-object resource: {}".format(
                            resource
                        )
                    )

    def test_unconditional_deny_relationship_survives(self):
        deny_stmts = [d for d in self.reduced if d.is_deny()]
        self.assertTrue(
            any(
                not d.has_condition() and d.resources == [TARGET]
                for d in deny_stmts
            ),
            "reduced policy lost the unconditional Deny on {}".format(TARGET),
        )
        target_deny = next(
            d
            for d in self.reduced
            if d.is_deny() and not d.has_condition() and TARGET in d.resources
        )
        self.assertEqual(target_deny.resources, [TARGET])

        # Parliament must still reproduce the deny relationship on the reduced
        # policy: the target object is excluded while a sibling stays allowed.
        policy = analyze_policy_string(render_policy(self.reduced))
        self.assertTrue(policy.is_valid)
        observed = set(
            policy.get_allowed_resources(PRIVILEGE_PREFIX, PRIVILEGE_NAME)
        )
        self.assertNotIn(TARGET, observed)
        sibling_objects = [o for o in OBJECTS if o != TARGET]
        self.assertTrue(
            any(o in observed for o in sibling_objects),
            "reduction must preserve an allowed sibling object",
        )

    def test_surviving_statement_positions_and_sids_are_preserved(self):
        reduced_sids = [d.sid for d in self.reduced]
        # The reduced policy is a subsequence of the original: no reordering,
        # no renamed/recreated statements.
        indices = [self.original_sids.index(sid) for sid in reduced_sids]
        self.assertEqual(indices, sorted(indices))
        self.assertEqual(len(indices), len(set(indices)))

        original_policy = analyze_policy_string(render_policy(self.original))
        reduced_policy = analyze_policy_string(render_policy(self.reduced))
        original_by_sid = {s.sid: s for s in original_policy.statements}
        for stmt in reduced_policy.statements:
            self.assertIn(stmt.sid, original_by_sid)
            original_stmt = original_by_sid[stmt.sid]
            # Effect and resources travel with the same statement identity, so
            # a location reported on the reduced policy cannot point elsewhere.
            self.assertEqual(
                stmt.effect_allow, original_stmt.effect_allow
            )
            self.assertEqual(
                set(
                    stmt.get_resources_for_privilege(
                        PRIVILEGE_PREFIX, PRIVILEGE_NAME
                    )
                ),
                set(
                    original_stmt.get_resources_for_privilege(
                        PRIVILEGE_PREFIX, PRIVILEGE_NAME
                    )
                ),
            )

    def test_minimal_manual_fixture_matches_reducer_output_relationship(self):
        # A hand-written two-statement reduction exhibits the same contract.
        sibling = OBJECTS[2]
        manual = [
            DStmt("BroadAllow", "Allow", EXPLICIT_ACTION, [TARGET, sibling]),
            DStmt("TargetDeny", "Deny", EXPLICIT_ACTION, [TARGET]),
        ]
        policy = analyze_policy_string(render_policy(manual))
        self.assertTrue(policy.is_valid)
        observed = set(
            policy.get_allowed_resources(PRIVILEGE_PREFIX, PRIVILEGE_NAME)
        )
        self.assertEqual(observed, {sibling})
        refs = policy.get_references(PRIVILEGE_PREFIX, PRIVILEGE_NAME)
        self.assertEqual(
            sorted(s.sid for s in refs[TARGET]),
            ["BroadAllow", "TargetDeny"],
        )


class TestArbitraryArnIntersectionIsNotPromised(unittest.TestCase):
    """Boundary characterization, not an IAM authorization guarantee."""

    def test_glob_allow_and_concrete_deny_are_separate_resource_keys(self):
        glob_resource = "arn:aws:s3:::b-alpha/*"
        concrete = "arn:aws:s3:::b-alpha/logs/a.txt"
        dstmts = [
            DStmt("PatternAllow", "Allow", EXPLICIT_ACTION, [glob_resource]),
            DStmt("ConcreteDeny", "Deny", EXPLICIT_ACTION, [concrete]),
        ]
        policy = analyze_policy_string(render_policy(dstmts))
        self.assertTrue(policy.is_valid)

        refs = policy.get_references(PRIVILEGE_PREFIX, PRIVILEGE_NAME)
        # The two resource strings never intersect inside Parliament.
        self.assertEqual(
            sorted(refs.keys()), sorted([glob_resource, concrete])
        )
        self.assertEqual(
            [s.sid for s in refs[glob_resource]], ["PatternAllow"]
        )
        self.assertEqual(
            [s.sid for s in refs[concrete]], ["ConcreteDeny"]
        )

        observed = set(
            policy.get_allowed_resources(PRIVILEGE_PREFIX, PRIVILEGE_NAME)
        )
        # Current implementation keeps the glob Allow even though a real IAM
        # evaluation could deny the concrete object covered by the glob.
        self.assertEqual(observed, {glob_resource})
        self.assertNotIn(concrete, observed)

    def test_concrete_allow_and_glob_deny_are_separate_resource_keys(self):
        glob_resource = "arn:aws:s3:::b-alpha/*"
        concrete = "arn:aws:s3:::b-alpha/logs/a.txt"
        dstmts = [
            DStmt("ConcreteAllow", "Allow", EXPLICIT_ACTION, [concrete]),
            DStmt("PatternDeny", "Deny", EXPLICIT_ACTION, [glob_resource]),
        ]
        policy = analyze_policy_string(render_policy(dstmts))
        self.assertTrue(policy.is_valid)
        observed = set(
            policy.get_allowed_resources(PRIVILEGE_PREFIX, PRIVILEGE_NAME)
        )
        # Mirror-image case: the glob Deny does not suppress the concrete
        # Allow, because no pattern intersection is performed.
        self.assertEqual(observed, {concrete})

    def test_star_resource_remains_a_special_case(self):
        # Only the literal "*" has dedicated clear-to-empty handling; an
        # ARN-shaped wildcard does not get that treatment.
        glob_resource = "arn:aws:s3:::b-alpha/*"
        dstmts = [
            DStmt("AllowObject", "Allow", EXPLICIT_ACTION, [OBJECTS[0]]),
            DStmt("GlobDeny", "Deny", EXPLICIT_ACTION, [glob_resource]),
        ]
        policy = analyze_policy_string(render_policy(dstmts))
        self.assertTrue(policy.is_valid)
        observed = policy.get_allowed_resources(
            PRIVILEGE_PREFIX, PRIVILEGE_NAME
        )
        self.assertNotEqual(
            observed, [], "ARN-glob deny must not trigger the '*' early return"
        )
        self.assertIn(OBJECTS[0], observed)


if __name__ == "__main__":
    unittest.main()
