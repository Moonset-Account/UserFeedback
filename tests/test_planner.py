"""Behavioural tests for the requirements stated in the task."""

import json
from fractions import Fraction
import unittest

from wallet_planner import (
    MAX_DISCLOSURE_GROUPS,
    SpecError,
    UnsatisfiableError,
    parse_spec,
    plan_from_dict,
    plan_presentation,
    require_plan,
)


def credential(cid, issuer="gov", holder="alice", groups=(), verified=True):
    return {
        "id": cid,
        "issuer": issuer,
        "holder": holder,
        "verified": verified,
        "groups": [{"id": gid, "attributes": list(attrs)} for gid, attrs in groups],
    }


def plan(credentials, conditions, weights=None):
    return plan_presentation(
        {
            "attribute_weights": weights or {},
            "credentials": credentials,
            "conditions": conditions,
        }
    )


class MinimalWeightTests(unittest.TestCase):
    def test_prefers_cheaper_single_group_over_two_cheap_ones(self):
        # name is available alone on c1 and bundled with address on c2.
        result = plan(
            [
                credential(
                    "c1", groups=[("c1.name", ["name"]), ("c1.addr", ["address"])]
                ),
                credential("c2", groups=[("c2.both", ["name", "address"])]),
            ],
            [{"id": "k", "alternatives": [["name"]]}],
        )
        self.assertEqual(result.selected_group_ids, ("c1.name",))
        self.assertEqual(result.total_weight, 1)

    def test_weight_beats_credential_count(self):
        # c1.a + c1.b and c2.ab both expose {a, b} at weight 2 with one
        # credential, so the stable credential number decides: c1 wins.
        result = plan(
            [
                credential("c1", groups=[("c1.a", ["a"]), ("c1.b", ["b"])]),
                credential("c2", groups=[("c2.c", ["c"]), ("c2.ab", ["a", "b"])]),
            ],
            [{"id": "k", "alternatives": [["a", "b"]]}],
            weights={"a": 1, "b": 1, "c": 10},
        )
        self.assertEqual(result.total_weight, 2)
        self.assertEqual(result.credential_count, 1)
        self.assertEqual(result.selected_group_ids, ("c1.a", "c1.b"))

    def test_weight_is_primary_over_credential_count(self):
        # Exposing {a, b} on one credential costs an expensive c; splitting it
        # over two cheap credentials exposes less weight and must win.
        result = plan(
            [
                credential("c1", groups=[("c1.a", ["a"])]),
                credential("c2", groups=[("c2.b", ["b"])]),
                credential("c3", groups=[("c3.ab", ["a", "b", "c"])]),
            ],
            [{"id": "k", "alternatives": [["a", "b"]]}],
            weights={"a": 1, "b": 1, "c": 10},
        )
        self.assertEqual(result.total_weight, 2)
        self.assertEqual(result.credential_count, 2)
        self.assertEqual(result.selected_group_ids, ("c1.a", "c2.b"))

    def test_zero_weight_attributes_are_allowed(self):
        result = plan(
            [credential("c1", groups=[("c1.g", ["a"])])],
            [{"id": "k", "alternatives": [["a"]]}],
            weights={"a": 0},
        )
        self.assertEqual(result.total_weight, 0)
        self.assertEqual(result.selected_group_ids, ("c1.g",))


