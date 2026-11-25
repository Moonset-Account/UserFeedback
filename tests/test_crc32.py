"""crc32_combine / 补零外推的正确性测试：与 zlib 全量计算逐条对照。"""

from __future__ import annotations

import random
import unittest
import zlib

from chunk_checksum.crc32 import (
    crc32_combine,
    crc32_zero_extend,
    crc32_zeros,
)


class TestCrc32Combine(unittest.TestCase):
    def test_combine_matches_zlib_random_splits(self):
        rng = random.Random(20261126)
        for _ in range(500):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 600)))
            cut = rng.randrange(0, len(data) + 1)
            left, right = data[:cut], data[cut:]
            crc1 = zlib.crc32(left)
            crc2 = zlib.crc32(right)
            with self.subTest(length=len(data), cut=cut):
                self.assertEqual(
                    crc32_combine(crc1, crc2, len(right)), zlib.crc32(data)
                )

    def test_combine_large_second_segment(self):
        # 跨越 256/512 字节平方链边界的长度
        rng = random.Random(99)
        for len_b in (255, 256, 257, 303, 511, 512, 1024, 4097):
            a = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 100)))
            b = bytes(rng.randrange(256) for _ in range(len_b))
            with self.subTest(len_b=len_b):
                self.assertEqual(
                    crc32_combine(zlib.crc32(a), zlib.crc32(b), len(b)),
                    zlib.crc32(a + b),
                )

    def test_combine_empty_second_segment_is_identity(self):
        crc = zlib.crc32(b"anything")
        self.assertEqual(crc32_combine(crc, 0, 0), crc)

    def test_combine_empty_first_segment(self):
        rng = random.Random(5)
        b = bytes(rng.randrange(256) for _ in range(50))
        self.assertEqual(
            crc32_combine(zlib.crc32(b""), zlib.crc32(b), len(b)),
            zlib.crc32(b),
        )

    def test_combine_three_way_associative(self):
        rng = random.Random(7)
        a = bytes(rng.randrange(256) for _ in range(13))
        b = bytes(rng.randrange(256) for _ in range(21))
        c = bytes(rng.randrange(256) for _ in range(8))
        ab = crc32_combine(zlib.crc32(a), zlib.crc32(b), len(b))
        abc = crc32_combine(ab, zlib.crc32(c), len(c))
        self.assertEqual(abc, zlib.crc32(a + b + c))

    def test_negative_length_rejected(self):
        with self.assertRaises(ValueError):
            crc32_combine(0, 0, -1)
        with self.assertRaises(ValueError):
            crc32_zero_extend(0, -1)
        with self.assertRaises(ValueError):
            crc32_zeros(-1)


class TestCrc32ZeroExtend(unittest.TestCase):
    def test_zero_extend_matches_real_zero_bytes(self):
        rng = random.Random(31)
        for n in (0, 1, 2, 7, 8, 31, 63, 64, 65, 255, 256, 4096, 70000):
            base = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 60)))
            expected = zlib.crc32(base + b"\x00" * n)
            with self.subTest(nzeros=n):
                self.assertEqual(
                    crc32_zero_extend(zlib.crc32(base), n), expected
                )

    def test_zero_extend_from_empty(self):
        for n in (1, 100, 5000):
            self.assertEqual(
                crc32_zero_extend(0, n), zlib.crc32(b"\x00" * n)
            )

    def test_crc32_zeros_helper(self):
        for n in (0, 1, 99, 65535, 65536, 65537, 200003):
            with self.subTest(n=n):
                self.assertEqual(crc32_zeros(n), zlib.crc32(b"\x00" * n))


if __name__ == "__main__":
    unittest.main()
