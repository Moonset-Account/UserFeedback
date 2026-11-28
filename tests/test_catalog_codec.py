"""帧目录与编解码层的单元测试（全部使用本地合成数据）。"""

import unittest

from frame_merger import (
    CONTROL_MARKER,
    FRAME_MARKER,
    INCOMPLETE,
    REJECTED,
    FrameCatalog,
    build_ambiguous_v1_v2,
    build_control_packet,
    crc8,
    encode_frame,
    encode_measurement,
    sum8,
)


class ChecksumTests(unittest.TestCase):
    def test_sum8_vectors(self):
        self.assertEqual(sum8(b"\x03"), 0x03)
        self.assertEqual(sum8(b"\xff\xff"), 0xFE)
        self.assertEqual(sum8(b""), 0)

    def test_crc8_known_check_value(self):
        # 多项式 0x07、初值 0 的 CRC-8 对 "123456789" 的标准校验值。
        self.assertEqual(crc8(b"123456789"), 0xF4)

    def test_crc8_chunks_equivalent_to_whole(self):
        data = bytes(range(256))
        whole = crc8(data)
        acc = 0
        # 逐字节喂入与一次性计算必须一致（CRC 的基本性质）。
        for byte in data:
            acc ^= byte
            for _ in range(8):
                acc = ((acc << 1) ^ 0x07) & 0xFF if acc & 0x80 else (acc << 1) & 0xFF
        self.assertEqual(acc, whole)


class CodecRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.catalog = FrameCatalog.default()
        self.v1 = self.catalog.spec(1)
        self.v2 = self.catalog.spec(2)

    def test_measurement_round_trip_including_negative(self):
        for point_id, value in [(0, 0), (7, -21931), (255, 32767), (1, -32768)]:
            wire = encode_measurement(point_id, value)
            self.assertEqual(len(wire), 3)
            outcome = self.v1.try_parse(
                FRAME_MARKER + b"\x03" + wire + bytes((sum8(b"\x03" + wire),)),
                0,
            )
            self.assertEqual(outcome.status, "ok")
            self.assertEqual(outcome.measurements, [(point_id, value)])

    def test_v1_frame_layout_and_parse(self):
        payload = encode_measurement(0x10, 1234) + encode_measurement(0x11, -5)
        frame = encode_frame(self.v1, payload)
        self.assertEqual(len(frame), 2 + 1 + 6 + 1)
        self.assertTrue(frame.startswith(FRAME_MARKER + b"\x06"))
        outcome = self.v1.try_parse(frame, 0)
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.end_offset, len(frame))
        self.assertEqual(
            outcome.measurements, [(0x10, 1234), (0x11, -5)]
        )
        self.assertIsNone(outcome.timestamp)
        self.assertEqual(outcome.payload_remainder, 0)

    def test_v2_frame_timestamp_and_alignment(self):
        payload = encode_measurement(0x20, 42)
        frame = encode_frame(self.v2, payload, timestamp=0xBEEF)
        # 2+1+3+2+1 = 9，补齐到 4 的倍数 => 12。
        self.assertEqual(len(frame), 12)
        self.assertEqual(len(frame) % 4, 0)
        outcome = self.v2.try_parse(frame, 0)
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.timestamp, 0xBEEF)
        self.assertEqual(outcome.measurements, [(0x20, 42)])

    def test_v2_nonzero_padding_is_rejected(self):
        frame = bytearray(encode_frame(self.v2, encode_measurement(1, 1)))
        frame[-1] = 0xFF  # 破坏零填充
        self.assertEqual(self.v2.try_parse(bytes(frame), 0).status, REJECTED)

    def test_v2_corrupted_crc_is_rejected(self):
        frame = bytearray(encode_frame(self.v2, encode_measurement(1, 1)))
        frame[-4] ^= 0x01  # CRC 字节翻转
        outcome = self.v2.try_parse(bytes(frame), 0)
        self.assertEqual(outcome.status, REJECTED)
        self.assertTrue(outcome.reject_reason.startswith("checksum_mismatch"))

    def test_v1_bad_checksum_is_rejected(self):
        frame = bytearray(encode_frame(self.v1, encode_measurement(1, 1)))
        frame[-1] ^= 0x01
        outcome = self.v1.try_parse(bytes(frame), 0)
        self.assertEqual(outcome.status, REJECTED)
        self.assertEqual(outcome.reject_reason, "checksum_mismatch:sum8")

    def test_incomplete_frame_waits_for_more_bytes(self):
        frame = encode_frame(self.v1, encode_measurement(1, 1))
        for cut in range(1, len(frame)):
            self.assertEqual(
                self.v1.try_parse(frame[:cut], 0).status, INCOMPLETE
            )

    def test_payload_remainder_is_reported_not_silently_dropped(self):
        payload = encode_measurement(1, 1) + b"\x7f"  # 4 字节，余 1
        frame = encode_frame(self.v2, payload)
        outcome = self.v2.try_parse(frame, 0)
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.measurements, [(1, 1)])
        self.assertEqual(outcome.payload_remainder, 1)


class ControlPacketTests(unittest.TestCase):
    def setUp(self):
        self.catalog = FrameCatalog.default()

    def test_control_packet_round_trip(self):
        packet = build_control_packet(2)
        self.assertEqual(packet, CONTROL_MARKER + b"\x02\x02")
        status, version, end = self.catalog.parse_control_packet(packet, 0)
        self.assertEqual(status, "ok")
        self.assertEqual(version, 2)
        self.assertEqual(end, 4)

    def test_unknown_version_rejected(self):
        status, version, _ = self.catalog.parse_control_packet(
            CONTROL_MARKER + b"\x09\x09", 0
        )
        self.assertEqual(status, REJECTED)
        self.assertIsNone(version)

    def test_bad_control_checksum_rejected(self):
        status, _, _ = self.catalog.parse_control_packet(
            CONTROL_MARKER + b"\x01\x00", 0
        )
        self.assertEqual(status, REJECTED)

    def test_partial_control_packet_incomplete(self):
        packet = build_control_packet(1)
        for cut in range(0, 4):
            status, _, _ = self.catalog.parse_control_packet(packet[:cut], 0)
            self.assertEqual(status, INCOMPLETE)


class AmbiguousSampleTests(unittest.TestCase):
    def test_same_bytes_are_valid_in_both_versions_with_distinct_ends(self):
        catalog = FrameCatalog.default()
        wire = build_ambiguous_v1_v2(catalog, point_id=0x10, value=-250)
        outcomes = catalog.evaluate_all(wire, 0)
        self.assertEqual(outcomes[1].status, "ok")
        self.assertEqual(outcomes[2].status, "ok")
        self.assertEqual(outcomes[1].end_offset, 7)
        self.assertEqual(outcomes[2].end_offset, 12)
        self.assertNotEqual(
            outcomes[1].end_offset, outcomes[2].end_offset
        )
        # 两个版本解出的测点值一致（同前缀载荷）。
        self.assertEqual(
            outcomes[1].measurements, outcomes[2].measurements
        )
        self.assertEqual(outcomes[1].measurements, [(0x10, -250)])
        # V2 额外携带时间戳，V1 没有可选字段。
        self.assertIsNone(outcomes[1].timestamp)
        self.assertIsNotNone(outcomes[2].timestamp)


if __name__ == "__main__":
    unittest.main()
