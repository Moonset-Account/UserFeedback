"""Synthetic in-memory registry hive fixtures for BigData / KeyValue.data QA.

Every hive is assembled from ``c_regf`` structures so that each byte and each
cell offset can be accounted for forensically. The builder records a
:class:`CellRecord` for every allocated cell (role, HCELL offset, file offset,
payload length and payload digest); the QA cases in
``tests/bigdata_cases.py`` assert against these records and replay them from
fixed seeds.

Only test fixtures live here -- the production parser in
``dissect/regf/regf.py`` is not modified.
"""

from __future__ import annotations

import hashlib
import io
import struct
from dataclasses import dataclass

from dissect.regf import regf
from dissect.regf.c_regf import KEY, VALUE, c_regf

HBASE_BLOCK_SIZE = len(c_regf._HBASE_BLOCK)  # 4096
HBIN_HEADER_SIZE = len(c_regf._HBIN)  # 32
CM_KEY_VALUE_BIG = 0x3FD8  # 16344, max bytes per BigData segment
DATA_IN_INLINE = 0x80000000  # DataLength high bit: data is stored inline
PADDING_BYTE = 0xCC  # marker byte for cell slack / inter-cell gaps


def _align(value: int, alignment: int = 8) -> int:
    return (value + alignment - 1) // alignment * alignment


@dataclass
class CellRecord:
    """Forensic provenance for one allocated cell."""

    role: str
    offset: int  # HCELL_INDEX, relative to the start of the first hbin
    size: int  # allocated cell size, including the 4-byte size header
    payload_length: int  # meaningful payload bytes (excluding padding)
    payload_sha256: str

    @property
    def file_offset(self) -> int:
        return HBASE_BLOCK_SIZE + self.offset

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "hcell": f"0x{self.offset:x}",
            "file_offset": f"0x{self.file_offset:x}",
            "cell_size": self.size,
            "payload_length": self.payload_length,
            "payload_sha256_16": self.payload_sha256[:16],
        }


