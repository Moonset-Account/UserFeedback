"""Tests for scq_compiler.  Run from the workspace root:

    python3 -m unittest discover -s tests -v
"""

import unittest

from scq_compiler import (
    Atom, Const, Counterexample, FrozenVar, Query, UnsafeQueryError, Var,
    canonical_form, check_safe, compute_mcds, contains, equivalent, evaluate,
    expand, find_rewritings, format_instance, format_query, parse_query,
    parse_views, verify_candidate,
)


# Reusable scenario: 2-path view over base relation R.
VIEWS_2PATH = parse_views("""
    V(A, C) :- R(A, B), R(B, C)
""")
# 4-path query, answerable with two references to V.
QUERY_4PATH = parse_query("Q(X, Z) :- R(X, P), R(P, Q), R(Q, R), R(R, Z)")


class TestSafetyAndParsing(unittest.TestCase):
    def test_output_variable_must_appear_in_body(self):
        with self.assertRaises(UnsafeQueryError):
            check_safe(parse_query("Q(X, Z) :- R(X, Y)"))

    def test_empty_body_rejected(self):
        with self.assertRaises(UnsafeQueryError):
            check_safe(Query((Var("X"),), (), "Q"))

    def test_parser_roundtrip_with_constants(self):
        q = parse_query("Q(X, _y) :- R(X, 'usa', 7), S(_y, X, foo)")
        self.assertEqual(q.name, "Q")
        self.assertEqual(q.head, (Var("X"), Var("_y")))
        r, s = q.body
        self.assertEqual(r, Atom("R", (Var("X"), Const("usa"), Const(7))))
        self.assertEqual(s, Atom("S", (Var("_y"), Var("X"), Const("foo"))))
        # format_query produces a parseable string with the same meaning.
        q2 = parse_query(format_query(q))
        self.assertTrue(equivalent(q, q2))

    def test_parse_views_and_duplicate_detection(self):
        views = parse_views("A(X, Y) :- R(X, Y)\n\n# comment\nB(X) :- R(X, Z)")
        self.assertEqual(set(views), {"A", "B"})
        with self.assertRaises(ValueError):
            parse_views("A(X) :- R(X)\nA(Y) :- R(Y)")


class TestContainmentAndEquivalence(unittest.TestCase):
    def test_strict_containment(self):
        qa = parse_query("Q(X) :- R(X, Y)")
        qb = parse_query("Q(X) :- R(X, X)")
        self.assertTrue(contains(qb, qa))    # qb is more restrictive
        self.assertFalse(contains(qa, qb))
        self.assertFalse(equivalent(qa, qb))

    def test_redundant_atom_is_equivalent(self):
        qa = parse_query("Q(X) :- R(X, Y)")
        qb = parse_query("Q(X) :- R(X, Y), R(X, Z)")
        self.assertTrue(equivalent(qa, qb))

    def test_variable_names_do_not_matter(self):
        qa = parse_query("Q(X) :- R(X, Y), S(Y)")
        qb = parse_query("Q(A) :- R(A, B), S(B)")
        self.assertTrue(equivalent(qa, qb))

    def test_constants_must_match(self):
        qa = parse_query("Q(X) :- R(X, 'a')")
        qb = parse_query("Q(X) :- R(X, 'b')")
        self.assertFalse(contains(qa, qb))
        self.assertFalse(contains(qb, qa))

    def test_arity_mismatch_not_contained(self):
        qa = parse_query("Q(X) :- R(X)")
        qb = parse_query("Q(X, Y) :- R(X, Y)")
        self.assertFalse(contains(qa, qb))
        self.assertFalse(contains(qb, qa))


class TestEvaluation(unittest.TestCase):
    def test_set_semantics_collapse_duplicates(self):
        q = parse_query("Q(X) :- R(X, Y)")
        inst = {"R": [("a", 1), ("a", 1), ("b", 2), ("a", 1)]}
        self.assertEqual(evaluate(q, inst), {("a",), ("b",)})

    def test_join_and_constants(self):
        q = parse_query("Q(X, Z) :- R(X, 'k', Y), S(Y, Z)")
        inst = {"R": [("a", "k", 1), ("b", "no", 2)], "S": [(1, "z1"), (2, "z2")]}
        self.assertEqual(evaluate(q, inst), {("a", "z1")})


