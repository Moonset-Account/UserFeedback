"""归并状态机主流程测试：正常分段、局部恢复、缓冲与 flush 边界。"""

import unittest

from frame_merger import (
    FRAME_MARKER,
    DiscontinuityReason,
    FrameCatalog,
    FrameEvent,
    FrameMerger,
    GapEvent,
    VersionEvent,
    build_control_packet,
    encode_frame,
    encode_measurement,
)
from frame_merger.merger import _earliest_marker, _earliest_offset


def frames(events):
    return [e for e in events if isinstance(e, FrameEvent)]


def gaps(events):
    return [e for e in events if isinstance(e, GapEvent)]


def versions(events):
    return [e for e in events if isinstance(e, VersionEvent)]


class ConfirmedParsingTests(unittest.TestCase):
    def setUp(self):
        self.catalog = FrameCatalog.default()
        self.v1 = self.catalog.spec(1)
        self.v2 = self.catalog.spec(2)

    def test_v1_confirmed_stream_emits_measurements(self):
        merger = FrameMerger(self.catalog)
        f1 = encode_frame(
            self.v1, encode_measurement(3, 100) + encode_measurement(4, -7)
        )
        f2 = encode_frame(self.v1, encode_measurement(5, 9))
        events = merger.push(build_control_packet(1) + f1 + f2)

        self.assertEqual(merger.confirmed_version, 1)
        self.assertEqual(len(versions(events)), 1)
        self.assertEqual(versions(events)[0], VersionEvent(1, None, False))
        self.assertEqual(len(frames(events)), 2)
        self.assertEqual(
            frames(events)[0].measurements, ((3, 100), (4, -7))
        )
        self.assertEqual(frames(events)[0].sequence, 1)
        self.assertEqual(frames(events)[1].sequence, 2)
        for frame in frames(events):
            self.assertEqual(frame.version, 1)
            self.assertFalse(frame.deferred)
            self.assertIsNone(frame.timestamp)
        self.assertEqual(merger.flush(), [])

    def test_v2_carries_optional_timestamp(self):
        merger = FrameMerger(self.catalog)
        frame = encode_frame(
            self.v2, encode_measurement(9, -300), timestamp=0x0102
        )
        events = merger.push(build_control_packet(2) + frame)
        published = frames(events)
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].version, 2)
        self.assertEqual(published[0].timestamp, 0x0102)
        self.assertEqual(published[0].measurements, ((9, -300),))

    def test_arbitrary_chunking_matches_whole_stream(self):
        f1 = encode_frame(self.v2, encode_measurement(1, 11))
        f2 = encode_frame(self.v2, encode_measurement(2, 22))
        f3 = encode_frame(self.v2, encode_measurement(3, 33))
        wire = build_control_packet(2) + f1 + f2 + f3

        whole = FrameMerger(self.catalog).push(wire)
        for chunk_size in (1, 2, 3, 5, 7, 64):
            merger = FrameMerger(self.catalog)
            collected = []
            for i in range(0, len(wire), chunk_size):
                collected.extend(merger.push(wire[i : i + chunk_size]))
            collected.extend(merger.flush())
            self.assertEqual(frames(collected), frames(whole))
            self.assertEqual(versions(collected), versions(whole))
            self.assertEqual(gaps(collected), [])

    def test_checksum_failure_local_recovery_keeps_neighbors(self):
        good1 = encode_frame(self.v1, encode_measurement(1, 10))
        good2 = encode_frame(self.v1, encode_measurement(2, 20))
        bad = bytearray(encode_frame(self.v1, encode_measurement(9, 99)))
        bad[-1] ^= 0xFF  # 只破坏校验字节
        merger = FrameMerger(self.catalog)
        events = merger.push(build_control_packet(1) + good1 + bytes(bad) + good2)

        published = frames(events)
        self.assertEqual(
            [f.measurements for f in published], [((1, 10),), ((2, 20),)]
        )
        self.assertEqual([f.sequence for f in published], [1, 2])
        gap_list = gaps(events)
        self.assertEqual(len(gap_list), 1)
        self.assertEqual(gap_list[0].reason, DiscontinuityReason.CHECKSUM_MISMATCH)
        # 坏帧长度（7 字节）必须被完整跳过，恢复点正好落在 good2 标记处。
        self.assertEqual(gap_list[0].bytes_skipped, len(bad))

    def test_delimiter_bytes_inside_payload_do_not_false_resync(self):
        # 载荷里故意出现 AA 55 与 C3 9C，成帧必须依赖长度字段而非扫描。
        # 5 字节载荷 = 1 个测点(3) + 残段(2)，残段正好是完整数据定界符。
        payload = encode_measurement(1, 1) + FRAME_MARKER
        self.assertEqual(len(payload), 5)
        frame = encode_frame(self.v1, payload)
        merger = FrameMerger(self.catalog)
        events = merger.push(build_control_packet(1) + frame)
        published = frames(events)
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].measurements, ((1, 1),))
        self.assertEqual(published[0].payload_remainder, 2)
        self.assertEqual(
            [g.reason for g in gaps(events)],
            [DiscontinuityReason.MALFORMED_PAYLOAD],
        )

    def test_leading_noise_is_skipped_without_losing_first_frame(self):
        frame = encode_frame(self.v1, encode_measurement(6, 60))
        merger = FrameMerger(self.catalog)
        events = merger.push(b"\x00\x11\x22" + build_control_packet(1) + frame)
        self.assertEqual(len(frames(events)), 1)
        self.assertEqual(gaps(events)[0].reason, DiscontinuityReason.RESYNC_SKIPPED)
        self.assertEqual(gaps(events)[0].bytes_skipped, 3)

    def test_v2_alignment_violation_rejects_and_resyncs(self):
        bad = bytearray(encode_frame(self.v2, encode_measurement(1, 1)))
        bad[-1] = 0x01  # 非零填充
        good = encode_frame(self.v2, encode_measurement(2, 2))
        merger = FrameMerger(self.catalog)
        events = merger.push(build_control_packet(2) + bytes(bad) + good)
        self.assertEqual(len(frames(events)), 1)
        self.assertEqual(frames(events)[0].measurements, ((2, 2),))
        self.assertEqual(
            gaps(events)[0].reason, DiscontinuityReason.RESYNC_SKIPPED
        )

    def test_unknown_control_packet_is_skipped_locally(self):
        good = encode_frame(self.v1, encode_measurement(1, 1))
        bogus = b"\xc3\x9c\x09\x09"
        merger = FrameMerger(self.catalog)
        events = merger.push(bogus + build_control_packet(1) + good)
        self.assertEqual(merger.confirmed_version, 1)
        self.assertEqual(len(frames(events)), 1)
        self.assertEqual(
            gaps(events)[0].reason, DiscontinuityReason.RESYNC_SKIPPED
        )
        self.assertEqual(gaps(events)[0].bytes_skipped, 4)


