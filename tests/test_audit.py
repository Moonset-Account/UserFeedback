"""ChainAuditor 主流程测试：拆分/混样、迟到证据、来源归因与矛盾。"""

from __future__ import annotations

import unittest
from fractions import Fraction

from chain_audit import (
    ANON,
    BlindGenerationDoc,
    ChainAuditor,
    DisclosedLinkDoc,
    InputError,
    IntakeDoc,
    MeasurementDoc,
    MixDecl,
    SplitDecl,
)


def shares_by_id(attribution):
    return {s.source_id: s for s in attribution.shares}


class BasicFlowTests(unittest.TestCase):
    def test_split_unique_source_and_conservation(self):
        auditor = ChainAuditor()
        report = auditor.ingest(
            IntakeDoc("I1", "B1", "S1", 100, 100),
            SplitDecl("P1", "B1", "S1", ("T1", "T2")),
            MeasurementDoc("M1", "B1", "T1", 40, 60),
            MeasurementDoc("M2", "B1", "T2", 40, 60),
        )
        self.assertEqual(report.status, "satisfiable")
        self.assertEqual(auditor.node_interval("S1"), (100, 100))
        self.assertEqual(auditor.node_interval("T1"), (40, 60))
        self.assertEqual(auditor.node_interval("T2"), (40, 60))

        attr = auditor.sources_of("T1")
        self.assertTrue(attr.unique)
        self.assertEqual(attr.source_ids, ("S1",))
        share = attr.shares[0]
        self.assertEqual((share.amount_lo, share.amount_hi), (40, 60))
        self.assertEqual(share.fraction_lo, Fraction(1))
        self.assertEqual(share.fraction_hi, Fraction(1))
        self.assertTrue(share.exact)

        witness = auditor.feasible_witness()
        self.assertIsNotNone(witness)

    def test_split_measurements_propagate_to_each_other(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S1", 100, 100),
            SplitDecl("P1", "B1", "S1", ("T1", "T2")),
            MeasurementDoc("M1", "B1", "T1", 55, 70),
            MeasurementDoc("M2", "B1", "T2", 40, 60),
        )
        # T1+T2=100：T1 最大不能超过 100-40=60；T2 最小不能低于 100-70=30
        self.assertEqual(auditor.node_interval("T1"), (55, 60))
        self.assertEqual(auditor.node_interval("T2"), (40, 45))

    def test_point_intervals_and_chain_of_custody(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S1", 120, 120),
            SplitDecl("P1", "B1", "S1", ("T", "R")),
            MeasurementDoc("MT", "B1", "T", 70, 70),
            MeasurementDoc("MR", "B1", "R", 50, 50),
            SplitDecl("P2", "B1", "T", ("U1", "U2")),
            MeasurementDoc("MU1", "B1", "U1", 30, 30),
            MeasurementDoc("MU2", "B1", "U2", 40, 40),
        )
        self.assertEqual(auditor.status, "satisfiable")
        attr = auditor.sources_of("U1")
        self.assertTrue(attr.unique)
        self.assertEqual(attr.source_ids, ("S1",))
        share = shares_by_id(attr)["S1"]
        self.assertEqual((share.amount_lo, share.amount_hi), (30, 30))
        self.assertEqual(share.fraction_lo, Fraction(1))

    def test_mix_declaration_two_sources(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S1", 50, 50),
            IntakeDoc("I2", "B1", "S2", 50, 50),
            MixDecl("X1", "B1", ("S1", "S2"), "M"),
            MeasurementDoc("MM", "B1", "M", 100, 100),
        )
        attr = auditor.sources_of("M")
        self.assertFalse(attr.unique)
        ids = set(attr.source_ids)
        self.assertEqual(ids, {"S1", "S2"})
        # 两条入边都被混样声明固定为守恒的唯一路径，每个来源恰好 50 mg
        for share in attr.shares:
            self.assertEqual((share.amount_lo, share.amount_hi), (50, 50))
            self.assertTrue(share.exact)
            self.assertEqual(share.fraction_lo, Fraction(1, 2))
            self.assertEqual(share.fraction_hi, Fraction(1, 2))

    def test_mix_with_edge_disclosure_narrows_shares(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S1", 60, 60),
            IntakeDoc("I2", "B1", "S2", 40, 40),
            MixDecl("X1", "B1", ("S1", "S2"), "M"),
            DisclosedLinkDoc("L1", "B1", "S1", "M", 35, 35),
            MeasurementDoc("MM", "B1", "M", 60, 60),
        )
        attr = auditor.sources_of("M")
        got = shares_by_id(attr)
        self.assertEqual(
            (got["S1"].amount_lo, got["S1"].amount_hi), (35, 35)
        )
        self.assertEqual(
            (got["S2"].amount_lo, got["S2"].amount_hi), (25, 25)
        )
        self.assertEqual(got["S1"].fraction_lo, Fraction(35, 60))
        self.assertEqual(got["S2"].fraction_lo, Fraction(25, 60))


