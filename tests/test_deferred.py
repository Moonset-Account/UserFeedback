"""推迟确认、多边界并存与跨版本交错专题测试。

所有数据均为本地合成，不访问任何真实串口或网络目标。
"""

import unittest

from frame_merger import (
    DiscontinuityReason,
    FrameCatalog,
    FrameEvent,
    FrameMerger,
    GapEvent,
    VersionEvent,
    build_ambiguous_v1_v2,
    build_control_packet,
    encode_frame,
    encode_measurement,
)


def frames(events):
    return [e for e in events if isinstance(e, FrameEvent)]


def gaps(events):
    return [e for e in events if isinstance(e, GapEvent)]


def versions(events):
    return [e for e in events if isinstance(e, VersionEvent)]


class DeferredConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = FrameCatalog.default()
        self.v1 = self.catalog.spec(1)
        self.v2 = self.catalog.spec(2)
        self.ambiguous = build_ambiguous_v1_v2(
            self.catalog, point_id=0x10, value=-250
        )

    def test_two_provable_boundaries_coexist_before_confirmation(self):
        merger = FrameMerger(self.catalog)
        events = merger.push(self.ambiguous)

        # 版本未确认前：一帧都不允许释放。
        self.assertEqual(events, [])
        self.assertIsNone(merger.confirmed_version)

        # 但两个可证实边界必须同时保留，且帧尾不同。
        hypotheses = merger.pending_hypotheses
        self.assertEqual(len(hypotheses), 1)
        ends = next(iter(hypotheses.values()))
        self.assertEqual(set(ends), {1, 2})
        self.assertEqual(ends[1], 7)
        self.assertEqual(ends[2], 12)

    def test_confirmation_v1_releases_deferred_frame_as_v1(self):
        merger = FrameMerger(self.catalog)
        merger.push(self.ambiguous)
        events = merger.push(build_control_packet(1))

        self.assertEqual(versions(events), [VersionEvent(1, None, True)])
        published = frames(events)
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].version, 1)
        self.assertEqual(published[0].deferred, True)
        self.assertEqual(published[0].measurements, ((0x10, -250),))
        self.assertIsNone(published[0].timestamp)
        self.assertEqual(merger.buffered_bytes, 0)
        self.assertEqual(merger.flush(), [])

    def test_v1_confirmation_reports_v2_only_tail_as_discontinuity(self):
        # 同前缀样例中 V1 帧尾在第 7 字节，剩余 5 字节是 V2 专属的时间戳
        # 低字节/CRC/填充；确认 V1 后它们不属于任何 V1 帧，必须作为局部
        # 恢复断续显式报告，不能静默吞掉。
        merger = FrameMerger(self.catalog)
        merger.push(self.ambiguous)
        events = merger.push(build_control_packet(1))
        gap_list = gaps(events)
        self.assertEqual(len(gap_list), 1)
        self.assertEqual(
            gap_list[0].reason, DiscontinuityReason.RESYNC_SKIPPED
        )
        self.assertEqual(gap_list[0].bytes_skipped, 5)
        self.assertEqual(merger.buffered_bytes, 0)

    def test_confirmation_v2_releases_same_bytes_as_v2_with_timestamp(self):
        merger = FrameMerger(self.catalog)
        merger.push(self.ambiguous)
        events = merger.push(build_control_packet(2))

        self.assertEqual(versions(events), [VersionEvent(2, None, True)])
        published = frames(events)
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].version, 2)
        self.assertEqual(published[0].deferred, True)
        self.assertEqual(published[0].measurements, ((0x10, -250),))
        self.assertIsNotNone(published[0].timestamp)

    def test_ambiguous_bytes_chunked_one_byte_at_a_time(self):
        # 关键交错场景：每个字节单独喂入，确认前后行为必须与整段一致。
        for announcing_version in (1, 2):
            with self.subTest(version=announcing_version):
                merger = FrameMerger(self.catalog)
                collected = []
                for byte in self.ambiguous:
                    collected.extend(merger.push(bytes((byte,))))
                self.assertEqual(collected, [])
                self.assertEqual(
                    len(merger.pending_hypotheses), 1
                )
                control = build_control_packet(announcing_version)
                for byte in control:
                    collected.extend(merger.push(bytes((byte,))))
                published = frames(collected)
                self.assertEqual(len(published), 1)
                self.assertEqual(
                    published[0].version, announcing_version
                )
                self.assertTrue(published[0].deferred)
                self.assertEqual(
                    published[0].measurements, ((0x10, -250),)
                )

    def test_multiple_ambiguous_frames_accumulate_then_release_together(self):
        a = build_ambiguous_v1_v2(self.catalog, 1, 100)
        b = build_ambiguous_v1_v2(self.catalog, 2, 200)
        merger = FrameMerger(self.catalog)
        self.assertEqual(merger.push(a + b), [])
        # 两个标记各保留一组双版本边界。
        self.assertEqual(len(merger.pending_hypotheses), 2)

        events = merger.push(build_control_packet(1))
        published = frames(events)
        self.assertEqual(len(published), 2)
        self.assertTrue(all(f.deferred for f in published))
        self.assertEqual(
            [f.measurements for f in published],
            [((1, 100),), ((2, 200),)],
        )
        self.assertEqual([f.sequence for f in published], [1, 2])

    def test_deferred_flag_resets_on_later_non_deferred_traffic(self):
        merger = FrameMerger(self.catalog)
        merger.push(self.ambiguous)
        released = merger.push(build_control_packet(2))
        self.assertTrue(frames(released)[0].deferred)
        self.assertEqual(
            versions(released), [VersionEvent(2, None, True)]
        )

        later = merger.push(
            encode_frame(self.v2, encode_measurement(3, 3), timestamp=1)
        )
        self.assertEqual(len(frames(later)), 1)
        self.assertFalse(frames(later)[0].deferred)

    def test_control_packet_split_across_pushes_still_confirms(self):
        control = build_control_packet(1)
        merger = FrameMerger(self.catalog)
        merger.push(self.ambiguous)
        collected = []
        # 控制包按 1 字节分片到达，任何中间态都不能提前确认。
        for byte in control:
            collected.extend(merger.push(bytes((byte,))))
        self.assertEqual(merger.confirmed_version, 1)
        self.assertEqual(len(frames(collected)), 1)
        self.assertTrue(frames(collected)[0].deferred)

    def test_frame_straddling_confirmation_boundary(self):
        # 帧的前缀在确认前到达，帧尾与控制包在之后到达。
        frame = encode_frame(self.v1, encode_measurement(7, 70))
        merger = FrameMerger(self.catalog)
        merger.push(frame[:4])  # 未确认，且帧不完整
        self.assertEqual(merger.pending_hypotheses, {})
        events = merger.push(frame[4:] + build_control_packet(1))
        # 控制包确认后重解析整段缓冲，帧被完整物化。
        published = frames(events)
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].measurements, ((7, 70),))
        self.assertTrue(published[0].deferred)


