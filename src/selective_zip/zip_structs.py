"""ZIP / ZIP64 二进制结构解析。

只实现选择性采集需要的部分：
- 在文件尾部窗口中定位 EOCD，必要时沿 ZIP64 Locator/Record 找到中央目录；
- 解析中央目录文件头（Central Directory Header）及其 ZIP64 扩展字段；
- 解析本地文件头（Local File Header）；
- 校验中央目录条目与本地文件头的一致性；
- 解析 bit 3 数据描述符。

所有解析都针对"已经通过 Range 取回的字节块"进行，本模块不发起任何网络请求。
"""

from __future__ import annotations

import dataclasses
import struct
from typing import Optional

from . import protocol as P
from .errors import HeaderMismatch, ZipStructureError


# ----------------------------------------------------------------------
# 数据模型
# ----------------------------------------------------------------------
@dataclasses.dataclass
class EocdInfo:
    """EOCD（及可能存在的 ZIP64 结构）解析结果。"""

    disk_number: int
    cd_disk_number: int
    entries_on_disk: int
    total_entries: int
    central_dir_size: int
    central_dir_offset: int
    comment: bytes
    is_zip64: bool
    zip64_eocd_offset: Optional[int] = None


@dataclasses.dataclass
class CentralEntry:
    """一条中央目录记录。"""

    version_made_by: int
    version_needed: int
    flags: int
    compression: int
    mod_time: int
    mod_date: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    filename: str
    raw_filename: bytes
    extra: bytes
    comment: bytes
    disk_number_start: int
    internal_attrs: int
    external_attrs: int
    local_header_offset: int

    @property
    def is_encrypted(self) -> bool:
        return bool(self.flags & P.FLAG_ENCRYPTED)

    @property
    def has_data_descriptor(self) -> bool:
        return bool(self.flags & P.FLAG_DATA_DESCRIPTOR)

    @property
    def uses_utf8_name(self) -> bool:
        return bool(self.flags & P.FLAG_UTF8_NAMES)


@dataclasses.dataclass
class LocalHeader:
    """一个本地文件头。"""

    version_needed: int
    flags: int
    compression: int
    mod_time: int
    mod_date: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    raw_filename: bytes
    extra: bytes
    header_total_size: int  # 含文件名与扩展字段，数据区相对本地头起点的偏移


@dataclasses.dataclass
class DataDescriptor:
    """bit 3 数据描述符。"""

    crc32: int
    compressed_size: int
    uncompressed_size: int
    has_signature: bool
    total_size: int


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _decode_name(raw: bytes, utf8_flag: bool) -> str:
    if utf8_flag:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ZipStructureError(f"声明 UTF-8 的文件名无法解码：{exc}") from exc
    # 传统 ZIP 文件名默认 CP437
    return raw.decode("cp437", errors="replace")


def parse_zip64_extra(extra: bytes) -> dict[str, int]:
    """解析中央目录/本地头中的 ZIP64 扩展字段（0x0001）。

    返回可能包含 ``uncompressed_size`` / ``compressed_size`` /
    ``local_header_offset`` / ``disk_number`` 中的若干键，
    仅当原始 32 位字段为 0xFFFFFFFF（或盘号为 0xFFFF）时才会出现。
    """
    result: dict[str, int] = {}
    i = 0
    n = len(extra)
    while i + 4 <= n:
        tag, size = struct.unpack_from("<HH", extra, i)
        i += 4
        if i + size > n:
            raise ZipStructureError("扩展字段长度超出实际数据，ZIP 结构可能已截断")
        payload = extra[i : i + size]
        i += size
        if tag != 0x0001:
            continue
        # ZIP64 extra 字段内字段顺序固定（未压缩大小、压缩大小、本地头偏移、盘号），
        # 调用方按"哪些原始字段是占位值"决定取用前几个，这里只负责按 8 字节顺序拆出。
        cursor = 0

        def take8() -> int:
            nonlocal cursor
            if cursor + 8 > len(payload):
                raise ZipStructureError("ZIP64 扩展字段内容不足")
            value = struct.unpack_from("<Q", payload, cursor)[0]
            cursor += 8
            return value

        ordered: list[int] = []
        while cursor + 8 <= len(payload):
            ordered.append(take8())
        if cursor < len(payload):
            # 盘号是 4 字节，只会作为最后一个字段出现
            if len(payload) - cursor == 4:
                ordered.append(struct.unpack_from("<I", payload, cursor)[0])
            else:
                raise ZipStructureError("ZIP64 扩展字段存在无法解释的残余字节")
        result["_ordered"] = ordered
    return result