class LateEvidenceTests(unittest.TestCase):
    def test_measurements_before_and_after_split_declaration(self):
        auditor = ChainAuditor()
        # 只有两份测量时，两个节点各自独立、均可解释
        r1 = auditor.ingest(
            MeasurementDoc("M1", "B1", "T1", 55, 70),
            MeasurementDoc("M2", "B1", "T2", 40, 60),
        )
        self.assertEqual(r1.status, "satisfiable")
        before_t1 = auditor.node_interval("T1")

        # 迟到的入库单 + 拆分声明带来守恒约束，区间被收窄
        r2 = auditor.ingest(
            IntakeDoc("I1", "B1", "S1", 100, 100),
            SplitDecl("P1", "B1", "S1", ("T1", "T2")),
        )
        self.assertEqual(r2.status, "satisfiable")
        lo, hi = auditor.node_interval("T1")
        self.assertGreaterEqual(lo, before_t1[0])
        self.assertLessEqual(hi, before_t1[1])
        self.assertEqual(auditor.node_interval("T1"), (55, 60))
        self.assertEqual(auditor.node_interval("T2"), (40, 45))

    def test_late_disclosure_collapses_to_unique_source(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S1", 60, 60),
            IntakeDoc("I2", "B1", "S2", 60, 60),
            SplitDecl("P1", "B1", "S1", ("T", "R1")),
            SplitDecl("P2", "B1", "S2", ("T", "R2")),
            MeasurementDoc("MR1", "B1", "R1", 10, 10),
            MeasurementDoc("MR2", "B1", "R2", 10, 10),
            MeasurementDoc("MT", "B1", "T", 100, 100),
        )
        # 披露前：两个来源都合法（匿名来源因总量已被两条边吃满而量为 0）
        attr_before = auditor.sources_of("T")
        self.assertEqual(set(attr_before.source_ids), {"S1", "S2"})
        self.assertFalse(attr_before.unique)

        auditor.ingest(
            DisclosedLinkDoc("L1", "B1", "S1", "T", 50, 50),
            DisclosedLinkDoc("L2", "B1", "S2", "T", 50, 50),
        )
        attr_after = auditor.sources_of("T")
        self.assertEqual(set(attr_after.source_ids), {"S1", "S2"})
        got = shares_by_id(attr_after)
        self.assertEqual(
            (got["S1"].amount_lo, got["S1"].amount_hi), (50, 50)
        )
        self.assertEqual(
            (got["S2"].amount_lo, got["S2"].amount_hi), (50, 50)
        )

    def test_order_independence(self):
        docs_a = [
            IntakeDoc("I1", "B1", "S1", 100, 100),
            SplitDecl("P1", "B1", "S1", ("T1", "T2")),
            MeasurementDoc("M1", "B1", "T1", 55, 70),
            MeasurementDoc("M2", "B1", "T2", 40, 60),
        ]
        docs_b = list(reversed(docs_a))
        a = ChainAuditor()
        b = ChainAuditor()
        a.ingest(*docs_a)
        b.ingest(*docs_b)
        for node in ("S1", "T1", "T2"):
            self.assertEqual(a.node_interval(node), b.node_interval(node))
        self.assertEqual(
            a.sources_of("T1").source_ids, b.sources_of("T1").source_ids
        )

    def test_monotonic_narrowing_across_ingests(self):
        auditor = ChainAuditor()
        auditor.ingest(IntakeDoc("I1", "B1", "S1", 100, 100))
        auditor.ingest(SplitDecl("P1", "B1", "S1", ("T1", "T2")))
        first = auditor.node_interval("T1")
        auditor.ingest(MeasurementDoc("M1", "B1", "T1", 30, 40))
        second = auditor.node_interval("T1")
        self.assertGreaterEqual(second[0], first[0])
        self.assertLessEqual(second[1], first[1])
        self.assertEqual(second, (30, 40))