class CredentialCountTieBreakTests(unittest.TestCase):
    def test_fewer_credentials_after_equal_weight(self):
        # Both plans expose exactly {a}: one group on c1, two groups on c2.
        result = plan(
            [
                credential("c1", groups=[("c1.g1", ["a"])]),
                credential("c2", groups=[("c2.g1", ["a"])]),
            ],
            [{"id": "k", "alternatives": [["a"]]}],
        )
        self.assertEqual(result.credential_ids, ("c1",))
        self.assertEqual(result.credential_count, 1)

    def test_stable_choice_by_credential_number(self):
        # Identical exposures; the lower credential number must win.
        result = plan(
            [
                credential("cred-10", groups=[("cred-10.g1", ["a"])]),
                credential("cred-2", groups=[("cred-2.g1", ["a"])]),
            ],
            [{"id": "k", "alternatives": [["a"]]}],
        )
        self.assertEqual(result.credential_ids, ("cred-2",))
        self.assertEqual(result.selected_group_ids, ("cred-2.g1",))

    def test_credential_numbers_use_natural_not_lexicographic_order(self):
        # "c9" < "c10" is the natural reading of credential numbers; a plain
        # string comparison would wrongly pick "c10".
        result = plan(
            [
                credential("c10", groups=[("c10.g1", ["a"])]),
                credential("c9", groups=[("c9.g1", ["a"])]),
            ],
            [{"id": "k", "alternatives": [["a"]]}],
        )
        self.assertEqual(result.credential_ids, ("c9",))

    def test_stable_choice_by_group_number_within_credential(self):
        result = plan(
            [
                credential(
                    "c1",
                    groups=[("c1.g10", ["a"]), ("c1.g2", ["a"])],
                )
            ],
            [{"id": "k", "alternatives": [["a"]]}],
        )
        self.assertEqual(result.selected_group_ids, ("c1.g2",))

    def test_integer_credential_numbers_are_stable(self):
        result = plan(
            [
                credential(10, groups=[("g10", ["a"])]),
                credential(2, groups=[("g2", ["a"])]),
            ],
            [{"id": "k", "alternatives": [["a"]]}],
        )
        self.assertEqual(result.credential_ids, ("2",))
        self.assertEqual(result.selected_group_ids, ("g2",))


class GroupIndivisibilityTests(unittest.TestCase):
    def test_group_cannot_be_split(self):
        # The only source of `a` also discloses a very expensive `b`.
        result = plan(
            [credential("c1", groups=[("c1.g", ["a", "b"])])],
            [{"id": "k", "alternatives": [["a"]]}],
            weights={"a": 1, "b": 100},
        )
        self.assertEqual(result.selected_group_ids, ("c1.g",))
        self.assertEqual(result.exposed_attributes, ("a", "b"))
        self.assertEqual(result.total_weight, 101)
        provenance = result.conditions[0]
        self.assertEqual(provenance.extra_attributes, ("b",))

    def test_disclosure_group_is_order_independent(self):
        result = plan(
            [credential("c1", groups=[("c1.g", ["b", "a"])])],
            [{"id": "k", "alternatives": [["a"]]}],
        )
        self.assertEqual(result.exposed_attributes, ("a", "b"))


class RepeatedGroupTests(unittest.TestCase):
    def test_same_group_twice_counts_once(self):
        result = plan(
            [credential("c1", groups=[("c1.g", ["a"])])],
            [
                {"id": "k1", "alternatives": [["a"]]},
                {"id": "k2", "alternatives": [["a"]]},
                {"id": "k3", "alternatives": [["a"]]},
            ],
            weights={"a": 3},
        )
        self.assertEqual(result.selected_group_ids, ("c1.g",))
        self.assertEqual(result.total_weight, 3)
        self.assertEqual(result.group_count, 1)
        for provenance in result.conditions:
            self.assertEqual(provenance.groups, ("c1.g",))
            self.assertEqual(provenance.shared_groups, ("c1.g",))

    def test_repeat_does_not_multiply_exposure(self):
        # Two conditions each citing the bundle must cost 3, not 6.
        result = plan(
            [credential("c1", groups=[("c1.g", ["a", "b"])])],
            [
                {"id": "k1", "alternatives": [["a"]]},
                {"id": "k2", "alternatives": [["b"]]},
            ],
            weights={"a": 1, "b": 2},
        )
        self.assertEqual(result.total_weight, 3)