class HiveBuilder:
    """Builds a minimal but legal single-hbin hive in memory."""

    def __init__(self, minor: int = 5, major: int = 1, filename: str = "bigdata-qa"):
        self.major = major
        self.minor = minor
        self.filename = filename
        self.records: list[CellRecord] = []
        self._cells: list[tuple[int, bytes]] = []
        self._next = HBIN_HEADER_SIZE  # hbin header occupies the first 32 bytes

    def gap(self, size: int) -> None:
        """Leave ``size`` bytes of legal inter-cell slack (0xCC filled)."""
        self._next += size

    def add_cell(self, payload: bytes, role: str, slack: int = 0) -> int:
        """Allocate a cell, return its HCELL_INDEX offset.

        ``slack`` adds extra in-cell padding after the payload; all padding is
        filled with the 0xCC marker byte so tests can prove padding never
        leaks into parsed evidence.
        """
        offset = _align(self._next)
        size = _align(4 + len(payload) + slack)
        padding = size - 4 - len(payload)
        raw = struct.pack("<i", -size) + payload + bytes([PADDING_BYTE]) * padding
        self._cells.append((offset, raw))
        self.records.append(
            CellRecord(
                role=role,
                offset=offset,
                size=size,
                payload_length=len(payload),
                payload_sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
        self._next = offset + size
        return offset

    def record_for(self, role: str) -> CellRecord:
        for record in self.records:
            if record.role == role:
                return record
        raise KeyError(role)

    def build(self, root_offset: int) -> bytes:
        """Assemble the final hive image (HBASE_BLOCK + one hbin)."""
        hbin_size = _align(self._next, 4096)

        hbin = c_regf._HBIN()
        hbin.Signature = 0x6E696268  # "hbin"
        hbin.FileOffset = 0
        hbin.Size = hbin_size

        header = c_regf._HBASE_BLOCK()
        header.Signature = 0x66676572  # "regf"
        header.Sequence1 = 1
        header.Sequence2 = 1
        header.Major = self.major
        header.Minor = self.minor
        header.RootCell = root_offset
        header.Length = hbin_size
        header.FileName = self.filename

        blob = bytearray(bytes(header))
        struct.pack_into("<I", blob, 508, regf.xor32_crc(bytes(blob[:508])))

        region = bytearray(bytes([PADDING_BYTE]) * hbin_size)
        region[0:HBIN_HEADER_SIZE] = bytes(hbin)
        for offset, raw in self._cells:
            region[offset : offset + len(raw)] = raw

        return bytes(blob) + bytes(region)


def key_node_payload(name: str, value_offsets: list[int], value_list_offset: int) -> bytes:
    """Root ``nk`` cell payload referencing the given value list."""
    node = c_regf._CM_KEY_NODE()
    node.Signature = b"nk"
    node.Flags = KEY.HIVE_ENTRY | KEY.COMP_NAME
    node.LastWriteTime = 0
    node.Spare = 0
    node.Parent = 0xFFFFFFFF
    node.SubKeyCounts = [0, 0]
    node.SubKeyLists = [0xFFFFFFFF, 0xFFFFFFFF]
    node.ValueList.Count = len(value_offsets)
    node.ValueList.List = value_list_offset
    node.Security = 0xFFFFFFFF
    node.Class = 0xFFFFFFFF
    node.MaxNameLen = 0
    node.MaxClassLen = 0
    node.MaxValueNameLen = 0
    node.MaxValueDataLen = 0
    node.WorkVar = 0
    node.NameLength = len(name.encode())
    node.ClassLength = 0
    return bytes(node) + name.encode()


def key_value_payload(name: str, data_length: int, data: int, value_type: int = 0x3) -> bytes:
    """``vk`` cell payload; ``data`` is a HCELL_INDEX or the inline dword."""
    key_value = c_regf._CM_KEY_VALUE()
    key_value.Signature = b"vk"
    name_bytes = name.encode()
    key_value.NameLength = len(name_bytes)
    key_value.DataLength = data_length
    key_value.Data = data
    key_value.Type = value_type
    key_value.Flags = VALUE.COMP_NAME
    key_value.Spare = 0
    return bytes(key_value) + name_bytes


def big_data_payload(segment_list_offset: int, count: int) -> bytes:
    """``db`` cell payload pointing at a segment list cell."""
    big_data = c_regf._CM_BIG_DATA()
    big_data.Signature = b"db"
    big_data.Count = count
    big_data.List = segment_list_offset
    return bytes(big_data)


def uint32_list_payload(offsets: list[int]) -> bytes:
    """Raw cell payload for value lists and BigData segment lists."""
    return struct.pack(f"<{len(offsets)}I", *offsets)


def add_big_value(builder: HiveBuilder, name: str, content: bytes, value_type: int = 0x3, slack: int = 0) -> int:
    """Wire a complete BigData value into the builder, return the vk offset."""
    segments = [content[i : i + CM_KEY_VALUE_BIG] for i in range(0, len(content), CM_KEY_VALUE_BIG)]
    segment_offsets = [
        builder.add_cell(segment, f"segment:{name}:{index}", slack=slack) for index, segment in enumerate(segments)
    ]
    list_offset = builder.add_cell(uint32_list_payload(segment_offsets), f"segment-list:{name}")
    db_offset = builder.add_cell(big_data_payload(list_offset, len(segment_offsets)), f"db:{name}")
    return builder.add_cell(key_value_payload(name, len(content), db_offset, value_type), f"vk:{name}")


def finalize_hive(builder: HiveBuilder, vk_offsets: list[int]) -> bytes:
    """Add the value list and ROOT key node, return the hive image."""
    list_offset = builder.add_cell(uint32_list_payload(vk_offsets), "value-list")
    root_offset = builder.add_cell(key_node_payload("ROOT", vk_offsets, list_offset), "root")
    return builder.build(root_offset)


def values_by_name(hive: regf.RegistryHive) -> dict[str, regf.KeyValue]:
    return {value.name: value for value in hive.root().values()}


class ProvenanceIO:
    """File-like wrapper recording every read as ``(file_offset, length)``.

    Used to prove which raw offsets the parser actually reads, and in which
    order BigData segments are concatenated.
    """

    def __init__(self, data: bytes):
        self._fh = io.BytesIO(data)
        self.reads: list[tuple[int, int]] = []

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._fh.seek(offset, whence)

    def read(self, size: int = -1) -> bytes:
        offset = self._fh.tell()
        data = self._fh.read(size)
        self.reads.append((offset, len(data)))
        return data

    def tell(self) -> int:
        return self._fh.tell()

    def read_offsets(self) -> list[int]:
        return [offset for offset, _ in self.reads]
