"""边界与交错场景专项测试：零边界、整数守恒、多轮收窄、盲号隔离。

所有数据均为本地合成，数量为整数毫克。
"""

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
from chain_audit.compiler import compile_documents
from chain_audit.intervals import propagate


def _check_witness_satisfies_equations(auditor):
    """强不变量：可行见证必须逐条满足编译出的全部守恒方程。"""
    model = auditor._model
    witness = auditor.feasible_witness()
    assert witness is not None
    for eq in model.equations:
        total = sum(c * witness[v] for v, c in eq.coeffs)
        assert total == eq.rhs, (eq.coeffs, eq.rhs, total)
    # 所有变量都在其声明整数区间内
    for name, (lo, hi) in model.domains.items():
        assert lo <= witness[name] <= hi, name
    return witness


class ZeroBoundaryTests(unittest.TestCase):
    def test_zero_amount_split_child_is_feasible(self):
        auditor = ChainAuditor()
        report = auditor.ingest(
            IntakeDoc("I1", "B1", "S", 100, 100),
            SplitDecl("P1", "B1", "S", ("A", "B")),
            MeasurementDoc("MA", "B1", "A", 0, 0),
            MeasurementDoc("MB", "B1", "B", 100, 100),
        )
        self.assertEqual(report.status, "satisfiable")
        self.assertEqual(auditor.node_interval("A"), (0, 0))
        self.assertEqual(auditor.node_interval("B"), (100, 100))
        _check_witness_satisfies_equations(auditor)

    def test_zero_width_interval_is_point(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S", 42, 42),
            MeasurementDoc("M1", "B1", "S", 42, 42),
        )
        self.assertEqual(auditor.node_interval("S"), (42, 42))

    def test_interval_intersection_tightens(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S", 100, 200),
            MeasurementDoc("M1", "B1", "S", 150, 300),
        )
        # 交集 [150, 200]
        self.assertEqual(auditor.node_interval("S"), (150, 200))

    def test_empty_intersection_is_contradiction(self):
        auditor = ChainAuditor()
        report = auditor.ingest(
            IntakeDoc("I1", "B1", "S", 0, 50),
            MeasurementDoc("M1", "B1", "S", 60, 80),
        )
        self.assertEqual(report.status, "unsatisfiable")
        self.assertIsNotNone(auditor.contradiction)


class IntegerConservationTests(unittest.TestCase):
    def test_witness_respects_every_equation_on_chain(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S", 1000, 1000),
            SplitDecl("P1", "B1", "S", ("T", "R")),
            SplitDecl("P2", "B1", "T", ("U1", "U2", "U3")),
            MeasurementDoc("MR", "B1", "R", 250, 250),
            MeasurementDoc("MU1", "B1", "U1", 100, 300),
            MeasurementDoc("MU2", "B1", "U2", 200, 400),
            MeasurementDoc("MU3", "B1", "U3", 50, 150),
        )
        self.assertEqual(auditor.status, "satisfiable")
        witness = _check_witness_satisfies_equations(auditor)
        # 终端整数毫克之和等于原样总量
        total = sum(
            witness[auditor._model.node_var[n]] for n in ("R", "U1", "U2", "U3")
        )
        self.assertEqual(total, 1000)

    def test_mix_then_split_multi_source_attribution(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S1", 600, 600),
            IntakeDoc("I2", "B1", "S2", 400, 400),
            MixDecl("X1", "B1", ("S1", "S2"), "M"),
            SplitDecl("P2", "B1", "M", ("K1", "K2")),
            MeasurementDoc("MK1", "B1", "K1", 300, 300),
            MeasurementDoc("MK2", "B1", "K2", 700, 700),
        )
        self.assertEqual(auditor.status, "satisfiable")
        _check_witness_satisfies_equations(auditor)
        for node in ("K1", "K2", "M"):
            attr = auditor.sources_of(node)
            self.assertEqual(set(attr.source_ids), {"S1", "S2"})
            self.assertFalse(attr.unique)
            # 各来源份额都是非负整数区间，且并集覆盖节点量
            self.assertTrue(all(s.amount_lo >= 0 for s in attr.shares))