class PartialDisclosureTests(unittest.TestCase):
    def test_undisclosed_material_keeps_anonymous_source(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S1", 100, 100),
            DisclosedLinkDoc("L1", "B1", "S1", "T1", 10, 90),
            MeasurementDoc("M1", "B1", "T1", 100, 100),
        )
        attr = auditor.sources_of("T1")
        self.assertIn(ANON, attr.source_ids)
        self.assertIn("S1", attr.source_ids)
        self.assertFalse(attr.unique)
        got = shares_by_id(attr)
        # 披露边只约束那条已声明路径（10..90）；S1 的料仍可经未披露
        # 路径进入，故 S1 份额上界可达节点总量 100。
        self.assertEqual(
            (got["S1"].amount_lo, got["S1"].amount_hi), (10, 100)
        )
        # 匿名来源补足其余，上界 = 100 - 披露边下界 10 = 90
        self.assertEqual(
            (got[ANON].amount_lo, got[ANON].amount_hi), (0, 90)
        )
        self.assertEqual(got[ANON].fraction_hi, Fraction(9, 10))

    def test_zero_amount_share_is_dropped(self):
        # 封闭混样节点的两条入边恰好覆盖终端总量：匿名来源恒为 0，
        # 不进入合法来源列表。
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S1", 60, 60),
            IntakeDoc("I2", "B1", "S2", 40, 40),
            MixDecl("X1", "B1", ("S1", "S2"), "T"),
            DisclosedLinkDoc("L1", "B1", "S1", "T", 60, 60),
            DisclosedLinkDoc("L2", "B1", "S2", "T", 40, 40),
            MeasurementDoc("MT", "B1", "T", 100, 100),
        )
        attr = auditor.sources_of("T")
        self.assertNotIn(ANON, attr.source_ids)
        self.assertEqual(set(attr.source_ids), {"S1", "S2"})


class ContradictionTests(unittest.TestCase):
    def _classic_docs(self):
        return [
            IntakeDoc("I1", "B1", "S", 100, 100),
            SplitDecl("SP", "B1", "S", ("A", "C")),
            MeasurementDoc("M1", "B1", "A", 80, 80),
            MeasurementDoc("M2", "B1", "C", 80, 80),
        ]

    def test_each_measurement_alone_is_explainable(self):
        # 单看任一测量单都可解释（没有总量对照时）
        for measured in ("M1", "M2"):
            auditor = ChainAuditor()
            docs = [
                d
                for d in self._classic_docs()
                if not (isinstance(d, MeasurementDoc) and d.doc_id != measured)
            ]
            report = auditor.ingest(*docs)
            self.assertEqual(
                report.status,
                "satisfiable",
                f"测量单 {measured} 单独出现时应可解释",
            )

    def test_joint_conservation_contradiction_lists_documents(self):
        all_docs = self._classic_docs()
        auditor = ChainAuditor()
        report = auditor.ingest(*all_docs)
        self.assertEqual(report.status, "unsatisfiable")
        contradiction = auditor.contradiction
        self.assertIsNotNone(contradiction)
        ids = set(contradiction.doc_ids)
        # 矛盾核心必须包含至少一张可核实的区间单据
        self.assertTrue(ids & {"M1", "M2", "I1"})
        # 以及提供父子守恒关系的结构性单据
        self.assertIn("SP", ids)
        for doc_id in contradiction.doc_ids:
            self.assertTrue(
                any(doc_id in reason for reason in contradiction.reasons)
            )
        # MUS 性质实测：删除核心中的任一单据后必须恢复可满足
        for doc_id in contradiction.doc_ids:
            smaller = ChainAuditor()
            sub = [d for d in all_docs if d.doc_id != doc_id]
            self.assertEqual(smaller.ingest(*sub).status, "satisfiable")

    def test_interval_only_contradiction(self):
        all_docs = [
            IntakeDoc("I1", "B1", "S", 100, 100),
            SplitDecl("SP", "B1", "S", ("A", "C")),
            MeasurementDoc("M1", "B1", "A", 60, 80),
            MeasurementDoc("M2", "B1", "C", 60, 80),
        ]
        auditor = ChainAuditor()
        report = auditor.ingest(*all_docs)
        # 下界之和 120 > 100，区间单据同样能形成矛盾
        self.assertEqual(report.status, "unsatisfiable")
        self.assertIsNotNone(auditor.contradiction)
        ids = set(auditor.contradiction.doc_ids)
        self.assertIn("SP", ids)
        self.assertTrue(ids & {"M1", "M2", "I1"})
        # MUS 性质复核
        for doc_id in auditor.contradiction.doc_ids:
            smaller = ChainAuditor()
            sub = [d for d in all_docs if d.doc_id != doc_id]
            self.assertEqual(smaller.ingest(*sub).status, "satisfiable")

    def test_disclosed_edge_bound_contradiction(self):
        all_docs = [
            IntakeDoc("I1", "B1", "S", 100, 100),
            SplitDecl("SP", "B1", "S", ("A", "C")),
            DisclosedLinkDoc("L1", "B1", "S", "A", 60, 60),
            MeasurementDoc("M2", "B1", "C", 50, 50),
        ]
        auditor = ChainAuditor()
        report = auditor.ingest(*all_docs)
        self.assertEqual(report.status, "unsatisfiable")
        ids = set(auditor.contradiction.doc_ids)
        self.assertIn("L1", ids)
        # 单单据最小矛盾（披露边在无节点量承接时即不可满足）也合法；
        # 若核心含多张单据，则必须带上守恒前提。
        if len(ids) > 1:
            self.assertTrue(
                {"M2", "I1", "SP"} & ids,
                f"多单据矛盾核心应含守恒前提单据，实际 {ids}",
            )
        # MUS 性质复核：删核心内任一单据都恢复可满足
        for doc_id in auditor.contradiction.doc_ids:
            smaller = ChainAuditor()
            sub = [d for d in all_docs if d.doc_id != doc_id]
            self.assertEqual(smaller.ingest(*sub).status, "satisfiable")

    def test_node_interval_none_under_contradiction(self):
        auditor = ChainAuditor()
        auditor.ingest(*self._classic_docs())
        self.assertIsNone(auditor.node_interval("A"))


