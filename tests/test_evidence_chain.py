"""依据链与状态传播细节测试。"""
from __future__ import annotations

import unittest

from vuln_ledger.engine import evaluate
from vuln_ledger.parser import parse_input

from tests.scenarios import (
    SCENARIOS,
    merge_conflict_input,
    merge_resolved_input,
    multi_fix_input,
    two_line_scope_input,
)


def _run(raw: dict) -> dict:
    return evaluate(parse_input(raw))


def _dep(result: dict, build_id: str) -> dict:
    return next(d for d in result["deployments"] if d["build_id"] == build_id)


class EvidenceChainTest(unittest.TestCase):
    def test_linear_chain_order_and_states(self) -> None:
        scenario = next(s for s in SCENARIOS if s["case_id"] == "SC-01-linear-lifecycle")
        result = _run(scenario["input"])
        dep = _dep(result, "B-restored")
        chain_ids = [node["commit"] for node in dep["evidence_chain"]]
        # 依据链按拓扑序（父先于子）覆盖来源提交的全部祖先
        self.assertEqual(chain_ids, ["c0", "c1", "c2", "c3"])
        states_seq = [node["states"]["F1"] for node in dep["evidence_chain"]]
        self.assertEqual(
            states_seq, ["absent", "active", "inactive", "active"]
        )
        event_types = [ev["type"] for node in dep["evidence_chain"] for ev in node["events"]]
        self.assertIn("fix", event_types)
        self.assertIn("revert", event_types)
        self.assertEqual(event_types.count("fix"), 2)

    def test_merge_conflict_evidence_contains_parent_states(self) -> None:
        result = _run(merge_conflict_input())
        dep = _dep(result, "B-merge-unknown")
        merge_node = next(n for n in dep["evidence_chain"] if n["commit"] == "m1")
        merge_event = next(ev for ev in merge_node["events"] if ev["type"] == "merge")
        self.assertEqual(merge_event["result"], "unknown")
        self.assertEqual(merge_event["parent_states"]["a1"], "active")
        self.assertEqual(merge_event["parent_states"]["b1"], "absent")
        conflict = next(
            m for m in dep["missing_records"] if m["code"] == "merge_conflict"
        )
        self.assertEqual(conflict["active_parents"], ["a1"])
        self.assertEqual(conflict["inactive_parents"], [])
        # b1 是 absent，不算 inactive 父线；矛盾来源是 active vs absent 路径在同一合并点汇合
        # 按需求，只有显式解决记录才能消除未知
        self.assertTrue(
            any(m["code"] == "missing_merge_resolution" for m in dep["missing_records"])
        )

    def test_resolution_clears_unknown_and_leaves_event(self) -> None:
        result = _run(merge_resolved_input())
        dep = _dep(result, "B-resolved-fixed")
        merge_node = next(n for n in dep["evidence_chain"] if n["commit"] == "m1")
        resolution_event = next(
            ev for ev in merge_node["events"] if ev["type"] == "resolution"
        )
        self.assertEqual(resolution_event["resolved"], "fixed")
        self.assertTrue(resolution_event["cleared_unknown"])
        self.assertEqual(merge_node["states"]["F1"], "active")
        self.assertEqual(dep["missing_records"], [])

    def test_line_scope_does_not_leak_between_builds(self) -> None:
        result = _run(two_line_scope_input())
        lts = _dep(result, "B-lts")
        main = _dep(result, "B-main")
        self.assertEqual(lts["fix_statuses"]["F1"]["state"], "active")
        self.assertEqual(main["fix_statuses"]["F2"]["state"], "absent")
        # MAIN 线上 F1 未被认可，只产生异常记录，不进入 fix_statuses 的键
        self.assertEqual(set(main["fix_statuses"].keys()), {"F2"})
        self.assertTrue(
            all(m["build_id"] == "B-main" for m in main["missing_records"])
        )

    def test_multi_fix_independent_states(self) -> None:
        result = _run(multi_fix_input())
        dep = _dep(result, "B-mixed")
        self.assertEqual(dep["fix_statuses"]["F1"]["state"], "inactive")
        self.assertEqual(dep["fix_statuses"]["F2"]["state"], "active")
        self.assertEqual(dep["fix_statuses"]["F1"]["status"], "reaffected")
        self.assertEqual(dep["fix_statuses"]["F2"]["status"], "fixed")

    def test_unknown_propagates_after_merge(self) -> None:
        raw = merge_conflict_input()
        raw["commits"].append(
            {"id": "m2", "parents": ["m1"], "fix_ids": [], "reverts": []}
        )
        raw["builds"] = [{"build_id": "B-later", "line": "LTS", "source_commit": "m2"}]
        result = _run(raw)
        dep = _dep(result, "B-later")
        self.assertEqual(dep["status"], "unknown")
        propagated = [
            m for m in dep["missing_records"] if m["code"] == "unknown_propagated"
        ]
        self.assertTrue(propagated)
        self.assertEqual(propagated[0]["parent"], "m1")

    def test_refix_resolves_after_revert_on_same_line(self) -> None:
        scenario = next(s for s in SCENARIOS if s["case_id"] == "SC-01-linear-lifecycle")
        result = _run(scenario["input"])
        reverted = _dep(result, "B-reverted")
        restored = _dep(result, "B-restored")
        self.assertEqual(reverted["fix_statuses"]["F1"]["state"], "inactive")
        self.assertEqual(restored["fix_statuses"]["F1"]["state"], "active")


if __name__ == "__main__":
    unittest.main()
