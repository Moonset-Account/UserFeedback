"""协议子集负面清单与边界规则的补充断言。

每个用例直接构造内存中的 ICS 文本（仍走公开 collect 入口），
验证不支持的规则被结构化报告、支持的规则正确展开。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from collector.service import RunRequest, collect


def _run_ics(ics: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "case.ics"
        path.write_text(ics, encoding="utf-8")
        return collect(RunRequest(
            source=str(path),
            range_start="2026-11-01",
            range_end="2027-01-15",
        ))


def _wrap(vevent_body: str) -> str:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "PRODID:-//Test//Subset//ZH\r\n" + vevent_body + "END:VCALENDAR\r\n"
    )


class TestUnsupportedSubset(unittest.TestCase):
    def test_multiple_rrules_reported(self):
        ics = _wrap(
            "BEGIN:VEVENT\r\nUID:multi\r\nSUMMARY:X\r\nDTSTAMP:20261001T000000Z\r\n"
            "DTSTART:20261102T100000\r\n"
            "RRULE:FREQ=WEEKLY\r\nRRULE:FREQ=DAILY\r\nEND:VEVENT\r\n"
        )
        result = _run_ics(ics)
        self.assertIn("RRULE_MULTIPLE", [i["code"] for i in result["issues"]])
        self.assertEqual(result["instances"], [])

    def test_count_and_until_together_rejected(self):
        ics = _wrap(
            "BEGIN:VEVENT\r\nUID:both\r\nSUMMARY:X\r\nDTSTAMP:20261001T000000Z\r\n"
            "DTSTART:20261102T100000\r\n"
            "RRULE:FREQ=DAILY;COUNT=3;UNTIL=20261201T100000\r\nEND:VEVENT\r\n"
        )
        result = _run_ics(ics)
        self.assertIn("RRULE_COUNT_UNTIL_BOTH", [i["code"] for i in result["issues"]])

    def test_minutely_and_secondly_reported(self):
        for freq, code in (("MINUTELY", "RRULE_MINUTELY"),
                           ("SECONDLY", "RRULE_SECONDLY")):
            ics = _wrap(
                "BEGIN:VEVENT\r\nUID:f\r\nSUMMARY:X\r\nDTSTAMP:20261001T000000Z\r\n"
                f"DTSTART:20261102T100000Z\r\nRRULE:FREQ={freq};COUNT=5\r\nEND:VEVENT\r\n"
            )
            result = _run_ics(ics)
            self.assertIn(code, [i["code"] for i in result["issues"]])

    def test_byweekno_reported(self):
        ics = _wrap(
            "BEGIN:VEVENT\r\nUID:wk\r\nSUMMARY:X\r\nDTSTAMP:20261001T000000Z\r\n"
            "DTSTART;VALUE=DATE:20261102\r\nRRULE:FREQ=YEARLY;BYWEEKNO=20\r\nEND:VEVENT\r\n"
        )
        result = _run_ics(ics)
        self.assertIn("RRULE_BY_WEEK_NO", [i["code"] for i in result["issues"]])

    def test_recurrence_id_range_param_reported(self):
        ics = _wrap(
            "BEGIN:VEVENT\r\nUID:rid-range\r\nSUMMARY:X\r\nDTSTAMP:20261001T000000Z\r\n"
            "DTSTART:20261102T100000\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:rid-range\r\nSUMMARY:X2\r\nDTSTAMP:20261001T000000Z\r\n"
            "DTSTART:20261103T100000\r\n"
            "RECURRENCE-ID;RANGE=THISANDPRIOR:20261109T100000\r\nSEQUENCE:1\r\nEND:VEVENT\r\n"
        )
        result = _run_ics(ics)
        self.assertIn("RECURRENCE_ID_RANGE",
                      [i["code"] for i in result["issues"]])


class TestSupportedRules(unittest.TestCase):
    def test_monthly_byday_with_ordinal(self):
        # 每月最后一个周五，区间内 11/27 与 12/25
        ics = _wrap(
            "BEGIN:VEVENT\r\nUID:last-friday\r\nSUMMARY:X\r\nDTSTAMP:20261001T000000Z\r\n"
            "DTSTART;VALUE=DATE:20260130\r\n"
            "RRULE:FREQ=MONTHLY;BYDAY=-1FR;UNTIL=20270101\r\nEND:VEVENT\r\n"
        )
        result = _run_ics(ics)
        starts = [i["start"] for i in result["instances"]]
        self.assertIn("2026-11-27", starts)
        self.assertIn("2026-12-25", starts)
        self.assertTrue(all(i["all_day"] for i in result["instances"]))

    def test_count_bounded_expansion(self):
        ics = _wrap(
            "BEGIN:VEVENT\r\nUID:cnt\r\nSUMMARY:X\r\nDTSTAMP:20261001T000000Z\r\n"
            "DTSTART:20261102T080000\r\nDTEND:20261102T090000\r\n"
            "RRULE:FREQ=DAILY;COUNT=4\r\nEND:VEVENT\r\n"
        )
        result = _run_ics(ics)
        self.assertEqual(len(result["instances"]), 4)
        self.assertEqual(result["instances"][0]["start"], "2026-11-02T08:00:00")
        self.assertEqual(result["instances"][-1]["start"], "2026-11-05T08:00:00")

    def test_multiday_duration_carries_to_each_instance(self):
        ics = _wrap(
            "BEGIN:VEVENT\r\nUID:dur\r\nSUMMARY:X\r\nDTSTAMP:20261001T000000Z\r\n"
            "DTSTART:20261102T220000\r\nDTEND:20261103T010000\r\n"
            "RRULE:FREQ=WEEKLY;COUNT=2\r\nEND:VEVENT\r\n"
        )
        result = _run_ics(ics)
        for inst in result["instances"]:
            self.assertEqual(inst["duration"], {"seconds": 3 * 3600})
        # 跨日实例的结束日期在次日
        self.assertEqual(result["instances"][0]["end"], "2026-11-03T01:00:00")

    def test_range_limit_issue_when_cap_exceeded(self):
        ics = _wrap(
            "BEGIN:VEVENT\r\nUID:cap\r\nSUMMARY:X\r\nDTSTAMP:20261001T000000Z\r\n"
            "DTSTART:20261101T000000Z\r\nRRULE:FREQ=DAILY\r\nEND:VEVENT\r\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cap.ics"
            path.write_text(ics, encoding="utf-8")
            result = collect(RunRequest(
                source=str(path), range_start="2026-11-01",
                range_end="2027-06-01", max_instances=10,
            ))
        self.assertIn("RANGE_LIMIT", [i["code"] for i in result["issues"]])


if __name__ == "__main__":
    unittest.main()