def _apply_zip64_central(entry_kwargs: dict, extra: bytes) -> None:
    """根据 ZIP64 扩展字段回填中央目录条目的真实大小/偏移/盘号。"""
    parsed = parse_zip64_extra(extra)
    ordered = parsed.get("_ordered")
    if not ordered:
        return
    queue = list(ordered)
    # 顺序与 APPNOTE 一致：未压缩大小、压缩大小、本地头偏移、盘号
    if entry_kwargs["uncompressed_size"] == P.ZIP64_MAGIC:
        entry_kwargs["uncompressed_size"] = queue.pop(0)
    if entry_kwargs["compressed_size"] == P.ZIP64_MAGIC:
        entry_kwargs["compressed_size"] = queue.pop(0)
    if entry_kwargs["local_header_offset"] == P.ZIP64_MAGIC:
        entry_kwargs["local_header_offset"] = queue.pop(0)
    if entry_kwargs["disk_number_start"] == P.UINT16_MAX:
        entry_kwargs["disk_number_start"] = queue.pop(0)
    if queue:
        raise ZipStructureError("ZIP64 中央目录扩展字段含有多余字段")


def _apply_zip64_local(header: LocalHeader) -> None:
    """根据 ZIP64 扩展字段回填本地头的真实大小。

    本地头 ZIP64 extra 的字段顺序固定为：未压缩大小、压缩大小（APPNOTE 4.5.3）。
    """
    parsed = parse_zip64_extra(header.extra)
    ordered = parsed.get("_ordered")
    if not ordered:
        return
    queue = list(ordered)
    if header.uncompressed_size == P.ZIP64_MAGIC:
        header.uncompressed_size = queue.pop(0)
    if header.compressed_size == P.ZIP64_MAGIC:
        header.compressed_size = queue.pop(0)
    # 本地头 ZIP64 扩展不含偏移/盘号
    if queue:
        raise ZipStructureError("ZIP64 本地头扩展字段含有多余字段")


