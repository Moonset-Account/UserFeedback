"""EXPB 解析器与样例生成器的单元测试。"""

import hashlib
import json
import os
import struct
import tempfile
import unittest

from rec import generator
from rec.expb import (
    FOOTER_LEN,
    FOOTER_PREFIX,
    FOOTER_SUFFIX,
    MAGIC,
    ExpbError,
    parse_expb,
)
from tests import ensure_dataset


class TestExpbRoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = ensure_dataset()

    def test_generated_objects_parse_and_match_truth(self) -> None:
        for name, entry in self.manifest["objects"].items():
            with open(entry["file"], "rb") as fh:
                data = fh.read()
            obj = parse_expb(data)
            summary = obj.summary
            expected = entry["summary"]
            for key in (
                "object_name", "record_count", "groups",
                "temperature_min", "temperature_max", "temperature_avg",
                "measured_sum", "checked_count", "sha256", "size",
            ):
                self.assertEqual(
                    summary[key], expected[key],
                    f"{name} 摘要字段 {key} 与独立真值不一致",
                )
            # 整文件字节摘要同样要与真值一致
            self.assertEqual(
                hashlib.sha256(data).hexdigest(), expected["file_sha256"],
            )

    def test_records_are_well_formed(self) -> None:
        entry = self.manifest["objects"]["tiny-v1.expb"]
        with open(entry["file"], "rb") as fh:
            obj = parse_expb(fh.read())
        for i, rec in enumerate(obj.records):
            self.assertEqual(rec["id"], i)
            self.assertIn(rec["group"], ("A", "B", "C"))
            self.assertIsInstance(rec["checked"], bool)


class TestExpbCorruption(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = ensure_dataset()
        with open(
            cls.manifest["objects"]["tiny-v1.expb"]["file"], "rb",
        ) as fh:
            cls.good = fh.read()

    def _assert_reject(self, data: bytes, fragment: str) -> None:
        with self.assertRaises(ExpbError) as ctx:
            parse_expb(data)
        self.assertIn(fragment, str(ctx.exception))

    def test_bad_magic(self) -> None:
        self._assert_reject(b"XXXX" + self.good[4:], "魔数")

    def test_truncated_object(self) -> None:
        self._assert_reject(self.good[:40], "最小合法布局")

    def test_whole_file_digest_mismatch(self) -> None:
        # 篡改 payload 中间一个字节，footer 摘要必然失配
        tampered = bytearray(self.good)
        tampered[40] ^= 0xFF
        self._assert_reject(bytes(tampered), "SHA-256")

    def test_declared_length_mismatch(self) -> None:
        # 把 payload_len 改大，长度推算与实际对象不符
        name_len = self.good[len(MAGIC)]
        payload_len_pos = len(MAGIC) + 1 + name_len + 2
        tampered = bytearray(self.good)
        struct.pack_into(">Q", tampered, payload_len_pos, 999999)
        self._assert_reject(bytes(tampered), "payload_len")

    def test_footer_marker_missing(self) -> None:
        tampered = bytearray(self.good)
        # 用随机字节覆盖 footer 前缀标记
        start = len(self.good) - FOOTER_LEN
        tampered[start : start + len(FOOTER_PREFIX)] = b"?" * len(FOOTER_PREFIX)
        self._assert_reject(bytes(tampered), "footer")

    def test_bad_record_field_rejected(self) -> None:
        # 手工构造一个记录字段越界的对象
        rng_records = [generator.make_record(0, generator.random.Random(1))]
        rng_records[0]["temperature"] = 9999.0  # 超出协议范围
        data = generator.build_expb("bad.expb", rng_records)
        self._assert_reject(data, "temperature")

    def test_record_count_mismatch(self) -> None:
        # 头部声明的记录数改成与实际帧数不符
        name_len = self.good[len(MAGIC)]
        count_pos = len(MAGIC) + 1 + name_len
        tampered = bytearray(self.good)
        struct.pack_into(">H", tampered, count_pos, 60000)
        # 头部变化后必须重算摘要才能走到“帧数不符”分支
        footer_start = len(tampered) - FOOTER_LEN
        new_digest = hashlib.sha256(bytes(tampered[:footer_start])).hexdigest()
        prefix_end = footer_start + len(FOOTER_PREFIX)
        tampered[prefix_end : prefix_end + 64] = new_digest.encode("ascii")
        self._assert_reject(bytes(tampered), "记录")

    def test_footer_digest_hex_structure(self) -> None:
        tampered = bytearray(self.good)
        footer_start = len(tampered) - FOOTER_LEN
        prefix_end = footer_start + len(FOOTER_PREFIX)
        tampered[prefix_end : prefix_end + 64] = b"z" * 64  # 非十六进制
        self._assert_reject(bytes(tampered), "十六进制")


class TestGeneratorDeterminism(unittest.TestCase):
    def test_regenerate_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            m1 = generator.generate_all(tmp)
            first = open(m1["objects"]["tiny-v1.expb"]["file"], "rb").read()
            m2 = generator.generate_all(tmp)
            second = open(m2["objects"]["tiny-v1.expb"]["file"], "rb").read()
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
