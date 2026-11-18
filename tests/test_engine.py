"""发布判定引擎验收测试。

覆盖题面交叉约束：

- 可接受结果：合并后累计开放恰等于额度（边界放行）
- 应被阻止：超出额度并返回导致超量的时间段
- 边界/交错：同一控制项拆多次申请共享额度、重叠豁免只算一次、
  未到期但已收回的区间在实际关闭时结束、as_of 冻结历史、窗口裁剪、
  环境范围与缺失额度的防御性拒绝、异常事件告警
"""

import unittest
from datetime import timedelta

from release_gate import (
    REASON_OUT_OF_SCOPE,
    REASON_PROJECTION_EMPTY,
    REASON_QUOTA_EXCEEDED,
    REASON_QUOTA_MISSING,
    evaluate_bundle,
    real_open_segments,
)
from release_gate.engine import used_open_intervals

from factory import (
    DEFAULT_AS_OF,
    application,
    dt,
    event,
    exemption,
    hours,
    iv,
    make_bundle,
    quota,
)


def single_ex_bundle(opens, closes, quota_duration="PT4H", as_of=DEFAULT_AS_OF):
    """构造只有一条豁免 EX-1、一组 OPEN/CLOSE 的判定包（无新申请）。"""
    ex = exemption()
    events = []
    if opens is not None:
        events.append(event("EV-OPEN", "EX-1", "OPEN", opens))
    if closes is not None:
        events.append(event("EV-CLOSE", "EX-1", "CLOSE", closes))
    return make_bundle(
        quotas=[quota(duration=quota_duration)],
        exemptions=[ex],
        events=events,
        as_of=as_of,
    ), ex, events


class TestRealOpenSegments(unittest.TestCase):
    def test_closed_before_effective_end_ends_at_real_close(self):
        # 尚未到期但已收回：真实开放区间在实际 CLOSE 时结束，而不是生效结束。
        bundle, ex, events = single_ex_bundle(
            "2026-11-02T00:00:00Z", "2026-11-02T03:00:00Z"
        )
        segments, warnings = real_open_segments(
            ex, events, bundle.window, bundle.as_of
        )
        self.assertEqual(
            segments, [iv("2026-11-02T00:00:00Z", "2026-11-02T03:00:00Z")]
        )
        self.assertEqual(warnings, [])

    def test_still_open_ends_at_as_of_not_effective_end(self):
        # 仍打开、未到期：截至考核时点 as_of，不把未来时间计入已用额度。
        bundle, ex, events = single_ex_bundle(
            "2026-11-05T08:00:00Z", None, as_of="2026-11-05T12:00:00Z"
        )
        segments, _ = real_open_segments(ex, events, bundle.window, bundle.as_of)
        self.assertEqual(
            segments, [iv("2026-11-05T08:00:00Z", "2026-11-05T12:00:00Z")]
        )

    def test_open_past_effective_end_ends_at_effective_end(self):
        # 到期未关闭：截至生效结束时间，且不产生重复 CLOSE 告警。
        ex = exemption(
            start="2026-11-02T00:00:00Z", end="2026-11-02T06:00:00Z"
        )
        events = [event("EV-OPEN", "EX-1", "OPEN", "2026-11-02T00:00:00Z")]
        bundle = make_bundle(
            quotas=[quota()],
            exemptions=[ex],
            events=events,
            as_of="2026-11-05T00:00:00Z",
        )
        segments, warnings = real_open_segments(
            ex, events, bundle.window, bundle.as_of
        )
        self.assertEqual(
            segments, [iv("2026-11-02T00:00:00Z", "2026-11-02T06:00:00Z")]
        )
        self.assertEqual(warnings, [])

    def test_future_events_after_as_of_are_ignored(self):
        # 历史复核：as_of 之后的 CLOSE 不参与计算，开放段截至 as_of。
        bundle, ex, events = single_ex_bundle(
            "2026-11-02T00:00:00Z",
            "2026-11-03T00:00:00Z",
            as_of="2026-11-02T12:00:00Z",
        )
        segments, _ = real_open_segments(ex, events, bundle.window, bundle.as_of)
        self.assertEqual(
            segments, [iv("2026-11-02T00:00:00Z", "2026-11-02T12:00:00Z")]
        )

    def test_window_clips_real_segments(self):
        # OPEN 早于窗口开始：开放段被裁剪到窗口内。
        ex = exemption(
            start="2026-10-30T00:00:00Z", end="2026-11-03T00:00:00Z"
        )
        events = [
            event("EV-OPEN", "EX-1", "OPEN", "2026-10-31T00:00:00Z"),
            event("EV-CLOSE", "EX-1", "CLOSE", "2026-11-02T00:00:00Z"),
        ]
        bundle = make_bundle(exemptions=[ex], events=events)
        segments, _ = real_open_segments(
            ex, events, bundle.window, bundle.as_of
        )
        self.assertEqual(
            segments, [iv("2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z")]
        )

    def test_anomalous_events_emit_warnings_but_still_defensive(self):
        ex = exemption()
        events = [
            event("EV-C1", "EX-1", "CLOSE", "2026-11-02T01:00:00Z"),
            event("EV-O1", "EX-1", "OPEN", "2026-11-02T02:00:00Z"),
            event("EV-O2", "EX-1", "OPEN", "2026-11-02T03:00:00Z"),
            event("EV-C2", "EX-1", "CLOSE", "2026-11-02T05:00:00Z"),
        ]
        bundle = make_bundle(exemptions=[ex], events=events)
        segments, warnings = real_open_segments(
            ex, events, bundle.window, bundle.as_of
        )
        self.assertEqual(
            segments, [iv("2026-11-02T02:00:00Z", "2026-11-02T05:00:00Z")]
        )
        self.assertEqual(len(warnings), 2)


