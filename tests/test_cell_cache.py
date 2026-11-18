from __future__ import annotations

import io

import pytest

from dissect.regf import CellCache, RegistryHive, regf
from tests.synth_hive import build_working_hive


class CountingReader(io.BytesIO):
    """A file-like adapter that records every read as ``(offset, size)``."""

    def __init__(self, data: bytes):
        super().__init__(data)
        self.reads: list[tuple[int, int]] = []

    def read(self, size: int = -1) -> bytes:  # noqa: D102
        offset = self.tell()
        data = super().read(size)
        self.reads.append((offset, len(data)))
        return data

    def payload_reads(self, offset: int, length: int) -> int:
        return sum(1 for start, size in self.reads if start == offset and size == length)


class FailingReader(CountingReader):
    """Raises for reads that start at ``fail_at``; everything else behaves normally."""

    def __init__(self, data: bytes, fail_at: int):
        super().__init__(data)
        self.fail_at = fail_at

    def read(self, size: int = -1) -> bytes:  # noqa: D102
        if self.tell() == self.fail_at:
            self.reads.append((self.tell(), 0))
            raise OSError("synthetic read failure")
        return super().read(size)


@pytest.fixture
def working() -> dict:
    return build_working_hive("SYNTH", tag=b"\x41")


def test_hot_cell_reuse_reads_payload_once(working: dict) -> None:
    fh = CountingReader(working["blob"])
    cache = CellCache(1024 * 64)
    hive = RegistryHive(fh, cache=cache)

    offset = working["children"]["Alpha"]["node_offset"]
    length = working["builder"].payload_length(offset)
    payload_start = 4096 + offset + 4

    assert fh.payload_reads(payload_start, length) == 0
    first = hive.cell_data(offset)
    assert fh.payload_reads(payload_start, length) == 1
    for _ in range(5):
        assert hive.cell_data(offset) == first

    assert fh.payload_reads(payload_start, length) == 1
    assert cache.stats()["hits"] >= 5
    assert first[:2] == b"nk"


def test_only_payload_bytes_are_cached(working: dict) -> None:
    cache = CellCache(1024)
    hive = RegistryHive(io.BytesIO(working["blob"]), cache=cache)
    baseline = cache.stats()["resident_bytes"]

    offset = working["children"]["Bravo"]["blob_value_offset"]
    length = working["builder"].payload_length(offset)
    hive.cell_data(offset)

    assert cache.resident_bytes == baseline + length
    entry = cache._entries[hive._cache_key(offset, length)]
    # The cached bytes are exactly the cell payload, not the size header or a parsed object.
    assert len(entry) == length
    assert cache.stats()["payload_bytes_fetched"] == baseline + length


def test_lru_eviction_and_resident_budget(working: dict) -> None:
    cache = CellCache(200)
    hive = RegistryHive(io.BytesIO(working["blob"]), cache=cache)

    accessed = []
    for child in working["children"].values():
        for offset in (child["node_offset"], child["blob_data_offset"]):
            accessed.append(offset)
            hive.cell_data(offset)
            assert cache.resident_bytes <= cache.budget

    stats = cache.stats()
    assert stats["evictions"] > 0
    assert stats["entries"] < len(accessed)
    assert cache.resident_bytes <= 200


def test_oversized_cell_is_read_but_not_retained() -> None:
    payload = b"\x5A" * 4096
    spec = build_working_hive("BIG", extra_payloads=(payload,))
    offset = spec["extra_offsets"][0]

    cache = CellCache(256)
    hive = RegistryHive(io.BytesIO(spec["blob"]), cache=cache)

    data = hive.cell_data(offset)
    assert data[: len(payload)] == payload
    assert cache.resident_bytes <= 256
    assert cache.stats()["oversized"] == 1
    assert offset not in [key[3] for key in cache._entries]


