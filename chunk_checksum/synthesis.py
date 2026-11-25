"""分块校验合成核心逻辑。

合成策略（固定分块网格，块起点恒为 ``index * chunk_size``）：

1. 全覆盖块：块内容完全来自本次新字节，直接对新字节算 CRC32，不读旧对象。
2. 边缘块：块内只有一部分被覆盖，按“旧字节 / 新字节 / 补零”切段；
   仅对 ``old`` 段向读取器申请旧对象的对应范围，新段与零段不产生读取。
3. 未触及的整块：CRC32 直接从旧清单复用，零读取、零重算。
4. 截断落在某块内部时，该块幸存前缀若仍为旧字节，同样只读取该前缀；
   截断点之后的块整体消失。
5. 整对象 CRC32 由各块 CRC32 经 :func:`crc32_combine` 按块长度拼接，
   与 ``zlib.crc32(完整新对象)`` 严格相等。
"""

from __future__ import annotations

import zlib

from .crc32 import crc32_combine, crc32_zero_extend
from .models import ByteRange, Chunk, ChunkManifest, OverwriteResult
from .reader import Reader

# 段类型：old=旧对象字节，new=本次写入字节，zero=EOF 外补零
_SEG_OLD = "old"
_SEG_NEW = "new"
_SEG_ZERO = "zero"


def build_manifest(
    data: bytes | bytearray | memoryview, chunk_size: int
) -> ChunkManifest:
    """由完整字节按固定块大小直接构造分块清单。"""
    if not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size 必须为正整数")
    payload = bytes(data)
    chunks: list[Chunk] = []
    for index, start in enumerate(range(0, len(payload), chunk_size)):
        piece = payload[start : start + chunk_size]
        chunks.append(
            Chunk(index=index, length=len(piece), crc32=zlib.crc32(piece))
        )
    return ChunkManifest(
        chunk_size=chunk_size,
        chunks=tuple(chunks),
        object_length=len(payload),
    )


def _merge_ranges(ranges: list[ByteRange]) -> tuple[ByteRange, ...]:
    """合并排序重叠/相邻的字节范围，并去重。"""
    ordered = sorted(ranges, key=lambda r: (r.start, r.end))
    merged: list[ByteRange] = []
    for rng in ordered:
        if merged and rng.start <= merged[-1].end:
            if rng.end > merged[-1].end:
                prev = merged[-1]
                merged[-1] = ByteRange(prev.start, rng.end)
        else:
            merged.append(rng)
    return tuple(merged)


def _merge_adjacent_segments(
    segments: list[tuple[int, int, str]],
) -> list[tuple[int, int, str]]:
    """合并相邻且类型相同的段，避免边界把同类型内容切碎后妨碍整块复用。"""
    merged: list[tuple[int, int, str]] = []
    for seg in segments:
        if merged and merged[-1][2] == seg[2] and merged[-1][1] == seg[0]:
            prev_start, _, kind = merged[-1]
            merged[-1] = (prev_start, seg[1], kind)
        else:
            merged.append(seg)
    return merged


def _whole_object_crc(manifest: ChunkManifest) -> int:
    """按块顺序组合各块 CRC，得到整对象 CRC32（无需原始字节）。"""
    crc = 0
    for chunk in manifest.chunks:
        crc = crc32_combine(crc, chunk.crc32, chunk.length)
    return crc