class TestExpansionAndRenaming(unittest.TestCase):
    def test_internal_vars_renamed_per_reference(self):
        cand = parse_query("Q(X, Z) :- V(X, W), V(W, Z)")
        exp = expand(cand, VIEWS_2PATH)
        # Four distinct base atoms, internal columns of the two references
        # must be distinct fresh variables.
        self.assertEqual(len(exp.body), 4)
        internal = {t.name for a in exp.body for t in a.args
                    if isinstance(t, Var) and t.name.startswith("__V_")}
        self.assertEqual(len(internal), 2)
        self.assertTrue(equivalent(exp, QUERY_4PATH))

    def test_buggy_shared_internal_names_collapse(self):
        cand = parse_query("Q(X, Z) :- V(X, W), V(W, Z)")
        buggy = expand(cand, VIEWS_2PATH, alpha_rename=False)
        # Shared internal variable B ties the two references together.
        self.assertEqual(len(buggy.body), 4)
        self.assertFalse(equivalent(buggy, QUERY_4PATH))

    def test_unknown_view_and_arity_mismatch(self):
        with self.assertRaises(ValueError):
            expand(parse_query("Q(X) :- Nope(X)"), VIEWS_2PATH)
        with self.assertRaises(ValueError):
            expand(parse_query("Q(X) :- V(X)"), VIEWS_2PATH)


class TestFindRewritings(unittest.TestCase):
    def test_minimal_two_reference_rewriting(self):
        result = find_rewritings(QUERY_4PATH, VIEWS_2PATH)
        self.assertTrue(result)
        self.assertEqual(result.num_refs, 2)
        best = result.best
        self.assertEqual([a.relation for a in best.body], ["V", "V"])
        # The middle column is shared, endpoints are the query outputs.
        for rw in result.rewritings:
            v = verify_candidate(QUERY_4PATH, VIEWS_2PATH, rw)
            self.assertTrue(v.equivalent, format_query(rw))

    def test_budget_is_respected(self):
        self.assertFalse(find_rewritings(QUERY_4PATH, VIEWS_2PATH, budget=1))
        q8 = parse_query(
            "Q(X, Z) :- R(X, A), R(A, B), R(B, C), R(C, D), "
            "R(D, E), R(E, F), R(F, G), R(G, Z)")
        self.assertFalse(find_rewritings(q8, VIEWS_2PATH, budget=3))
        result = find_rewritings(q8, VIEWS_2PATH, budget=4)
        self.assertTrue(result)
        self.assertEqual(result.num_refs, 4)
        self.assertTrue(
            verify_candidate(q8, VIEWS_2PATH, result.best).equivalent)

    def test_no_rewriting_when_relation_not_covered(self):
        q = parse_query("Q(X) :- Missing(X, Y)")
        self.assertFalse(find_rewritings(q, VIEWS_2PATH))

    def test_self_join_and_repeated_view(self):
        views = parse_views("V(A, B) :- R(A, B)")
        q = parse_query("Q(X) :- R(X, Y), R(Y, X)")
        result = find_rewritings(q, views)
        self.assertTrue(result)
        self.assertEqual(result.num_refs, 2)
        self.assertTrue(verify_candidate(q, views, result.best).equivalent)

    def test_constants_in_views_and_queries(self):
        views = parse_views("V(A, B) :- R(A, 'usa', B)")
        q = parse_query("Q(Y) :- R('ann', 'usa', Y)")
        result = find_rewritings(q, views)
        self.assertTrue(result)
        best = result.best
        self.assertEqual(best.body[0].args[0], Const("ann"))
        self.assertTrue(verify_candidate(q, views, best).equivalent)
        # A candidate binding the wrong constant must fail verification.
        wrong = parse_query("Q(Y) :- V('bob', Y)")
        v = verify_candidate(q, views, wrong)
        self.assertFalse(v.equivalent)
        self.assertFalse(v.query_contained_in_candidate)
        self.assertFalse(v.candidate_contained_in_query)
        self.assertEqual(len(v.counterexamples), 2)

    def test_lexicographic_tie_break(self):
        views = parse_views("""
            B(X, Y) :- R(X, Y)
            A(X, Y) :- R(X, Y)
        """)
        q = parse_query("Q(X, Y) :- R(X, Y)")
        result = find_rewritings(q, views)
        self.assertEqual(len(result.rewritings), 2)
        self.assertEqual(result.best.body[0].relation, "A")
        forms = [canonical_form(rw) for rw in result.rewritings]
        self.assertEqual(forms, sorted(forms))

    def test_canonical_form_invariant_to_names_and_order(self):
        qa = parse_query("Q(X) :- R(X, Y), S(Y, X)")
        qb = parse_query("Q(A) :- S(B, A), R(A, B)")
        self.assertEqual(canonical_form(qa), canonical_form(qb))


