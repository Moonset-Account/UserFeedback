"""分块校验合成的完整测试。

对照基准 tests/reference.py 中的朴素全量实现：任何合成结果必须与
“在完整字节上重放覆盖写后用 zlib.crc32 全量重算”完全一致。
"""

from __future__ import annotations

import random
import unittest
import zlib

from chunk_checksum import (
    ByteRange,
    Chunk,
    ChunkManifest,
    InMemoryReader,
    apply_overwrite,
    build_manifest,
)
from tests.reference import full_recompute_manifest, simulate_overwrite

CHUNK_SIZE = 8


def _pattern(length: int, seed: int) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(length))


def _assert_matches_full_recompute(testcase: unittest.TestCase,
                                   result, expected: bytes,
                                   chunk_size: int) -> None:
    """合成结果必须与全量重算逐字段一致。"""
    expected_manifest, expected_crc = full_recompute_manifest(
        expected, chunk_size
    )
    testcase.assertEqual(result.object_length, len(expected))
    testcase.assertEqual(
        [(c.index, c.length, c.crc32) for c in result.chunks],
        [(c.index, c.length, c.crc32) for c in expected_manifest.chunks],
    )
    testcase.assertEqual(result.object_crc32, expected_crc)
    testcase.assertEqual(result.object_crc32, zlib.crc32(expected))
    testcase.assertEqual(result.manifest.object_length, len(expected))


def _assert_read_invariants(testcase: unittest.TestCase, result,
                            offset: int, write_len: int,
                            chunk_size: int) -> None:
    """读取最小性/安全性不变量。

    1. 实际读取与声明范围的总字节数一致，且每个实际读取落在某声明范围内；
    2. 永不读取本次写入区间 [offset, offset+write_len)；
    3. 单次读取不会达到整块大小（整块只允许复用或全量由新字节构成）。
    """
    testcase.assertEqual(
        result.bytes_read, sum(r.length for r in result.read_ranges)
    )
    for performed in result.performed_reads:
        testcase.assertLess(performed.length, chunk_size)
        testcase.assertTrue(
            any(
                r.start <= performed.start and performed.end <= r.end
                for r in result.read_ranges
            ),
            f"实际读取 {performed} 不在声明范围 {result.read_ranges} 内",
        )
        # 与写入区间不相交
        testcase.assertTrue(
            performed.end <= offset or performed.start >= offset + write_len,
            f"读取 {performed} 与写入区间 [{offset},{offset + write_len}) 相交",
        )


class TestManifestValidation(unittest.TestCase):
    def test_build_manifest_chunking(self):
        data = _pattern(20, seed=1)
        manifest = build_manifest(data, CHUNK_SIZE)
        self.assertEqual(len(manifest.chunks), 3)
        self.assertEqual([c.length for c in manifest.chunks], [8, 8, 4])
        self.assertEqual(manifest.object_length, 20)
        self.assertEqual(
            manifest.chunk_crcs,
            tuple(zlib.crc32(data[i : i + 8]) for i in (0, 8))
            + (zlib.crc32(data[16:]),),
        )

    def test_empty_object_manifest(self):
        manifest = build_manifest(b"", CHUNK_SIZE)
        self.assertEqual(manifest.chunks, ())
        self.assertEqual(manifest.object_length, 0)

    def test_invalid_manifest_rejected(self):
        good = Chunk(0, 8, 0)
        with self.assertRaises(ValueError):
            ChunkManifest(chunk_size=0, chunks=(good,), object_length=8)
        with self.assertRaises(ValueError):
            ChunkManifest(
                chunk_size=8,
                chunks=(Chunk(1, 8, 0),),  # 索引不从 0 开始
                object_length=8,
            )
        with self.assertRaises(ValueError):
            ChunkManifest(
                chunk_size=8,
                chunks=(Chunk(0, 9, 0),),  # 块长度超过 chunk_size
                object_length=9,
            )
        with self.assertRaises(ValueError):
            ChunkManifest(
                chunk_size=8,
                chunks=(Chunk(0, 8, 0), Chunk(1, 7, 0)),
                object_length=16,  # 长度之和与 object_length 不自洽
            )
        with self.assertRaises(ValueError):
            Chunk(0, 0, 0)
        with self.assertRaises(ValueError):
            Chunk(0, 1, 1 << 32)

    def test_byte_range_validation(self):
        rng = ByteRange(3, 10)
        self.assertEqual(rng.length, 7)
        self.assertEqual(str(rng), "[3, 10)")
        with self.assertRaises(ValueError):
            ByteRange(-1, 2)
        with self.assertRaises(ValueError):
            ByteRange(5, 4)


