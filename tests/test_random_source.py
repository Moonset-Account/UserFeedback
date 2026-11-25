"""RandomByteStream 的读取、定位、耗尽与状态测试。"""

from __future__ import annotations

import io
import unittest

from selector import RandomByteStream, RandomnessExhaustedError


def u64(value: int) -> bytes:
    return value.to_bytes(8, "big", signed=False)


class BytesSourceTests(unittest.TestCase):
    def test_read_exact_and_offset(self) -> None:
        stream = RandomByteStream(b"0123456789")
        self.assertEqual(stream.offset, 0)
        self.assertEqual(stream.read_exact(3), b"012")
        self.assertEqual(stream.offset, 3)
        self.assertEqual(stream.read_exact(4), b"3456")
        self.assertEqual(stream.offset, 7)

    def test_seek_forward_and_backward(self) -> None:
        stream = RandomByteStream(b"abcdef")
        stream.read_exact(2)
        stream.seek(5)
        self.assertEqual(stream.read_exact(1), b"f")
        stream.seek(0)
        self.assertEqual(stream.read_exact(1), b"a")
        with self.assertRaises(ValueError):
            stream.seek(-1)
        with self.assertRaises(RandomnessExhaustedError):
            stream.seek(99)

    def test_exhaustion_reports_shortage(self) -> None:
        stream = RandomByteStream(b"abc")
        with self.assertRaises(RandomnessExhaustedError):
            stream.read_exact(4)
        # 耗尽后位置停在流末尾
        self.assertEqual(stream.offset, 3)

    def test_read_uint64_big_endian(self) -> None:
        stream = RandomByteStream(u64(0x0102030405060708))
        self.assertEqual(stream.read_uint64(), 0x0102030405060708)
        self.assertEqual(stream.offset, 8)

    def test_uint64_short_stream_exhausted(self) -> None:
        stream = RandomByteStream(b"\x00" * 7)
        with self.assertRaises(RandomnessExhaustedError):
            stream.read_uint64()
        self.assertEqual(stream.offset, 7)

    def test_snapshot_restore_offset(self) -> None:
        stream = RandomByteStream(b"abcdefgh")
        stream.read_exact(3)
        state = stream.snapshot()
        stream.read_exact(5)
        stream.restore(state)
        self.assertEqual(stream.offset, 3)
        self.assertEqual(stream.read_exact(2), b"de")

    def test_invalid_source_type(self) -> None:
        with self.assertRaises(TypeError):
            RandomByteStream(123)  # type: ignore[arg-type]

    def test_bytearray_and_memoryview_supported(self) -> None:
        stream = RandomByteStream(bytearray(b"xy"))
        self.assertEqual(stream.read_exact(2), b"xy")
        view = RandomByteStream(memoryview(b"zw"))
        self.assertEqual(view.read_exact(2), b"zw")


class IteratorSourceTests(unittest.TestCase):
    def test_chunked_reads_and_offset(self) -> None:
        stream = RandomByteStream(iter([b"abc", b"def", b"gh"]))
        self.assertEqual(stream.read_exact(4), b"abcd")
        self.assertEqual(stream.offset, 4)
        self.assertEqual(stream.read_exact(4), b"efgh")
        self.assertEqual(stream.offset, 8)

    def test_forward_seek_skips_bytes(self) -> None:
        stream = RandomByteStream(iter([b"abcdef", b"ghij"]))
        stream.seek(5)
        self.assertEqual(stream.offset, 5)
        self.assertEqual(stream.read_exact(3), b"fgh")

    def test_backward_seek_rejected(self) -> None:
        stream = RandomByteStream(iter([b"abcdef"]))
        stream.read_exact(3)
        with self.assertRaises(RandomnessExhaustedError):
            stream.seek(1)

    def test_exhaustion_mid_read(self) -> None:
        stream = RandomByteStream(iter([b"ab"]))
        with self.assertRaises(RandomnessExhaustedError):
            stream.read_exact(5)

    def test_skip_beyond_end_exhausted(self) -> None:
        stream = RandomByteStream(iter([b"ab", b"cd"]))
        with self.assertRaises(RandomnessExhaustedError):
            stream.seek(10)

    def test_non_bytes_chunk_type_error(self) -> None:
        stream = RandomByteStream(iter(["not-bytes"]))  # type: ignore[list-item]
        with self.assertRaises(TypeError):
            stream.read_exact(1)

    def test_snapshot_restore_via_seek(self) -> None:
        # 顺序迭代器只能前移：恢复时在全新迭代器上定位到快照偏移。
        chunks = [b"abcdefgh"]
        stream = RandomByteStream(iter(chunks))
        stream.read_exact(2)
        state = stream.snapshot()
        stream.read_exact(4)
        restored = RandomByteStream(iter([b"abcdefgh"]))
        restored.restore(state)  # 新流前移到记录偏移 2
        self.assertEqual(restored.offset, 2)
        self.assertEqual(restored.read_exact(2), b"cd")


class FileSourceTests(unittest.TestCase):
    def test_read_and_forward_seek(self) -> None:
        stream = RandomByteStream(io.BytesIO(b"0123456789"))
        self.assertEqual(stream.read_exact(4), b"0123")
        stream.seek(7)
        self.assertEqual(stream.read_exact(2), b"78")

    def test_short_file_exhausted(self) -> None:
        stream = RandomByteStream(io.BytesIO(b"\x01\x02"))
        with self.assertRaises(RandomnessExhaustedError):
            stream.read_uint64()

    def test_backward_seek_rejected(self) -> None:
        stream = RandomByteStream(io.BytesIO(b"abcdef"))
        stream.read_exact(4)
        with self.assertRaises(RandomnessExhaustedError):
            stream.seek(2)


if __name__ == "__main__":
    unittest.main()