class TestQuotaDecisions(unittest.TestCase):
    def _used_four_hours_bundle(self):
        # 已用真实开放 4h：EX-1 开 3h、EX-2 开 1h（同一控制项、同一环境）。
        ex1 = exemption(
            "EX-1",
            start="2026-11-02T00:00:00Z",
            end="2026-11-02T12:00:00Z",
        )
        ex2 = exemption(
            "EX-2",
            start="2026-11-03T00:00:00Z",
            end="2026-11-03T12:00:00Z",
        )
        events = [
            event("E1", "EX-1", "OPEN", "2026-11-02T00:00:00Z"),
            event("E2", "EX-1", "CLOSE", "2026-11-02T03:00:00Z"),
            event("E3", "EX-2", "OPEN", "2026-11-03T00:00:00Z"),
            event("E4", "EX-2", "CLOSE", "2026-11-03T01:00:00Z"),
        ]
        return make_bundle(
            quotas=[quota(duration="PT4H")],
            exemptions=[ex1, ex2],
            events=events,
        )

    def test_boundary_exactly_at_quota_is_allowed(self):
        # 可接受结果（边界）：已用 4h，再申请 0 秒新增、与已用区间完全重叠 ->
        # 合并后仍恰为 4h，必须放行，不能把重叠时间重复扣减。
        bundle = self._used_four_hours_bundle()
        bundle.applications = [
            application(
                "APP-BOUNDARY",
                intervals=[iv("2026-11-02T00:00:00Z", "2026-11-02T03:00:00Z")],
            )
        ]
        result = evaluate_bundle(bundle)
        decision = result.decisions[0]
        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.reason)
        self.assertEqual(decision.used_duration, hours(4))
        self.assertEqual(decision.combined_duration, hours(4))
        self.assertEqual(decision.overage_segments, [])

    def test_one_second_over_quota_is_blocked_with_overage_segment(self):
        # 应被阻止：合并后 4h+1s，返回精确到秒的超量时间段。
        bundle = self._used_four_hours_bundle()
        bundle.applications = [
            application(
                "APP-OVER",
                intervals=[iv("2026-11-06T00:00:00Z", "2026-11-06T00:00:01Z")],
            )
        ]
        decision = evaluate_bundle(bundle).decisions[0]
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, REASON_QUOTA_EXCEEDED)
        self.assertEqual(
            decision.overage_segments,
            [iv("2026-11-06T00:00:00Z", "2026-11-06T00:00:01Z")],
        )
        self.assertEqual(
            decision.combined_duration - decision.quota, timedelta(seconds=1)
        )

    def test_split_applications_share_one_quota(self):
        # 同一控制项拆成两次申请：各自都在额度内，但合计共享同一额度，
        # 第二份并入当期已用后必须被阻止。
        ex = exemption(
            "EX-1",
            start="2026-11-02T00:00:00Z",
            end="2026-11-02T12:00:00Z",
        )
        events = [
            event("E1", "EX-1", "OPEN", "2026-11-02T00:00:00Z"),
            event("E2", "EX-1", "CLOSE", "2026-11-02T01:00:00Z"),
        ]
        bundle = make_bundle(
            quotas=[quota(duration="PT5H")],
            exemptions=[ex],
            events=events,
            applications=[
                application(
                    "APP-A",
                    intervals=[iv("2026-11-03T00:00:00Z", "2026-11-03T02:00:00Z")],
                ),
                application(
                    "APP-B",
                    intervals=[iv("2026-11-04T00:00:00Z", "2026-11-04T03:00:00Z")],
                ),
            ],
        )
        decisions = {d.application_id: d for d in evaluate_bundle(bundle).decisions}
        self.assertTrue(decisions["APP-A"].allowed)  # 1+2=3h <= 5h
        b = decisions["APP-B"]  # 1+2+3=6h > 5h
        self.assertFalse(b.allowed)
        self.assertEqual(b.reason, REASON_QUOTA_EXCEEDED)
        self.assertEqual(
            b.overage_segments,
            [iv("2026-11-04T02:00:00Z", "2026-11-04T03:00:00Z")],
        )

    def test_overlapping_exemptions_counted_once(self):
        # 两条豁免真实开放区间重叠 5h：朴素求和 13h，并集只有 8h；
        # 新申请 3h，合并 11h，10h 额度下超量 1h。
        ex1 = exemption(
            "EX-1",
            start="2026-11-02T00:00:00Z",
            end="2026-11-02T13:00:00Z",
        )
        ex2 = exemption(
            "EX-2",
            start="2026-11-02T03:00:00Z",
            end="2026-11-05T00:00:00Z",
        )
        events = [
            event("E1", "EX-1", "OPEN", "2026-11-02T00:00:00Z"),
            event("E2", "EX-1", "CLOSE", "2026-11-02T08:00:00Z"),
            event("E3", "EX-2", "OPEN", "2026-11-02T03:00:00Z"),
            event("E4", "EX-2", "CLOSE", "2026-11-02T11:00:00Z"),
        ]
        bundle = make_bundle(
            quotas=[quota(duration="PT10H")],
            exemptions=[ex1, ex2],
            events=events,
            applications=[
                application(
                    "APP-1",
                    intervals=[iv("2026-11-06T00:00:00Z", "2026-11-06T03:00:00Z")],
                )
            ],
        )
        open_by_key, _ = used_open_intervals(bundle)
        used = open_by_key[("prod", "C-01")]
        self.assertEqual(used, [iv("2026-11-02T00:00:00Z", "2026-11-02T11:00:00Z")])
        decision = evaluate_bundle(bundle).decisions[0]
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.used_duration, hours(11))
        self.assertEqual(decision.combined_duration, hours(14))
        # 已用 11h、额度 10h：预算已耗尽，整个 3h 申请区间都是超量段。
        self.assertEqual(
            decision.overage_segments,
            [iv("2026-11-06T00:00:00Z", "2026-11-06T03:00:00Z")],
        )

    def test_early_revocation_frees_quota(self):
        # 未到期已收回：只消耗实际开放的 2h，后续 4h 申请在 5h 额度内放行。
        ex = exemption(
            "EX-1",
            start="2026-11-06T00:00:00Z",
            end="2026-11-09T00:00:00Z",
        )
        events = [
            event("E1", "EX-1", "OPEN", "2026-11-06T00:00:00Z"),
            event("E2", "EX-1", "CLOSE", "2026-11-06T02:00:00Z"),
        ]
        bundle = make_bundle(
            quotas=[quota(duration="PT6H")],
            exemptions=[ex],
            events=events,
            applications=[
                application(
                    "APP-1",
                    intervals=[iv("2026-11-08T00:00:00Z", "2026-11-08T04:00:00Z")],
                )
            ],
            as_of="2026-11-07T00:00:00Z",
        )
        decision = evaluate_bundle(bundle).decisions[0]
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.used_duration, hours(2))
    def test_as_of_freezes_past_decision(self):
        # 交错场景：同一输入在不同考核时点结论不同。
        # as_of=12:00 时已开放 4h（08:00 起），1h 新申请超 4h 额度被阻止；
        # as_of=10:00 时只开放 2h，同样的申请放行。
        ex = exemption(
            "EX-1",
            start="2026-11-05T00:00:00Z",
            end="2026-11-09T00:00:00Z",
        )
        events = [
            event("E1", "EX-1", "OPEN", "2026-11-05T08:00:00Z"),
            event("E2", "EX-1", "CLOSE", "2026-11-05T16:00:00Z"),
        ]
        app = application(
            "APP-1",
            intervals=[iv("2026-11-06T00:00:00Z", "2026-11-06T01:00:00Z")],
        )
        noon = make_bundle(
            quotas=[quota(duration="PT4H")],
            exemptions=[ex],
            events=events,
            applications=[app],
            as_of="2026-11-05T12:00:00Z",
        )
        ten = make_bundle(
            quotas=[quota(duration="PT4H")],
            exemptions=[ex],
            events=events,
            applications=[app],
            as_of="2026-11-05T10:00:00Z",
        )
        self.assertFalse(evaluate_bundle(noon).decisions[0].allowed)
        decision_ten = evaluate_bundle(ten).decisions[0]
        self.assertTrue(decision_ten.allowed)
        self.assertEqual(decision_ten.used_duration, hours(2))

    def test_projection_outside_window_is_clipped(self):
        # 预计开放区间超出窗口的部分不参与扣减：10h 申请只有 6h 落在窗口内。
        bundle = make_bundle(
            quotas=[quota(duration="PT7H")],
            applications=[
                application(
                    "APP-1",
                    intervals=[
                        iv(
                            "2026-11-10T18:00:00Z",
                            "2026-11-11T04:00:00Z",
                        )
                    ],
                )
            ],
        )
        decision = evaluate_bundle(bundle).decisions[0]
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.projected_duration, hours(6))
        self.assertEqual(
            decision.projected_inside_window,
            [iv("2026-11-10T18:00:00Z", "2026-11-11T00:00:00Z")],
        )

    def test_projection_completely_outside_window_blocked(self):
        bundle = make_bundle(
            quotas=[quota(duration="PT3H")],
            applications=[
                application(
                    "APP-1",
                    intervals=[
                        iv("2026-11-12T00:00:00Z", "2026-11-12T01:00:00Z")
                    ],
                )
            ],
        )
        decision = evaluate_bundle(bundle).decisions[0]
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, REASON_PROJECTION_EMPTY)