class JointVersusStandaloneTests(unittest.TestCase):
    def test_prompt_scenario_chooses_cheaper_joint_presentation(self):
        # Condition A alone is cheapest with c2.g1 (weight 1).
        # Condition B needs the address, only in the c1 bundle (weight 2).
        # Together: c1.g1 alone (weight 2) beats c2.g1 + c1.g1 (weight 3).
        spec = {
            "attribute_weights": {"name": 1, "address": 1},
            "credentials": [
                credential("c1", groups=[("c1.g1", ["name", "address"])]),
                credential("c2", groups=[("c2.g1", ["name"])]),
            ],
            "conditions": [
                {"id": "A", "alternatives": [["name"]]},
                {"id": "B", "alternatives": [["address"]]},
            ],
        }
        result = plan_presentation(spec)
        self.assertEqual(result.selected_group_ids, ("c1.g1",))
        self.assertEqual(result.total_weight, 2)

        by_id = {item.condition_id: item for item in result.conditions}
        self.assertEqual(by_id["A"].standalone_groups, ("c2.g1",))
        self.assertEqual(by_id["A"].standalone_weight, 1)
        self.assertFalse(by_id["A"].follows_standalone)
        self.assertEqual(by_id["A"].groups, ("c1.g1",))
        self.assertEqual(by_id["A"].extra_attributes, ("address",))
        self.assertIn("lower total cost", by_id["A"].reason)

    def test_joint_choice_reports_self_cheapest_form_for_each_condition(self):
        result = plan(
            [
                credential("c1", groups=[("c1.bundle", ["x", "y"])]),
                credential("c2", groups=[("c2.x", ["x"])]),
                credential("c3", groups=[("c3.y", ["y"])]),
            ],
            [
                {"id": "K1", "alternatives": [["x"]]},
                {"id": "K2", "alternatives": [["y"]]},
            ],
            weights={"x": 1, "y": 1},
        )
        # Standalone: c2.x + c3.y = weight 2, 2 credentials.
        # Joint alternative: c1.bundle = weight 2, 1 credential -> wins tie.
        self.assertEqual(result.selected_group_ids, ("c1.bundle",))
        self.assertEqual(result.credential_count, 1)
        by_id = {item.condition_id: item for item in result.conditions}
        self.assertEqual(
            by_id["K1"].standalone_groups, ("c2.x",)
        )
        self.assertEqual(by_id["K2"].standalone_groups, ("c3.y",))
        self.assertFalse(by_id["K1"].follows_standalone)
        self.assertFalse(by_id["K2"].follows_standalone)

    def test_standalone_optimum_kept_when_joint_is_worse(self):
        result = plan(
            [
                credential("c1", groups=[("c1.bundle", ["x", "y"])]),
                credential("c2", groups=[("c2.x", ["x"])]),
                credential("c3", groups=[("c3.y", ["y"])]),
            ],
            [
                {"id": "K1", "alternatives": [["x"]]},
                {"id": "K2", "alternatives": [["y"]]},
            ],
            weights={"x": 1, "y": 1},
        )
        self.assertEqual(result.selected_group_ids, ("c1.bundle",))

    def test_joint_beats_standalone_on_weight_not_just_credentials(self):
        # Bundle weighs 2 (x=0.5, y=0.5) vs separate 0.5 + 0.5 = 1.
        result = plan(
            [
                credential("c1", groups=[("c1.bundle", ["x", "y", "z"])]),
                credential("c2", groups=[("c2.x", ["x"])]),
                credential("c3", groups=[("c3.y", ["y"])]),
            ],
            [
                {"id": "K1", "alternatives": [["x"]]},
                {"id": "K2", "alternatives": [["y"]]},
            ],
            weights={"x": 1, "y": 1, "z": 100},
        )
        self.assertEqual(result.selected_group_ids, ("c2.x", "c3.y"))
        self.assertEqual(result.total_weight, 2)

    def test_extra_address_case(self):
        """A condition alone is cheapest on a bare credential, but sharing with
        another condition would disclose an extra address.  The planner must
        still pick the lower total cost and say so."""

        spec = {
            "attribute_weights": {"name": 1, "address": 5, "birthdate": 1},
            "credentials": [
                # cheapest form of `name` alone
                credential("c1", groups=[("c1.name", ["name"])]),
                # cheap `name` but bundled with the address
                credential("c2", groups=[("c2.bundle", ["name", "address"])]),
                # `birthdate` only comes bundled with `name`
                credential("c3", groups=[("c3.bundle", ["name", "birthdate"])]),
            ],
            "conditions": [
                {"id": "name", "alternatives": [["name"]]},
                {"id": "birthdate", "alternatives": [["birthdate"]]},
            ],
        }
        result = plan_presentation(spec)
        # Cheapest: c3.bundle alone exposes {name, birthdate} = weight 2 on one
        # credential, satisfying both conditions.  Involving c2 would add the
        # address (weight 5) and is never competitive.
        self.assertEqual(result.selected_group_ids, ("c3.bundle",))
        self.assertEqual(result.exposed_attributes, ("birthdate", "name"))
        self.assertEqual(result.total_weight, 2)

    def test_cheaper_joint_combination_chosen_over_per_condition_minimum(self):
        # Standalone minima: A alone -> a1 (weight 1); B alone -> b1 (weight 1).
        # But a1 and b1 sit on different credentials, and a shared bundle on a
        # single credential exposes {a, b} for weight 2 as well -- the tie is
        # broken by credential count, so the shared bundle wins.
        result = plan(
            [
                credential("c1", groups=[("c1.a", ["a"])]),
                credential("c2", groups=[("c2.b", ["b"])]),
                credential("c3", groups=[("c3.ab", ["a", "b"])]),
            ],
            [
                {"id": "A", "alternatives": [["a"]]},
                {"id": "B", "alternatives": [["b"]]},
            ],
            weights={"a": 1, "b": 1},
        )
        self.assertEqual(result.selected_group_ids, ("c3.ab",))
        self.assertEqual(result.credential_count, 1)
        by_id = {item.condition_id: item for item in result.conditions}
        self.assertEqual(by_id["A"].standalone_groups, ("c1.a",))
        self.assertEqual(by_id["B"].standalone_groups, ("c2.b",))
        self.assertFalse(by_id["A"].follows_standalone)
        self.assertFalse(by_id["B"].follows_standalone)

    def test_joint_avoids_extra_address_that_standalone_minimum_would_add(self):
        # `name` alone is cheapest on c1.g1.  `birthdate` is only on c3, which
        # also bundles name.  Selecting c1.g1 for name would *add* a group;
        # using c3.bundle for both is strictly cheaper, even though c1.g1 is
        # the cheapest way to satisfy `name` in isolation.
        result = plan(
            [
                credential("c1", groups=[("c1.g1", ["name"])]),
                credential(
                    "c3", groups=[("c3.bundle", ["name", "birthdate", "address"])]
                ),
            ],
            [
                {"id": "name", "alternatives": [["name"]]},
                {"id": "birthdate", "alternatives": [["birthdate"]]},
            ],
            weights={"name": 1, "birthdate": 1, "address": 4},
        )
        self.assertEqual(result.selected_group_ids, ("c3.bundle",))
        self.assertEqual(result.total_weight, 6)
        by_id = {item.condition_id: item for item in result.conditions}
        self.assertEqual(by_id["name"].standalone_groups, ("c1.g1",))
        self.assertEqual(by_id["name"].standalone_weight, 1)
        self.assertFalse(by_id["name"].follows_standalone)
        self.assertIn("address", by_id["birthdate"].extra_attributes)
        self.assertIn("lower total cost", by_id["name"].reason)