def test_failed_read_is_not_cached() -> None:
    spec = build_working_hive("FAIL")
    offset = spec["children"]["Charlie"]["node_offset"]
    payload_start = 4096 + offset + 4

    cache = CellCache(4096)
    hive = RegistryHive(io.BytesIO(spec["blob"]), cache=cache)
    baseline = cache.stats()
    hive.fh = FailingReader(spec["blob"], payload_start)

    with pytest.raises(OSError):
        hive.cell_data(offset)

    stats = cache.stats()
    assert stats["failed_reads"] == 1
    assert stats["entries"] == baseline["entries"]
    assert cache.resident_bytes == baseline["resident_bytes"]
    assert offset not in [key[3] for key in cache._entries]


def test_short_read_is_not_cached_as_empty() -> None:
    spec = build_working_hive("SHORT")
    offset = spec["children"]["Delta"]["node_offset"]

    blob = spec["blob"]
    cache = CellCache(4096)
    hive = RegistryHive(io.BytesIO(blob), cache=cache)
    baseline = cache.stats()
    hive.fh = io.BytesIO(blob[: 4096 + offset + 4])

    assert hive.cell_data(offset) == b""
    stats = cache.stats()
    assert stats["short_reads"] == 1
    assert stats["entries"] == baseline["entries"]
    assert cache.resident_bytes == baseline["resident_bytes"]
    assert offset not in [key[3] for key in cache._entries]


def test_same_offset_across_hives_is_isolated() -> None:
    one = build_working_hive("SAME.NAME", tag=b"\x41")
    two = build_working_hive("SAME.NAME", tag=b"\x42")
    offset = one["children"]["Alpha"]["blob_data_offset"]

    assert offset == two["children"]["Alpha"]["blob_data_offset"]
    length = one["builder"].payload_length(offset)
    assert length == two["builder"].payload_length(offset)
    payload_len = len(one["children"]["Alpha"]["blob_payload"])

    cache = CellCache(4096)
    hive_one = RegistryHive(io.BytesIO(one["blob"]), cache=cache)
    hive_two = RegistryHive(io.BytesIO(two["blob"]), cache=cache)
    assert cache.stats()["entries"] == 2  # two root cells, same offset, still separate entries

    assert hive_one.cell_data(offset)[:payload_len] == b"\x41" * payload_len
    assert hive_two.cell_data(offset)[:payload_len] == b"\x42" * payload_len
    assert cache.stats()["entries"] == 4
    assert cache.resident_bytes >= 2 * length
    assert hive_one._cache_id != hive_two._cache_id


def test_collection_version_change_isolates_cache() -> None:
    first = build_working_hive("VER", tag=b"\x41")
    second = build_working_hive("VER", tag=b"\x42")
    offset = first["children"]["Bravo"]["blob_data_offset"]
    length = first["builder"].payload_length(offset)
    payload_len = len(first["children"]["Bravo"]["blob_payload"])

    fh = io.BytesIO(first["blob"])
    cache = CellCache(4096)
    hive = RegistryHive(fh, cache=cache)

    assert hive.cell_data(offset)[:payload_len] == b"\x41" * payload_len
    entries_after_first = cache.stats()["entries"]
    assert entries_after_first == 2  # root cell cached at construction, plus the blob cell

    fh.seek(0)
    fh.write(second["blob"])
    old_key = hive._cache_key(offset, length)
    hive.set_collection_version("acquisition-2")

    # Every entry of the old acquisition is dropped; only the fresh read is retained.
    assert old_key not in cache
    assert cache.stats()["entries"] == 0
    assert cache.resident_bytes == 0
    assert hive.cell_data(offset)[:payload_len] == b"\x42" * payload_len
    assert cache.stats()["entries"] == 1
    assert cache.resident_bytes == length


