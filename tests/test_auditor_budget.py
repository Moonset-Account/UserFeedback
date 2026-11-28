"""端到端预算诚实性：审计器在搜索预算耗尽时必须把份额标记为非精确。"""

from __future__ import annotations

import unittest

from chain_audit import ChainAuditor, IntakeDoc, MeasurementDoc, MixDecl
from chain_audit import audit as audit_mod


class AuditorBudgetTests(unittest.TestCase):
    def test_share_marked_inexact_when_search_budget_tiny(self):
        auditor = ChainAuditor()
        auditor.ingest(
            IntakeDoc("I1", "B1", "S1", 0, 500),
            IntakeDoc("I2", "B1", "S2", 0, 500),
            MixDecl("X1", "B1", ("S1", "S2"), "M"),
            MeasurementDoc("MM", "B1", "M", 200, 800),
        )
        original = audit_mod.SEARCH_BUDGET
        audit_mod.SEARCH_BUDGET = 1
        try:
            attribution = auditor.sources_of("M")
        finally:
            audit_mod.SEARCH_BUDGET = original
        # 预算耗尽时仍要给出保守区间，但 exact 必须为 False，
        # 不能把未证实的界当成精确结论。
        self.assertTrue(attribution.shares)
        self.assertTrue(any(not s.exact for s in attribution.shares))


if __name__ == "__main__":
    unittest.main()
