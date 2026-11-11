"""对已生成的 ZIP 做字节级篡改，专门制造验收所需的"坏包"。

篡改都基于 ZIP 内的偏移直接改字节，不经过采集器代码，
保证测试面对的是真实的网络字节流而非内存对象。
"""

from __future__ import annotations

import struct

from selective_zip.protocol import (
    SIG_CENTRAL_DIR_HEADER,
    SIZE_CENTRAL_DIR_HEADER_FIXED,
    SIZE_LOCAL_FILE_HEADER_FIXED,
)


def find_local_header_offset(blob: bytes, member_filename: str) -> int:
    """通过中央目录反查某成员的本地头偏移。"""
    target = member_filename.encode("utf-8")
    pos = 0
    while True:
        idx = blob.find(SIG_CENTRAL_DIR_HEADER, pos)
        if idx < 0:
            raise KeyError(f"中央目录中找不到成员 {member_filename!r}")
        fields = struct.unpack_from("<4s6H3L5H2L", blob, idx)
        name_len, extra_len, comment_len = fields[10], fields[11], fields[12]
        local_offset = fields[16]
        name_start = idx + SIZE_CENTRAL_DIR_HEADER_FIXED
        name = blob[name_start : name_start + name_len]
        if name == target:
            return local_offset
        pos = idx + 4


def _local_header_fields(blob: bytes, offset: int):
    if blob[offset : offset + 4] != b"PK\x03\x04":
        raise ValueError("指定偏移处不是本地文件头")
    return struct.unpack_from("<4s5H3L2H", blob, offset)


def flip_compressed_data_byte(blob: bytes, member_filename: str) -> bytes:
    """翻转成员压缩数据区第一个字节，制造 CRC-32 不匹配。

    stored 成员会直接破坏内容；deflate 通常导致解压异常或 CRC 失败，
    两种情况采集器都必须拒收。
    """
    local_off = find_local_header_offset(blob, member_filename)
    fields = _local_header_fields(blob, local_off)
    name_len, extra_len = fields[9], fields[10]
    data_off = local_off + SIZE_LOCAL_FILE_HEADER_FIXED + name_len + extra_len
    out = bytearray(blob)
    out[data_off] ^= 0xFF
    return bytes(out)


def corrupt_local_header_crc(blob: bytes, member_filename: str) -> bytes:
    """把本地头里的 CRC-32 改成错误值，而中央目录保持正确，制造头冲突。"""
    local_off = find_local_header_offset(blob, member_filename)
    fields = _local_header_fields(blob, local_off)
    flags = fields[2]
    if flags & 0x08:
        raise ValueError("bit3 成员本地头 CRC 允许为 0，请改用非 bit3 成员制造冲突")
    crc_field_offset = local_off + 14  # 固定头内 CRC 字段偏移
    out = bytearray(blob)
    original = struct.unpack_from("<L", out, crc_field_offset)[0]
    struct.pack_into("<L", out, crc_field_offset, original ^ 0xA5A5A5A5)
    return bytes(out)


def corrupt_local_header_sizes(blob: bytes, member_filename: str) -> bytes:
    """把本地头压缩/未压缩大小改错，制造头冲突。"""
    local_off = find_local_header_offset(blob, member_filename)
    out = bytearray(blob)
    csize_off = local_off + 18
    usize_off = local_off + 22
    csize = struct.unpack_from("<L", out, csize_off)[0]
    usize = struct.unpack_from("<L", out, usize_off)[0]
    struct.pack_into("<L", out, csize_off, max(csize + 7, 1))
    struct.pack_into("<L", out, usize_off, max(usize + 13, 1))
    return bytes(out)


def rewrite_local_compression(blob: bytes, member_filename: str, new_compression: int) -> bytes:
    """仅改写本地头压缩方式字段，使本地头与中央目录冲突。"""
    local_off = find_local_header_offset(blob, member_filename)
    out = bytearray(blob)
    struct.pack_into("<H", out, local_off + 8, new_compression)
    return bytes(out)
