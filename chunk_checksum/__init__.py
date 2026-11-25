"""分块 CRC32 校验合成模块。

公开 API：

- :class:`ByteRange`           旧对象上的字节读取范围 [start, end)
- :class:`Chunk`               单块元数据（索引、长度、CRC32）
- :class:`ChunkManifest`       分块清单（固定块大小 + 块元数据）
- :class:`OverwriteResult`     一次覆盖合成的结果
- :class:`Reader` / :class:`InMemoryReader`  旧对象读取抽象与内存实现
- :func:`build_manifest`       由完整字节直接构造分块清单
- :func:`apply_overwrite`      一次覆盖写（可带截断）的校验合成
"""

from .models import ByteRange, Chunk, ChunkManifest, OverwriteResult
from .reader import InMemoryReader, Reader
from .synthesis import apply_overwrite, build_manifest

__all__ = [
    "ByteRange",
    "Chunk",
    "ChunkManifest",
    "OverwriteResult",
    "Reader",
    "InMemoryReader",
    "build_manifest",
    "apply_overwrite",
]
