from __future__ import annotations

import logging
import os
import struct
import threading
from collections import OrderedDict
from functools import cached_property, lru_cache
from io import BytesIO
from itertools import count
from typing import TYPE_CHECKING, BinaryIO, TypeAlias

from dissect.util.ts import wintimestamp

from dissect.regf.c_regf import (
    KEY,
    REG_BINARY,
    REG_DWORD,
    REG_DWORD_BIG_ENDIAN,
    REG_EXPAND_SZ,
    REG_FULL_RESOURCE_DESCRIPTOR,
    REG_MULTI_SZ,
    REG_NONE,
    REG_QWORD,
    REG_RESOURCE_REQUIREMENTS_LIST,
    REG_SZ,
    VALUE,
    c_regf,
)
from dissect.regf.exceptions import (
    Error,
    RegistryKeyNotFoundError,
    RegistryValueNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from datetime import datetime

log = logging.getLogger(__name__)
log.setLevel(os.getenv("DISSECT_LOG_REGF", "CRITICAL"))


CellType: TypeAlias = "IndexLeaf | FastLeaf | HashLeaf | IndexRoot | KeyNode | KeyValue"

STABLE = 0
VOLATILE = 1

_hive_ids = count(1)


class CellCache:
    """A shared cache for raw registry cell payload bytes.

    A single ``CellCache`` can be passed to :class:`RegistryHive` instances that are read together,
    e.g. a SYSTEM, SOFTWARE and NTUSER hive from the same acquisition. It lets the caller put a
    total byte budget on the raw cell payloads that are kept around for reuse.

    Only the raw cell payload (the bytes after the 4-byte cell size) is cached. Parsed ``Cell``
    objects, the per-hive ``lru_cache`` of parsed cells, and any data already held by the caller are
    outside of this budget. The budget is therefore *not* a limit on the memory usage of the whole
    process; it only bounds the sum of the cached raw payload bytes.

    Entries are keyed by hive identity, the hive's current acquisition version, a generation
    counter and the cell offset/length. Two hives - or two acquisitions of the same hive - can
    therefore never accidentally share bytes for the same offset. The cache is evicted in LRU
    order. A single cell payload larger than the whole budget is read directly and not retained.
    Failed or incomplete reads are never stored.

    Releasing a hive only drops that hive's entries; entries of other hives stay intact. No file
    handles are opened or closed by this class.
    """

    def __init__(self, budget: int):
        if not isinstance(budget, int):
            raise TypeError("budget must be an integer number of bytes")
        if budget < 0:
            raise ValueError("budget must be non-negative")

        self.budget = budget
        self._entries: OrderedDict[tuple, bytes] = OrderedDict()
        self._resident = 0
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._oversized = 0
        self._failed_reads = 0
        self._short_reads = 0
        self._payload_bytes_fetched = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, key: tuple) -> bool:
        with self._lock:
            return key in self._entries

    @property
    def resident_bytes(self) -> int:
        """The sum of the raw payload bytes currently retained."""
        with self._lock:
            return self._resident

    def fetch(self, key: tuple, reader: Callable[[], bytes], length: int) -> bytes:
        """Return cached bytes for ``key`` or call ``reader`` and cache a complete payload.

        ``reader`` is called without the cache lock held. If it raises, the exception propagates
        and nothing is cached. A short read is returned to the caller unchanged (matching the
        behaviour of a plain file read) but is not cached as if it were valid data.
        """
        with self._lock:
            if (data := self._entries.get(key)) is not None:
                self._entries.move_to_end(key)
                self._hits += 1
                return data
            self._misses += 1

        try:
            data = reader()
        except BaseException:
            with self._lock:
                self._failed_reads += 1
            raise

        with self._lock:
            self._payload_bytes_fetched += len(data)
            if length > 0 and len(data) != length:
                self._short_reads += 1

        # Empty payloads and short reads are not cached: the latter commonly means the underlying
        # read failed or the cell is truncated, and must not be turned into a cached empty value.
        if length > 0 and len(data) == length:
            self.put(key, data)

        return data

    def put(self, key: tuple, data: bytes) -> bool:
        """Store ``data`` for ``key``, evicting LRU entries as needed. Returns whether it was kept."""
        size = len(data)
        with self._lock:
            if key in self._entries:
                return True

            # Empty payloads carry no reusable bytes; caching them would only grow metadata.
            if size == 0:
                return False

            if size > self.budget:
                self._oversized += 1
                return False

            while self._resident + size > self.budget and self._entries:
                _, old = self._entries.popitem(last=False)
                self._resident -= len(old)
                self._evictions += 1

            self._entries[key] = data
            self._resident += size
            return True

    def release(self, hive) -> int:
        """Drop all entries belonging to ``hive`` (a :class:`RegistryHive` or a hive id).

        Entries of other hives are left untouched. Returns the number of entries removed.
        """
        hive_id = getattr(hive, "_cache_id", hive)

        with self._lock:
            keys = [key for key in self._entries if key[0] == hive_id]
            for key in keys:
                self._resident -= len(self._entries.pop(key))

            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._resident = 0

    def stats(self) -> dict[str, int]:
        """Return a snapshot of cache counters, useful for read-reuse and eviction checks."""
        with self._lock:
            return {
                "budget": self.budget,
                "entries": len(self._entries),
                "resident_bytes": self._resident,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "oversized": self._oversized,
                "failed_reads": self._failed_reads,
                "short_reads": self._short_reads,
                "payload_bytes_fetched": self._payload_bytes_fetched,
            }