class TestDefensiveRejections(unittest.TestCase):
    def test_environment_out_of_scope_blocked(self):
        bundle = make_bundle(
            environments=("prod",),
            quotas=[quota(env_id="prod")],
            applications=[
                application(
                    "APP-1",
                    env_id="dr-site",
                    intervals=[iv("2026-11-06T00:00:00Z", "2026-11-06T01:00:00Z")],
                )
            ],
        )
        decision = evaluate_bundle(bundle).decisions[0]
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, REASON_OUT_OF_SCOPE)

    def test_missing_quota_blocked(self):
        bundle = make_bundle(
            quotas=[],
            applications=[
                application(
                    "APP-1",
                    intervals=[iv("2026-11-06T00:00:00Z", "2026-11-06T01:00:00Z")],
                )
            ],
        )
        decision = evaluate_bundle(bundle).decisions[0]
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, REASON_QUOTA_MISSING)

    def test_quota_is_per_environment(self):
        # 不同环境的同编号控制项不共享额度。
        ex_prod = exemption("EX-P", env_id="prod")
        ex_stage = exemption("EX-S", env_id="staging")
        events = [
            event("EP1", "EX-P", "OPEN", "2026-11-02T00:00:00Z", env_id="prod"),
            event("EP2", "EX-P", "CLOSE", "2026-11-02T03:00:00Z", env_id="prod"),
            event("ES1", "EX-S", "OPEN", "2026-11-02T00:00:00Z", env_id="staging"),
            event("ES2", "EX-S", "CLOSE", "2026-11-02T03:00:00Z", env_id="staging"),
        ]
        bundle = make_bundle(
            environments=("prod", "staging"),
            quotas=[
                quota(env_id="prod", control_id="C-01", duration="PT5H"),
                quota(env_id="staging", control_id="C-01", duration="PT5H"),
            ],
            exemptions=[ex_prod, ex_stage],
            events=events,
            applications=[
                application(
                    "APP-P",
                    env_id="prod",
                    control_id="C-01",
                    intervals=[iv("2026-11-06T00:00:00Z", "2026-11-06T02:00:00Z")],
                ),
                application(
                    "APP-S",
                    env_id="staging",
                    control_id="C-01",
                    intervals=[iv("2026-11-06T00:00:00Z", "2026-11-06T02:00:00Z")],
                ),
            ],
            as_of="2026-11-05T00:00:00Z",
        )
        decisions = {d.application_id: d for d in evaluate_bundle(bundle).decisions}
        self.assertTrue(decisions["APP-P"].allowed)
        self.assertTrue(decisions["APP-S"].allowed)


if __name__ == "__main__":
    unittest.main()