def apply_overwrite(
    old_manifest: ChunkManifest,
    offset: int,
    new_data: bytes | bytearray | memoryview,
    reader: Reader | None,
    *,
    truncate: int | None = None,
) -> OverwriteResult:
    """在旧对象上执行一次覆盖写并合成新的分块与整对象校验。

    参数
    ----
    old_manifest:
        旧对象的分块清单（含每块长度与 IEEE CRC32）。
    offset:
        覆盖写起始偏移，必须满足 ``0 <= offset <= 旧对象长度``；
        不支持越过 EOF 写洞（追加请使用 ``offset == 旧对象长度``）。
    new_data:
        本次写入的新字节。
    reader:
        旧对象读取器；仅当确实需要读取旧字节（边缘块/块内截断）时才会
        被调用。若本次合成无需读取，可传 ``None``。
    truncate:
        可选的写后截断位置（新对象最终长度）。语义同 ftruncate：
        小于写后长度则截短，大于则以零字节填充。``None`` 表示不截断，
        最终长度为 ``max(旧长度, offset + len(new_data))``。

    返回
    ----
    :class:`OverwriteResult`，含新对象长度、更新后清单、整对象 CRC32、
    理论读取范围与实际读取记录。
    """
    if not isinstance(old_manifest, ChunkManifest):
        raise TypeError("old_manifest 必须为 ChunkManifest 实例")
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("offset 必须为非负整数")

    payload = bytes(new_data)
    old_len = old_manifest.object_length
    chunk_size = old_manifest.chunk_size

    if offset > old_len:
        raise ValueError(
            f"offset={offset} 超出旧对象长度 {old_len}，不支持越过 EOF 写洞"
        )

    write_end = offset + len(payload)
    if truncate is None:
        final_len = max(old_len, write_end)
    else:
        if not isinstance(truncate, int) or truncate < 0:
            raise ValueError("truncate 必须为非负整数")
        final_len = truncate

    # 截成空对象：一切清零，无需读取
    if final_len == 0:
        empty_manifest = ChunkManifest(
            chunk_size=chunk_size, chunks=(), object_length=0
        )
        return OverwriteResult(
            object_length=0,
            manifest=empty_manifest,
            object_crc32=0,
            read_ranges=(),
            performed_reads=(),
            bytes_read=0,
        )

    # 空写且不改变长度：纯幂等操作，原样返回，零读取、零重算
    if len(payload) == 0 and final_len == old_len:
        return OverwriteResult(
            object_length=old_len,
            manifest=old_manifest,
            object_crc32=_whole_object_crc(old_manifest),
            read_ranges=(),
            performed_reads=(),
            bytes_read=0,
        )

    old_by_index = {c.index: c for c in old_manifest.chunks}
    last_index = (final_len - 1) // chunk_size

    # 每个块的段计划：(index, ((seg_start, seg_end, kind), ...), 复用CRC或None)
    chunk_plans: list[tuple[int, tuple, int | None]] = []
    planned_reads: list[ByteRange] = []

    for index in range(0, last_index + 1):
        cstart = index * chunk_size
        cend = min((index + 1) * chunk_size, final_len)

        # 用块边界 + 三个全局边界（写起点、写终点、旧 EOF）切段
        cuts = {cstart, cend}
        for boundary in (offset, write_end, old_len):
            if cstart < boundary < cend:
                cuts.add(boundary)
        ordered_cuts = sorted(cuts)

        segments: list[tuple[int, int, str]] = []
        for seg_start, seg_end in zip(ordered_cuts, ordered_cuts[1:]):
            if offset <= seg_start < write_end:
                kind = _SEG_NEW
            elif seg_start < old_len:
                kind = _SEG_OLD
            else:
                kind = _SEG_ZERO
            segments.append((seg_start, seg_end, kind))
        segments = _merge_adjacent_segments(segments)

        # 整块仍是旧字节，且计划覆盖的字节区间与旧块完全一致
        # （注意末块可能短于 chunk_size，必须按实际区间而非网格长度比较）
        # -> 直接复用旧 CRC，零读取、零重算
        old_chunk = old_by_index.get(index)
        reuse_crc: int | None = None
        if (
            len(segments) == 1
            and segments[0][2] == _SEG_OLD
            and old_chunk is not None
            and old_chunk.length == segments[0][1] - segments[0][0]
        ):
            reuse_crc = old_chunk.crc32
        else:
            for seg_start, seg_end, kind in segments:
                if kind == _SEG_OLD:
                    planned_reads.append(ByteRange(seg_start, seg_end))

        chunk_plans.append((index, tuple(segments), reuse_crc))

    read_ranges = _merge_ranges(planned_reads)

    # 实际读取：合并后的范围一次性读取，段级别通过缓存切片复用
    performed: list[ByteRange] = []
    cache: dict[tuple[int, int], bytes] = {}

    def get_old(seg_start: int, seg_end: int) -> bytes:
        key = (seg_start, seg_end)
        if key in cache:
            return cache[key]
        if reader is None:
            raise ValueError("本次合成需要读取旧对象字节，但未提供 reader")
        if not isinstance(reader, Reader):
            raise TypeError("reader 必须实现 read_range(start, end) 方法")
        piece = bytes(reader.read_range(seg_start, seg_end))
        if len(piece) != seg_end - seg_start:
            raise OSError(
                f"reader 返回 {len(piece)} 字节，请求 {seg_end - seg_start} 字节"
            )
        cache[key] = piece
        performed.append(ByteRange(seg_start, seg_end))
        return piece

    new_chunks: list[Chunk] = []
    for index, segments, reuse_crc in chunk_plans:
        cstart = index * chunk_size
        cend = min((index + 1) * chunk_size, final_len)
        length = cend - cstart

        if reuse_crc is not None:
            new_chunks.append(Chunk(index=index, length=length, crc32=reuse_crc))
            continue

        crc = 0
        for seg_start, seg_end, kind in segments:
            seg_len = seg_end - seg_start
            if kind == _SEG_NEW:
                crc = zlib.crc32(
                    payload[seg_start - offset : seg_end - offset], crc
                )
            elif kind == _SEG_OLD:
                crc = zlib.crc32(get_old(seg_start, seg_end), crc)
            else:
                # EOF 外补零：不分配零字节缓冲，直接用 CRC 组合外推
                crc = crc32_zero_extend(crc, seg_len)
        new_chunks.append(Chunk(index=index, length=length, crc32=crc))

    # 整对象 CRC = 按块顺序组合各块 CRC（无需任何原始字节）
    object_crc = _whole_object_crc(
        ChunkManifest(
            chunk_size=chunk_size,
            chunks=tuple(new_chunks),
            object_length=final_len,
        )
    )

    new_manifest = ChunkManifest(
        chunk_size=chunk_size,
        chunks=tuple(new_chunks),
        object_length=final_len,
    )
    return OverwriteResult(
        object_length=final_len,
        manifest=new_manifest,
        object_crc32=object_crc,
        read_ranges=read_ranges,
        performed_reads=tuple(performed),
        bytes_read=sum(r.length for r in performed),
    )