class RegistryHive:
    def __init__(
        self,
        fh: BinaryIO,
        *,
        cache: CellCache | None = None,
        collection_version: object | None = None,
    ):
        self.fh = fh

        # The shared cache and the identity/version state used for its keys. The default path
        # (no cache) behaves exactly like it did before this option existed.
        self.cache = cache
        self._cache_id = next(_hive_ids)
        self._cache_generation = 0

        self.fh.seek(0)
        data = self.fh.read(len(c_regf._HBASE_BLOCK))
        self.header = c_regf._HBASE_BLOCK(data)
        self.version = self.header.Minor
        self.filename = self.header.FileName.rstrip("\x00")

        if collection_version is None:
            # Default acquisition version: enough header state to tell a re-collected hive apart
            # from the one this instance was originally opened on.
            collection_version = (
                self.header.Sequence1,
                self.header.Sequence2,
                self.header.TimeStamp,
                self.header.Length,
                self.header.CheckSum,
            )
        hash(collection_version)  # fail early on unusable version tokens
        self._collection_version = collection_version

        self.dirty = xor32_crc(data[:508]) == self.header.CheckSum
        if self.dirty:
            log.warning(
                "Checksum failed, the %r hive is dirty, recovery needed, "
                "may not be able to read keys and values properly",
                self.filename,
            )
        else:
            log.debug("Hive %r checksum OK", self.filename)

        self.in_transaction = self.header.Sequence1 != self.header.Sequence2
        if self.in_transaction:
            log.warning(
                "The hive %r is undergoing a transaction, may not be able to read keys and values properly",
                self.filename,
            )
        else:
            log.debug("Hive %r is not undergoing any transactions", self.filename)

        self._hbin_offset = len(c_regf._HBASE_BLOCK)
        self._root = self.cell(self.header.RootCell)

        self.cell = lru_cache(4096)(self.cell)

    @property
    def collection_version(self) -> object:
        return self._collection_version

    def set_collection_version(self, version: object) -> None:
        """Mark a new acquisition version for cache keys.

        Payload bytes cached for this hive under the previous version are dropped, so a later read
        of the same offset cannot return bytes from the old acquisition. Other hives sharing the
        cache are unaffected and no file handle is touched.

        This method scopes the *raw payload cache* only. Parsed ``Cell`` objects memoised on this
        instance (``root()``, ``KeyNode._cache``, the per-hive ``lru_cache``) are not rebuilt; if the
        underlying bytes changed, construct a new :class:`RegistryHive` instead.
        """
        hash(version)
        self._collection_version = version
        self._cache_generation += 1
        if self.cache is not None:
            self.cache.release(self)

    def release_cache(self) -> int:
        """Drop this hive's entries from the shared cache, leaving other hives intact."""
        if self.cache is None:
            return 0
        return self.cache.release(self)

    def root(self) -> KeyNode:
        return self._root

    def cell(self, offset: int) -> CellType:
        return self.parse_cell_data(self.cell_data(offset))

    def _cache_key(self, offset: int, length: int) -> tuple:
        return (self._cache_id, self._collection_version, self._cache_generation, offset, length)

    def _cell_data(self, offset: int) -> tuple[bool, bytes]:
        self.fh.seek(self._hbin_offset + offset)

        allocated = False
        if (size := struct.unpack("<i", self.fh.read(4))[0]) < 0:
            size = -size
            allocated = True

        length = size - 4
        if self.cache is None or length <= 0:
            return allocated, self.fh.read(length)

        # Only the payload itself is cached; the size header is still read on every lookup so that
        # truncated cells and allocation state are always observed from the underlying handle.
        def read_payload() -> bytes:
            self.fh.seek(self._hbin_offset + offset + 4)
            return self.fh.read(length)

        data = self.cache.fetch(self._cache_key(offset, length), read_payload, length)
        if len(data) == length:
            # Leave the handle positioned as if the payload had been read from the file, whether it
            # came from the cache or not. A short read already left the handle at end-of-file.
            self.fh.seek(self._hbin_offset + offset + 4 + length)
        return allocated, data

    def cell_data(self, offset: int) -> bytes:
        _, data = self._cell_data(offset)
        return data

    def parse_cell_data(self, data: bytes) -> CellType:
        sig = data[:2]

        if cls := _CELL_CLASSES.get(sig):
            return cls(self, data)

        raise NotImplementedError(repr(sig))

    def open(self, path: str) -> CellType:
        path = path.strip("\\")
        parts = path.split("\\") if path else []

        realpath = []
        node = self._root
        for part in parts:
            subkey = node.subkey(part)
            realpath.append(subkey.name)

            node = subkey

        return node

    def walk(self) -> Iterator[tuple[int, bool, CellType | bytes]]:
        next_hbin = self._hbin_offset
        hive_size = self.header.Length + self._hbin_offset

        while next_hbin < hive_size:
            self.fh.seek(next_hbin)
            header = c_regf._HBIN(self.fh)
            if header.Signature != 0x6E696268:
                break

            next_hbin += header.Size

            offset = self.fh.tell()
            while offset < next_hbin:
                allocated, data = self._cell_data(offset - self._hbin_offset)
                cell = _CELL_CLASSES.get(data[:2], lambda hive, buf: buf)(self, data)

                yield offset, allocated, cell
                offset += 4 + len(data)


