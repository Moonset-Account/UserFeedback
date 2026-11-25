"""数据模型：字节范围、分块元数据、清单与合成结果。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ByteRange:
    """旧对象上需要读取的半开字节区间 ``[start, end)``。"""

    start: int
    end: int

    def __post_init__(self) -> None:
        if not isinstance(self.start, int) or not isinstance(self.end, int):
            raise TypeError("ByteRange 边界必须为整数")
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"非法字节范围: [{self.start}, {self.end})")

    @property
    def length(self) -> int:
        return self.end - self.start

    def __str__(self) -> str:
        return f"[{self.start}, {self.end})"


@dataclass(frozen=True)
class Chunk:
    """单块元数据。

    - ``index``       块在固定分块网格中的序号，块起点 = ``index * chunk_size``
    - ``length``      块实际字节数（最后一块可小于块大小）
    - ``crc32``       该块内容的 IEEE CRC32（``zlib.crc32`` 语义）
    """

    index: int
    length: int
    crc32: int

    def __post_init__(self) -> None:
        if not isinstance(self.index, int) or self.index < 0:
            raise ValueError("chunk index 必须为非负整数")
        if not isinstance(self.length, int) or self.length <= 0:
            raise ValueError("chunk length 必须为正整数")
        if not isinstance(self.crc32, int) or not (0 <= self.crc32 <= 0xFFFFFFFF):
            raise ValueError("crc32 必须落在 32 位无符号整数范围内")

    @property
    def crc32_hex(self) -> str:
        return f"{self.crc32:08x}"


@dataclass(frozen=True)
class ChunkManifest:
    """对象的分块清单。

    分块网格固定：第 ``i`` 块覆盖 ``[i * chunk_size, (i + 1) * chunk_size)``，
    最后一块允许更短。``object_length`` 与各块长度必须自洽。
    """

    chunk_size: int
    chunks: tuple[Chunk, ...]
    object_length: int

    def __post_init__(self) -> None:
        if not isinstance(self.chunk_size, int) or self.chunk_size <= 0:
            raise ValueError("chunk_size 必须为正整数")
        if not isinstance(self.object_length, int) or self.object_length < 0:
            raise ValueError("object_length 必须为非负整数")
        if not isinstance(self.chunks, tuple):
            object.__setattr__(self, "chunks", tuple(self.chunks))
        chunks = self.chunks
        if self.object_length == 0:
            if chunks:
                raise ValueError("零长度对象不应包含任何块")
            return
        if not chunks:
            raise ValueError("非零长度对象必须至少包含一个块")
        expected_indexes = list(range(len(chunks)))
        for position, chunk in enumerate(chunks):
            if chunk.index != expected_indexes[position]:
                raise ValueError("块索引必须从 0 开始连续递增")
            if chunk.length > self.chunk_size:
                raise ValueError("块长度不能超过 chunk_size")
            is_last = position == len(chunks) - 1
            if not is_last and chunk.length != self.chunk_size:
                raise ValueError("除最后一块外，其余块长度必须等于 chunk_size")
        last = chunks[-1]
        if (
            last.length < self.chunk_size
            and self.object_length % self.chunk_size == 0
        ):
            raise ValueError(
                "最后一块短于 chunk_size 时，object_length 不能被 chunk_size 整除"
            )
        total = sum(c.length for c in chunks)
        if total != self.object_length:
            raise ValueError(
                f"各块长度之和 {total} 与 object_length {self.object_length} 不一致"
            )

    @property
    def chunk_crcs(self) -> tuple[int, ...]:
        return tuple(c.crc32 for c in self.chunks)


@dataclass(frozen=True)
class OverwriteResult:
    """一次覆盖写（含可选截断）后的合成结果。

    - ``object_length``       写/截断后的新对象长度
    - ``manifest``            更新后的分块清单
    - ``object_crc32``        新对象的整对象 CRC32（与全量重算一致）
    - ``read_ranges``         理论上需要从旧对象读取的字节范围（已合并排序）
    - ``performed_reads``     实际向读取器发起的读取范围（分段粒度）
    - ``bytes_read``          实际读取的总字节数
    """

    object_length: int
    manifest: ChunkManifest
    object_crc32: int
    read_ranges: tuple[ByteRange, ...]
    performed_reads: tuple[ByteRange, ...]
    bytes_read: int

    @property
    def chunks(self) -> tuple[Chunk, ...]:
        """更新后的分块清单中的块元数据（``manifest.chunks`` 的便捷视图）。"""
        return self.manifest.chunks

    @property
    def object_crc32_hex(self) -> str:
        return f"{self.object_crc32:08x}"

    def describe_read_ranges(self) -> str:
        """人类可读的读取范围描述。"""
        if not self.read_ranges:
            return "无需读取旧对象（全部由新字节/复用块/对齐补零得到）"
        parts = [str(r) for r in self.read_ranges]
        return f"需读取 {len(parts)} 段共 {self.bytes_read} 字节: " + ", ".join(parts)