# ----------------------------------------------------------------------
# EOCD 定位
# ----------------------------------------------------------------------
def find_eocd(tail: bytes, total_size: int) -> tuple[EocdInfo, int, int]:
    """在文件尾部字节块中定位并解析 EOCD。

    参数 ``tail`` 为文件末尾的若干字节，``total_size`` 为文件总长度。
    返回 ``(EocdInfo, eocd_absolute_offset, tail_start_offset)``。
    若声明 ZIP64 但尾部窗口不足以覆盖 ZIP64 结构，抛出异常由调用方扩大读取。
    """
    idx = tail.rfind(P.SIG_END_OF_CENTRAL_DIR)
    if idx < 0:
        raise ZipStructureError(
            "在文件尾部窗口中未找到 EOCD 签名（PK\\x05\\x06），"
            "可能不是 ZIP 文件或尾部已截断。"
        )
    tail_start = total_size - len(tail)
    eocd_offset = tail_start + idx
    if idx + P.SIZE_EOCD_FIXED > len(tail):
        raise ZipStructureError("EOCD 固定头不完整，文件可能被截断")
    fields = struct.unpack_from("<4s4H2LH", tail, idx)
    (
        _sig,
        disk_number,
        cd_disk_number,
        entries_on_disk,
        total_entries,
        cd_size,
        cd_offset,
        comment_len,
    ) = fields
    comment_start = idx + P.SIZE_EOCD_FIXED
    if comment_start + comment_len != len(tail):
        # 窗口可能包含 EOCD 之后的多余数据（正常不会），或注释被截断
        if comment_start + comment_len > len(tail):
            raise ZipStructureError("EOCD 注释长度超出文件尾部，文件被截断")
    comment = tail[comment_start : comment_start + comment_len]

    is_zip64 = (
        disk_number == P.UINT16_MAX
        or cd_disk_number == P.UINT16_MAX
        or total_entries == P.UINT16_MAX
        or entries_on_disk == P.UINT16_MAX
        or cd_offset == P.ZIP64_MAGIC
        or cd_size == P.ZIP64_MAGIC
    )
    info = EocdInfo(
        disk_number=disk_number,
        cd_disk_number=cd_disk_number,
        entries_on_disk=entries_on_disk,
        total_entries=total_entries,
        central_dir_size=cd_size,
        central_dir_offset=cd_offset,
        comment=comment,
        is_zip64=False,
    )
    if not is_zip64:
        return info, eocd_offset, tail_start

    # ---- ZIP64 路径：EOCD 前 20 字节应为 Locator ----
    locator_idx = idx - P.SIZE_ZIP64_EOCD_LOCATOR
    if locator_idx < 0:
        raise ZipStructureError(
            "EOCD 指示 ZIP64，但当前尾部窗口未包含 ZIP64 EOCD Locator，请扩大读取范围"
        )
    if tail[locator_idx:idx][:4] != P.SIG_ZIP64_EOCD_LOCATOR:
        raise ZipStructureError("ZIP64 标志已设置但 Locator 签名缺失")
    _lsig, loc_disk, z64_eocd_offset, num_disks = struct.unpack_from("<4sLQL", tail, locator_idx)
    if num_disks != 1 or loc_disk != 0:
        # 多卷在更上层也会拦截，这里先给出结构层错误
        raise ZipStructureError(f"ZIP64 Locator 指示多卷归档（磁盘数={num_disks}），不在支持子集内")
    # ZIP64 EOCD Record 必须位于 Locator 之前
    z64_record_relative = z64_eocd_offset - tail_start
    if z64_record_relative < 0:
        raise ZipStructureError(
            "ZIP64 EOCD Record 位于当前尾部窗口之前，请扩大读取范围"
        )
    if z64_record_relative + P.SIZE_ZIP64_EOCD_RECORD_FIXED > len(tail):
        raise ZipStructureError("ZIP64 EOCD Record 不完整，文件可能被截断")
    rfields = struct.unpack_from("<4sQ2H2Q2L2Q", tail, z64_record_relative)
    (
        _rsig,
        _record_size,
        _ver_made,
        _ver_needed,
        r_disk,
        r_cd_disk,
        r_entries_disk,
        r_total_entries,
        r_cd_size,
        r_cd_offset,
    ) = rfields
    if r_disk != 0 or r_cd_disk != 0:
        raise ZipStructureError("ZIP64 EOCD Record 指示多卷归档，不在支持子集内")
    info.is_zip64 = True
    info.zip64_eocd_offset = z64_eocd_offset
    info.disk_number = r_disk
    info.cd_disk_number = r_cd_disk
    info.entries_on_disk = r_entries_disk
    info.total_entries = r_total_entries
    info.central_dir_size = r_cd_size
    info.central_dir_offset = r_cd_offset
    return info, eocd_offset, tail_start