class Cell:
    __signature__ = b""
    __struct__ = None

    def __init__(self, hive: RegistryHive, data: bytes, strict: bool = True):
        self.hive = hive

        if data[:2] != self.__signature__:
            raise Error(f"Invalid {self.__class__.__name__} signature {data[:2]!r}, expected {self.__signature__!r}")

        self.cell = self.__struct__(data)


class KeyNode(Cell):
    __signature__ = b"nk"
    __struct__ = c_regf._CM_KEY_NODE

    def __init__(self, hive: RegistryHive, data: bytes):
        super().__init__(hive, data)
        self._cache = {}

        self.class_name = None
        if (class_idx := self.cell.Class) != 0xFFFFFFFF:
            self.class_name = self.hive.cell_data(class_idx)[: self.cell.ClassLength].decode("utf-16-le")

        name_length = self.cell.NameLength
        name_blob = data[len(self.__struct__) :][:name_length]
        self.name = decode_name(name_blob, name_length, KEY.COMP_NAME in self.cell.Flags)

    def __repr__(self) -> str:
        return f"<KeyNode {self.name}>"

    @cached_property
    def timestamp(self) -> datetime:
        return wintimestamp(self.cell.LastWriteTime)

    @property
    def path(self) -> str:
        # As long as we are not the ROOT key, we add our name to the stack.
        #
        # The path is relative to the hive of this key. Adding a name for the
        # ROOT key will lead to issues when this hive is mapped on a subkey of
        # another hive. The full path to this key is constructed using both the
        # path of the subkey in the other hive and this key's path.
        #
        # If ROOT would be part of that path, that part (and thus the whole
        # path) would not be accesible, nor is the presence of the ROOT part in
        # the path expected by the user (it is never visible in e.g. regedit).
        parts = []

        current = self
        parent = self.hive.cell(current.cell.Parent) if KEY.HIVE_ENTRY not in current.cell.Flags else None
        while parent is not None:
            parts.append(current.name)
            current = parent
            parent = self.hive.cell(current.cell.Parent) if KEY.HIVE_ENTRY not in current.cell.Flags else None

        return "\\".join(list(reversed(parts)))

    @cached_property
    def _subkey_list(self) -> IndexLeaf | FastLeaf | HashLeaf | IndexRoot | None:
        num_sk = self.cell.SubKeyCounts[STABLE]
        if not num_sk:
            return None

        subkey_list: KeyIndex = self.hive.cell(self.cell.SubKeyLists[STABLE])
        if num_sk != subkey_list.count:
            log.debug(
                "KeyNode %s has %d subkeys, while the %s has %d elements",
                self.name,
                num_sk,
                subkey_list.__class__.__name__,
                subkey_list.count,
            )

        return subkey_list

    def subkeys(self) -> Iterator[KeyNode]:
        if self._subkey_list:
            yield from self._subkey_list

    def subkey(self, name: str) -> KeyNode:
        lname = name.lower()

        try:
            return self._cache[lname]
        except KeyError:
            pass

        if self._subkey_list and (sk := self._subkey_list.subkey(name)):
            self._cache[lname] = sk
            return sk

        raise RegistryKeyNotFoundError(name)

    def values(self) -> Iterator[KeyValue]:
        if num_values := self.cell.ValueList.Count:
            data = self.hive.cell_data(self.cell.ValueList.List)

            # Possible slack values
            if len(data) // 4 < num_values:
                num_values = len(data) // 4
                bytes_short = num_values * 4 - len(data)
                if bytes_short:
                    log.debug(
                        "Value list of key %r is %d bytes short reading %d values instead of %d, "
                        "the difference could be due to slack values",
                        self.name,
                        bytes_short,
                        num_values,
                        num_values,
                    )

            yield from ValueList(self.hive, data, num_values)

    def value(self, name: str) -> KeyValue:
        for value in self.values():
            if value.name.lower() == name.lower():
                return value

        raise RegistryValueNotFoundError(name)