class TestOverwriteBasic(unittest.TestCase):
    def setUp(self):
        self.old = _pattern(37, seed=11)
        self.manifest = build_manifest(self.old, CHUNK_SIZE)

    def test_fully_covered_single_chunk_needs_no_read(self):
        # 整块覆盖 [8,16)：直接算新值，reader=None 也必须成功
        data = b"ABCDEFGH"
        result = apply_overwrite(self.manifest, 8, data, None)
        expected = simulate_overwrite(self.old, 8, data)
        _assert_matches_full_recompute(self, result, expected, CHUNK_SIZE)
        self.assertEqual(result.read_ranges, ())
        self.assertEqual(result.bytes_read, 0)
        self.assertEqual(result.performed_reads, ())

    def test_partial_overwrite_reads_only_two_edge_ranges(self):
        # 覆盖 [5,18)：左边缘块读 [0? 不，读前缀 5..8]，右边缘块读 [18,24)
        data = _pattern(13, seed=22)
        reader = InMemoryReader(self.old)
        result = apply_overwrite(self.manifest, 5, data, reader)
        expected = simulate_overwrite(self.old, 5, data)
        _assert_matches_full_recompute(self, result, expected, CHUNK_SIZE)
        # 左边缘块保留旧前缀 [0,5)，右边缘块保留旧后缀 [18,24)
        self.assertEqual(result.read_ranges, (ByteRange(0, 5), ByteRange(18, 24)))
        self.assertEqual(result.bytes_read, 5 + 6)
        self.assertEqual(reader.performed_reads, result.performed_reads)
        _assert_read_invariants(self, result, 5, 13, CHUNK_SIZE)

    def test_whole_object_overwrite_no_reads(self):
        data = _pattern(len(self.old), seed=33)
        result = apply_overwrite(self.manifest, 0, data, None)
        expected = simulate_overwrite(self.old, 0, data)
        _assert_matches_full_recompute(self, result, expected, CHUNK_SIZE)
        self.assertEqual(result.bytes_read, 0)

    def test_overwrite_longer_than_object_appends(self):
        data = _pattern(10, seed=44)
        reader = InMemoryReader(self.old)
        result = apply_overwrite(self.manifest, 33, data, reader)
        expected = simulate_overwrite(self.old, 33, data)
        self.assertEqual(result.object_length, 43)
        _assert_matches_full_recompute(self, result, expected, CHUNK_SIZE)
        # 仅 [32,33) 一个旧字节需要读；追加部分为新字节
        self.assertEqual(result.read_ranges, (ByteRange(32, 33),))

    def test_exact_append_at_eof(self):
        data = _pattern(7, seed=55)
        reader = InMemoryReader(self.old)
        result = apply_overwrite(self.manifest, 37, data, reader)
        expected = simulate_overwrite(self.old, 37, data)
        _assert_matches_full_recompute(self, result, expected, CHUNK_SIZE)
        # offset=37 落在旧末块 [32,37) 内，追加后该块含旧 5 字节 + 新 3 字节，
        # 必须读取旧前缀；追加部分本身为新字节不产生读取
        self.assertEqual(result.read_ranges, (ByteRange(32, 37),))
        self.assertEqual(result.bytes_read, 5)
        self.assertEqual(result.object_length, 44)
        # 前 4 个整块（[0,32)）原样复用
        for idx in range(4):
            self.assertEqual(
                result.chunks[idx].crc32, self.manifest.chunks[idx].crc32
            )

    def test_chunk_aligned_append_needs_no_read(self):
        # 追加起点落在块边界时，旧块全部整块复用，纯追加零读取
        aligned = _pattern(32, seed=909)
        aligned_manifest = build_manifest(aligned, CHUNK_SIZE)
        result = apply_overwrite(aligned_manifest, 32, b"ABC", None)
        expected = simulate_overwrite(aligned, 32, b"ABC")
        _assert_matches_full_recompute(self, result, expected, CHUNK_SIZE)
        self.assertEqual(result.bytes_read, 0)

    def test_empty_write_is_identity(self):
        reader = InMemoryReader(self.old)
        result = apply_overwrite(self.manifest, 10, b"", reader)
        self.assertEqual(result.object_length, 37)
        self.assertEqual(result.manifest.chunk_crcs, self.manifest.chunk_crcs)
        self.assertEqual(result.object_crc32, zlib.crc32(self.old))
        self.assertEqual(result.bytes_read, 0)