class IssuerTests(unittest.TestCase):
    def test_branch_issuer_restriction_is_enforced(self):
        result = plan(
            [
                credential("c1", issuer="gov", groups=[("c1.g", ["a"])]),
                credential("c2", issuer="bank", groups=[("c2.g", ["a"])]),
            ],
            [{"id": "k", "acceptable_issuers": ["gov"], "alternatives": [["a"]]}],
        )
        self.assertEqual(result.selected_group_ids, ("c1.g",))

    def test_alternative_issuer_restriction_is_enforced(self):
        result = plan(
            [
                credential("c1", issuer="gov", groups=[("c1.g", ["a"])]),
                credential("c2", issuer="bank", groups=[("c2.g", ["a"])]),
            ],
            [
                {
                    "id": "k",
                    "alternatives": [
                        {"attributes": ["a"], "acceptable_issuers": ["bank"]}
                    ],
                }
            ],
        )
        self.assertEqual(result.selected_group_ids, ("c2.g",))

    def test_branch_and_alternative_issuers_intersect(self):
        result = plan_presentation(
            {
                "credentials": [
                    credential("c1", issuer="gov", groups=[("c1.g", ["a"])]),
                ],
                "conditions": [
                    {
                        "id": "k",
                        "acceptable_issuers": ["gov"],
                        "alternatives": [
                            {"attributes": ["a"], "acceptable_issuers": ["bank"]}
                        ],
                    }
                ],
            }
        )
        self.assertFalse(result.satisfied)

    def test_multi_attribute_requirement_cannot_span_issuers(self):
        # `a` only from gov, `b` only from bank; one alternative needs both.
        result = plan_presentation(
            {
                "credentials": [
                    credential("c1", issuer="gov", groups=[("c1.a", ["a"])]),
                    credential("c2", issuer="bank", groups=[("c2.b", ["b"])]),
                ],
                "conditions": [
                    {
                        "id": "k",
                        "acceptable_issuers": ["gov"],
                        "alternatives": [["a", "b"]],
                    }
                ],
            }
        )
        self.assertFalse(result.satisfied)

    def test_unverified_credentials_are_ignored(self):
        result = plan(
            [
                credential("c1", groups=[("c1.g", ["a"])], verified=False),
                credential("c2", groups=[("c2.g", ["a"])]),
            ],
            [{"id": "k", "alternatives": [["a"]]}],
        )
        self.assertEqual(result.selected_group_ids, ("c2.g",))

    def test_alternative_fallback_when_first_is_unavailable(self):
        result = plan(
            [credential("c1", groups=[("c1.b", ["b"])])],
            [{"id": "k", "alternatives": [["a"], ["b"]]}],
        )
        self.assertEqual(result.selected_group_ids, ("c1.b",))
        self.assertEqual(result.conditions[0].alternative_index, 1)


