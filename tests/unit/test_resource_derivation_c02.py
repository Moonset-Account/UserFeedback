"""C02 - bare "*" Deny clearing and Condition-bearing Denies.

Contract under test (see Policy.get_allowed_resources in parliament/policy.py):

* An unconditional Deny whose resource is "*" short-circuits the whole
  derivation: get_allowed_resources returns [] even when concrete Allows exist.
* A Deny carrying a Condition is skipped by the allow/deny decision because it
  cannot be assumed to hold in every request context.  This applies both to
  "*" and to concrete resources.
"""

import unittest

from parliament import analyze_policy_string

from tests.support.resource_contracts import (
    DStmt,
    EXPLICIT_ACTION,
    MFA_CONDITION,
    OBJECTS,
    PRIVILEGE_NAME,
    PRIVILEGE_PREFIX,
    oracle_allowed_resources,
    oracle_references,
    render_policy,
)


class TestStarDenyClears(unittest.TestCase):
    def test_unconditional_star_deny_empties_all_concrete_allows(self):
        dstmts = [
            DStmt("ConcreteAllow", "Allow", EXPLICIT_ACTION, OBJECTS),
            DStmt("StarDeny", "Deny", EXPLICIT_ACTION, ["*"]),
        ]
        expected, star_deny = oracle_allowed_resources(dstmts)
        self.assertTrue(star_deny)
        self.assertEqual(expected, set())

        policy = analyze_policy_string(render_policy(dstmts))
        self.assertTrue(policy.is_valid)
        observed = policy.get_allowed_resources(
            PRIVILEGE_PREFIX, PRIVILEGE_NAME
        )
        self.assertEqual(observed, [])

    def test_star_deny_clears_even_when_deny_is_first_or_last(self):
        allow = DStmt("ConcreteAllow", "Allow", EXPLICIT_ACTION, OBJECTS)
        deny = DStmt("StarDeny", "Deny", EXPLICIT_ACTION, ["*"])
        for ordered in ([allow, deny], [deny, allow]):
            _, star_deny = oracle_allowed_resources(ordered)
            self.assertTrue(star_deny)
            policy = analyze_policy_string(render_policy(ordered))
            self.assertTrue(policy.is_valid)
            self.assertEqual(
                policy.get_allowed_resources(
                    PRIVILEGE_PREFIX, PRIVILEGE_NAME
                ),
                [],
                "ordering {} must still clear to []".format(
                    [d.sid for d in ordered]
                ),
            )

    def test_allow_on_star_is_not_treated_as_a_deny(self):
        # An Allow on "*" is a legitimate grant; it must not trigger the
        # deny-specific early return.  The oracle only predicts concrete
        # resources, so the concrete Allow is what we compare here.
        dstmts = [
            DStmt("StarAllow", "Allow", EXPLICIT_ACTION, ["*"]),
            DStmt("ConcreteAllow", "Allow", EXPLICIT_ACTION, [OBJECTS[0]]),
        ]
        expected, star_deny = oracle_allowed_resources(dstmts)
        self.assertFalse(star_deny)
        self.assertEqual(expected, {OBJECTS[0]})
        policy = analyze_policy_string(render_policy(dstmts))
        self.assertTrue(policy.is_valid)
        observed = set(
            policy.get_allowed_resources(PRIVILEGE_PREFIX, PRIVILEGE_NAME)
        )
        self.assertIn(OBJECTS[0], observed)


class TestConditionalDenyIsNotUniversal(unittest.TestCase):
    def test_conditional_star_deny_does_not_clear_concrete_allows(self):
        dstmts = [
            DStmt("ConcreteAllow", "Allow", EXPLICIT_ACTION, OBJECTS),
            DStmt(
                "ConditionalStarDeny",
                "Deny",
                EXPLICIT_ACTION,
                ["*"],
                condition=MFA_CONDITION,
            ),
        ]
        expected, star_deny = oracle_allowed_resources(dstmts)
        self.assertFalse(star_deny)
        self.assertEqual(expected, set(OBJECTS))

        policy = analyze_policy_string(render_policy(dstmts))
        self.assertTrue(policy.is_valid)
        observed = set(
            policy.get_allowed_resources(PRIVILEGE_PREFIX, PRIVILEGE_NAME)
        )
        self.assertEqual(observed, set(OBJECTS))

    def test_conditional_concrete_deny_does_not_remove_matching_allow(self):
        target = OBJECTS[0]
        dstmts = [
            DStmt("AllowTarget", "Allow", EXPLICIT_ACTION, [target]),
            DStmt(
                "ConditionalDenyTarget",
                "Deny",
                EXPLICIT_ACTION,
                [target],
                condition=MFA_CONDITION,
            ),
        ]
        expected, star_deny = oracle_allowed_resources(dstmts)
        self.assertFalse(star_deny)
        self.assertEqual(expected, {target})

        policy = analyze_policy_string(render_policy(dstmts))
        self.assertTrue(policy.is_valid)
        observed = set(
            policy.get_allowed_resources(PRIVILEGE_PREFIX, PRIVILEGE_NAME)
        )
        self.assertEqual(observed, {target})

    def test_unconditional_and_conditional_deny_mix_on_same_resource(self):
        target = OBJECTS[0]
        sibling = OBJECTS[1]
        dstmts = [
            DStmt("AllowAll", "Allow", EXPLICIT_ACTION, [target, sibling]),
            DStmt(
                "ConditionalDenyTarget",
                "Deny",
                EXPLICIT_ACTION,
                [target],
                condition=MFA_CONDITION,
            ),
            DStmt("HardDenyTarget", "Deny", EXPLICIT_ACTION, [target]),
        ]
        expected, star_deny = oracle_allowed_resources(dstmts)
        self.assertFalse(star_deny)
        # The unconditional Deny alone is enough; the conditional Deny adds
        # nothing, and the sibling object is untouched.
        self.assertEqual(expected, {sibling})
        policy = analyze_policy_string(render_policy(dstmts))
        self.assertTrue(policy.is_valid)
        observed = set(
            policy.get_allowed_resources(PRIVILEGE_PREFIX, PRIVILEGE_NAME)
        )
        self.assertEqual(observed, {sibling})

    def test_references_still_report_conditional_deny_statement(self):
        # get_references does not reason about Condition at all; the
        # Condition-aware skipping happens only inside get_allowed_resources.
        target = OBJECTS[0]
        dstmts = [
            DStmt("AllowTarget", "Allow", EXPLICIT_ACTION, [target]),
            DStmt(
                "ConditionalDenyTarget",
                "Deny",
                EXPLICIT_ACTION,
                [target],
                condition=MFA_CONDITION,
            ),
        ]
        expected_refs = oracle_references(dstmts)
        policy = analyze_policy_string(render_policy(dstmts))
        self.assertTrue(policy.is_valid)
        refs = policy.get_references(PRIVILEGE_PREFIX, PRIVILEGE_NAME)
        self.assertEqual(
            {resource: sorted(s.sid for s in stmts) for resource, stmts in refs.items()},
            {resource: sorted(sids) for resource, sids in expected_refs.items()},
        )
        conditional_stmt = next(
            s for s in refs[target] if s.sid == "ConditionalDenyTarget"
        )
        self.assertFalse(conditional_stmt.effect_allow)
        self.assertIn("Condition", conditional_stmt.stmt)


if __name__ == "__main__":
    unittest.main()