class ValueList:
    def __init__(self, hive: RegistryHive, data: bytes, count: int):
        self.hive = hive
        self._values = c_regf.int32[count](data)

    def __iter__(self) -> Iterator[KeyValue]:
        for entry in self._values:
            if entry <= 2:
                continue

            data = self.hive.cell_data(entry)
            if data[:2] != KeyValue.__signature__:
                log.warning("Invalid cell signature %r at offset 0x%x", data[:2], entry)
                continue

            yield KeyValue(self.hive, data)


class KeyValue(Cell):
    __signature__ = b"vk"
    __struct__ = c_regf._CM_KEY_VALUE

    def __init__(self, hive: RegistryHive, data: bytes):
        super().__init__(hive, data)

        if (name_length := self.cell.NameLength) == 0:
            self.name = "(Default)"
        else:
            name_blob = data[len(self.__struct__) :][:name_length]
            self.name = decode_name(name_blob, name_length, VALUE.COMP_NAME in self.cell.Flags)

    def __repr__(self) -> str:
        return f"<KeyValue {self.name}={self.value!r}>"

    @cached_property
    def type(self) -> int:
        return self.cell.Type

    @cached_property
    def size(self) -> int:
        return self.cell.DataLength & ~0x80000000

    @cached_property
    def is_big_value(self) -> bool:
        # HSYS_WHISTLER_BETA1, CM_KEY_VALUE_SPECIAL_SIZE, CM_KEY_VALUE_BIG
        return self.hive.version > 4 and self.cell.DataLength < 0x80000000 and self.cell.DataLength > 0x3FD8

    @cached_property
    def data(self) -> bytes:
        data_size = self.size
        if self.cell.DataLength & 0x80000000:
            return struct.pack("<I", self.cell.Data)[:data_size]

        if self.is_big_value:
            bd = self.hive.cell(self.cell.Data)
            if not isinstance(bd, BigData):
                raise Error(f"Expected BigData, got {bd.__class__.__name__}")

            return bd.data[:data_size]

        return self.hive.cell_data(self.cell.Data)[:data_size]

    @cached_property
    def value(self) -> int | str | list[str] | bytes:
        return parse_value(self.type, self.data)


