"""离线对账核心逻辑单元测试。每个用例带稳定 case_id 注释。"""
import json
import random
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reconcile import ManifestError, reconcile, validate_records


def coll(seqs, tenant="t1", shard="s0", status="received"):
    return [
        {"tenant": tenant, "shard": shard, "seq": s, "digest": f"d{s:03d}", "status": status}
        for s in seqs
    ]


def arch(seqs, tenant="t1", shard="s0", status="persisted", digests=None):
    records = []
    for s in seqs:
        digest = (digests or {}).get(s, f"d{s:03d}")
        records.append(
            {"tenant": tenant, "shard": shard, "seq": s, "digest": digest, "status": status}
        )
    return records


def only_group(report):
    assert len(report["groups"]) == 1, report
    return report["groups"][0]


class WatermarkTests(unittest.TestCase):
    def test_case_wm_001_watermark_blocked_by_digest_mismatch(self):
        """CASE-WM-001: 水位线被摘要不一致阻断，停在前一个已确认序号。"""
        report = reconcile(coll([1, 2, 3, 4, 5]), arch([1, 2, 3, 4, 5], digests={3: "bad"}))
        group = only_group(report)
        self.assertEqual(group["watermark"], 2)
        self.assertEqual([m["seq"] for m in group["digest_mismatches"]], [3])

    def test_case_wm_002_watermark_blocked_by_missing_gap(self):
        """CASE-WM-002: 水位线被缺失缺口阻断，缺口后的已确认记录不推进水位。"""
        report = reconcile(coll([1, 2, 3, 4, 5]), arch([1, 2, 4, 5]))
        group = only_group(report)
        self.assertEqual(group["watermark"], 2)
        self.assertEqual(group["gaps"], [{"start": 3, "end": 3}])

    def test_case_wm_003_start_not_confirmed(self):
        """CASE-WM-003: 起始序号未确认时水位线为 start_seq - 1。"""
        report = reconcile(coll([1, 2, 3]), arch([2, 3]))
        group = only_group(report)
        self.assertEqual(group["start_seq"], 1)
        self.assertEqual(group["watermark"], 0)

    def test_case_wm_004_all_confirmed(self):
        """CASE-WM-004: 全部已确认时水位线推进到最大序号。"""
        report = reconcile(coll([1, 2, 3, 4]), arch([1, 2, 3, 4]))
        group = only_group(report)
        self.assertEqual(group["watermark"], 4)
        self.assertEqual(group["gaps"], [])
        self.assertEqual(group["retransmit_plan"], [])

    def test_case_wm_005_pending_status_not_confirmed(self):
        """CASE-WM-005: 归档端 status 非 persisted 的记录不计入水位线。"""
        report = reconcile(coll([1, 2, 3]), arch([1, 2, 3], status="pending"))
        group = only_group(report)
        self.assertEqual(group["watermark"], 0)
        self.assertEqual(group["gaps"], [{"start": 1, "end": 3}])


class GapTests(unittest.TestCase):
    def test_case_gap_001_contiguous_gaps_merged(self):
        """CASE-GAP-001: 连续缺失合并为一个区间，不相邻缺失分成两段。"""
        report = reconcile(coll([1, 2, 3, 4, 5, 6, 7, 8]), arch([1, 2, 6, 7]))
        group = only_group(report)
        self.assertEqual(group["gaps"], [{"start": 3, "end": 5}, {"start": 8, "end": 8}])

    def test_case_gap_002_mismatch_not_double_counted_as_gap(self):
        """CASE-GAP-002: 摘要不一致的序号只进不一致清单，不重复计入缺口。"""
        report = reconcile(coll([1, 2, 3]), arch([1, 2, 3], digests={2: "bad"}))
        group = only_group(report)
        self.assertEqual(group["gaps"], [])
        self.assertEqual([m["seq"] for m in group["digest_mismatches"]], [2])


class MismatchTests(unittest.TestCase):
    def test_case_mism_001_mismatch_fields(self):
        """CASE-MISM-001: 摘要不一致记录携带双方摘要与归档端状态。"""
        report = reconcile(coll([1, 2]), arch([1, 2], digests={2: "corrupted"}))
        group = only_group(report)
        self.assertEqual(
            group["digest_mismatches"],
            [
                {
                    "seq": 2,
                    "collection_digest": "d002",
                    "archive_digest": "corrupted",
                    "archive_status": "persisted",
                }
            ],
        )


class ExtraTests(unittest.TestCase):
    def test_case_extra_001_archive_extras(self):
        """CASE-EXTRA-001: 归档端多出的记录按序号升序列出。"""
        report = reconcile(coll([1, 2]), arch([1, 2, 5, 9]))
        group = only_group(report)
        self.assertEqual([e["seq"] for e in group["archive_extras"]], [5, 9])

    def test_case_extra_002_unknown_group_all_extra(self):
        """CASE-EXTRA-002: 采集端没有的 (租户,分片) 组整体记为多出，水位为 None。"""
        report = reconcile(coll([1], tenant="t1"), arch([7], tenant="t2"))
        self.assertEqual(len(report["groups"]), 2)
        extra_group = [g for g in report["groups"] if g["tenant"] == "t2"][0]
        self.assertIsNone(extra_group["watermark"])
        self.assertEqual([e["seq"] for e in extra_group["archive_extras"]], [7])
        self.assertEqual(extra_group["retransmit_plan"], [])