class InterleavingTests(unittest.TestCase):
    def test_three_round_narrowing_monotone(self):
        auditor = ChainAuditor()
        auditor.ingest(IntakeDoc("I1", "B1", "S", 100, 100))
        auditor.ingest(SplitDecl("P1", "B1", "S", ("A", "B")))
        first = auditor.node_interval("A")
        auditor.ingest(MeasurementDoc("M1", "B1", "A", 30, 70))
        second = auditor.node_interval("A")
        auditor.ingest(MeasurementDoc("M2", "B1", "B", 40, 40))
        third = auditor.node_interval("A")
        self.assertGreaterEqual(second[0], first[0])
        self.assertLessEqual(second[1], first[1])
        self.assertGreaterEqual(third[0], second[0])
        self.assertLessEqual(third[1], second[1])
        # B 固定 40 后，A = 100-40 = 60
        self.assertEqual(third, (60, 60))

    def test_full_shuffle_gives_same_result(self):
        docs = [
            IntakeDoc("I1", "B1", "S", 500, 500),
            SplitDecl("P1", "B1", "S", ("A", "B")),
            MeasurementDoc("M1", "B1", "A", 200, 260),
            MeasurementDoc("M2", "B1", "B", 240, 300),
            DisclosedLinkDoc("L1", "B1", "S", "A", 200, 260),
        ]
        orders = [
            docs,
            list(reversed(docs)),
            [docs[3], docs[0], docs[4], docs[1], docs[2]],
        ]
        ref = None
        for order in orders:
            auditor = ChainAuditor()
            auditor.ingest(*order)
            snapshot = {
                n: auditor.node_interval(n) for n in ("S", "A", "B")
            }
            if ref is None:
                ref = snapshot
            else:
                self.assertEqual(snapshot, ref)

    def test_contradiction_persists_through_further_ingest(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S", 100, 100),
            SplitDecl("P1", "B1", "S", ("A", "B")),
            MeasurementDoc("M1", "B1", "A", 80, 80),
            MeasurementDoc("M2", "B1", "B", 80, 80),
        )
        self.assertEqual(auditor.status, "unsatisfiable")
        report = auditor.ingest(MeasurementDoc("M3", "B1", "A", 80, 80))
        self.assertEqual(report.status, "unsatisfiable")
        self.assertIsNotNone(auditor.contradiction)

    def test_mus_flag_exact_on_small_instance(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S", 100, 100),
            SplitDecl("P1", "B1", "S", ("A", "B")),
            MeasurementDoc("M1", "B1", "A", 80, 80),
            MeasurementDoc("M2", "B1", "B", 80, 80),
        )
        self.assertTrue(auditor.contradiction.minimal)


class BlindGenerationIsolationTests(unittest.TestCase):
    def test_same_blind_different_generation_resolves_distinctly(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S", 100, 100),
            SplitDecl("P1", "B1", "S", ("A", "B")),
            BlindGenerationDoc("G0", "B1", "BL-1", 0, "S"),
            BlindGenerationDoc("G1", "B1", "BL-1", 1, "A"),
            BlindGenerationDoc("G2", "B1", "BL-1", 2, "B"),
        )
        self.assertEqual(auditor.resolve_blind("B1", "BL-1", 0), "S")
        self.assertEqual(auditor.resolve_blind("B1", "BL-1", 1), "A")
        self.assertEqual(auditor.resolve_blind("B1", "BL-1", 2), "B")
        # 未登记的世代返回 None，而不是猜测成同盲号的其它世代
        self.assertIsNone(auditor.resolve_blind("B1", "BL-1", 9))

    def test_cross_batch_reused_blind_independent_intervals(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S1", 100, 100),
            SplitDecl("P1", "B1", "S1", ("X1", "R1")),
            MeasurementDoc("MX1", "B1", "X1", 30, 30),
            MeasurementDoc("MR1", "B1", "R1", 70, 70),
            IntakeDoc("I2", "B2", "S2", 200, 200),
            SplitDecl("P2", "B2", "S2", ("X2", "R2")),
            MeasurementDoc("MX2", "B2", "X2", 150, 150),
            MeasurementDoc("MR2", "B2", "R2", 50, 50),
            BlindGenerationDoc("G1", "B1", "BL-7", 1, "X1"),
            BlindGenerationDoc("G2", "B2", "BL-7", 1, "X2"),
        )
        n1 = auditor.resolve_blind("B1", "BL-7", 1)
        n2 = auditor.resolve_blind("B2", "BL-7", 1)
        self.assertNotEqual(n1, n2)
        self.assertEqual(auditor.node_interval(n1), (30, 30))
        self.assertEqual(auditor.node_interval(n2), (150, 150))

    def test_late_blind_binding_after_measurements(self):
        # 盲号单据最后才到，也能正确绑定到早已存在的节点
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S", 50, 50),
            MeasurementDoc("M1", "B1", "T", 50, 50),
        )
        self.assertIsNone(auditor.resolve_blind("B1", "BL-3", 1))
        auditor.ingest(BlindGenerationDoc("G1", "B1", "BL-3", 1, "T"))
        self.assertEqual(auditor.resolve_blind("B1", "BL-3", 1), "T")


class FractionTests(unittest.TestCase):
    def test_share_fractions_are_exact_fractions(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S1", 30, 30),
            IntakeDoc("I2", "B1", "S2", 70, 70),
            MixDecl("X1", "B1", ("S1", "S2"), "M"),
            MeasurementDoc("MM", "B1", "M", 100, 100),
        )
        got = {s.source_id: s for s in auditor.sources_of("M").shares}
        self.assertEqual(got["S1"].fraction_lo, Fraction(3, 10))
        self.assertEqual(got["S2"].fraction_lo, Fraction(7, 10))

    def test_legal_sources_all_retained_when_not_unique(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S1", 100, 100),
            IntakeDoc("I2", "B1", "S2", 100, 100),
            DisclosedLinkDoc("L1", "B1", "S1", "T", 10, 40),
            DisclosedLinkDoc("L2", "B1", "S2", "T", 10, 40),
            MeasurementDoc("MT", "B1", "T", 50, 50),
        )
        attr = auditor.sources_of("T")
        self.assertFalse(attr.unique)
        # 没有唯一来源时，两个合法来源区间都必须保留
        self.assertEqual(set(attr.source_ids), {"S1", "S2", ANON})
        for share in attr.shares:
            self.assertIsInstance(share.fraction_lo, Fraction)


if __name__ == "__main__":
    unittest.main()
