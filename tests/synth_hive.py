"""Synthetic registry hive fixtures.

The fixtures generated here are entirely synthetic: fixed content, no real acquisition data and no
account or credential material. They exist so the shared cell cache can be exercised with cells of
known size and known offsets, including equal offsets across different hives.
"""

from __future__ import annotations

import struct

from dissect.regf.c_regf import c_regf as c

HBASE_SIZE = len(c._HBASE_BLOCK)
HBIN_SIZE = len(c._HBIN)

ROOT_OFFSET = HBIN_SIZE

CHILD_NAMES = ("Alpha", "Bravo", "Charlie", "Delta")


def _pad8(size: int) -> int:
    return (size + 7) & ~7


def key_node(
    name: str,
    *,
    parent: int = 0xFFFFFFFF,
    flags: int = 0,
    subkey_list: int = 0,
    subkey_count: int = 0,
    value_list: int = 0,
    value_count: int = 0,
    class_name: bytes | None = None,
    class_offset: int = 0xFFFFFFFF,
) -> bytes:
    """Build an ``nk`` cell payload with ``name`` appended after the fixed struct."""
    node = c._CM_KEY_NODE()
    node.Signature = b"nk"
    node.Flags = c.KEY(flags)
    node.Parent = parent
    node.SubKeyCounts = [subkey_count, 0]
    node.SubKeyLists = [subkey_list, 0]
    node.ValueList = c._CHILD_LIST(Count=value_count, List=value_list)
    node.Security = 0xFFFFFFFF
    node.Class = class_offset
    node.ClassLength = len(class_name) if class_name is not None else 0

    if c.KEY.COMP_NAME in node.Flags:
        name_blob = name.encode("ascii")
    else:
        name_blob = name.encode("utf-16-le")

    node.NameLength = len(name_blob)
    return node.dumps() + name_blob


def key_value(name: str | None, data: bytes, data_type: int = 4, *, data_offset: int = 0) -> bytes:
    """Build a ``vk`` cell payload. ``data`` of up to 4 bytes is stored inline."""
    value = c._CM_KEY_VALUE()
    value.Signature = b"vk"
    value.Type = data_type

    if name is None:
        value.NameLength = 0
        name_blob = b""
        value.Flags = c.VALUE.COMP_NAME
    else:
        name_blob = name.encode("utf-16-le")
        value.NameLength = len(name_blob)
        value.Flags = c.VALUE(0)

    if len(data) <= 4:
        value.DataLength = 0x80000000 | len(data)
        value.Data = struct.unpack("<I", data.ljust(4, b"\x00"))[0]
    else:
        value.DataLength = len(data)
        value.Data = data_offset

    return value.dumps() + name_blob


def index_leaf(offsets: list[int]) -> bytes:
    leaf = c._CM_KEY_INDEX()
    leaf.Signature = b"li"
    leaf.Count = len(offsets)
    leaf.List = offsets
    return leaf.dumps()


def big_data(segment_list: int, count: int) -> bytes:
    bd = c._CM_BIG_DATA()
    bd.Signature = b"db"
    bd.Count = count
    bd.List = segment_list
    return bd.dumps()