class TestTwoSequentialLocalOverwrites(unittest.TestCase):
    """题面核心要求：连续两次局部覆盖 == 全量重算。"""

    def test_two_local_overwrites_equal_full_recompute(self):
        old = _pattern(37, seed=101)
        manifest = build_manifest(old, CHUNK_SIZE)

        first_off, first_data = 5, _pattern(13, seed=202)   # [5,18)
        second_off, second_data = 14, _pattern(11, seed=303)  # [14,25) 与首次交错

        reader1 = InMemoryReader(old)
        r1 = apply_overwrite(manifest, first_off, first_data, reader1)
        after_first = simulate_overwrite(old, first_off, first_data)
        _assert_matches_full_recompute(self, r1, after_first, CHUNK_SIZE)
        self.assertGreater(r1.bytes_read, 0)
        self.assertLess(r1.bytes_read, len(old))

        reader2 = InMemoryReader(after_first)
        r2 = apply_overwrite(
            r1.manifest, second_off, second_data, reader2
        )
        after_second = simulate_overwrite(
            after_first, second_off, second_data
        )

        # 与全量重算逐块、整对象完全一致
        _assert_matches_full_recompute(
            self, r2, after_second, CHUNK_SIZE
        )
        # 等价于直接在原始对象上重放两次写
        direct = simulate_overwrite(
            simulate_overwrite(old, first_off, first_data),
            second_off,
            second_data,
        )
        self.assertEqual(after_second, direct)
        self.assertEqual(r2.object_crc32, zlib.crc32(direct))

        # 第二次仍只做局部读取，没有任何整块被读
        _assert_read_invariants(
            self, r2, second_off, len(second_data), CHUNK_SIZE
        )

    def test_untouched_chunks_reused_verbatim(self):
        old = _pattern(40, seed=404)
        manifest = build_manifest(old, CHUNK_SIZE)
        # 只覆盖块 2（[16,24)），块 0/1/3/4 必须原样复用
        result = apply_overwrite(manifest, 16, b"Z" * 8, None)
        old_crcs = manifest.chunk_crcs
        new_crcs = result.manifest.chunk_crcs
        for idx in (0, 1, 3, 4):
            self.assertEqual(new_crcs[idx], old_crcs[idx])
        self.assertNotEqual(new_crcs[2], old_crcs[2])
        self.assertEqual(new_crcs[2], zlib.crc32(b"Z" * 8))


