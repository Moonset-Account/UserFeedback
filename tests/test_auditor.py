"""变化驱动审查：专用路由撤下后子段回退未批准备用中转的完整主线。"""

import unittest

from route_audit import (
    Change,
    ChangeAction,
    ChangeAuditor,
    NextHopMap,
    Prefix,
    Probe,
    Route,
    RoutingState,
    TraceStatus,
    TransitDirection,
    TransitGrant,
)


def _r(route_id: str, domain: str, prefix: str, next_hop: str | None,
       metric: int = 100) -> Route:
    return Route(route_id, domain, Prefix(prefix), next_hop, metric)


def build_topology() -> tuple[RoutingState, list[Probe]]:
    """四域拓扑：corp 源、edge-a 已批准中转、edge-b 未批准、dc 目的。"""
    nh = NextHopMap()
    nh.add("corp", "nh-primary", "edge-a")
    nh.add("corp", "nh-backup", "edge-b")
    nh.add("edge-a", "nh-dc", "dc")
    nh.add("edge-b", "nh-dc", "dc")
    nh.add("dc", "nh-local", "dc")

    state = RoutingState(
        domains=["corp", "edge-a", "edge-b", "dc"],
        next_hop_map=nh,
        grants=[
            TransitGrant("edge-a", "corp", "dc",
                         TransitDirection.BIDIRECTIONAL),
        ],
    )

    # corp：两段专用 /24 走已批准的 edge-a；一条 /16 备用走未批准的 edge-b
    state.add_route(_r("corp-ded-1", "corp", "10.10.1.0/24", "nh-primary", 50))
    state.add_route(_r("corp-ded-2", "corp", "10.10.2.0/24", "nh-primary", 50))
    state.add_route(_r("corp-bkp", "corp", "10.10.0.0/16", "nh-backup", 200))
    state.add_route(_r("edge-a-dc", "edge-a", "10.10.0.0/16", "nh-dc"))
    state.add_route(_r("edge-b-dc", "edge-b", "10.10.0.0/16", "nh-dc"))
    state.add_route(_r("dc-local", "dc", "10.10.0.0/16", "nh-local"))

    probes = [
        Probe("probe-seg-1", "corp", Prefix("10.10.1.0/24")),
        Probe("probe-seg-2", "corp", Prefix("10.10.2.0/24")),
    ]
    return state, probes


