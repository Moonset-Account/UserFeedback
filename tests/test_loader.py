"""JSON 加载器校验测试与示例数据端到端判定。"""

import json
import unittest
from pathlib import Path

from release_gate import (
    REASON_QUOTA_EXCEEDED,
    evaluate_bundle,
    load_bundle,
    load_bundle_file,
)
from release_gate.intervals import parse_duration

from factory import hours

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "bundle.json"


def minimal_bundle_dict():
    return {
        "window": {
            "start": "2026-11-01T00:00:00Z",
            "end": "2026-11-11T00:00:00Z",
        },
        "as_of": "2026-11-05T12:00:00Z",
        "environments": ["prod"],
        "quotas": [
            {"env_id": "prod", "control_id": "C-01", "max_open_duration": "PT4H"}
        ],
        "exemptions": [
            {
                "exemption_id": "EX-1",
                "env_id": "prod",
                "control_id": "C-01",
                "effective_start": "2026-11-02T00:00:00Z",
                "effective_end": "2026-11-09T00:00:00Z",
            }
        ],
        "events": [
            {
                "event_id": "EV-1",
                "exemption_id": "EX-1",
                "env_id": "prod",
                "control_id": "C-01",
                "action": "open",
                "occurred_at": "2026-11-02T00:00:00Z",
            },
            {
                "event_id": "EV-2",
                "exemption_id": "EX-1",
                "env_id": "prod",
                "control_id": "C-01",
                "action": "CLOSE",
                "occurred_at": "2026-11-02T02:00:00Z",
            },
        ],
        "applications": [
            {
                "application_id": "APP-1",
                "env_id": "prod",
                "control_id": "C-01",
                "projected_intervals": [
                    ["2026-11-06T00:00:00Z", "2026-11-06T01:00:00Z"]
                ],
            }
        ],
    }


class TestLoader(unittest.TestCase):
    def test_load_minimal_bundle_normalizes_action_and_array_interval(self):
        bundle = load_bundle(minimal_bundle_dict())
        self.assertEqual(bundle.events[0].action, "OPEN")  # 小写动作被规范化
        self.assertEqual(
            bundle.applications[0].projected_intervals[0].end.isoformat(),
            "2026-11-06T01:00:00+00:00",
        )
        self.assertEqual(bundle.quotas[0].max_open_duration, parse_duration("PT4H"))

    def test_event_referencing_unknown_exemption_rejected(self):
        data = minimal_bundle_dict()
        data["events"][0]["exemption_id"] = "EX-GHOST"
        with self.assertRaises(ValueError):
            load_bundle(data)

    def test_event_env_mismatch_rejected(self):
        data = minimal_bundle_dict()
        data["events"][0]["env_id"] = "staging"
        with self.assertRaises(ValueError):
            load_bundle(data)

    def test_quota_out_of_scope_env_rejected(self):
        data = minimal_bundle_dict()
        data["quotas"][0]["env_id"] = "dr-site"
        with self.assertRaises(ValueError):
            load_bundle(data)

    def test_duplicate_exemption_id_rejected(self):
        data = minimal_bundle_dict()
        data["exemptions"].append(dict(data["exemptions"][0]))
        with self.assertRaises(ValueError):
            load_bundle(data)

    def test_missing_required_field_rejected(self):
        data = minimal_bundle_dict()
        del data["window"]
        with self.assertRaises(ValueError):
            load_bundle(data)

    def test_unknown_action_rejected(self):
        data = minimal_bundle_dict()
        data["events"][0]["action"] = "DESTROY"
        with self.assertRaises(ValueError):
            load_bundle(data)

    def test_example_file_end_to_end(self):
        bundle = load_bundle_file(EXAMPLE)
        evaluation = evaluate_bundle(bundle)
        payload = evaluation.to_dict()
        # 序列化结果必须是 JSON 可往返的。
        json.dumps(payload, ensure_ascii=False)
        decisions = {d.application_id: d for d in evaluation.decisions}
        # EX-1 实际开放 4h、EX-2 开放 2h、EX-3 截至 as_of 开放 4h，
        # 并集 10h；额度 12h。
        self.assertEqual(decisions["APP-OK"].used_duration, hours(10))
        self.assertTrue(decisions["APP-OK"].allowed)
        over = decisions["APP-OVER"]
        self.assertFalse(over.allowed)
        self.assertEqual(over.reason, REASON_QUOTA_EXCEEDED)
        # 批次内 APP-OK 先放行并占用 1h：轮到 APP-OVER 时已用 11h、额度 12h，
        # 仅剩 1h 预算，故 3h 申请的超量段从 01:00 开始（共 2h）。
        self.assertEqual(
            over.overage_segments[0].start.isoformat(),
            "2026-11-06T01:00:00+00:00",
        )
        self.assertEqual(
            over.overage_segments[0].end.isoformat(),
            "2026-11-06T03:00:00+00:00",
        )


if __name__ == "__main__":
    unittest.main()