# ----------------------------------------------------------------------
# 中央目录
# ----------------------------------------------------------------------
def parse_central_directory(data: bytes, expected_entries: int) -> list[CentralEntry]:
    """顺序解析整块中央目录字节。"""
    entries: list[CentralEntry] = []
    offset = 0
    n = len(data)
    while offset < n:
        if data[offset : offset + 4] != P.SIG_CENTRAL_DIR_HEADER:
            raise ZipStructureError(
                f"中央目录偏移 {offset} 处签名不是 PK\\x01\\x02，目录结构损坏"
            )
        if offset + P.SIZE_CENTRAL_DIR_HEADER_FIXED > n:
            raise ZipStructureError("中央目录文件头不完整，数据被截断")
        fields = struct.unpack_from("<4s6H3L5H2L", data, offset)
        (
            _sig,
            version_made_by,
            version_needed,
            flags,
            compression,
            mod_time,
            mod_date,
            crc32,
            compressed_size,
            uncompressed_size,
            name_len,
            extra_len,
            comment_len,
            disk_number_start,
            internal_attrs,
            external_attrs,
            local_header_offset,
        ) = fields
        name_start = offset + P.SIZE_CENTRAL_DIR_HEADER_FIXED
        extra_start = name_start + name_len
        comment_start = extra_start + extra_len
        end = comment_start + comment_len
        if end > n:
            raise ZipStructureError("中央目录条目超出目录块边界，数据被截断")
        raw_name = data[name_start:extra_start]
        extra = data[extra_start:comment_start]
        comment = data[comment_start:end]
        kwargs = dict(
            version_made_by=version_made_by,
            version_needed=version_needed,
            flags=flags,
            compression=compression,
            mod_time=mod_time,
            mod_date=mod_date,
            crc32=crc32,
            compressed_size=compressed_size,
            uncompressed_size=uncompressed_size,
            filename=_decode_name(raw_name, bool(flags & P.FLAG_UTF8_NAMES)),
            raw_filename=raw_name,
            extra=extra,
            comment=comment,
            disk_number_start=disk_number_start,
            internal_attrs=internal_attrs,
            external_attrs=external_attrs,
            local_header_offset=local_header_offset,
        )
        _apply_zip64_central(kwargs, extra)
        entries.append(CentralEntry(**kwargs))
        offset = end
    if len(entries) != expected_entries:
        raise ZipStructureError(
            f"中央目录实际解析出 {len(entries)} 条记录，与 EOCD 声明的 {expected_entries} 条不符"
        )
    return entries


# ----------------------------------------------------------------------
# 本地文件头
# ----------------------------------------------------------------------
def parse_local_header(data: bytes) -> LocalHeader:
    """从本地头起始字节块解析本地文件头（块需覆盖完整固定头+文件名+扩展）。"""
    if len(data) < P.SIZE_LOCAL_FILE_HEADER_FIXED:
        raise ZipStructureError("本地文件头数据不足 30 字节，成员被截断")
    if data[:4] != P.SIG_LOCAL_FILE_HEADER:
        raise ZipStructureError("本地文件头签名不是 PK\\x03\\x04")
    fields = struct.unpack_from("<4s5H3L2H", data, 0)
    (
        _sig,
        version_needed,
        flags,
        compression,
        mod_time,
        mod_date,
        crc32,
        compressed_size,
        uncompressed_size,
        name_len,
        extra_len,
    ) = fields
    total = P.SIZE_LOCAL_FILE_HEADER_FIXED + name_len + extra_len
    if total > len(data):
        raise ZipStructureError(
            f"本地文件头需要 {total} 字节（含文件名/扩展字段），当前块只有 {len(data)} 字节"
        )
    raw_name = data[P.SIZE_LOCAL_FILE_HEADER_FIXED : P.SIZE_LOCAL_FILE_HEADER_FIXED + name_len]
    extra_start = P.SIZE_LOCAL_FILE_HEADER_FIXED + name_len
    extra = data[extra_start : extra_start + extra_len]
    header = LocalHeader(
        version_needed=version_needed,
        flags=flags,
        compression=compression,
        mod_time=mod_time,
        mod_date=mod_date,
        crc32=crc32,
        compressed_size=compressed_size,
        uncompressed_size=uncompressed_size,
        raw_filename=raw_name,
        extra=extra,
        header_total_size=total,
    )
    _apply_zip64_local(header)
    return header