class TestChangeAuditor(unittest.TestCase):
    def test_baseline_is_compliant_via_approved_transit(self) -> None:
        state, probes = build_topology()
        auditor = ChangeAuditor(state, probes)
        baseline = auditor.run_baseline()

        for result in baseline.values():
            self.assertEqual(result.status, TraceStatus.DELIVERED)
            self.assertTrue(result.is_compliant)
        self.assertEqual(auditor.all_findings, ())
        # 基线路径确实经过已批准的 edge-a
        seg1 = baseline["probe-seg-1"]
        self.assertEqual([s.domain for s in seg1.steps],
                         ["corp", "edge-a", "dc"])

    def test_withdraw_dedicated_route_exposes_unauthorized_backup_segment(self) -> None:
        state, probes = build_topology()
        auditor = ChangeAuditor(state, probes)
        auditor.run_baseline()

        change = Change(
            change_id="chg-withdraw-ded-1",
            action=ChangeAction.WITHDRAW,
            domain="corp",
            route_id="corp-ded-1",
        )
        new_findings = auditor.apply_change(change)

        # 题面三元组：受影响前缀段、首个未授权跨域步骤、触发变化
        self.assertEqual(len(new_findings), 1)
        finding = new_findings[0]
        self.assertEqual(finding.affected_prefix, Prefix("10.10.1.0/24"))
        self.assertEqual(finding.trigger_change_id, "chg-withdraw-ded-1")

        step = finding.first_unauthorized_step
        self.assertEqual(step.domain, "edge-b")       # 未批准的备用中转域
        self.assertEqual(step.peer, "dc")
        self.assertFalse(step.authorized)
        # 源域出域跳不算违规，违规跳是第 2 跳
        self.assertEqual(step.order, 1)
        self.assertEqual(step.route.route_id, "edge-b-dc")

        # 撤下后该段在 corp 内回退到 /16 备用路由（LPM 次优）
        trace = auditor.latest_traces["probe-seg-1"]
        self.assertEqual(trace.status, TraceStatus.UNAUTHORIZED_TRANSIT)
        self.assertEqual(trace.steps[0].route.route_id, "corp-bkp")
        self.assertEqual(trace.steps[0].peer, "edge-b")

    def test_unaffected_sibling_segment_keeps_working(self) -> None:
        state, probes = build_topology()
        auditor = ChangeAuditor(state, probes)
        auditor.run_baseline()
        auditor.apply_change(
            Change("chg-1", ChangeAction.WITHDRAW, "corp", "corp-ded-1")
        )
        # 另一小段的专用路由仍在，继续走已批准路径，不产生结论
        seg2 = auditor.latest_traces["probe-seg-2"]
        self.assertEqual(seg2.status, TraceStatus.DELIVERED)
        self.assertEqual(
            [s.domain for s in seg2.steps], ["corp", "edge-a", "dc"]
        )
        self.assertEqual(
            [f.probe_id for f in auditor.all_findings], ["probe-seg-1"]
        )

    def test_reinject_restores_compliance_and_clears_active_finding(self) -> None:
        state, probes = build_topology()
        auditor = ChangeAuditor(state, probes)
        auditor.run_baseline()
        auditor.apply_change(
            Change("chg-withdraw", ChangeAction.WITHDRAW, "corp", "corp-ded-1")
        )
        self.assertEqual(len(auditor.active_findings), 1)

        restore = Change(
            change_id="chg-restore",
            action=ChangeAction.ADD,
            domain="corp",
            route_id="corp-ded-1",
            route=_r("corp-ded-1", "corp", "10.10.1.0/24", "nh-primary", 50),
        )
        new_findings = auditor.apply_change(restore)
        self.assertEqual(new_findings, ())
        self.assertEqual(auditor.active_findings, ())
        trace = auditor.latest_traces["probe-seg-1"]
        self.assertEqual(trace.status, TraceStatus.DELIVERED)
        self.assertEqual(trace.steps[0].route.route_id, "corp-ded-1")
        # 历史结论保留，但已不再活动
        self.assertEqual(len(auditor.all_findings), 1)

    def test_repeated_violation_after_second_withdraw_is_reported_again(self) -> None:
        state, probes = build_topology()
        auditor = ChangeAuditor(state, probes)
        auditor.run_baseline()
        withdraw = Change("chg-w", ChangeAction.WITHDRAW, "corp", "corp-ded-1")
        restore = Change(
            "chg-r", ChangeAction.ADD, "corp", "corp-ded-1",
            route=_r("corp-ded-1", "corp", "10.10.1.0/24", "nh-primary", 50),
        )
        first = auditor.apply_change(withdraw)
        auditor.apply_change(restore)
        second = auditor.apply_change(withdraw)

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].trigger_change_id, "chg-w")
        self.assertEqual(len(auditor.all_findings), 2)
        self.assertEqual(len(auditor.active_findings), 1)

    def test_persistent_violation_not_duplicated_on_unrelated_change(self) -> None:
        state, probes = build_topology()
        auditor = ChangeAuditor(state, probes)
        auditor.run_baseline()
        auditor.apply_change(
            Change("chg-w", ChangeAction.WITHDRAW, "corp", "corp-ded-1")
        )
        # 另一个域上的无关变化不会让同一违规重复报出
        unrelated = Change(
            "chg-unrelated", ChangeAction.WITHDRAW, "dc", "dc-local"
        )
        new_findings = auditor.apply_change(unrelated)
        self.assertEqual(new_findings, ())
        self.assertEqual(len(auditor.all_findings), 1)

    def test_baseline_violation_has_none_trigger(self) -> None:
        # 初始快照本身就让 seg-1 走未批准中转：基线违规 trigger_change_id 为 None
        state, probes = build_topology()
        state.withdraw_route("corp", "corp-ded-1")
        auditor = ChangeAuditor(state, probes)
        auditor.run_baseline()
        self.assertEqual(len(auditor.all_findings), 1)
        self.assertIsNone(auditor.all_findings[0].trigger_change_id)

    def test_run_all_report_groups_traces_by_change(self) -> None:
        state, probes = build_topology()
        auditor = ChangeAuditor(state, probes)
        report = auditor.run_all(
            [Change("chg-w", ChangeAction.WITHDRAW, "corp", "corp-ded-1")]
        )
        self.assertEqual(len(report.findings), 1)
        self.assertIn("chg-w", report.traces_by_change)
        self.assertEqual(
            report.findings_for_change("chg-w")[0].affected_prefix,
            Prefix("10.10.1.0/24"),
        )
        self.assertEqual(report.findings_for_change("missing"), ())


if __name__ == "__main__":
    unittest.main()