class KeyIndex(Cell):
    def __len__(self) -> int:
        raise NotImplementedError

    def __iter__(self) -> Iterator[KeyNode]:
        raise NotImplementedError

    @cached_property
    def count(self) -> int:
        raise NotImplementedError

    def subkey(self, name: str) -> KeyNode | None:
        raise NotImplementedError


class IndexRoot(KeyIndex):
    __signature__ = b"ri"
    __struct__ = c_regf._CM_KEY_INDEX

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[KeyNode]:
        for entry in self.cell.List:
            yield from self.hive.cell(entry)

    @cached_property
    def count(self) -> int:
        return self.cell.Count

    def subkey(self, name: str) -> KeyNode | None:
        for entry in self.cell.List:
            if sk := self.hive.cell(entry).subkey(name):
                return sk
        return None


class IndexLeaf(KeyIndex):
    __signature__ = b"li"
    __struct__ = c_regf._CM_KEY_INDEX

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[KeyNode]:
        for entry in self.cell.List:
            yield self.hive.cell(entry)

    @cached_property
    def count(self) -> int:
        return self.cell.Count

    def subkey(self, name: str) -> KeyNode | None:
        for entry in self.cell.List:
            if (sk := self.hive.cell(entry)).name == name:
                return sk
        return None


class HashLeaf(KeyIndex):
    __signature__ = b"lh"
    __struct__ = c_regf._CM_KEY_HASH_INDEX

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[KeyNode]:
        for entry in self.cell.List:
            yield self.hive.cell(entry.Cell)

    @cached_property
    def count(self) -> int:
        return self.cell.Count

    def subkey(self, name: str) -> KeyNode | None:
        name_hash = hashname(name)
        name = name.lower()

        for entry in self.cell.List:
            if name_hash == entry.HashKey and (sk := self.hive.cell(entry.Cell)).name.lower() == name:
                return sk

        return None


class FastLeaf(KeyIndex):
    __signature__ = b"lf"
    __struct__ = c_regf._CM_KEY_FAST_INDEX

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[KeyNode]:
        for entry in self.cell.List:
            yield self.hive.cell(entry.Cell)

    @cached_property
    def count(self) -> int:
        return self.cell.Count

    def subkey(self, name: str) -> KeyNode | None:
        name = name.lower()
        name_hint = name[:4]

        for entry in self.cell.List:
            # If names are < 4 characters, the name hint is padded with
            # 0-bytes, the characters are stored from the lowest byte number
            # up.
            # Note that names of keys are only supposed to contain "printable
            # characters except the `\' character", which probably is the
            # printable subset of ascii (MS documentation is inconclusive on
            # this).
            if (
                name_hint == entry.NameHint.rstrip(b"\x00").decode("ascii").lower()
                and (sk := self.hive.cell(entry.Cell)).name.lower() == name
            ):
                return sk

        return None


class KeySecurity(Cell):
    __signature__ = b"sk"
    __struct__ = c_regf._CM_KEY_SECURITY


class BigData(Cell):
    __signature__ = b"db"
    __struct__ = c_regf._CM_BIG_DATA

    @property
    def data(self) -> bytes:
        parts = []
        segment_list = self.hive.cell_data(self.cell.List)
        for segment in c_regf.int32[self.cell.Count](segment_list):
            part = self.hive.cell_data(segment)
            parts.append(part[:16344])  # CM_KEY_VALUE_BIG

        return b"".join(parts)


_CELL_CLASSES = {
    KeyNode.__signature__: KeyNode,
    KeyValue.__signature__: KeyValue,
    IndexRoot.__signature__: IndexRoot,
    IndexLeaf.__signature__: IndexLeaf,
    FastLeaf.__signature__: FastLeaf,
    HashLeaf.__signature__: HashLeaf,
    KeySecurity.__signature__: KeySecurity,
    BigData.__signature__: BigData,
}