class TestTruncation(unittest.TestCase):
    def setUp(self):
        self.old = _pattern(37, seed=505)
        self.manifest = build_manifest(self.old, CHUNK_SIZE)

    def test_truncate_inside_untouched_chunk_reads_prefix_only(self):
        reader = InMemoryReader(self.old)
        result = apply_overwrite(
            self.manifest, 0, b"", reader, truncate=19
        )
        expected = simulate_overwrite(self.old, 0, b"", truncate=19)
        _assert_matches_full_recompute(self, result, expected, CHUNK_SIZE)
        # 块 0/1 整块复用；块 2 截断到 [16,19)，只读 3 个旧字节
        self.assertEqual(result.read_ranges, (ByteRange(16, 19),))
        self.assertEqual(result.bytes_read, 3)

    def test_truncate_at_chunk_boundary_no_reads(self):
        result = apply_overwrite(self.manifest, 0, b"", None, truncate=24)
        expected = simulate_overwrite(self.old, 0, b"", truncate=24)
        _assert_matches_full_recompute(self, result, expected, CHUNK_SIZE)
        self.assertEqual(result.bytes_read, 0)

    def test_truncate_to_zero(self):
        result = apply_overwrite(
            self.manifest, 3, b"abc", None, truncate=0
        )
        self.assertEqual(result.object_length, 0)
        self.assertEqual(result.chunks, ())
        self.assertEqual(result.object_crc32, 0)
        self.assertEqual(result.read_ranges, ())

    def test_truncate_before_write_start(self):
        # 写入完全落在截断点之后 -> 全部丢弃，仅截断生效
        reader = InMemoryReader(self.old)
        result = apply_overwrite(
            self.manifest, 20, b"QQQQ", reader, truncate=10
        )
        expected = simulate_overwrite(self.old, 20, b"QQQQ", truncate=10)
        _assert_matches_full_recompute(self, result, expected, CHUNK_SIZE)
        self.assertEqual(result.object_length, 10)
        # 块 0/1 边界 8 处截断：块 1 仅需读 [8,10)
        self.assertEqual(result.read_ranges, (ByteRange(8, 10),))

    def test_truncate_inside_written_region(self):
        reader = InMemoryReader(self.old)
        result = apply_overwrite(
            self.manifest, 5, _pattern(13, seed=606), reader, truncate=10
        )
        expected = simulate_overwrite(
            self.old, 5, _pattern(13, seed=606), truncate=10
        )
        _assert_matches_full_recompute(self, result, expected, CHUNK_SIZE)
        # [5,10) 全是新字节；[0,5) 为旧前缀 -> 仅读 [0,5)
        self.assertEqual(result.read_ranges, (ByteRange(0, 5),))

    def test_truncate_extends_with_zeros(self):
        # 用块对齐的旧对象（长度 32），扩展到 50：新增块全部为纯补零，
        # 既无旧前缀也无新字节，因此不需要读取旧对象
        aligned = _pattern(32, seed=808)
        aligned_manifest = build_manifest(aligned, CHUNK_SIZE)
        result = apply_overwrite(
            aligned_manifest, 0, b"", None, truncate=50
        )
        expected = simulate_overwrite(aligned, 0, b"", truncate=50)
        _assert_matches_full_recompute(self, result, expected, CHUNK_SIZE)
        self.assertEqual(result.bytes_read, 0)
        # 新增的 [32,40) 全零块（索引 4）与 [40,42)? 实际扩展到 50：
        # 块 4=[32,40) 全零、块 5=[40,48) 全零、块 6=[48,50) 全零
        self.assertEqual(len(result.chunks), 7)
        self.assertEqual(result.chunks[4].crc32, zlib.crc32(b"\x00" * 8))
        self.assertEqual(result.chunks[5].crc32, zlib.crc32(b"\x00" * 8))
        self.assertEqual(result.chunks[6].crc32, zlib.crc32(b"\x00" * 2))

    def test_extension_partial_chunk_with_old_prefix(self):
        # 截断到 35（< 旧长度 37，块内截短）：块 4=[32,35) 为旧前缀
        reader = InMemoryReader(self.old)
        result = apply_overwrite(
            self.manifest, 0, b"", reader, truncate=35
        )
        expected = simulate_overwrite(self.old, 0, b"", truncate=35)
        _assert_matches_full_recompute(self, result, expected, CHUNK_SIZE)
        self.assertEqual(result.read_ranges, (ByteRange(32, 35),))

        # 在块对齐的对象上扩展：[32,?) 起点即边界，纯补零不读取
        aligned = _pattern(32, seed=808)
        aligned_manifest = build_manifest(aligned, CHUNK_SIZE)
        result2 = apply_overwrite(
            aligned_manifest, 0, b"", None, truncate=42
        )
        expected2 = simulate_overwrite(aligned, 0, b"", truncate=42)
        _assert_matches_full_recompute(
            self, result2, expected2, CHUNK_SIZE
        )
        self.assertEqual(result2.bytes_read, 0)


