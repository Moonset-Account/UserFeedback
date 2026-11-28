"""多批次综合端到端场景：盲号重用、拆分混样、迟到披露、守恒矛盾全链路。"""

from __future__ import annotations

import unittest
from fractions import Fraction

from chain_audit import (
    ANON,
    BlindGenerationDoc,
    ChainAuditor,
    DisclosedLinkDoc,
    IntakeDoc,
    MeasurementDoc,
    MixDecl,
    SplitDecl,
)


class EndToEndScenarioTests(unittest.TestCase):
    def test_full_investigation_workflow(self):
        auditor = ChainAuditor()

        # 第一批：原样 S1 拆成两个送检子样，实验室只看到盲号。
        r1 = auditor.ingest(
            IntakeDoc("IN-S1", "B2026A", "S1", 200, 200),
            SplitDecl(
                "SP-S1", "B2026A", "S1", ("S1-A", "S1-B")
            ),
            MeasurementDoc("M-A", "B2026A", "S1-A", 90, 110),
            MeasurementDoc("M-B", "B2026A", "S1-B", 90, 110),
            BlindGenerationDoc("BG-1", "B2026A", "BL-500", 0, "S1"),
            BlindGenerationDoc("BG-2", "B2026A", "BL-500", 1, "S1-A"),
        )
        self.assertEqual(r1.status, "satisfiable")
        # 两个子量之和严格等于 200，各自区间被守恒交叉收窄
        self.assertEqual(auditor.node_interval("S1-A"), (90, 110))
        attr_a = auditor.sources_of("S1-A")
        self.assertTrue(attr_a.unique)
        self.assertEqual(attr_a.source_ids, ("S1",))

        # 第二批：另一原样，复用同一盲号但世代不同，必须隔离。
        r2 = auditor.ingest(
            IntakeDoc("IN-S2", "B2026B", "S2", 150, 150),
            SplitDecl("SP-S2", "B2026B", "S2", ("S2-A", "S2-B")),
            MeasurementDoc("M-C", "B2026B", "S2-A", 70, 70),
            MeasurementDoc("M-D", "B2026B", "S2-B", 80, 80),
            BlindGenerationDoc("BG-3", "B2026B", "BL-500", 0, "S2"),
            BlindGenerationDoc("BG-4", "B2026B", "BL-500", 1, "S2-A"),
        )
        self.assertEqual(r2.status, "satisfiable")
        self.assertNotEqual(
            auditor.resolve_blind("B2026A", "BL-500", 1),
            auditor.resolve_blind("B2026B", "BL-500", 1),
        )

        # 迟到的分拣单只披露 S1-A 的一条父子边（部分披露）。
        r3 = auditor.ingest(
            DisclosedLinkDoc(
                "DL-1", "B2026A", "S1", "S1-A", 95, 105
            ),
        )
        self.assertEqual(r3.status, "satisfiable")
        # 披露边进一步收窄该子样的可行量
        lo, hi = auditor.node_interval("S1-A")
        self.assertGreaterEqual(lo, 95)
        self.assertLessEqual(hi, 105)

        # 第三批：混样，两个来源汇入一个终端，无唯一来源时保留全部合法区间。
        r4 = auditor.ingest(
            IntakeDoc("IN-S3", "B2026C", "S3", 60, 60),
            IntakeDoc("IN-S4", "B2026C", "S4", 40, 40),
            MixDecl("MX-1", "B2026C", ("S3", "S4"), "MIX"),
            MeasurementDoc("M-MIX", "B2026C", "MIX", 100, 100),
        )
        self.assertEqual(r4.status, "satisfiable")
        mix_attr = auditor.sources_of("MIX")
        self.assertFalse(mix_attr.unique)
        self.assertEqual(set(mix_attr.source_ids), {"S3", "S4"})
        shares = {s.source_id: s for s in mix_attr.shares}
        self.assertEqual(shares["S3"].fraction_lo, Fraction(3, 5))
        self.assertEqual(shares["S4"].fraction_lo, Fraction(2, 5))

        # 全程单据可枚举、版本号随 ingest 单调递增。
        self.assertEqual(r4.version, 4)
        self.assertGreaterEqual(len(auditor.documents), 13)

    def test_contradiction_workflow_identifies_documents(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("IN", "B1", "S", 100, 100),
            SplitDecl("SP", "B1", "S", ("A", "B")),
            # 各实验室结果单看都落在合理范围
            MeasurementDoc("M-A", "B1", "A", 70, 70),
            MeasurementDoc("M-B", "B1", "B", 70, 70),
        )
        self.assertEqual(auditor.status, "unsatisfiable")
        report = auditor.contradiction
        self.assertIsNotNone(report)
        # 报告由可查单据编号形成，且每条都有人类可读原因。
        self.assertTrue(report.doc_ids)
        self.assertEqual(len(report.doc_ids), len(report.reasons))
        # 至少一张测量/入库区间单据在核心内。
        self.assertTrue(set(report.doc_ids) & {"M-A", "M-B", "IN"})


if __name__ == "__main__":
    unittest.main()