def decode_name(blob: bytes, size: int, is_comp_name: bool) -> str:
    if is_comp_name:
        try:
            return blob.decode()
        except UnicodeDecodeError:
            pass

        try:
            return blob.decode("latin1")
        except UnicodeDecodeError:
            pass
    else:
        try:
            return c_regf.wchar[size // 2](blob)
        except UnicodeDecodeError:
            pass

    return repr(blob)


def try_decode_sz(data: bytes) -> str:
    if not len(data):
        return ""

    try:
        if (data.isascii() or data.endswith(b"\x00")) and data[1:2] != b"\x00":
            # This will return the string latin1 decoded up until the first
            # NULL byte.
            return data.split(b"\x00")[0].decode("latin1")

        if len(data) % 2 != 0:
            data = data.ljust(len(data) + 1, b"\x00")

        # This will return the string utf-16-le decoded up until the first
        # double NULL byte.
        # A naive split on two NULL bytes will not work as the possibility
        # exists that the first NULL byte is the high byte of the first
        # character and the second NULL byte the low byte of the second
        # character. So the first NULL byte should start at an even index in
        # the data.
        idx = -1
        while (idx := data.find(b"\x00\x00", idx + 1)) & 1:
            if idx == -1:
                idx = len(data)
                break
        return data[:idx].decode("utf-16-le")

    except UnicodeDecodeError:
        # Last ditch effort, decode the whole bytestring as if it were utf-16,
        # any decoding errors, which incase of utf-16 will be invalid utf-16
        # surrogates, will be ignored.
        return data.decode("utf-16-le", "ignore").strip("\x00")


def parse_value(data_type: int, data: bytes) -> int | str | list[str] | bytes:
    if data_type in (REG_DWORD, REG_DWORD_BIG_ENDIAN):
        if len(data) == 0:
            return 0

        if data_type == REG_DWORD:
            return c_regf.uint32(data[:4])

        if data_type == REG_DWORD_BIG_ENDIAN:
            return struct.unpack(">I", data[:4])[0]

    if data_type == REG_QWORD:
        return c_regf.uint64(data) if len(data) else 0

    if data_type in (REG_SZ, REG_EXPAND_SZ):
        return try_decode_sz(data)

    if data_type in (REG_BINARY, REG_NONE):
        return data

    if data_type == REG_MULTI_SZ:
        data_len = len(data)
        data = BytesIO(data)

        multi_string = []
        while data.tell() < data_len:
            string = read_null_terminated_wstring(data)
            if string == "":
                break

            multi_string.append(string)

        return multi_string

    if data_type == REG_FULL_RESOURCE_DESCRIPTOR:
        log.warning("Unimplemented REG_FULL_RESOURCE_DESCRIPTOR")
        return data

    if data_type == REG_RESOURCE_REQUIREMENTS_LIST:
        log.warning("Unimplemented REG_RESOURCE_REQUIREMENTS_LIST")
        return data

    log.warning("Data type 0x%x not supported", data_type)
    return data


def read_null_terminated_wstring(stream: BinaryIO, encoding: str = "utf-16-le") -> str:
    """Adapted function to read null terminated wide strings.

    The cstruct way raises EOFError when the end of the stream is reached.
    This is fine, but not what we want for this particular implementation.
    """
    wide_string = b""
    while True:
        wide_char = stream.read(2)

        if len(wide_char) != 2 or wide_char == b"\x00\x00":
            break

        wide_string += wide_char

    return wide_string.decode(encoding)


def hashname(name: str) -> int:
    # Note that `name' is a python str(), which means the ord() used to
    # calculate name_hash works (it wouldn't for byte()).
    # Also note that names of keys are only supposed to contain "printable
    # characters except the `\' character", which probably is the printable
    # subset of ascii (MS documentation is inconclusive on this).
    name_hash = 0
    for char in name.upper():
        name_hash = (name_hash * 37 + ord(char)) & 0xFFFFFFFF

    return name_hash


def xor32_crc(data: bytes) -> int:
    crc = 0
    for ii in c_regf.uint32[len(data) // 4](data):
        crc ^= ii

    return crc