class SameCredentialTests(unittest.TestCase):
    def test_attributes_must_come_from_one_credential(self):
        # `a` is on c1, `b` on c2; the condition needs both from one credential.
        result = plan_presentation(
            {
                "credentials": [
                    credential("c1", groups=[("c1.a", ["a"])]),
                    credential("c2", groups=[("c2.b", ["b"])]),
                    credential("c3", groups=[("c3.both", ["a", "b"])]),
                ],
                "conditions": [
                    {
                        "id": "k",
                        "alternatives": [["a", "b"]],
                        "same_credential": [["a", "b"]],
                    }
                ],
            }
        )
        self.assertEqual(result.selected_group_ids, ("c3.both",))

    def test_unsatisfiable_when_no_single_credential_holds_all(self):
        result = plan_presentation(
            {
                "credentials": [
                    credential("c1", groups=[("c1.a", ["a"])]),
                    credential("c2", groups=[("c2.b", ["b"])]),
                ],
                "conditions": [
                    {
                        "id": "k",
                        "alternatives": [["a", "b"]],
                        "same_credential": [["a", "b"]],
                    }
                ],
            }
        )
        self.assertFalse(result.satisfied)
        self.assertIn("same-credential", result.conditions[0].reason)

    def test_same_credential_applies_only_to_named_attributes(self):
        # Constraint covers a+b; c may come from another credential.
        result = plan(
            [
                credential("c1", groups=[("c1.ab", ["a", "b"])]),
                credential("c2", groups=[("c2.c", ["c"])]),
            ],
            [
                {
                    "id": "k",
                    "alternatives": [["a", "b", "c"]],
                    "same_credential": [["a", "b"]],
                }
            ],
        )
        self.assertEqual(result.selected_group_ids, ("c1.ab", "c2.c"))

    def test_group_id_form_of_same_credential(self):
        # The documented alternate spelling groups by credential id.
        result = plan_presentation(
            {
                "credentials": [
                    credential("c1", groups=[("c1.a", ["a"])]),
                    credential("c2", groups=[("c2.b", ["b"])]),
                ],
                "conditions": [
                    {
                        "id": "k",
                        "alternatives": [["a", "b"]],
                        "same_credential_groups": [["a", "b"]],
                    }
                ],
            }
        )
        self.assertFalse(result.satisfied)