class StatusTests(unittest.TestCase):
    def test_case_status_001_non_received_collection_record_not_expected(self):
        """CASE-STATUS-001: 采集端非 received 状态记录不纳入期望，归档端对应记录记为多出。"""
        collection = coll([1, 2])
        collection[1]["status"] = "dropped"
        report = reconcile(collection, arch([1, 2]))
        group = only_group(report)
        self.assertEqual(group["start_seq"], 1)
        self.assertEqual(group["watermark"], 1)
        self.assertEqual([e["seq"] for e in group["archive_extras"]], [2])


class PlanTests(unittest.TestCase):
    def test_case_plan_001_plan_keeps_original_seq_and_digest(self):
        """CASE-PLAN-001: 补传计划保持原序号与采集端原摘要，按序号升序。"""
        report = reconcile(coll([1, 2, 3, 4]), arch([1], digests={}))
        group = only_group(report)
        plan = group["retransmit_plan"]
        self.assertEqual([p["seq"] for p in plan], [2, 3, 4])
        self.assertEqual([p["digest"] for p in plan], ["d002", "d003", "d004"])
        self.assertTrue(all(p["reason"] == "missing" for p in plan))

    def test_case_plan_002_mismatch_in_plan_uses_collection_digest(self):
        """CASE-PLAN-002: 摘要不一致的补传项使用采集端摘要并标注原因。"""
        report = reconcile(coll([1, 2]), arch([1, 2], digests={2: "bad"}))
        group = only_group(report)
        self.assertEqual(
            [(p["seq"], p["digest"], p["reason"]) for p in group["retransmit_plan"]],
            [(2, "d002", "digest_mismatch")],
        )

    def test_case_det_001_same_input_same_plan_regardless_of_order(self):
        """CASE-DET-001: 输入记录顺序打乱后报告完全一致（确定性）。"""
        collection = coll(list(range(1, 11)))
        archive = arch([1, 2, 3, 6, 8, 10], digests={6: "bad"})
        baseline = json.dumps(reconcile(collection, archive), sort_keys=True)
        rng = random.Random(20261114)
        for _ in range(5):
            shuffled_c = collection[:]
            shuffled_a = archive[:]
            rng.shuffle(shuffled_c)
            rng.shuffle(shuffled_a)
            self.assertEqual(
                json.dumps(reconcile(shuffled_c, shuffled_a), sort_keys=True), baseline
            )


class ValidationTests(unittest.TestCase):
    def test_case_dup_001_duplicate_seq_rejected(self):
        """CASE-DUP-001: 同一 (租户,分片,序号) 重复出现视为非法输入。"""
        records = coll([1, 1])
        with self.assertRaises(ManifestError):
            validate_records(records, "采集端")

    def test_case_val_001_missing_field_rejected(self):
        """CASE-VAL-001: 缺少必填字段的记录被拒绝。"""
        with self.assertRaises(ManifestError):
            validate_records([{"tenant": "t", "shard": "s", "seq": 1, "status": "received"}], "采集端")

    def test_case_val_002_bad_seq_rejected(self):
        """CASE-VAL-002: 负数或布尔序号被拒绝。"""
        with self.assertRaises(ManifestError):
            validate_records(
                [{"tenant": "t", "shard": "s", "seq": -1, "digest": "d", "status": "received"}],
                "采集端",
            )
        with self.assertRaises(ManifestError):
            validate_records(
                [{"tenant": "t", "shard": "s", "seq": True, "digest": "d", "status": "received"}],
                "采集端",
            )


class EmptyTests(unittest.TestCase):
    def test_case_empty_001_both_empty(self):
        """CASE-EMPTY-001: 双方清单都为空时报告没有组。"""
        self.assertEqual(reconcile([], []), {"groups": []})

    def test_case_empty_002_empty_archive(self):
        """CASE-EMPTY-002: 归档端为空时全部期望序号进入缺口与补传计划。"""
        report = reconcile(coll([1, 2, 3]), [])
        group = only_group(report)
        self.assertEqual(group["watermark"], 0)
        self.assertEqual(group["gaps"], [{"start": 1, "end": 3}])
        self.assertEqual([p["seq"] for p in group["retransmit_plan"]], [1, 2, 3])


class CliTests(unittest.TestCase):
    def test_case_cli_001_sample_end_to_end(self):
        """CASE-CLI-001: CLI 跑本题样例，报告与期望文件完全一致，水位停在 2。"""
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "app.py"),
                "--collection",
                str(ROOT / "samples" / "collection.json"),
                "--archive",
                str(ROOT / "samples" / "archive.json"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        expected = json.loads((ROOT / "samples" / "expected_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report, expected)
        group = only_group(report)
        self.assertEqual(group["watermark"], 2)
        self.assertEqual(group["gaps"], [{"start": 4, "end": 5}, {"start": 7, "end": 7}])
        self.assertEqual([m["seq"] for m in group["digest_mismatches"]], [3])
        self.assertEqual([e["seq"] for e in group["archive_extras"]], [9])

    def test_case_cli_002_bad_input_exit_code(self):
        """CASE-CLI-002: 非法输入时 CLI 以退出码 2 报错。"""
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "app.py"),
                "--collection",
                str(ROOT / "samples" / "collection.json"),
                "--archive",
                str(ROOT / "samples" / "no-such-file.json"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("输入错误", proc.stderr)


if __name__ == "__main__":
    unittest.main()
