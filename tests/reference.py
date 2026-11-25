"""测试辅助：全量参考实现。

不使用任何分块合成逻辑，直接在内存里执行覆盖写/截断/补零，
再用 :func:`zlib.crc32` 全量重算，作为合成结果的对照基准。
"""

from __future__ import annotations

import zlib

from chunk_checksum import build_manifest


def simulate_overwrite(
    old: bytes,
    offset: int,
    data: bytes,
    truncate: int | None = None,
) -> bytes:
    """朴素地模拟一次覆盖写 + 可选 ftruncate 截断，返回完整新对象。"""
    buf = bytearray(old)
    write_end = offset + len(data)
    if write_end > len(buf):
        # 追加写：先扩展到写终点（扩展部分随后被 data 覆盖）
        buf.extend(b"\x00" * (write_end - len(buf)))
    buf[offset:write_end] = data
    if truncate is not None:
        if truncate > len(buf):
            buf.extend(b"\x00" * (truncate - len(buf)))
        else:
            del buf[truncate:]
    return bytes(buf)


def full_recompute_manifest(data: bytes, chunk_size: int):
    """全量重算分块清单与整对象 CRC32。"""
    manifest = build_manifest(data, chunk_size)
    return manifest, zlib.crc32(data)