class HolderTests(unittest.TestCase):
    def test_holders_are_never_mixed(self):
        spec = {
            "credentials": [
                credential("c1", holder="alice", groups=[("c1.a", ["a"])]),
                credential("c2", holder="bob", groups=[("c2.b", ["b"])]),
            ],
            "conditions": [
                {"id": "k1", "alternatives": [["a"]]},
                {"id": "k2", "alternatives": [["b"]]},
            ],
        }
        result = plan_presentation(spec)
        self.assertFalse(result.satisfied)
        self.assertIn("mixing holders", result.notes[0])

    def test_best_single_holder_is_chosen(self):
        # alice can use c1.a; bob has c2.a as well.  Equal weight, equal
        # credential count, so the credential number decides -- c1 (alice).
        spec = {
            "attribute_weights": {"a": 1},
            "credentials": [
                credential("c2", holder="bob", groups=[("c2.a", ["a"])]),
                credential("c3", holder="bob", groups=[("c3.b", ["b"])]),
                credential("c1", holder="alice", groups=[("c1.a", ["a"])]),
            ],
            "conditions": [{"id": "k", "alternatives": [["a"]]}],
        }
        result = plan_presentation(spec)
        self.assertTrue(result.satisfied)
        self.assertEqual(result.holder, "alice")
        self.assertEqual(result.selected_group_ids, ("c1.a",))

    def test_holder_with_fewer_credentials_wins_equal_weight(self):
        # alice needs two credentials for {a, b}; bob needs one.  Weight ties
        # at 2, so the smaller credential count decides for bob.
        spec = {
            "attribute_weights": {"a": 1, "b": 1},
            "credentials": [
                credential("c1", holder="alice", groups=[("c1.a", ["a"])]),
                credential("c2", holder="alice", groups=[("c2.b", ["b"])]),
                credential("c3", holder="bob", groups=[("c3.ab", ["a", "b"])]),
            ],
            "conditions": [
                {"id": "k1", "alternatives": [["a"]]},
                {"id": "k2", "alternatives": [["b"]]},
            ],
        }
        result = plan_presentation(spec)
        self.assertEqual(result.holder, "bob")
        self.assertEqual(result.selected_group_ids, ("c3.ab",))

    def test_all_groups_in_a_plan_share_one_holder(self):
        # bob cannot cover `b`, so alice's two-credential plan is the only one.
        spec = {
            "attribute_weights": {"a": 1, "b": 5},
            "credentials": [
                credential("c1", holder="alice", groups=[("c1.a", ["a"])]),
                credential("c2", holder="alice", groups=[("c2.b", ["b"])]),
                credential("c3", holder="bob", groups=[("c3.ab", ["a"])]),
            ],
            "conditions": [
                {"id": "k1", "alternatives": [["a"]]},
                {"id": "k2", "alternatives": [["b"]]},
            ],
        }
        result = plan_presentation(spec)
        self.assertEqual(result.holder, "alice")
        self.assertEqual({item.holder for item in result.selected}, {"alice"})


class ProvenanceTests(unittest.TestCase):
    def test_provenance_names_groups_and_requirements(self):
        result = plan(
            [credential("c1", groups=[("c1.g1", ["a", "b"])])],
            [{"id": "k", "alternatives": [["a"], ["b"]]}],
        )
        provenance = result.conditions[0]
        self.assertEqual(provenance.condition_id, "k")
        self.assertTrue(provenance.satisfied)
        self.assertEqual(provenance.required_attributes, ("a",))
        self.assertEqual(provenance.alternative_index, 0)
        self.assertEqual(provenance.groups, ("c1.g1",))
        self.assertEqual(provenance.disclosed_attributes, ("a", "b"))
        self.assertEqual(provenance.extra_attributes, ("b",))
        self.assertTrue(provenance.follows_standalone)

    def test_json_round_trip_is_structured(self):
        result = plan(
            [credential("c1", groups=[("c1.g1", ["a"])])],
            [{"id": "k", "alternatives": [["a"]]}],
        )
        decoded = json.loads(result.to_json())
        self.assertTrue(decoded["satisfied"])
        self.assertEqual(decoded["holder"], "alice")
        self.assertEqual(decoded["total_weight"], "1")
        self.assertEqual(decoded["selected"][0]["group_id"], "c1.g1")
        self.assertEqual(decoded["conditions"][0]["groups"], ["c1.g1"])

    def test_fractional_weights_stay_exact(self):
        result = plan(
            [credential("c1", groups=[("c1.g", ["a"]), ("c1.h", ["b"])])],
            [{"id": "k", "alternatives": [["a", "b"]]}],
            weights={"a": 0.1, "b": 0.2},
        )
        self.assertEqual(result.total_weight, Fraction(3, 10))