class HiveBuilder:
    """Build a minimal but structurally valid registry hive with deterministic cell offsets."""

    def __init__(self, filename: str = "SYNTH", *, minor: int = 5, sequence: tuple[int, int] = (1, 1)):
        self.filename = filename
        self.minor = minor
        self.sequence = sequence
        self._cells: list[bytes] = []  # whole cell (size header + payload), already padded
        self._next = HBIN_SIZE
        self._offsets: list[int] = []
        self._payload_lens: dict[int, int] = {}

    @property
    def next_offset(self) -> int:
        return self._next

    def add_cell(self, payload: bytes, *, allocated: bool = True) -> int:
        """Append a cell and return its offset relative to the first hbin."""
        size = _pad8(4 + len(payload))
        header = struct.pack("<i", -size if allocated else size)
        self._cells.append(header + payload.ljust(size - 4, b"\x00"))

        offset = self._next
        self._offsets.append(offset)
        self._payload_lens[offset] = size - 4
        self._next += size
        return offset

    def payload_length(self, offset: int) -> int:
        """The number of payload bytes (cell size minus the 4-byte size header)."""
        return self._payload_lens[offset]

    def patch_cell(self, offset: int, payload: bytes) -> None:
        """Replace a cell's payload in place; the padded size must stay exactly the same."""
        index = self._offsets.index(offset)
        current = self._cells[index]
        size = len(current)
        if _pad8(4 + len(payload)) != size:
            raise ValueError("patched payload must keep the padded cell size")

        allocated = struct.unpack("<i", current[:4])[0] < 0
        header = struct.pack("<i", -size if allocated else size)
        self._cells[index] = header + payload.ljust(size - 4, b"\x00")

    def offsets(self) -> list[int]:
        return list(self._offsets)

    def build(self) -> bytes:
        hbin_body = b"".join(self._cells)
        hbin = c._HBIN()
        hbin.Signature = 0x6E696268
        hbin.FileOffset = 0
        hbin.Size = _pad8(HBIN_SIZE + len(hbin_body))
        hbin_blob = hbin.dumps() + hbin_body.ljust(hbin.Size - HBIN_SIZE, b"\x00")

        header = c._HBASE_BLOCK()
        header.Signature = 0x66676572
        header.Sequence1, header.Sequence2 = self.sequence
        header.Major = 1
        header.Minor = self.minor
        header.Type = 0
        header.Format = 1
        header.RootCell = ROOT_OFFSET
        header.Length = len(hbin_blob)
        header.Cluster = 1
        header.FileName = self.filename
        header.CheckSum = 0
        header.BootType = 0
        header.BootRecover = 0

        blob = bytearray(header.dumps())
        blob[:HBIN_SIZE] = header.dumps()[:HBIN_SIZE]
        crc = 0
        for (word,) in struct.iter_unpack("<I", bytes(blob[:508])):
            crc ^= word
        header.CheckSum = crc

        return header.dumps() + hbin_blob


def build_working_hive(
    filename: str = "SYNTH",
    *,
    minor: int = 5,
    sequence: tuple[int, int] = (1, 1),
    tag: bytes = b"\x41",
    extra_payloads: tuple[bytes, ...] = (),
) -> dict:
    """Build a hive with a root key, four subkeys, two values each and optional extra cells.

    ``tag`` fills every value payload, so two hives can share an identical layout (and thus
    identical cell offsets) while holding different bytes.
    """
    if not tag:
        raise ValueError("tag must not be empty")

    builder = HiveBuilder(filename, minor=minor, sequence=sequence)
    root_offset = builder.add_cell(key_node("ROOT", flags=c.KEY.HIVE_ENTRY, parent=0xFFFFFFFF))

    children: dict[str, dict] = {}
    for index, name in enumerate(CHILD_NAMES):
        inline_off = builder.add_cell(key_value(f"Num{index}", bytes([tag[0], index, 0, 0])))

        payload = tag * (16 + index * 8)
        data_off = builder.add_cell(payload)
        blob_off = builder.add_cell(key_value(f"Blob{index}", payload, 3, data_offset=data_off))

        value_list_off = builder.add_cell(struct.pack("<II", inline_off, blob_off))
        node_off = builder.add_cell(key_node(name, parent=root_offset, value_list=value_list_off, value_count=2))

        children[name] = {
            "node_offset": node_off,
            "inline_value_offset": inline_off,
            "blob_value_offset": blob_off,
            "blob_data_offset": data_off,
            "value_list_offset": value_list_off,
            "blob_payload": payload,
        }

    index_off = builder.add_cell(index_leaf([child["node_offset"] for child in children.values()]))
    builder.patch_cell(
        root_offset,
        key_node(
            "ROOT",
            flags=c.KEY.HIVE_ENTRY,
            parent=0xFFFFFFFF,
            subkey_list=index_off,
            subkey_count=len(children),
        ),
    )

    extra_offsets = [builder.add_cell(payload) for payload in extra_payloads]

    return {
        "builder": builder,
        "blob": builder.build(),
        "root_offset": root_offset,
        "index_offset": index_off,
        "children": children,
        "extra_offsets": extra_offsets,
    }