class FirmwareSwitchTests(unittest.TestCase):
    def test_reannouncement_switches_spec_and_keeps_history(self):
        catalog = FrameCatalog.default()
        v1, v2 = catalog.spec(1), catalog.spec(2)
        old = encode_frame(v1, encode_measurement(1, -1))
        new = encode_frame(v2, encode_measurement(2, -2), timestamp=7)
        merger = FrameMerger(catalog)

        first = merger.push(build_control_packet(1) + old)
        second = merger.push(build_control_packet(2) + new)

        self.assertEqual(len(frames(first)), 1)
        self.assertEqual(frames(first)[0].version, 1)
        self.assertEqual(
            versions(second), [VersionEvent(2, previous=1, deferred=False)]
        )
        self.assertEqual(len(frames(second)), 1)
        self.assertEqual(frames(second)[0].version, 2)
        self.assertEqual(frames(second)[0].timestamp, 7)
        # 已发布帧序号连续，历史不回退。
        self.assertEqual(
            [f.sequence for f in frames(first) + frames(second)], [1, 2]
        )

    def test_same_version_reannouncement_emits_no_version_event(self):
        catalog = FrameCatalog.default()
        v1 = catalog.spec(1)
        merger = FrameMerger(catalog)
        merger.push(build_control_packet(1) + encode_frame(v1, encode_measurement(1, 1)))
        events = merger.push(
            build_control_packet(1) + encode_frame(v1, encode_measurement(2, 2))
        )
        self.assertEqual(versions(events), [])
        self.assertEqual(len(frames(events)), 1)