def verify_header_consistency(entry: CentralEntry, header: LocalHeader) -> None:
    """核对中央目录条目与本地文件头的一致性。

    任何冲突都抛出 :class:`HeaderMismatch`，该成员不得进入正式记录。
    """
    problems: list[str] = []
    if header.raw_filename != entry.raw_filename:
        problems.append(
            f"文件名不一致：中央目录={entry.raw_filename!r}，本地头={header.raw_filename!r}"
        )
    if header.compression != entry.compression:
        problems.append(
            f"压缩方式不一致：中央目录={entry.compression}，本地头={header.compression}"
        )
    # 标志位整体比较（bit 3 允许两边各自表达，但本项目生成器与子集要求两边一致）
    if header.flags != entry.flags:
        problems.append(f"通用位标志不一致：中央目录=0x{entry.flags:04x}，本地头=0x{header.flags:04x}")
    if header.version_needed != entry.version_needed:
        problems.append(
            f"提取版本不一致：中央目录={entry.version_needed}，本地头={header.version_needed}"
        )
    # bit 3 置位时本地头的 CRC/大小允许为 0，真实值在数据描述符中
    if not entry.has_data_descriptor:
        if header.crc32 != entry.crc32:
            problems.append(f"CRC-32 不一致：中央目录=0x{entry.crc32:08x}，本地头=0x{header.crc32:08x}")
        if header.compressed_size != entry.compressed_size:
            problems.append(
                f"压缩大小不一致：中央目录={entry.compressed_size}，本地头={header.compressed_size}"
            )
        if header.uncompressed_size != entry.uncompressed_size:
            problems.append(
                f"未压缩大小不一致：中央目录={entry.uncompressed_size}，本地头={header.uncompressed_size}"
            )
    else:
        # 即便允许为 0，也不允许本地头给出与目录冲突的非零值
        if header.crc32 not in (0, entry.crc32):
            problems.append(f"bit3 本地头 CRC 非零且与目录冲突：0x{header.crc32:08x} != 0x{entry.crc32:08x}")
        if header.compressed_size not in (0, entry.compressed_size):
            problems.append(
                f"bit3 本地头压缩大小非零且与目录冲突：{header.compressed_size} != {entry.compressed_size}"
            )
        if header.uncompressed_size not in (0, entry.uncompressed_size):
            problems.append(
                f"bit3 本地头未压缩大小非零且与目录冲突：{header.uncompressed_size} != {entry.uncompressed_size}"
            )
    if problems:
        raise HeaderMismatch(
            f"成员 {entry.filename!r} 的中央目录与本地文件头冲突：" + "；".join(problems)
        )


def parse_data_descriptor(data: bytes, expected: CentralEntry) -> tuple[DataDescriptor, int]:
    """解析压缩数据之后的数据描述符。

    接受带签名（16 字节）与不带签名（12 字节）两种形式，
    并与中央目录声明值核对。
    """
    if len(data) < 12:
        raise ZipStructureError("数据描述符至少需要 12 字节，当前数据不足")
    if data[:4] == P.SIG_DATA_DESCRIPTOR:
        if len(data) < 16:
            raise ZipStructureError("带签名的数据描述符需要 16 字节，当前数据不足")
        # 描述符固定为 32 位字段（APPNOTE 4.3.9.2），ZIP64 条目仍写 32 位
        crc, csize, usize = struct.unpack_from("<LLL", data, 4)
        descriptor = DataDescriptor(crc, csize, usize, True, 16)
    else:
        crc, csize, usize = struct.unpack_from("<LLL", data, 0)
        descriptor = DataDescriptor(crc, csize, usize, False, 12)
    problems = []
    if descriptor.crc32 != expected.crc32:
        problems.append(f"描述符 CRC=0x{descriptor.crc32:08x} 与目录 0x{expected.crc32:08x} 不符")
    if descriptor.compressed_size != expected.compressed_size:
        problems.append(f"描述符压缩大小 {descriptor.compressed_size} 与目录 {expected.compressed_size} 不符")
    if descriptor.uncompressed_size != expected.uncompressed_size:
        problems.append(
            f"描述符未压缩大小 {descriptor.uncompressed_size} 与目录 {expected.uncompressed_size} 不符"
        )
    if problems:
        raise HeaderMismatch(f"成员 {expected.filename!r} 数据描述符冲突：" + "；".join(problems))
    return descriptor, descriptor.total_size
