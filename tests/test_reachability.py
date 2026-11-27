"""可达性分析与引用链构造的单元测试。"""

import unittest

from sigcheck.inspector import Hop, reachable_chains
from sigcheck.model import BODY, FIELD

from tests.builders import DocBuilder, standard_revisions


def diamond_builder() -> DocBuilder:
    #        P
    #      /   \\
    #     A     B (field)
    #      \\   /
    #        C -> D
    builder = DocBuilder(standard_revisions(4))
    (
        builder.set_page_roots("P")
        .add_object("P", "r0", "p")
        .add_object("A", "r0", "a")
        .add_object("B", "r0", "b")
        .add_object("C", "r0", "c")
        .add_object("D", "r0", "d")
        .add_body_edge("P", "A", "r0")
        .add_field_edge("P", "B", "fbox", "r0")
        .add_body_edge("A", "C", "r0")
        .add_body_edge("B", "C", "r0")
        .add_body_edge("C", "D", "r0")
    )
    return builder


class ReachabilityTest(unittest.TestCase):
    def test_body_mode_excludes_field_edges(self) -> None:
        doc = diamond_builder().build()
        chains = reachable_chains(doc, 0, ("P",), (BODY,))
        self.assertEqual(set(chains), {"P", "A", "C", "D"})
        self.assertNotIn("B", chains)

    def test_full_mode_includes_field_edges(self) -> None:
        doc = diamond_builder().build()
        chains = reachable_chains(doc, 0, ("P",), (BODY, FIELD))
        self.assertEqual(set(chains), {"P", "A", "B", "C", "D"})
        # B 只经 field 边可达，链上保留 field 类型与字段名。
        self.assertEqual(
            chains["B"],
            (Hop("P", "B", FIELD, field_name="fbox"),),
        )

    def test_chain_for_diamond_target_prefers_shortest_path(self) -> None:
        doc = diamond_builder().build()
        chains = reachable_chains(doc, 0, ("P",), (BODY, FIELD))
        # BFS：C 经由 A 先到达（邻接按 target 排序 A 在 B 前）。
        chain_ids = [(hop.source_id, hop.target_id) for hop in chains["C"]]
        self.assertEqual(chain_ids, [("P", "A"), ("A", "C")])
        self.assertEqual(
            [(hop.source_id, hop.target_id) for hop in chains["D"]],
            [("P", "A"), ("A", "C"), ("C", "D")],
        )

    def test_root_itself_has_empty_chain(self) -> None:
        doc = diamond_builder().build()
        chains = reachable_chains(doc, 0, ("P",), (BODY,))
        self.assertEqual(chains["P"], ())

    def test_excluded_objects_are_unreachable(self) -> None:
        doc = diamond_builder().build()
        chains = reachable_chains(
            doc, 0, ("P",), (BODY, FIELD), excluded={"A"}
        )
        self.assertNotIn("A", chains)
        # A 被排除后，C 仍可经 B（full 模式）到达。
        self.assertIn("C", chains)
        self.assertEqual(
            [(hop.source_id, hop.target_id) for hop in chains["C"]],
            [("P", "B"), ("B", "C")],
        )

    def test_edges_removed_later_disappear_from_reachability(self) -> None:
        builder = (
            DocBuilder(standard_revisions(3))
            .set_page_roots("P")
            .add_object("P", "r0", "p")
            .add_object("C", "r0", "c")
            .add_body_edge("P", "C", "r0", removed_in="r2")
        )
        doc = builder.build()
        self.assertIn("C", reachable_chains(doc, 1, ("P",), (BODY,)))
        self.assertNotIn("C", reachable_chains(doc, 2, ("P",), (BODY,)))

    def test_multiple_roots_are_all_traversed(self) -> None:
        builder = (
            DocBuilder(standard_revisions(2))
            .set_page_roots("P1", "P2")
            .add_object("P1", "r0", "p1")
            .add_object("P2", "r0", "p2")
            .add_object("X", "r0", "x")
            .add_body_edge("P2", "X", "r0")
        )
        doc = builder.build()
        chains = reachable_chains(doc, 0, ("P1", "P2"), (BODY,))
        self.assertEqual(set(chains), {"P1", "P2", "X"})
        self.assertEqual(
            [(h.source_id, h.target_id) for h in chains["X"]],
            [("P2", "X")],
        )

    def test_dangling_edge_target_missing_is_ignored(self) -> None:
        builder = (
            DocBuilder(standard_revisions(2))
            .set_page_roots("P")
            .add_object("P", "r0", "p")
            .add_body_edge("P", "GHOST", "r0")
        )
        doc = builder.build()
        chains = reachable_chains(doc, 0, ("P",), (BODY,))
        self.assertEqual(set(chains), {"P"})


if __name__ == "__main__":
    unittest.main()