def test_release_only_affects_one_hive() -> None:
    one = build_working_hive("ONE", tag=b"\x41")
    two = build_working_hive("TWO", tag=b"\x42")
    offset = one["children"]["Charlie"]["node_offset"]

    cache = CellCache(4096)
    fh_one = CountingReader(one["blob"])
    fh_two = CountingReader(two["blob"])
    hive_one = RegistryHive(fh_one, cache=cache)
    hive_two = RegistryHive(fh_two, cache=cache)

    hive_one.cell_data(offset)
    hive_two.cell_data(offset)
    assert cache.stats()["entries"] == 4

    released = hive_one.release_cache()

    assert released == 2
    assert cache.stats()["entries"] == 2
    assert hive_two.cell_data(offset) != b""

    # A cached hit for hive two must not touch its file handle's payload region.
    payload_start = 4096 + offset + 4
    length = two["builder"].payload_length(offset)
    before = fh_two.payload_reads(payload_start, length)
    hive_two.cell_data(offset)
    assert fh_two.payload_reads(payload_start, length) == before

    assert not fh_one.closed
    assert not fh_two.closed


def test_default_constructor_is_unchanged(working: dict) -> None:
    fh = io.BytesIO(working["blob"])
    hive = RegistryHive(fh)

    assert hive.cache is None
    node = hive.root()
    assert node.name == "ROOT"
    assert len(list(node.subkeys())) == 4
    assert hive.cell(node.cell.SubKeyLists[0]) is hive.cell(node.cell.SubKeyLists[0])
    assert hive.release_cache() == 0
    assert not fh.closed


def test_cell_payload_keeps_handle_position(working: dict) -> None:
    offset = working["children"]["Alpha"]["node_offset"]
    length = working["builder"].payload_length(offset)

    plain = io.BytesIO(working["blob"])
    plain_hive = RegistryHive(io.BytesIO(working["blob"]))
    plain_hive.fh = plain
    plain_hive.cell_data(offset)
    expected = plain.tell()

    cached_hive = RegistryHive(io.BytesIO(working["blob"]), cache=CellCache(4096))
    cached_hive.cell_data(offset)
    assert cached_hive.fh.tell() == expected

    cached_hive.cell_data(offset)
    assert cached_hive.fh.tell() == expected
    assert expected == 4096 + offset + 4 + length


def test_cache_budget_validation() -> None:
    assert CellCache(0).budget == 0
    with pytest.raises(ValueError):
        CellCache(-1)
    with pytest.raises(TypeError):
        CellCache(1.5)


def test_cache_does_not_claim_process_memory_limit() -> None:
    doc = CellCache.__doc__ or ""
    assert "not" in doc and "process" in doc
    assert not hasattr(CellCache, "process_memory_limit")
    assert regf.CellCache is CellCache


def test_actions_keep_resident_within_budget_under_mixed_sizes() -> None:
    payloads = (b"\x01" * 8, b"\x02" * 512, b"\x03" * 1024, b"\x04" * 40)
    spec = build_working_hive("MIXED", extra_payloads=payloads)
    cache = CellCache(600)
    hive = RegistryHive(io.BytesIO(spec["blob"]), cache=cache)

    operations = [child["blob_data_offset"] for child in spec["children"].values()]
    operations.extend(spec["extra_offsets"])

    for _ in range(2):
        for offset in operations:
            hive.cell_data(offset)
            assert cache.resident_bytes <= cache.budget
            assert cache.resident_bytes == sum(len(value) for value in cache._entries.values())

    stats = cache.stats()
    assert stats["evictions"] > 0
    assert stats["oversized"] >= 1
    assert stats["budget"] == 600


def test_real_fixture_parity_between_plain_and_cached(system_hive: BinaryIO) -> None:
    raw = system_hive.read()
    plain = RegistryHive(io.BytesIO(raw))
    cache = CellCache(64 * 1024)
    cached = RegistryHive(io.BytesIO(raw), cache=cache)

    plain_walk = [(offset, allocated) for offset, allocated, _ in plain.walk()]
    cached_walk = [(offset, allocated) for offset, allocated, _ in cached.walk()]

    assert plain_walk == cached_walk
    assert cache.resident_bytes <= cache.budget

    key_path = "ControlSet001\\Control\\Lsa"
    assert plain.open(key_path).name == cached.open(key_path).name
    assert [value.value for value in plain.open(key_path).values()] == [
        value.value for value in cached.open(key_path).values()
    ]