class CrossVersionInterleavingTests(unittest.TestCase):
    def setUp(self):
        self.catalog = FrameCatalog.default()
        self.v1 = self.catalog.spec(1)
        self.v2 = self.catalog.spec(2)

    def test_firmware_switch_with_deferred_region_in_between(self):
        old = encode_frame(self.v1, encode_measurement(1, -1))
        ambiguous = build_ambiguous_v1_v2(self.catalog, 5, 55)
        new = encode_frame(self.v2, encode_measurement(6, -6), timestamp=9)
        merger = FrameMerger(self.catalog)

        first = merger.push(build_control_packet(1) + old)
        self.assertEqual(len(frames(first)), 1)
        self.assertFalse(frames(first)[0].deferred)

        # 切换窗口用独立链路模拟：新控制包尚未到达，先到一帧双版本可
        # 证实数据，归并器必须保留两个边界而不发布。
        switching = FrameMerger(self.catalog)
        self.assertEqual(switching.push(ambiguous), [])
        self.assertEqual(len(switching.pending_hypotheses), 1)

        # 把同一归并器的“前史”接到主链路语义上：主链路已确认 V1 并发布
        # 过一帧；切换链路上控制包 V2 到达后，积压按 V2 推迟释放。
        tail = switching.push(build_control_packet(2) + new)
        self.assertEqual(
            versions(tail), [VersionEvent(2, previous=None, deferred=True)]
        )
        published = frames(tail)
        self.assertEqual(len(published), 2)
        # 被推迟的帧按新确认版本 V2 释放。
        self.assertEqual(published[0].version, 2)
        self.assertTrue(published[0].deferred)
        self.assertEqual(published[0].measurements, ((5, 55),))
        # 切换后的常规帧同样按 V2，但不再带推迟标记。
        self.assertEqual(published[1].version, 2)
        self.assertFalse(published[1].deferred)
        self.assertEqual(published[1].timestamp, 9)

        # 主链路历史帧序号连续、不可回退；切换链路独立编号。
        self.assertEqual(frames(first)[0].sequence, 1)
        self.assertEqual([f.sequence for f in published], [1, 2])

    def test_reannounced_older_version_reparses_backlog_with_old_spec(self):
        ambiguous = build_ambiguous_v1_v2(self.catalog, 8, 88)
        merger = FrameMerger(self.catalog)
        merger.push(ambiguous)
        events = merger.push(build_control_packet(1))
        published = frames(events)
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].version, 1)
        self.assertIsNone(published[0].timestamp)