class BlindGenerationTests(unittest.TestCase):
    def test_reused_blind_id_across_batches_kept_distinct(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S1", 50, 50),
            SplitDecl("P1", "B1", "S1", ("T1", "R1")),
            MeasurementDoc("MT1", "B1", "T1", 30, 30),
            MeasurementDoc("MR1", "B1", "R1", 20, 20),
            IntakeDoc("I2", "B2", "S2", 80, 80),
            SplitDecl("P2", "B2", "S2", ("T2", "R2")),
            MeasurementDoc("MT2", "B2", "T2", 80, 80),
            BlindGenerationDoc("G1", "B1", "BL-9", 0, "S1"),
            BlindGenerationDoc("G2", "B1", "BL-9", 1, "T1"),
            BlindGenerationDoc("G3", "B2", "BL-9", 0, "S2"),
            BlindGenerationDoc("G4", "B2", "BL-9", 1, "T2"),
        )
        self.assertEqual(auditor.resolve_blind("B1", "BL-9", 1), "T1")
        self.assertEqual(auditor.resolve_blind("B2", "BL-9", 1), "T2")
        self.assertNotEqual(
            auditor.resolve_blind("B1", "BL-9", 1),
            auditor.resolve_blind("B2", "BL-9", 1),
        )
        self.assertEqual(auditor.node_interval("T1"), (30, 30))
        self.assertEqual(auditor.node_interval("T2"), (80, 80))

    def test_conflicting_generation_binding_is_input_error(self):
        auditor = ChainAuditor()
        auditor.ingest(
            BlindGenerationDoc("G1", "B1", "BL-9", 1, "T1"),
        )
        with self.assertRaises(InputError):
            auditor.ingest(BlindGenerationDoc("G2", "B1", "BL-9", 1, "T2"))


class InputValidationTests(unittest.TestCase):
    def test_reversed_interval_rejected(self):
        with self.assertRaises(InputError):
            IntakeDoc("I1", "B1", "S1", 90, 10)

    def test_negative_bound_rejected(self):
        with self.assertRaises(InputError):
            MeasurementDoc("M1", "B1", "T1", -1, 10)

    def test_non_integer_rejected(self):
        with self.assertRaises(InputError):
            IntakeDoc("I1", "B1", "S1", 1.5, 10)

    def test_duplicate_doc_id_rejected(self):
        auditor = ChainAuditor()
        auditor.ingest(IntakeDoc("D1", "B1", "S1", 10, 10))
        with self.assertRaises(InputError):
            auditor.ingest(MeasurementDoc("D1", "B1", "T1", 1, 2))

    def test_cross_batch_node_rejected(self):
        auditor = ChainAuditor()
        auditor.ingest(IntakeDoc("I1", "B1", "S1", 10, 10))
        with self.assertRaises(InputError):
            auditor.ingest(SplitDecl("P1", "B2", "S1", ("T1", "T2")))

    def test_cycle_rejected(self):
        auditor = ChainAuditor()
        with self.assertRaises(InputError):
            auditor.ingest(
                DisclosedLinkDoc("L1", "B1", "A", "B"),
                DisclosedLinkDoc("L2", "B1", "B", "A"),
            )

    def test_split_needs_two_children(self):
        with self.assertRaises(InputError):
            SplitDecl("P1", "B1", "S1", ("T1",))

    def test_unknown_node_query(self):
        auditor = ChainAuditor()
        auditor.ingest(IntakeDoc("I1", "B1", "S1", 10, 10))
        with self.assertRaises(InputError):
            auditor.node_interval("NOPE")


if __name__ == "__main__":
    unittest.main()