class BufferAndFlushTests(unittest.TestCase):
    def setUp(self):
        self.catalog = FrameCatalog.default()
        self.v1 = self.catalog.spec(1)

    def test_unconfirmed_overflow_drops_oldest_but_never_published_frames(self):
        # 第一阶段：一条已确认链路上发布一帧（已发布事件不占缓冲、不可回退）。
        confirmed = FrameMerger(self.catalog, max_buffer=32)
        published = confirmed.push(
            build_control_packet(1)
            + encode_frame(self.v1, encode_measurement(1, 1))
        )
        self.assertEqual(len(frames(published)), 1)

        # 第二阶段：另一条链路上控制包尚未到达（未确认），积压超出上限。
        # 构造不含任何定界符的噪声，确保整段都属于未发布积压。
        noise = bytes(((i * 31 + 7) & 0xFF for i in range(64)))
        self.assertNotIn(FRAME_MARKER, noise)
        merger = FrameMerger(self.catalog, max_buffer=32)
        events = merger.push(noise)
        self.assertIsNone(merger.confirmed_version)
        overflow = [
            g
            for g in gaps(events)
            if g.reason == DiscontinuityReason.BUFFER_OVERFLOW
        ]
        self.assertTrue(overflow)
        # 噪声中不含任何定界符字节，无真前缀可保留，整段 64 字节丢弃。
        self.assertEqual(overflow[0].bytes_skipped, 64)
        # 缓冲被压回上限以内（保留半个标记长度的尾部）。
        self.assertLessEqual(merger.buffered_bytes, merger.max_buffer)
        # 第一阶段的已发布帧完整保留，序号与载荷均未被改写或回退。
        self.assertEqual(frames(published)[0].sequence, 1)
        self.assertEqual(frames(published)[0].measurements, ((1, 1),))

        # 第三阶段：控制包到达后链路恢复，后续帧正常编号。
        recover = merger.push(
            build_control_packet(1)
            + encode_frame(self.v1, encode_measurement(2, 2))
        )
        self.assertEqual(frames(recover)[0].sequence, 1)
        self.assertEqual(frames(recover)[0].measurements, ((2, 2),))

    def test_overflow_then_marker_preserved_and_stream_recovers(self):
        merger = FrameMerger(self.catalog, max_buffer=24)
        frame = encode_frame(self.v1, encode_measurement(8, 80))
        # 40 字节无定界符噪声（足以超过上限），随后是完整的未确认帧；
        # 溢出丢弃只能发生在最后一个标记之前，标记与帧体必须保留。
        noise = bytes(((i * 37 + 11) & 0xFF for i in range(40)))
        self.assertNotIn(FRAME_MARKER, noise)
        first = merger.push(noise + frame)
        overflow_events = [
            g for g in gaps(first)
            if g.reason == DiscontinuityReason.BUFFER_OVERFLOW
        ]
        # 溢出只丢弃最后一个标记之前的 40 字节噪声，完整帧必须保留。
        self.assertEqual(len(overflow_events), 1)
        self.assertEqual(overflow_events[0].bytes_skipped, 40)
        self.assertEqual(merger.buffered_bytes, len(frame))

        events = merger.push(build_control_packet(1))
        self.assertEqual(len(frames(events)), 1)
        self.assertEqual(frames(events)[0].measurements, ((8, 80),))
        self.assertTrue(frames(events)[0].deferred)
        self.assertEqual(gaps(events), [])

    def test_confirmed_stream_is_not_bounded_by_max_buffer(self):
        # 已确认模式下边消费边发布，max_buffer 只约束未确认积压，不应
        # 误触发溢出（控制包位于流首，帧随控制包同一批进入即被消费）。
        big_stream = b"".join(
            encode_frame(self.v1, encode_measurement(i, i))
            for i in range(200)
        )
        merger = FrameMerger(self.catalog, max_buffer=32)
        events = merger.push(build_control_packet(1) + big_stream)
        published = frames(events)
        self.assertEqual(len(published), 200)
        self.assertEqual(
            [f.sequence for f in published[:3]], [1, 2, 3]
        )
        self.assertEqual(
            [g.reason for g in gaps(events)
             if g.reason == DiscontinuityReason.BUFFER_OVERFLOW],
            [],
        )

    def test_late_control_packet_recovers_overflown_unconfirmed_backlog(self):
        big_stream = b"".join(
            encode_frame(self.v1, encode_measurement(i, i))
            for i in range(200)
        )
        merger = FrameMerger(self.catalog, max_buffer=32)
        backlog_events = merger.push(big_stream)
        self.assertIsNone(merger.confirmed_version)
        self.assertTrue(
            any(
                g.reason == DiscontinuityReason.BUFFER_OVERFLOW
                for g in gaps(backlog_events)
            )
        )
        # 迟到的控制包必须能让链路恢复，缓冲中幸存的帧按推迟语义释放。
        tail = merger.push(
            build_control_packet(1)
            + encode_frame(self.v1, encode_measurement(9, 9))
        )
        self.assertEqual(merger.confirmed_version, 1)
        self.assertEqual(versions(tail)[0].deferred, True)
        self.assertTrue(len(frames(tail)) >= 1)
        self.assertEqual(frames(tail)[-1].measurements, ((9, 9),))

    def test_flush_reports_truncated_tail(self):
        merger = FrameMerger(self.catalog)
        merger.push(build_control_packet(1))
        merger.push(FRAME_MARKER + b"\x03\x01")  # 永远等不到剩余字节
        events = merger.flush()
        self.assertEqual(
            [g.reason for g in gaps(events)], [DiscontinuityReason.TRUNCATED]
        )
        self.assertEqual(merger.buffered_bytes, 0)
        # flush 之后仍可继续工作。
        again = merger.push(
            encode_frame(self.v1, encode_measurement(1, 1))
        )
        self.assertEqual(len(frames(again)), 1)

    def test_flush_ambiguous_without_confirmation_refuses_to_guess(self):
        from frame_merger import build_ambiguous_v1_v2

        merger = FrameMerger(self.catalog)
        ambiguous = build_ambiguous_v1_v2(self.catalog)
        merger.push(ambiguous)
        self.assertIsNone(merger.confirmed_version)
        events = merger.flush()
        self.assertEqual(frames(events), [])
        self.assertEqual(
            [g.reason for g in gaps(events)],
            [DiscontinuityReason.TRUNCATED_AMBIGUOUS],
        )
        self.assertIn("1,2", gaps(events)[0].detail)

    def test_flush_single_surviving_hypothesis_is_inferred(self):
        # 未确认期只有一个版本能通过形式约束的样例（V2 帧对 V1 校验失败）。
        v2 = self.catalog.spec(2)
        only_v2 = encode_frame(v2, encode_measurement(4, 44), timestamp=5)
        merger = FrameMerger(self.catalog)
        merger.push(only_v2)
        events = merger.flush()
        self.assertEqual(merger.confirmed_version, 2)
        # 推断发生时缓冲中确有积压，版本事件按推迟语义标注。
        self.assertEqual(versions(events), [VersionEvent(2, None, True)])
        self.assertEqual(len(frames(events)), 1)
        self.assertTrue(frames(events)[0].deferred)
        self.assertEqual(
            [g.reason for g in gaps(events)],
            [DiscontinuityReason.FLUSH_INFERRED],
        )


