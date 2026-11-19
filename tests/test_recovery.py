"""只读恢复判定的验收测试（unittest）。

覆盖：
  - 可接受结果：归属连续 + 采集成功的完整恢复（含末簇裁切）；
  - 应被阻止的结果：删除后再分配（内容碰巧相同也拒绝）、记录缺口、
    改写、采集缺失/错误/短读、映射缺失；
  - 边界与交错：时间窗边界、簇内多事件交错切分、多原因优先级、
    簇链短于需求、同簇邻接区间一成一拒。
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

# 保证从仓库根目录直接运行也能导入被测包。
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from readonly_forensics import (  # noqa: E402
    ACQUISITION_ERROR,
    ACQUISITION_MISSING,
    ACQUISITION_SHORT,
    Allocation,
    Chunk,
    MAPPING_MISSING,
    OVERWRITTEN,
    REALLOCATED,
    RECOVERED,
    RECORD_GAP,
    Overwrite,
    RecordGapEvent,
    RecoveryCase,
    recover_file,
)
from readonly_forensics.cli import main as cli_main  # noqa: E402

CLUSTER = 4096
T0, T1, T2, T3, T4 = 100, 200, 300, 400, 500


def ok_chunk(physical_cluster: int, data: bytes) -> Chunk:
    return Chunk(physical_cluster=physical_cluster, status="ok", data=data)


class BaseCaseMixin:
    """构造默认 3 簇案例的工具，具体测试再覆盖局部字段。"""

    def make_case(self, **overrides) -> RecoveryCase:
        file_size = overrides.pop(
            "file_size", CLUSTER * 2 + 100  # 2 个满簇 + 100 字节末簇
        )
        chain = overrides.pop("cluster_chain", (10, 20, 30))
        chunks = overrides.pop(
            "chunks",
            {
                10: ok_chunk(10, b"A" * CLUSTER),
                20: ok_chunk(20, b"B" * CLUSTER),
                30: ok_chunk(30, b"C" * 100 + b"X" * (CLUSTER - 100)),
            },
        )
        kwargs = dict(
            file_size=file_size,
            cluster_size=CLUSTER,
            deletion_time=T1,
            acquisition_time=T4,
            cluster_chain=chain,
            chunks=chunks,
            allocations=(),
            overwrites=(),
            record_gaps=(),
            file_name="deleted.dat",
        )
        kwargs.update(overrides)
        return RecoveryCase(**kwargs)


class TestAcceptableRecovery(BaseCaseMixin, unittest.TestCase):
    """可接受结果：连续归属 + 采集成功。"""

    def test_full_recovery_with_tail_trim(self):
        report = recover_file(self.make_case())

        self.assertTrue(report.fully_recovered)
        self.assertEqual(report.recovered_bytes, CLUSTER * 2 + 100)
        self.assertEqual(report.missing_bytes, 0)
        # 末簇被裁到 100 字节，总覆盖必须精确等于文件大小。
        self.assertEqual(
            sum(seg.length for seg in report.segments),
            CLUSTER * 2 + 100,
        )
        self.assertEqual(
            [seg.reason for seg in report.segments],
            [RECOVERED, RECOVERED, RECOVERED],
        )

        tail = report.segments[2]
        self.assertEqual(tail.file_offset_start, CLUSTER * 2)
        self.assertEqual(tail.file_offset_end, CLUSTER * 2 + 100)
        self.assertEqual(tail.physical_cluster, 30)
        self.assertEqual(
            (tail.cluster_offset_start, tail.cluster_offset_end), (0, 100)
        )
        self.assertEqual(tail.data, b"C" * 100)

        # materialize 拼出的原字节与各簇预期一致，末簇不含填充 X。
        self.assertEqual(
            report.materialize(),
            b"A" * CLUSTER + b"B" * CLUSTER + b"C" * 100,
        )

    def test_every_segment_is_physically_traceable(self):
        report = recover_file(self.make_case())
        for seg in report.segments:
            self.assertIsNotNone(seg.physical_cluster)
            self.assertGreaterEqual(seg.cluster_offset_start, 0)
            self.assertLessEqual(seg.cluster_offset_end, CLUSTER)
            self.assertTrue(
                any(e.startswith("chain:L") for e in seg.evidence),
                f"区段 {seg.index} 缺少簇链回查证据: {seg.evidence}",
            )
            # 逻辑偏移到物理簇内偏移的换算必须自洽。
            self.assertEqual(
                seg.file_offset_start - seg.logical_cluster * CLUSTER,
                seg.cluster_offset_start,
            )

    def test_exact_file_size_multiple_of_cluster(self):
        # 文件大小恰好是簇整数倍时不存在半空末簇。
        chunks = {
            10: ok_chunk(10, b"A" * CLUSTER),
            20: ok_chunk(20, b"B" * CLUSTER),
        }
        case = self.make_case(
            file_size=CLUSTER * 2,
            cluster_chain=(10, 20),
            chunks=chunks,
        )
        report = recover_file(case)
        self.assertTrue(report.fully_recovered)
        self.assertEqual(len(report.segments), 2)
        self.assertEqual(
            report.materialize(), b"A" * CLUSTER + b"B" * CLUSTER
        )

    def test_zero_size_file_has_no_segments(self):
        # 空文件不占用任何簇：无映射可判，结果为完整“恢复”且无区段。
        case = RecoveryCase(
            file_size=0,
            cluster_size=CLUSTER,
            deletion_time=T1,
            acquisition_time=T4,
            cluster_chain=(),
            chunks={},
        )
        report = recover_file(case)
        self.assertEqual(report.segments, ())
        self.assertEqual(report.materialize(), b"")
        self.assertTrue(report.fully_recovered)
        self.assertEqual(report.recovered_bytes, 0)


class TestReallocationRejection(BaseCaseMixin, unittest.TestCase):
    """应被阻止：再分配即使释放且字节相同也不能作为原文。"""

    def test_reallocated_then_freed_with_identical_bytes(self):
        # 簇 20 在删除后被对象 obj-7 占用，随后在采集前释放；
        # 采集到的字节与原文完全相同——仍然必须拒绝。
        allocation = Allocation(
            physical_cluster=20,
            start_time=T2,
            end_time=T3,
            object_id="obj-7",
            event_id="EV-ALLOC-1",
        )
        case = self.make_case(allocations=(allocation,))
        report = recover_file(case)

        self.assertFalse(report.fully_recovered)
        middle = report.segments[1]
        self.assertEqual(middle.reason, REALLOCATED)
        self.assertIsNone(middle.data)
        self.assertEqual(middle.physical_cluster, 20)
        self.assertTrue(
            any("EV-ALLOC-1" in e and "obj-7" in e for e in middle.evidence)
        )
        self.assertEqual(report.recovered_bytes, CLUSTER + 100)
        self.assertEqual(report.reason_counts[REALLOCATED], CLUSTER)

        # 相邻未受影响簇仍然恢复，形成“一成一拒”交错。
        self.assertEqual(report.segments[0].reason, RECOVERED)
        self.assertEqual(report.segments[2].reason, RECOVERED)
        self.assertEqual(report.materialize(), b"A" * CLUSTER + b"C" * 100)

    def test_reallocation_still_held_at_acquisition(self):
        allocation = Allocation(
            physical_cluster=30,
            start_time=T2,
            end_time=None,  # 到采集时仍占用
            object_id="obj-9",
            event_id="EV-ALLOC-2",
        )
        report = recover_file(self.make_case(allocations=(allocation,)))
        tail = report.segments[-1]
        self.assertEqual(tail.reason, REALLOCATED)
        # 末簇裁切仍生效：只拒绝真实属于文件的 100 字节。
        self.assertEqual(tail.length, 100)
        self.assertEqual(tail.cluster_offset_end, 100)


class TestBoundaryAndInterleaving(BaseCaseMixin, unittest.TestCase):
    """边界与交错场景。"""

    def test_time_window_boundaries(self):
        # 删除前的改写不算（start 恰为 deletion_time 才算窗内）。
        before_delete = Overwrite(
            physical_cluster=10, phy_start=0, phy_end=10,
            time=T0, event_id="EV-OLD",
        )
        at_delete = Overwrite(
            physical_cluster=10, phy_start=0, phy_end=10,
            time=T1, event_id="EV-AT-DEL",
        )
        at_acquire = Overwrite(
            physical_cluster=10, phy_start=10, phy_end=20,
            time=T4, event_id="EV-AT-ACQ",  # 采集时刻（不含）之后，忽略
        )
        case = self.make_case(overwrites=(before_delete, at_delete, at_acquire))
        report = recover_file(case)
        reasons = {
            (seg.cluster_offset_start, seg.reason)
            for seg in report.segments
            if seg.logical_cluster == 0
        }
        self.assertIn((0, OVERWRITTEN), reasons)  # [0,10) 拒绝
        # [10, ...) 仍可恢复；采集时刻的改写不影响结果。
        recovered_pieces = [
            seg
            for seg in report.segments
            if seg.logical_cluster == 0 and seg.reason == RECOVERED
        ]
        self.assertTrue(recovered_pieces)
        self.assertEqual(
            sum(seg.length for seg in recovered_pieces), CLUSTER - 10
        )

    def test_interleaved_overwrite_splits_cluster(self):
        # 簇 10 内窗中改写 [512, 1536)，其余区间仍归属连续。
        overwrite = Overwrite(
            physical_cluster=10,
            phy_start=512,
            phy_end=1536,
            time=T2,
            event_id="EV-OW-SPLIT",
        )
        report = recover_file(self.make_case(overwrites=(overwrite,)))
        cluster0 = [s for s in report.segments if s.logical_cluster == 0]
        self.assertEqual(
            [
                (s.cluster_offset_start, s.cluster_offset_end, s.reason)
                for s in cluster0
            ],
            [
                (0, 512, RECOVERED),
                (512, 1536, OVERWRITTEN),
                (1536, CLUSTER, RECOVERED),
            ],
        )
        self.assertEqual(report.reason_counts[OVERWRITTEN], 1536 - 512)
        # 被拒区间物理坐标可回查，且证据指向具体事件。
        bad = cluster0[1]
        self.assertIsNone(bad.data)
        self.assertTrue(any("EV-OW-SPLIT" in e for e in bad.evidence))

    def test_allocation_outranks_overwrite_and_gap(self):
        # 同一簇同时命中再分配、记录缺口、改写：报告最严重的再分配，
        # 但证据中保留所有相关事件以便回查。
        allocation = Allocation(
            physical_cluster=20, start_time=T2, end_time=T3,
            object_id="obj-x", event_id="EV-A",
        )
        gap = RecordGapEvent(
            physical_cluster=20, start_time=T1, end_time=T2,
            event_id="EV-G",
        )
        overwrite = Overwrite(
            physical_cluster=20, phy_start=0, phy_end=8,
            time=T2, event_id="EV-O",
        )
        report = recover_file(
            self.make_case(
                allocations=(allocation,),
                record_gaps=(gap,),
                overwrites=(overwrite,),
            )
        )
        middle = report.segments[1]
        self.assertEqual(middle.reason, REALLOCATED)
        joined = "|".join(middle.evidence)
        self.assertIn("EV-A", joined)
        self.assertIn("EV-G", joined)
        self.assertIn("EV-O", joined)

    def test_record_gap_rejects_continuity(self):
        gap = RecordGapEvent(
            physical_cluster=20, start_time=T2, end_time=None,
            event_id="EV-GAP-OPEN",
        )
        report = recover_file(self.make_case(record_gaps=(gap,)))
        self.assertEqual(report.segments[1].reason, RECORD_GAP)
        self.assertIsNone(report.segments[1].data)

    def test_gap_ending_before_deletion_is_ignored(self):
        gap = RecordGapEvent(
            physical_cluster=10, start_time=T0 - 50, end_time=T0,
            event_id="EV-GAP-OLD",
        )
        report = recover_file(self.make_case(record_gaps=(gap,)))
        self.assertTrue(report.fully_recovered)

    def test_mapping_missing_when_chain_too_short(self):
        # 簇链只给了前两个簇，末簇逻辑偏移无映射。
        case = self.make_case(cluster_chain=(10, 20))
        report = recover_file(case)
        tail = report.segments[-1]
        self.assertEqual(tail.reason, MAPPING_MISSING)
        self.assertIsNone(tail.physical_cluster)
        self.assertEqual(tail.length, 100)
        self.assertEqual(report.recovered_bytes, CLUSTER * 2)


class TestAcquisitionFailures(BaseCaseMixin, unittest.TestCase):
    """采集层面的拒绝路径。"""

    def test_missing_chunk_entry(self):
        case = self.make_case(
            chunks={
                10: ok_chunk(10, b"A" * CLUSTER),
                30: ok_chunk(30, b"C" * 100),
            }
        )
        report = recover_file(case)
        self.assertEqual(report.segments[1].reason, ACQUISITION_MISSING)

    def test_explicit_missing_status(self):
        case = self.make_case(
            chunks={
                10: ok_chunk(10, b"A" * CLUSTER),
                20: Chunk(20, "missing"),
                30: ok_chunk(30, b"C" * 100),
            }
        )
        report = recover_file(case)
        self.assertEqual(report.segments[1].reason, ACQUISITION_MISSING)

    def test_error_chunk(self):
        case = self.make_case(
            chunks={
                10: ok_chunk(10, b"A" * CLUSTER),
                20: Chunk(20, "error", error="read fault at sector 7"),
                30: ok_chunk(30, b"C" * 100),
            }
        )
        report = recover_file(case)
        seg = report.segments[1]
        self.assertEqual(seg.reason, ACQUISITION_ERROR)
        self.assertIn("read fault at sector 7", seg.detail)

    def test_short_read_only_rejects_uncovered_tail(self):
        # 簇 30 只采到 60 字节，而末簇需要 100 字节。
        case = self.make_case(
            chunks={
                10: ok_chunk(10, b"A" * CLUSTER),
                20: ok_chunk(20, b"B" * CLUSTER),
                30: ok_chunk(30, b"C" * 60),
            }
        )
        report = recover_file(case)
        self.assertEqual(report.segments[-1].reason, ACQUISITION_SHORT)
        self.assertEqual(report.recovered_bytes, CLUSTER * 2)

    def test_short_read_inside_full_cluster(self):
        # 满簇需要 4096 字节，只采到 3000：整簇范围按短读拒绝。
        case = self.make_case(
            chunks={
                10: ok_chunk(10, b"A" * CLUSTER),
                20: ok_chunk(20, b"B" * 3000),
                30: ok_chunk(30, b"C" * 100),
            }
        )
        report = recover_file(case)
        self.assertEqual(report.segments[1].reason, ACQUISITION_SHORT)
        self.assertEqual(report.reason_counts[ACQUISITION_SHORT], CLUSTER)


class TestInputValidationAndReadOnly(BaseCaseMixin, unittest.TestCase):
    """输入校验与只读保证。"""

    def test_reject_bad_chain_length_too_long(self):
        with self.assertRaises(ValueError):
            self.make_case(
                file_size=CLUSTER * 2 + 100,
                cluster_chain=(10, 20, 30, 40),
            )

    def test_reject_duplicate_physical_cluster(self):
        with self.assertRaises(ValueError):
            self.make_case(cluster_chain=(10, 10, 30))

    def test_reject_overwrite_outside_cluster(self):
        # 簇大小上下文在 RecoveryCase 层，越界改写由案例校验拒绝。
        bad = Overwrite(
            physical_cluster=10, phy_start=0, phy_end=CLUSTER + 1,
            time=T2, event_id="EV-BAD",
        )
        with self.assertRaises(ValueError):
            self.make_case(overwrites=(bad,))

    def test_reject_reversed_overwrite_range(self):
        with self.assertRaises(ValueError):
            Overwrite(
                physical_cluster=10, phy_start=10, phy_end=10,
                time=T2, event_id="EV-EMPTY",
            )

    def test_reject_inconsistent_time_order(self):
        with self.assertRaises(ValueError):
            self.make_case(deletion_time=T4, acquisition_time=T1)

    def test_reject_chunk_status_data_mismatch(self):
        with self.assertRaises(ValueError):
            Chunk(10, "missing", data=b"x")
        with self.assertRaises(ValueError):
            Chunk(10, "error")  # error 必须带说明

    def test_case_is_immutable(self):
        case = self.make_case()
        with self.assertRaises(Exception):
            case.file_size = 1  # type: ignore[misc]
        with self.assertRaises(TypeError):
            case.chunks[99] = ok_chunk(99, b"z")  # type: ignore[index]

    def test_recovery_does_not_mutate_chunk_bytes(self):
        original = b"A" * CLUSTER
        chunk = ok_chunk(10, original)
        case = self.make_case(
            chunks={
                10: chunk,
                20: ok_chunk(20, b"B" * CLUSTER),
                30: ok_chunk(30, b"C" * 100),
            }
        )
        report = recover_file(case)
        self.assertEqual(chunk.data, original)
        # 输出数据是独立副本，修改它不影响输入。
        first = report.segments[0]
        self.assertEqual(first.data, original)
        self.assertIsNot(first.data, chunk.data)


class TestSerializationAndCli(BaseCaseMixin, unittest.TestCase):
    """JSON 往返与命令行入口的真实断言。"""

    @staticmethod
    def _case_to_dict(case: RecoveryCase) -> dict:
        return {
            "file_size": case.file_size,
            "cluster_size": case.cluster_size,
            "deletion_time": case.deletion_time,
            "acquisition_time": case.acquisition_time,
            "cluster_chain": list(case.cluster_chain),
            "chunks": [
                {
                    "physical_cluster": pc,
                    "status": "ok",
                    "data": base64.b64encode(case.chunks[pc].data).decode(
                        "ascii"
                    ),
                }
                for pc in case.cluster_chain
            ],
            "allocations": [
                {
                    "physical_cluster": ev.physical_cluster,
                    "start_time": ev.start_time,
                    "end_time": ev.end_time,
                    "object_id": ev.object_id,
                    "event_id": ev.event_id,
                }
                for ev in case.allocations
            ],
            "overwrites": [
                {
                    "physical_cluster": ev.physical_cluster,
                    "phy_start": ev.phy_start,
                    "phy_end": ev.phy_end,
                    "time": ev.time,
                    "event_id": ev.event_id,
                }
                for ev in case.overwrites
            ],
            "record_gaps": [
                {
                    "physical_cluster": ev.physical_cluster,
                    "start_time": ev.start_time,
                    "end_time": ev.end_time,
                    "event_id": ev.event_id,
                }
                for ev in case.record_gaps
            ],
            "file_name": case.file_name,
        }

    def test_dict_roundtrip_base64(self):
        case = self.make_case(
            overwrites=(
                Overwrite(
                    physical_cluster=10, phy_start=0, phy_end=4,
                    time=T2, event_id="EV-JSON",
                ),
            )
        )
        report = recover_file(case)
        payload = report.to_dict()
        # JSON 可序列化且字节走 base64。
        encoded = json.dumps(payload, ensure_ascii=False)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["segments"][0]["reason"], OVERWRITTEN)
        first_recovered = next(
            s for s in decoded["segments"] if s["reason"] == RECOVERED
        )
        expected = next(
            s.data for s in report.segments if s.reason == RECOVERED
        )
        self.assertEqual(base64.b64decode(first_recovered["data_b64"]), expected)

        # from_dict 接受 base64 分块并得到等价判定。
        rebuilt = RecoveryCase.from_dict(self._case_to_dict(case))
        report2 = recover_file(rebuilt)
        self.assertEqual(
            [s.reason for s in report2.segments],
            [s.reason for s in report.segments],
        )

    def test_cli_json_to_stdout_and_text_format(self):
        case = self.make_case(
            allocations=(
                Allocation(
                    physical_cluster=20, start_time=T2, end_time=T3,
                    object_id="obj-7", event_id="EV-CLI",
                ),
            )
        )
        case_dict = self._case_to_dict(case)

        with tempfile.TemporaryDirectory() as tmp:
            case_path = os.path.join(tmp, "case.json")
            out_path = os.path.join(tmp, "report.txt")
            with open(case_path, "w", encoding="utf-8") as handle:
                json.dump(case_dict, handle)

            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli_main([case_path])
            self.assertEqual(code, 0)
            stdout_report = json.loads(buf.getvalue())
            self.assertIn(REALLOCATED, stdout_report["reason_counts"])

            code = cli_main(
                [case_path, "--format", "text", "--output", out_path]
            )
            self.assertEqual(code, 0)
            with open(out_path, "r", encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("取证恢复报告", text)
            self.assertIn("EV-CLI", text)
            self.assertIn("物理位置: 簇 20", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
