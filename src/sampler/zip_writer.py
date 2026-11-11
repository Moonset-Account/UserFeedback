"""最小 ZIP / ZIP64 写入器（仅服务于样例生成器）。

这是生成器侧的独立实现，与采集器 :mod:`selective_zip.zip_structs` 解析器
没有共享代码，避免"用同一套读写逻辑互相证明"。

支持能力刻意收敛：
- stored(0) 与 deflate(8)；
- 普通 ZIP 与强制 ZIP64（即使字段没越界也写 ZIP64 结构）；
- 可选 bit 3 数据描述符；
- 可选 UTF-8 文件名标志。
"""

from __future__ import annotations

import dataclasses
import struct
import zlib

from selective_zip.protocol import (
    COMPRESSION_DEFLATE,
    COMPRESSION_STORED,
    FLAG_UTF8_NAMES,
    SIG_CENTRAL_DIR_HEADER,
    SIG_DATA_DESCRIPTOR,
    SIG_END_OF_CENTRAL_DIR,
    SIG_LOCAL_FILE_HEADER,
    SIG_ZIP64_EOCD,
    SIG_ZIP64_EOCD_LOCATOR,
    SIZE_ZIP64_EOCD_RECORD_FIXED,
    UINT16_MAX,
    ZIP64_MAGIC,
    ZIP64_VERSION,
)


@dataclasses.dataclass
class ZipMember:
    """待写入的一个成员。"""

    filename: str
    data: bytes
    compression: int = COMPRESSION_DEFLATE
    force_utf8_flag: bool = False
    use_data_descriptor: bool = False


class ZipWriter:
    """把 :class:`ZipMember` 列表序列化为完整 ZIP 字节。"""

    def __init__(self, members: list[ZipMember], *, force_zip64: bool = False):
        self.members = members
        self.force_zip64 = force_zip64

    def render(self) -> bytes:
        local_blobs: list[bytes] = []
        central_blobs: list[bytes] = []
        offset = 0

        for member in self.members:
            raw_name = member.filename.encode("utf-8")
            flags = FLAG_UTF8_NAMES if member.force_utf8_flag else 0
            if member.use_data_descriptor:
                flags |= 0x08

            if member.compression == COMPRESSION_STORED:
                compressed = member.data
            elif member.compression == COMPRESSION_DEFLATE:
                compressed = _deflate_raw(member.data)
            else:
                raise ValueError(f"生成器不支持压缩方式 {member.compression}")

            crc = zlib.crc32(member.data) & 0xFFFFFFFF
            usize, csize = len(member.data), len(compressed)
            use_z64 = self.force_zip64 or csize >= ZIP64_MAGIC or usize >= ZIP64_MAGIC or offset >= ZIP64_MAGIC
            version_needed = ZIP64_VERSION if use_z64 else 20

            local_blob, local_size = self._render_local(
                raw_name, flags, member.compression, crc, csize, usize,
                compressed, use_z64, member.use_data_descriptor, version_needed,
            )
            central_blob = self._render_central(
                raw_name, flags, member.compression, crc, csize, usize,
                offset, use_z64, version_needed,
            )
            local_blobs.append(local_blob)
            central_blobs.append(central_blob)
            offset += local_size

        central_offset = offset
        central_blob = b"".join(central_blobs)
        central_size = len(central_blob)
        eocd_blob = self._render_eocd(
            len(self.members), central_size, central_offset,
        )
        return b"".join(local_blobs) + central_blob + eocd_blob

    # ------------------------------------------------------------------
    def _render_local(
        self, raw_name, flags, compression, crc, csize, usize,
        compressed, use_z64, use_descriptor, version_needed,
    ) -> tuple[bytes, int]:
        extra = b""
        if use_z64:
            # 本地头 ZIP64 extra：顺序为未压缩大小、压缩大小
            extra = struct.pack("<HH", 0x0001, 16) + struct.pack("<QQ", usize, csize)
        if use_descriptor:
            # bit3：本地头 CRC/大小固定写 0，真实值在数据描述符
            h_crc, h_csize, h_usize = 0, 0, 0
        elif use_z64:
            # ZIP64：32 位字段必须写占位值 0xFFFFFFFF，真实值在 extra
            h_crc, h_csize, h_usize = crc, ZIP64_MAGIC, ZIP64_MAGIC
        else:
            h_crc, h_csize, h_usize = crc, csize, usize
        fixed = struct.pack(
            "<4s5H3L2H",
            SIG_LOCAL_FILE_HEADER,
            version_needed,
            flags,
            compression,
            0,  # mod time
            0x21,  # mod date（虚构固定值）
            h_crc,
            h_csize,
            h_usize,
            len(raw_name),
            len(extra),
        )
        head = fixed + raw_name + extra
        body = head + compressed
        total = len(body)
        if use_descriptor:
            body += SIG_DATA_DESCRIPTOR + struct.pack("<LLL", crc, csize, usize)
            total = len(body)
        return body, total

    def _render_central(
        self, raw_name, flags, compression, crc, csize, usize,
        local_offset, use_z64, version_needed,
    ) -> bytes:
        extra = b""
        if use_z64:
            extra = struct.pack("<HH", 0x0001, 24) + struct.pack("<QQQ", usize, csize, local_offset)
        fixed = struct.pack(
            "<4s6H3L5H2L",
            SIG_CENTRAL_DIR_HEADER,
            20,  # version made by
            version_needed,
            flags,
            compression,
            0,
            0x21,
            crc,
            csize if not use_z64 else ZIP64_MAGIC,
            usize if not use_z64 else ZIP64_MAGIC,
            len(raw_name),
            len(extra),
            0,  # comment length
            0,  # disk number start
            0,  # internal attrs
            0,  # external attrs
            local_offset if not use_z64 else ZIP64_MAGIC,
        )
        return fixed + raw_name + extra

    def _render_eocd(self, count: int, central_size: int, central_offset: int) -> bytes:
        use_z64 = self.force_zip64 or central_size >= ZIP64_MAGIC or central_offset >= ZIP64_MAGIC or count >= UINT16_MAX
        if not use_z64:
            return struct.pack(
                "<4s4H2LH",
                SIG_END_OF_CENTRAL_DIR,
                0, 0, count, count, central_size, central_offset, 0,
            )
        # ZIP64 EOCD Record
        record = struct.pack(
            "<4sQ2H2Q2L2Q",
            SIG_ZIP64_EOCD,
            SIZE_ZIP64_EOCD_RECORD_FIXED - 12,  # record 自身之后的大小（44）
            45,  # version made by
            45,  # version needed
            0, 0,  # disk numbers
            count, count,
            central_size,
            central_offset,
        )
        locator = struct.pack(
            "<4sLQL",
            SIG_ZIP64_EOCD_LOCATOR,
            0,  # disk with zip64 eocd
            central_offset + central_size,  # zip64 eocd offset
            1,  # total disks
        )
        eocd = struct.pack(
            "<4s4H2LH",
            SIG_END_OF_CENTRAL_DIR,
            UINT16_MAX, UINT16_MAX, UINT16_MAX, UINT16_MAX,
            ZIP64_MAGIC, ZIP64_MAGIC, 0,
        )
        return record + locator + eocd


def _deflate_raw(data: bytes) -> bytes:
    compressor = zlib.compressobj(6, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()
