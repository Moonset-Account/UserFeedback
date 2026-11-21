"""Acceptance tests for input parsing and validation.

Every raised error is paired with the exact structural rule it protects,
so a regression that silently accepts bad input fails loudly.
"""

from __future__ import annotations

import unittest

from joinplan import InputError, MAX_TABLES, loads_query, parse_query


def base_request() -> dict:
    return {
        "nodes": 4,
        "tables": [
            {"name": "A", "bytes": 1000, "partition_key": "k"},
            {"name": "B", "bytes": 100},
        ],
        "edges": [{"left": "A.k", "right": "B.k"}],
        "outputs": [{"tables": ["A", "B"], "bytes": 4000}],
    }


def _chain_subsets(names):
    """Every contiguous run of >= 2 names in a chain of edges."""
    names = list(names)
    for start in range(len(names)):
        for end in range(start + 2, len(names) + 1):
            yield names[start:end]


class AcceptsValidInputTests(unittest.TestCase):
    def test_minimal_two_table_request_is_accepted(self):
        query = parse_query(base_request())
        self.assertEqual(query.nodes, 4)
        self.assertEqual(query.table("A").nbytes, 1000)
        self.assertEqual(query.table("A").partition_key, "k")
        self.assertIsNone(query.table("B").partition_key)
        self.assertEqual(query.result_bytes(frozenset({"A", "B"})), 4000)
        self.assertIsNone(query.broadcast_limit)

    def test_broadcast_limit_of_zero_is_valid_and_meaningful(self):
        request = base_request()
        request["broadcast_limit"] = 0
        query = parse_query(request)
        self.assertEqual(query.broadcast_limit, 0)

    def test_seven_tables_is_the_documented_maximum(self):
        request = {
            "nodes": 2,
            "tables": [{"name": f"T{index}", "bytes": 10} for index in range(MAX_TABLES)],
            "edges": [
                {"left": f"T{index}.k", "right": f"T{index + 1}.k"}
                for index in range(MAX_TABLES - 1)
            ],
            "outputs": [
                {"tables": sorted(names), "bytes": len(names) * 10}
                for names in _chain_subsets(f"T{index}" for index in range(MAX_TABLES))
            ],
        }
        query = parse_query(request)
        self.assertEqual(len(query.tables), MAX_TABLES)

    def test_outputs_are_stored_in_canonical_order(self):
        request = base_request()
        request["outputs"] = [{"tables": ["B", "A"], "bytes": 7}]
        query = parse_query(request)
        self.assertEqual(query.result_bytes(frozenset({"A", "B"})), 7)


class RejectsInvalidInputTests(unittest.TestCase):
    def assert_rejected(self, request, fragment):
        with self.assertRaises(InputError) as caught:
            parse_query(request)
        self.assertIn(fragment, str(caught.exception))

    def test_eight_tables_are_rejected(self):
        request = {
            "nodes": 2,
            "tables": [{"name": f"T{index}", "bytes": 10} for index in range(MAX_TABLES + 1)],
            "edges": [
                {"left": f"T{index}.k", "right": f"T{index + 1}.k"}
                for index in range(MAX_TABLES)
            ],
            "outputs": [
                {"tables": [f"T{index}", f"T{index + 1}"], "bytes": 20}
                for index in range(MAX_TABLES)
            ],
        }
        self.assert_rejected(request, "at most 7 tables")

    def test_unknown_top_level_field_is_rejected(self):
        request = base_request()
        request["strategy"] = "greedy"
        self.assert_rejected(request, "unknown input field")

    def test_negative_table_size_is_rejected(self):
        request = base_request()
        request["tables"][0]["bytes"] = -1
        self.assert_rejected(request, "bytes must be >= 0")

    def test_boolean_size_is_rejected_as_non_integer(self):
        request = base_request()
        request["tables"][0]["bytes"] = True
        self.assert_rejected(request, "must be an integer")

    def test_duplicate_table_names_are_rejected(self):
        request = base_request()
        request["tables"][1]["name"] = "A"
        request["edges"] = []
        request["outputs"] = []
        self.assert_rejected(request, "duplicate table name")

    def test_self_join_edge_is_rejected(self):
        request = base_request()
        request["edges"] = [{"left": "A.k", "right": "A.j"}]
        request["outputs"] = []
        self.assert_rejected(request, "self-join")

    def test_edge_referencing_unknown_table_is_rejected(self):
        request = base_request()
        request["edges"] = [{"left": "A.k", "right": "Z.k"}]
        self.assert_rejected(request, "unknown table")

    def test_duplicate_join_edge_is_rejected(self):
        request = base_request()
        request["edges"] = [
            {"left": "A.k", "right": "B.k"},
            {"left": "B.k", "right": "A.k"},
        ]
        self.assert_rejected(request, "duplicate join edge")

    def test_disconnected_graph_is_rejected(self):
        request = base_request()
        request["tables"].append({"name": "C", "bytes": 5})
        self.assert_rejected(request, "disconnected")

    def test_missing_output_estimate_is_rejected(self):
        request = base_request()
        request["outputs"] = []
        self.assert_rejected(request, "missing output byte estimate")

    def test_output_estimate_for_non_joinable_set_is_rejected(self):
        request = base_request()
        request["tables"].append({"name": "C", "bytes": 5})
        request["edges"] = [
            {"left": "A.k", "right": "B.k"},
            {"left": "A.k", "right": "C.k"},
        ]
        request["outputs"] = [
            {"tables": ["A", "B"], "bytes": 10},
            {"tables": ["A", "C"], "bytes": 10},
            {"tables": ["A", "B", "C"], "bytes": 10},
            # B and C share no edge and are not connected without A.
            {"tables": ["B", "C"], "bytes": 10},
        ]
        self.assert_rejected(request, "non-joinable table set")

    def test_duplicate_output_estimate_is_rejected(self):
        request = base_request()
        request["outputs"] = [
            {"tables": ["A", "B"], "bytes": 1},
            {"tables": ["B", "A"], "bytes": 2},
        ]
        self.assert_rejected(request, "duplicate output estimate")

    def test_non_object_top_level_is_rejected(self):
        with self.assertRaises(InputError):
            parse_query([1, 2, 3])

    def test_invalid_json_text_is_rejected(self):
        with self.assertRaises(InputError):
            loads_query("{not json}")

    def test_float_byte_estimate_is_rejected(self):
        request = base_request()
        request["outputs"][0]["bytes"] = 1000.5
        self.assert_rejected(request, "must be an integer")


if __name__ == "__main__":
    unittest.main()