class UnsatisfiableTests(unittest.TestCase):
    def test_missing_attribute_is_reported(self):
        result = plan_presentation(
            {
                "credentials": [credential("c1", groups=[("c1.g", ["a"])])],
                "conditions": [{"id": "k", "alternatives": [["zzz"]]}],
            }
        )
        self.assertFalse(result.satisfied)
        self.assertEqual(result.unmet_conditions, ("k",))
        self.assertIn("not disclosed", result.conditions[0].reason)

    def test_require_plan_raises(self):
        with self.assertRaises(UnsatisfiableError) as caught:
            require_plan(
                {
                    "credentials": [credential("c1", groups=[("c1.g", ["a"])])],
                    "conditions": [{"id": "k", "alternatives": [["zzz"]]}],
                }
            )
        self.assertFalse(caught.exception.plan.satisfied)

    def test_empty_condition_list_returns_empty_plan(self):
        result = plan_presentation({"credentials": [], "conditions": []})
        self.assertTrue(result.satisfied)
        self.assertEqual(result.selected, ())
        self.assertEqual(result.total_weight, 0)

    def test_empty_alternative_requires_nothing(self):
        result = plan(
            [credential("c1", groups=[("c1.g", ["a"])])],
            [{"id": "k", "alternatives": [[]]}],
        )
        self.assertTrue(result.satisfied)
        self.assertEqual(result.selected, ())


class ValidationTests(unittest.TestCase):
    def test_rejects_more_than_sixteen_groups(self):
        credentials = [
            credential("c%d" % i, groups=[("g%d" % i, ["a"])]) for i in range(17)
        ]
        with self.assertRaises(SpecError) as caught:
            parse_spec({"credentials": credentials, "conditions": []})
        self.assertIn(str(MAX_DISCLOSURE_GROUPS), str(caught.exception))

    def test_accepts_exactly_sixteen_groups(self):
        credentials = [
            credential("c%d" % i, groups=[("g%d" % i, ["a"])]) for i in range(16)
        ]
        spec = parse_spec({"credentials": credentials, "conditions": []})
        self.assertEqual(len(spec.credentials), 16)

    def test_rejects_duplicate_group_ids(self):
        with self.assertRaises(SpecError):
            parse_spec(
                {
                    "credentials": [
                        credential("c1", groups=[("dup", ["a"])]),
                        credential("c2", groups=[("dup", ["b"])]),
                    ],
                    "conditions": [],
                }
            )

    def test_rejects_negative_weight(self):
        with self.assertRaises(SpecError):
            parse_spec(
                {"credentials": [], "conditions": [], "attribute_weights": {"a": -1}}
            )

    def test_rejects_boolean_weight(self):
        with self.assertRaises(SpecError):
            parse_spec(
                {"credentials": [], "conditions": [], "attribute_weights": {"a": True}}
            )

    def test_rejects_ambiguous_conditions_and_branches(self):
        with self.assertRaises(SpecError):
            parse_spec({"credentials": [], "conditions": [], "branches": []})

    def test_rejects_empty_alternatives(self):
        with self.assertRaises(SpecError):
            parse_spec(
                {
                    "credentials": [credential("c1", groups=[("c1.g", ["a"])])],
                    "conditions": [{"id": "k", "alternatives": []}],
                }
            )


class AliasTests(unittest.TestCase):
    def test_plan_from_dict_alias(self):
        result = plan_from_dict(
            {
                "credentials": [credential("c1", groups=[("c1.g", ["a"])])],
                "conditions": [{"id": "k", "alternatives": [["a"]]}],
            }
        )
        self.assertTrue(result.satisfied)

    def test_branches_alias(self):
        result = plan_presentation(
            {
                "credentials": [credential("c1", groups=[("c1.g", ["a"])])],
                "branches": [{"id": "k", "alternatives": [["a"]]}],
            }
        )
        self.assertTrue(result.satisfied)

    def test_flat_alternative_shorthand(self):
        result = plan(
            [credential("c1", groups=[("c1.g", ["a"])])],
            [{"id": "k", "alternatives": ["a"]}],
        )
        self.assertTrue(result.satisfied)
        self.assertEqual(result.conditions[0].required_attributes, ("a",))


if __name__ == "__main__":
    unittest.main()
