"""核心展开断言：RRULE/RDATE/EXDATE、RECURRENCE-ID、全日/跨日/浮动/时区。

期望值来自 samples/generator.py 生成的独立真值（generated/truth），
采集结果只能通过 collect() 公开入口取得。
"""

from tests import CollectorTestCase


class TestNormalScenario(CollectorTestCase):
    def test_demo_normal_matches_truth(self):
        result = self.collect_preset("demo_normal")
        truth = self.load_truth("demo_normal")

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["instances"]), truth["expect_count"])

        by_rid = self.index_instances(result)
        for expected in truth["instances"]:
            inst = by_rid.get(
                f"fictional-planetarium-weekly-2026::{expected['recurrence_id']}")
            self.assertIsNotNone(inst, f"缺少实例 {expected['recurrence_id']}")
            self.assertEqual(inst["start"], expected["start"])
            self.assertEqual(inst["origin"], expected["origin"])
            self.assertEqual(inst["status"], expected["status"])
            self.assertEqual(inst["floating"], expected["floating"])
            self.assertIsNone(inst["start_utc"], "浮动时间不得伪造 UTC")

    def test_rdate_is_distinct_from_master(self):
        result = self.collect_preset("demo_normal")
        rdate = [i for i in result["instances"] if i["origin"] == "rdate"]
        self.assertEqual(len(rdate), 1)
        self.assertEqual(rdate[0]["start"], "2026-09-16T19:30:00")
        # 时长随主事件 DTEND（跨日时长语义同样适用）
        self.assertEqual(rdate[0]["end"], "2026-09-16T21:30:00")
        self.assertEqual(rdate[0]["duration"], {"seconds": 7200})


class TestBoundaryScenario(CollectorTestCase):
    def test_cancel_move_dst_and_until_forever(self):
        result = self.collect_preset("demo_boundary")
        truth = self.load_truth("demo_boundary")
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["instances"]), truth["expect_count"])

        by_rid = self.index_instances(result)

        # 1) 取消单次窗口：CANCELLED 实例保留，且没有同身份的常规实例残留
        cancelled = by_rid[
            "fictional-hilltop-stargazing-forever::dt:2026-11-07T20:00:00|off:-300"]
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(cancelled["origin"], "override_cancel")

        # 2) 移动一次窗口：身份仍是原 RECURRENCE-ID，开始时间变为周五
        moved = by_rid[
            "fictional-hilltop-stargazing-forever::dt:2026-11-14T20:00:00|off:-300"]
        self.assertEqual(moved["origin"], "override_move")
        self.assertEqual(moved["start"], "2026-11-13T19:00:00")
        self.assertEqual(moved["end"], "2026-11-13T22:00:00")
        # 旧的周六开始时间不得作为另一实例残留
        self.assertFalse(
            any(i["start"] == "2026-11-14T20:00:00" for i in result["instances"]))

        # 3) EXDATE 留证
        excluded_keys = {e["recurrence_id"] for e in result["excluded"]}
        self.assertIn("dt:2026-10-31T20:00:00|off:-240", excluded_keys)
        self.assertFalse(
            any(i["start"] == "2026-10-31T20:00:00" for i in result["instances"]))

        # 4) 夏令时：墙上时间保持 20:00，UTC 偏移随 DST 改变
        before = next(i for i in result["instances"]
                      if i["start"] == "2026-10-24T20:00:00")
        after = next(i for i in result["instances"]
                     if i["start"] == "2026-11-21T20:00:00")
        self.assertEqual(before["start_utc"], "2026-10-25T00:00:00+00:00")
        self.assertEqual(after["start_utc"], "2026-11-22T01:00:00+00:00")

        # 5) 无终止重复：受查询范围约束而有限展开
        starts = [i["start"] for i in result["instances"]]
        self.assertIn("2026-12-05T20:00:00", starts)  # 范围内最后一个周六
        self.assertNotIn("2026-12-12T20:00:00", starts)  # 范围外不展开

    def test_range_limit_is_reported(self):
        from collector.expand import DEFAULT_MAX_INSTANCES
        result = self.collect_preset("demo_boundary")
        # 小范围不应触发上限
        self.assertNotIn("RANGE_LIMIT", [i["code"] for i in result["issues"]])
        self.assertLessEqual(result["stats"]["in_range"], DEFAULT_MAX_INSTANCES)


class TestAllDayFloatingAndDuration(CollectorTestCase):
    def test_acceptance_special_time_kinds(self):
        result = self.collect_preset("acceptance_full")
        truth = self.load_truth("acceptance_full")
        by_uid: dict[str, list[dict]] = {}
        for inst in result["instances"]:
            by_uid.setdefault(inst["uid"], []).append(inst)

        # 全日 + 跨天：DTEND 半开，11/14 → 11/17 为 3 天
        workshop = by_uid[truth["all_day_instance"]["uid"]]
        first = workshop[0]
        self.assertTrue(first["all_day"])
        self.assertEqual(first["start"], "2026-11-14")
        self.assertEqual(first["end"], "2026-11-17")
        self.assertEqual(first["duration"], {"days": 3})
        # COUNT=2 的第二个月实例
        self.assertEqual(workshop[1]["start"], "2026-12-14")

        # 浮动时间：保持 naive，无 UTC 字段
        floating = by_uid[truth["floating_instance"]["uid"]]
        self.assertEqual(floating[0]["start"], "2026-12-01T06:30:00")
        self.assertTrue(floating[0]["floating"])
        self.assertIsNone(floating[0]["start_utc"])

        # 不支持的 BYYEARDAY 必须被报告，而不是静默处理
        codes = [i["code"] for i in result["issues"]]
        for code in truth["expect_issue_codes"]:
            self.assertIn(code, codes)
        # 含不支持规则的事件不产生实例
        self.assertNotIn("fictional-old-horology-byearday", by_uid)


class TestUnsupportedAndMalformed(CollectorTestCase):
    def test_unsupported_rules_reported(self):
        result = self.collect_preset("unsupported_rules")
        truth = self.load_truth("unsupported_rules")
        codes = {i["code"] for i in result["issues"]}
        for code in truth["expect_issue_codes"]:
            self.assertIn(code, codes)
        self.assertEqual(len(result["instances"]), 0)
        # 问题必须带行号证据
        for issue in result["issues"]:
            self.assertIsNotNone(issue["line"])

    def test_malformed_event_skipped_but_valid_one_kept(self):
        result = self.collect_preset("malformed")
        truth = self.load_truth("malformed")
        codes = [i["code"] for i in result["issues"]]
        self.assertIn("INVALID_CALENDAR", codes)
        self.assertEqual(len(result["instances"]), truth["expect_count"])
        self.assertEqual(result["instances"][0]["uid"],
                         truth["instances"][0]["uid"])