class TestFuzzAgainstFullRecompute(unittest.TestCase):
    """随机模糊：合成路径与全量重放/重算在大量交错场景下逐字节等价。"""

    def test_randomized_overwrites_and_truncations(self):
        rng = random.Random(9090)
        for iteration in range(200):
            chunk_size = rng.choice([1, 2, 3, 5, 8, 16])
            old = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 60)))
            manifest = build_manifest(old, chunk_size)

            offset = rng.randrange(0, len(old) + 1)
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 25)))

            mode = rng.randrange(4)
            truncate = None
            if mode == 1:
                truncate = rng.randrange(0, len(old) + len(data) + 10)
            elif mode == 2:
                truncate = rng.randrange(0, max(1, offset + 1))
            elif mode == 3 and len(old) > 0:
                truncate = 0

            reader = InMemoryReader(old)
            with self.subTest(
                iteration=iteration,
                old_len=len(old),
                chunk_size=chunk_size,
                offset=offset,
                data_len=len(data),
                truncate=truncate,
            ):
                result = apply_overwrite(
                    manifest, offset, data, reader, truncate=truncate
                )
                expected = simulate_overwrite(
                    old, offset, data, truncate=truncate
                )
                _assert_matches_full_recompute(
                    self, result, expected, chunk_size
                )
                _assert_read_invariants(
                    self, result, offset, len(data), chunk_size
                )
                self.assertEqual(
                    reader.performed_reads, result.performed_reads
                )

    def test_sequential_random_history_equals_replay(self):
        rng = random.Random(31337)
        data = bytes(rng.randrange(256) for _ in range(80))
        chunk_size = 7
        manifest = build_manifest(data, chunk_size)

        for step in range(60):
            offset = rng.randrange(0, len(data) + 1)
            payload = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 15)))
            truncate = (
                rng.randrange(0, len(data) + len(payload) + 12)
                if rng.random() < 0.5
                else None
            )
            reader = InMemoryReader(data)
            result = apply_overwrite(
                manifest, offset, payload, reader, truncate=truncate
            )
            data = simulate_overwrite(
                data, offset, payload, truncate=truncate
            )
            with self.subTest(step=step):
                _assert_matches_full_recompute(
                    self, result, data, chunk_size
                )
            manifest = result.manifest
        # 最终整对象 CRC 与一次性全量重算一致
        self.assertEqual(manifest.object_length, len(data))


class TestErrorPaths(unittest.TestCase):
    def setUp(self):
        self.old = _pattern(37, seed=707)
        self.manifest = build_manifest(self.old, CHUNK_SIZE)

    def test_offset_beyond_eof_rejected(self):
        with self.assertRaises(ValueError):
            apply_overwrite(self.manifest, 38, b"x", None)

    def test_negative_offset_rejected(self):
        with self.assertRaises(ValueError):
            apply_overwrite(self.manifest, -1, b"x", None)

    def test_negative_truncate_rejected(self):
        with self.assertRaises(ValueError):
            apply_overwrite(self.manifest, 0, b"x", None, truncate=-1)

    def test_missing_reader_raises_when_read_needed(self):
        with self.assertRaises(ValueError):
            apply_overwrite(self.manifest, 5, b"abc", None)

    def test_reader_oob_surfaces_error(self):
        class BrokenReader:
            def read_range(self, start, end):
                return b"too-short"

        with self.assertRaises(OSError):
            apply_overwrite(
                self.manifest, 5, b"abc", BrokenReader()
            )

    def test_bytearray_and_memoryview_accepted(self):
        expected = simulate_overwrite(self.old, 5, b"abc")
        r1 = apply_overwrite(
            self.manifest, 5, bytearray(b"abc"), InMemoryReader(self.old)
        )
        r2 = apply_overwrite(
            self.manifest, 5, memoryview(b"abc"), InMemoryReader(self.old)
        )
        self.assertEqual(r1.object_crc32, zlib.crc32(expected))
        self.assertEqual(r2.object_crc32, zlib.crc32(expected))


if __name__ == "__main__":
    unittest.main()