class TestVerifyCandidate(unittest.TestCase):
    def test_equivalent_candidate_has_no_counterexample(self):
        cand = parse_query("Q(X, Z) :- V(X, W), V(W, Z)")
        v = verify_candidate(QUERY_4PATH, VIEWS_2PATH, cand)
        self.assertTrue(v.equivalent)
        self.assertEqual(v.counterexamples, ())

    def test_candidate_too_loose_yields_witness(self):
        # W projects a single R edge; candidate Q(X) :- W(X, Y) is R(X, Y),
        # strictly more general than the mutual-edge query.
        views = parse_views("W(A, B) :- R(A, B)")
        q = parse_query("Q(X) :- R(X, Y), R(Y, X)")
        cand = parse_query("Q(X) :- W(X, Y)")
        v = verify_candidate(q, views, cand)
        self.assertFalse(v.equivalent)
        self.assertTrue(v.query_contained_in_candidate)
        self.assertFalse(v.candidate_contained_in_query)
        self.assertEqual(len(v.counterexamples), 1)
        cex = v.counterexamples[0]
        self.assertEqual(cex.direction, "candidate_not_subset_of_query")
        # The witness tuple really is in the candidate's result and not in
        # the query's result on the produced instance.
        self.assertIn(cex.witness_tuple, evaluate(v.expansion, cex.instance))
        self.assertNotIn(cex.witness_tuple, evaluate(q, cex.instance))

    def test_candidate_too_strict_yields_witness(self):
        # Candidate adds a 3-cycle constraint the query does not have.
        views = parse_views("V3(A, B) :- R(A, C), R(C, B), R(B, A)")
        q = parse_query("Q(X, Z) :- R(X, Y), R(Y, Z)")
        cand = parse_query("Q(X, Z) :- V3(X, Z)")
        v = verify_candidate(q, views, cand)
        self.assertFalse(v.equivalent)
        self.assertFalse(v.query_contained_in_candidate)
        self.assertTrue(v.candidate_contained_in_query)
        cex = v.counterexamples[0]
        self.assertEqual(cex.direction, "query_not_subset_of_candidate")
        self.assertIn(cex.witness_tuple, evaluate(q, cex.instance))
        self.assertNotIn(cex.witness_tuple, evaluate(v.expansion, cex.instance))

    def test_witness_instance_is_small(self):
        cand = parse_query("Q(X, Z) :- V(X, W), V(W, Z)")
        v = verify_candidate(QUERY_4PATH, VIEWS_2PATH, cand, alpha_rename=False)
        self.assertFalse(v.equivalent)
        cex = v.counterexamples[0]
        total_facts = sum(len(tups) for tups in cex.instance.values())
        self.assertLessEqual(total_facts, 4)  # frozen 4-path: 4 base tuples


class TestInternalColumnPitfall(unittest.TestCase):
    """The projected-away internal column of V is called B in both references.
    Forgetting to alpha-rename equates them; positive instances can still
    agree, so the counterexample entry must expose the lost tuple."""

    def setUp(self):
        self.cand = parse_query("Q(X, Z) :- V(X, W), V(W, Z)")
        self.correct = expand(self.cand, VIEWS_2PATH)
        self.buggy = expand(self.cand, VIEWS_2PATH, alpha_rename=False)

    def test_positive_sample_passes_even_with_the_bug(self):
        # A "there and back" instance: the shared-B constraint happens to
        # hold, so buggy and correct expansions agree with the query.
        inst = {"R": [(1, 2), (2, 3), (3, 2), (2, 4)]}
        expected = evaluate(QUERY_4PATH, inst)
        self.assertEqual(expected, {(1, 3), (1, 4), (2, 2), (3, 3), (3, 4)})
        self.assertEqual(evaluate(self.correct, inst), expected)
        self.assertEqual(evaluate(self.buggy, inst), expected)

    def test_counterexample_entry_finds_the_lost_tuple(self):
        v = verify_candidate(QUERY_4PATH, VIEWS_2PATH, self.cand,
                             alpha_rename=False)
        self.assertFalse(v.equivalent)
        self.assertFalse(v.query_contained_in_candidate)
        self.assertTrue(v.candidate_contained_in_query)
        cex = v.counterexamples[0]
        self.assertEqual(cex.direction, "query_not_subset_of_candidate")
        # The lost tuple: present in the query's (and correct expansion's)
        # result, dropped by the buggy expansion, on the witness instance.
        lost = cex.witness_tuple
        self.assertIn(lost, evaluate(QUERY_4PATH, cex.instance))
        self.assertIn(lost, evaluate(self.correct, cex.instance))
        self.assertNotIn(lost, evaluate(self.buggy, cex.instance))
        # The witness is the frozen 4-path with all-distinct internal nodes,
        # which the shared-B constraint cannot satisfy.
        self.assertEqual(len(cex.instance["R"]), 4)
        self.assertEqual(evaluate(self.buggy, cex.instance), set())

    def test_correct_expansion_is_verified_equivalent(self):
        v = verify_candidate(QUERY_4PATH, VIEWS_2PATH, self.cand)
        self.assertTrue(v.equivalent)


class TestMCDs(unittest.TestCase):
    def test_mcds_cover_each_matching_position(self):
        mcds = compute_mcds(QUERY_4PATH, VIEWS_2PATH)
        self.assertEqual(len(mcds), 3)  # 2-path can start at atom 0, 1 or 2
        for m in mcds:
            self.assertEqual(m.view, "V")
            self.assertEqual(len(m.covered), 2)
        heads = [m.head_terms for m in mcds]
        self.assertIn((Var("X"), Var("Q")), heads)
        self.assertIn((Var("Q"), Var("Z")), heads)


if __name__ == "__main__":
    unittest.main()
