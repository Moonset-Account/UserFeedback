"""实例身份与存储语义：UID + RECURRENCE-ID 身份、例外修改后不残留旧实例。"""

from collector.store import JsonStore
from tests import CollectorTestCase


class TestIdentityAndStorage(CollectorTestCase):
    def test_identity_is_uid_plus_recurrence_id_not_start_time(self):
        v1 = self.collect_preset("modify_override_v1")
        truth1 = self.load_truth("modify_override_v1")

        moved1 = next(i for i in v1["instances"] if i["origin"] == "override_move")
        self.assertEqual(moved1["instance_id"],
                         "fictional-mountain-modify-demo::float:2026-11-16T19:00:00")
        self.assertEqual(moved1["start"], truth1["moved_instance_start"])

        # 同 UID 下实例身份唯一，数量等于 COUNT=5
        ids = [i["instance_id"] for i in v1["instances"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 5)

    def test_recollect_replaces_old_exception_without_residue(self):
        store = JsonStore(self.__class__.__module__)  # 临时目录名仅用于隔离
        store_dir = store.base
        try:
            v1 = self.collect_preset("modify_override_v1", target_name="modify_demo")
            store.save("modify_demo", v1)
            v2 = self.collect_preset("modify_override_v2", target_name="modify_demo")
            store.save("modify_demo", v2)

            # 存储中只有最新一次结果（整体替换）
            latest = store.load_target("modify_demo")
            self.assertEqual(latest["run_id"], v2["run_id"])

            truth2 = self.load_truth("modify_override_v2")
            moved2 = next(i for i in latest["instances"]
                          if i["origin"] == "override_move")
            self.assertEqual(moved2["start"], truth2["moved_instance_start"])
            self.assertEqual(moved2["recurrence_id"],
                             truth2["moved_recurrence_id"])

            all_starts = [i["start"] for i in latest["instances"]]
            for forbidden in truth2["forbidden_starts"]:
                self.assertNotIn(forbidden, all_starts,
                                 "修改例外后旧实例不得残留")
            # 旧的周一 11-16 常规实例同样不得残留（已被例外接管）
            self.assertNotIn("2026-11-16T19:00:00", all_starts)
            self.assertEqual(len(latest["instances"]), 5)
        finally:
            import shutil
            shutil.rmtree(store_dir, ignore_errors=True)

    def test_same_start_different_uid_both_kept(self):
        """不同 UID 即使开始时间相同也都保留——不能只按开始时间去重。"""
        ics = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            "PRODID:-//Test//Identity//ZH\r\n"
            "BEGIN:VEVENT\r\nUID:uid-a\r\nSUMMARY:A\r\n"
            "DTSTAMP:20261001T000000Z\r\n"
            "DTSTART:20261110T180000\r\nDTEND:20261110T190000\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:uid-b\r\nSUMMARY:B\r\n"
            "DTSTAMP:20261001T000000Z\r\n"
            "DTSTART:20261110T180000\r\nDTEND:20261110T190000\r\nEND:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        from pathlib import Path
        import tempfile
        from collector.service import RunRequest, collect

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "same_start.ics"
            path.write_text(ics, encoding="utf-8")
            result = collect(RunRequest(
                source=str(path),
                range_start="2026-11-01",
                range_end="2026-12-01",
            ))
        self.assertEqual(len(result["instances"]), 2)
        self.assertEqual({i["uid"] for i in result["instances"]},
                         {"uid-a", "uid-b"})