class FlushAmbiguityTests(unittest.TestCase):
    def setUp(self):
        self.catalog = FrameCatalog.default()

    def test_flush_with_competing_versions_emits_ambiguous_gap(self):
        ambiguous = build_ambiguous_v1_v2(self.catalog)
        merger = FrameMerger(self.catalog)
        merger.push(ambiguous)
        events = merger.flush()
        self.assertEqual(frames(events), [])
        gap_list = gaps(events)
        self.assertEqual(
            [g.reason for g in gap_list],
            [DiscontinuityReason.TRUNCATED_AMBIGUOUS],
        )
        self.assertEqual(gap_list[0].bytes_skipped, len(ambiguous))
        self.assertIn("1,2", gap_list[0].detail)

    def test_flush_with_no_evidence_emits_ambiguous_gap_too(self):
        merger = FrameMerger(self.catalog)
        merger.push(b"\x01\x02\x03")
        events = merger.flush()
        self.assertEqual(frames(events), [])
        self.assertEqual(
            gaps(events)[0].reason,
            DiscontinuityReason.TRUNCATED_AMBIGUOUS,
        )
        self.assertEqual(gaps(events)[0].detail, "no_parseable_frame")

    def test_flush_then_merger_is_reusable(self):
        merger = FrameMerger(self.catalog)
        merger.flush()
        frame = encode_frame(self.catalog.spec(1), encode_measurement(1, 1))
        events = merger.push(build_control_packet(1) + frame)
        self.assertEqual(len(frames(events)), 1)


class ChunkedBacklogTests(unittest.TestCase):
    def test_long_deferred_backlog_chunked_matches_whole_release(self):
        catalog = FrameCatalog.default()
        v1 = catalog.spec(1)
        backlog = b"".join(
            encode_frame(v1, encode_measurement(i, i * 10)) for i in range(1, 6)
        )
        control = build_control_packet(1)
        wire = backlog + control

        reference = FrameMerger(catalog).push(wire)
        ref_frames = frames(reference)
        self.assertEqual(len(ref_frames), 5)
        self.assertTrue(all(f.deferred for f in ref_frames))

        for chunk_size in (1, 2, 7, 13):
            with self.subTest(chunk_size=chunk_size):
                merger = FrameMerger(catalog)
                collected = []
                for i in range(0, len(wire), chunk_size):
                    collected.extend(merger.push(wire[i : i + chunk_size]))
                collected.extend(merger.flush())
                self.assertEqual(frames(collected), ref_frames)
                self.assertEqual(
                    versions(collected), [VersionEvent(1, None, True)]
                )
                self.assertEqual(gaps(collected), [])


if __name__ == "__main__":
    unittest.main()