class ChunkedRandomizedTests(unittest.TestCase):
    def test_random_chunking_with_corruption_matches_reference(self):
        import random

        catalog = FrameCatalog.default()
        v1 = catalog.spec(1)
        good_a = encode_frame(v1, encode_measurement(1, 11))
        good_b = encode_frame(v1, encode_measurement(2, 22))
        bad = bytearray(encode_frame(v1, encode_measurement(3, 33)))
        bad[4] ^= 0x80
        good_c = encode_frame(v1, encode_measurement(4, 44))
        wire = (
            b"\x99"
            + build_control_packet(1)
            + good_a
            + bytes(bad)
            + good_b
            + good_c
        )

        reference = FrameMerger(catalog).push(wire) + []
        ref_frames = frames(reference)
        ref_gap_reasons = [g.reason for g in gaps(reference)]

        rng = random.Random(20261128)
        for _ in range(20):
            merger = FrameMerger(catalog)
            collected = []
            view = memoryview(wire)
            pos = 0
            while pos < len(wire):
                step = rng.randint(1, 5)
                collected.extend(merger.push(bytes(view[pos : pos + step])))
                pos += step
            collected.extend(merger.flush())
            # 分块方式可能改变“确认时缓冲是否已有帧字节”，因此这里比较
            # deferred 之外的全部字段；deferred 的独立语义在 test_deferred
            # 专题中逐字节分片严格验证。
            self.assertEqual(
                [(f.sequence, f.version, f.measurements, f.timestamp)
                 for f in frames(collected)],
                [(f.sequence, f.version, f.measurements, f.timestamp)
                 for f in ref_frames],
            )
            self.assertEqual(
                [g.reason for g in gaps(collected)], ref_gap_reasons
            )


class HelperTests(unittest.TestCase):
    def test_earliest_marker_helpers(self):
        self.assertIsNone(_earliest_marker(-1, -1))
        self.assertEqual(_earliest_marker(3, -1), (3, "frame"))
        self.assertEqual(_earliest_marker(5, 2), (2, "control"))
        self.assertEqual(_earliest_offset(-1, -1, default=9), 9)
        self.assertEqual(_earliest_offset(7, 4, default=9), 4)


if __name__ == "__main__":
    unittest.main()
