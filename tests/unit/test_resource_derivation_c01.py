"""C01 - finite S3 domain: Allow vs. unconditional Deny on the same resource.

The expected results are computed by an independent small-domain oracle in
``tests/support/resource_contracts.py`` rather than derived from Parliament
itself.  The combinatorial policies only use concrete S3 object ARNs (plus the
bare "*" case exercised separately in C02), so exact resource-string reasoning
is sufficient and no ARN-pattern intersection semantics are assumed.
"""

import random
import unittest

from parliament import analyze_policy_string

from tests.support.resource_contracts import (
    ACTIONS_COVERING_GETOBJECT,
    DStmt,
    EXPLICIT_ACTION,
    OBJECTS,
    PRIVILEGE_NAME,
    PRIVILEGE_PREFIX,
    oracle_allowed_resources,
    render_policy,
)


SEED = 20261118


def _parliament_allowed(policy_str):
    policy = analyze_policy_string(policy_str)
    assert policy.is_valid, "synthetic policy must analyze cleanly"
    return policy, set(
        policy.get_allowed_resources(PRIVILEGE_PREFIX, PRIVILEGE_NAME)
    )


class TestFiniteDomainOracle(unittest.TestCase):
    def test_allow_only_baseline(self):
        dstmts = [DStmt("S0Allow", "Allow", EXPLICIT_ACTION, OBJECTS)]
        expected, star_deny = oracle_allowed_resources(dstmts)
        self.assertFalse(star_deny)
        _, observed = _parliament_allowed(render_policy(dstmts))
        self.assertEqual(expected, set(OBJECTS))
        self.assertEqual(observed, expected)

    def test_generated_policies_match_oracle(self):
        # Fixed seed: every generated policy is exactly reproducible.
        rng = random.Random(SEED)
        generated = 8
        for case_id in range(generated):
            dstmts = []
            for index, resource in enumerate(OBJECTS):
                if rng.choice([True, False]):
                    dstmts.append(
                        DStmt(
                            "Allow{}".format(index),
                            "Allow",
                            EXPLICIT_ACTION,
                            [resource],
                        )
                    )
                if rng.choice([True, False]):
                    dstmts.append(
                        DStmt(
                            "Deny{}".format(index),
                            "Deny",
                            EXPLICIT_ACTION,
                            [resource],
                        )
                    )
            # Guarantee at least one Allow and one Deny so the run is
            # non-vacuous; the policy stays within the same finite domain.
            dstmts.append(
                DStmt("TrailingAllow", "Allow", EXPLICIT_ACTION, [OBJECTS[0]])
            )
            dstmts.append(
                DStmt("TrailingDeny", "Deny", EXPLICIT_ACTION, [OBJECTS[1]])
            )
            rng.shuffle(dstmts)

            expected, star_deny = oracle_allowed_resources(dstmts)
            self.assertFalse(
                star_deny,
                "C01 generator must not emit a bare-* deny (covered in C02)",
            )
            _, observed = _parliament_allowed(render_policy(dstmts))
            self.assertEqual(
                observed,
                expected,
                "seed={} case={} dstmts={}".format(
                    SEED,
                    case_id,
                    [(d.sid, d.effect, d.resources) for d in dstmts],
                ),
            )


class TestUnconditionalDenyPriority(unittest.TestCase):
    def _policy(self):
        target = OBJECTS[0]
        sibling = OBJECTS[2]
        return (
            target,
            sibling,
            [
                DStmt("AllowBoth", "Allow", EXPLICIT_ACTION, [target, sibling]),
                DStmt("DenyTarget", "Deny", EXPLICIT_ACTION, [target]),
            ],
        )

    def test_same_resource_unconditional_deny_overrides_allow(self):
        target, sibling, dstmts = self._policy()
        expected, star_deny = oracle_allowed_resources(dstmts)
        self.assertFalse(star_deny)
        self.assertEqual(expected, {sibling})

        policy, observed = _parliament_allowed(render_policy(dstmts))
        self.assertEqual(observed, {sibling})
        self.assertNotIn(target, observed)

        # The deny statement must be the reason: get_references groups both
        # effects under the same exact resource string.
        refs = policy.get_references(PRIVILEGE_PREFIX, PRIVILEGE_NAME)
        self.assertEqual(
            sorted(stmt.sid for stmt in refs[target]),
            ["AllowBoth", "DenyTarget"],
        )
        effects = {stmt.effect_allow for stmt in refs[target]}
        self.assertEqual(effects, {True, False})

    def test_deny_only_on_resource_is_not_an_allow(self):
        target = OBJECTS[0]
        dstmts = [DStmt("LoneDeny", "Deny", EXPLICIT_ACTION, [target])]
        expected, _ = oracle_allowed_resources(dstmts)
        self.assertEqual(expected, set())
        _, observed = _parliament_allowed(render_policy(dstmts))
        self.assertEqual(observed, set())

    def test_priority_is_symmetric_under_statement_reordering(self):
        target, sibling, dstmts = self._policy()
        reversed_dstmts = list(reversed(dstmts))
        self.assertEqual(
            [d.sid for d in reversed_dstmts], ["DenyTarget", "AllowBoth"]
        )
        expected, _ = oracle_allowed_resources(dstmts)
        _, observed_forward = _parliament_allowed(render_policy(dstmts))
        _, observed_reversed = _parliament_allowed(
            render_policy(reversed_dstmts)
        )
        self.assertEqual(observed_forward, expected)
        self.assertEqual(observed_reversed, expected)
        self.assertEqual(observed_forward, observed_reversed)

    def test_action_form_does_not_change_priority_on_same_domain(self):
        # Every action representation here covers s3:GetObject; the same
        # unconditional Deny must win in every representation.
        target, sibling, base = self._policy()
        for action in ACTIONS_COVERING_GETOBJECT:
            dstmts = [
                DStmt("AllowBoth", "Allow", action, [target, sibling]),
                DStmt("DenyTarget", "Deny", action, [target]),
            ]
            expected, star_deny = oracle_allowed_resources(dstmts)
            self.assertFalse(star_deny)
            self.assertEqual(expected, {sibling})
            _, observed = _parliament_allowed(render_policy(dstmts))
            self.assertEqual(
                observed,
                {sibling},
                "action representation {} changed the deny result".format(action),
            )


if __name__ == "__main__":
    unittest.main()
