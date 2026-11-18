"""QA cases C01-C04 for ``KeyValue.data`` / ``is_big_value`` / ``BigData.data``.

Each case builds synthetic in-memory hives with ``tests/bigdata_hive.py``,
runs real assertions against the production parser in ``dissect/regf/regf.py``
and returns a JSON-serialisable result record. The same functions back both
``tests/test_regf_bigdata.py`` (pytest) and ``tools/local_verify.py`` (CLI),
so the verification wrapper is a runner, not a separate proof.

No production code is modified by these cases.
"""

from __future__ import annotations

import hashlib
import io
import random
import struct
from pathlib import Path
from typing import Callable

from dissect.regf import regf
from dissect.regf.c_regf import REG_BINARY, REG_DWORD

from tests.bigdata_hive import (
    CM_KEY_VALUE_BIG,
    DATA_IN_INLINE,
    PADDING_BYTE,
    HBASE_BLOCK_SIZE,
    HiveBuilder,
    ProvenanceIO,
    add_big_value,
    big_data_payload,
    finalize_hive,
    key_node_payload,
    key_value_payload,
    uint32_list_payload,
    values_by_name,
)

SEED_C01 = 0xC01C01
SEED_C02 = 0xC02C02
# Fixed seed chosen so the legal layout is non-trivial: 5 segments, shuffled
# physical placement [3, 0, 2, 4, 1], mixed gaps and in-cell slack.
SEED_C04 = 0xC04C06


def _summarize(value: object) -> object:
    """Summarise a value for JSON output without exposing raw sample bytes."""
    if isinstance(value, (bytes, bytearray)):
        return {"bytes_len": len(value), "sha256_16": hashlib.sha256(bytes(value)).hexdigest()[:16]}
    if isinstance(value, str) and len(value) > 80:
        return value[:80] + "..."
    return value


class Asserter:
    """Collects assertion outcomes instead of stopping at the first failure."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def check(self, name: str, ok: bool, expected: object = None, observed: object = None) -> bool:
        self.records.append(
            {
                "name": name,
                "ok": bool(ok),
                "expected": _summarize(expected),
                "observed": _summarize(observed),
            }
        )
        return ok

    def eq(self, name: str, observed: object, expected: object) -> bool:
        return self.check(name, observed == expected, expected=expected, observed=observed)

    @property
    def failures(self) -> list[dict]:
        return [record for record in self.records if not record["ok"]]


def _probe_boundary(name: str, fn: Callable[[], object]) -> dict:
    """Run ``fn`` and record the parser's behaviour at a capability boundary."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - the exception *is* the observation
        return {
            "name": name,
            "raised": True,
            "error_type": f"{type(exc).__module__}.{type(exc).__name__}",
            "detail": str(exc)[:120],
        }
    return {"name": name, "raised": False, "error_type": None, "detail": "no exception raised"}


def _result(
    case_id: str,
    title: str,
    asserter: Asserter,
    *,
    seed: int | None = None,
    samples: list[dict] | None = None,
    boundaries: list[dict] | None = None,
    meta: dict | None = None,
) -> dict:
    failures = asserter.failures
    return {
        "case": case_id,
        "title": title,
        "assertions": len(asserter.records),
        "passed": len(asserter.records) - len(failures),
        "failed": len(failures),
        "skipped": 0,
        "seed": seed,
        "samples": samples or [],
        "boundaries": boundaries or [],
        "failures": failures,
        "meta": meta or {},
    }


def _production_sha256() -> str:
    return hashlib.sha256(Path(regf.__file__).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# C01: storage branch selection (inline / normal cell / BigData) driven by
# hive version and the DataLength high bit / threshold.
# ---------------------------------------------------------------------------


def case_c01() -> dict:
    a = Asserter()
    rng = random.Random(SEED_C01)

    normal_content = rng.randbytes(100)
    edge_eq_content = rng.randbytes(CM_KEY_VALUE_BIG)  # exactly 0x3FD8 bytes
    edge_plus_content = rng.randbytes(CM_KEY_VALUE_BIG + 1)  # 0x3FD9 bytes
    big_content = rng.randbytes(CM_KEY_VALUE_BIG + 10)

    # --- hive with Minor version 5: all three storage branches apply
    builder = HiveBuilder(minor=5)
    vk_inline4 = builder.add_cell(
        key_value_payload("Inline4", DATA_IN_INLINE | 4, 0x11223344, REG_DWORD), "vk:Inline4"
    )
    vk_inline1 = builder.add_cell(
        key_value_payload("Inline1", DATA_IN_INLINE | 1, 0x000000AB, REG_BINARY), "vk:Inline1"
    )
    data_normal = builder.add_cell(normal_content, "data:Normal")
    vk_normal = builder.add_cell(key_value_payload("Normal", len(normal_content), data_normal), "vk:Normal")
    data_edge_eq = builder.add_cell(edge_eq_content, "data:EdgeEq")
    vk_edge_eq = builder.add_cell(key_value_payload("EdgeEq", CM_KEY_VALUE_BIG, data_edge_eq), "vk:EdgeEq")
    vk_edge_plus = add_big_value(builder, "EdgePlus", edge_plus_content)
    vk_big = add_big_value(builder, "Big", big_content)
    hive5_bytes = finalize_hive(builder, [vk_inline4, vk_inline1, vk_normal, vk_edge_eq, vk_edge_plus, vk_big])

    hive5 = regf.RegistryHive(io.BytesIO(hive5_bytes))
    values5 = values_by_name(hive5)

    a.eq("minor5: hive version is 5", hive5.version, 5)

    inline4 = values5["Inline4"]
    a.eq("inline4: not a big value", inline4.is_big_value, False)
    a.eq("inline4: size masks the inline high bit", inline4.size, 4)
    a.eq("inline4: data is the Data dword, little-endian", inline4.data, struct.pack("<I", 0x11223344))
    a.eq("inline4: REG_DWORD value round-trips", inline4.value, 0x11223344)

    inline1 = values5["Inline1"]
    a.eq("inline1: not a big value", inline1.is_big_value, False)
    a.eq("inline1: size 1 keeps only the low byte", inline1.size, 1)
    a.eq("inline1: data truncated to the declared length", inline1.data, b"\xab")

    normal = values5["Normal"]
    a.eq("normal: not a big value", normal.is_big_value, False)
    a.eq("normal: size", normal.size, 100)
    a.eq("normal: data read from plain cell", normal.data, normal_content)

    edge_eq = values5["EdgeEq"]
    a.eq("edge-eq: DataLength == 0x3FD8 is not big", edge_eq.is_big_value, False)
    a.eq("edge-eq: data read from plain cell", edge_eq.data, edge_eq_content)

    edge_plus = values5["EdgePlus"]
    a.eq("edge-plus: DataLength == 0x3FD9 is big", edge_plus.is_big_value, True)
    a.eq("edge-plus: data reassembled from BigData segments", edge_plus.data, edge_plus_content)

    big = values5["Big"]
    a.eq("big: is a big value", big.is_big_value, True)
    a.eq("big: size", big.size, CM_KEY_VALUE_BIG + 10)
    a.eq("big: data reassembled from BigData segments", big.data, big_content)

    # --- hive with Minor version 4: the BigData branch is version-gated
    legacy_content = rng.randbytes(CM_KEY_VALUE_BIG + 10)
    builder4 = HiveBuilder(minor=4)
    data_legacy = builder4.add_cell(legacy_content, "data:Legacy")
    vk_legacy = builder4.add_cell(key_value_payload("Legacy", len(legacy_content), data_legacy), "vk:Legacy")
    hive4_bytes = finalize_hive(builder4, [vk_legacy])

    hive4 = regf.RegistryHive(io.BytesIO(hive4_bytes))
    legacy = values_by_name(hive4)["Legacy"]

    a.eq("minor4: hive version is 4", hive4.version, 4)
    a.eq("minor4: same DataLength is not big on version <= 4", legacy.is_big_value, False)
    a.eq("minor4: large value read from a single plain cell", legacy.data, legacy_content)

    samples = [record.as_dict() for record in builder.records + builder4.records]
    return _result(
        "C01",
        "storage branch selection by hive version and DataLength semantics",
        a,
        seed=SEED_C01,
        samples=samples,
        meta={
            "threshold": CM_KEY_VALUE_BIG,
            "inline_flag": DATA_IN_INLINE,
            "versions": [4, 5],
        },
    )


# ---------------------------------------------------------------------------
# C02: segment boundaries, last-segment padding, exact value length and
# cross-storage consistency of identical logical bytes.
# ---------------------------------------------------------------------------


def case_c02() -> dict:
    a = Asserter()

    pair_content = random.Random(SEED_C02).randbytes(4)
    one_seg_content = random.Random(SEED_C02 + 1).randbytes(CM_KEY_VALUE_BIG)
    two_seg_content = random.Random(SEED_C02 + 2).randbytes(CM_KEY_VALUE_BIG + 1)
    three_seg_content = random.Random(SEED_C02 + 3).randbytes(2 * CM_KEY_VALUE_BIG + 12)
    wide_content = random.Random(SEED_C02 + 4).randbytes(40000)

    builder = HiveBuilder(minor=5)

    # Same 4 logical bytes stored inline and in a plain cell.
    vk_pair_inline = builder.add_cell(
        key_value_payload("PairInline", DATA_IN_INLINE | 4, int.from_bytes(pair_content, "little"), REG_BINARY),
        "vk:PairInline",
    )
    data_pair = builder.add_cell(pair_content, "data:PairNormal")
    vk_pair_normal = builder.add_cell(key_value_payload("PairNormal", 4, data_pair), "vk:PairNormal")

    # Exactly one full segment worth of bytes, stored in a plain cell.
    data_one_seg = builder.add_cell(one_seg_content, "data:OneSeg")
    vk_one_seg = builder.add_cell(key_value_payload("OneSeg", len(one_seg_content), data_one_seg), "vk:OneSeg")

    # Two segments, the second holding a single byte plus 0xCC slack.
    vk_two_seg = add_big_value(builder, "TwoSegTiny", two_seg_content, slack=16)

    # Three segments, every segment cell carrying in-cell 0xCC slack.
    vk_three_seg = add_big_value(builder, "ThreeSeg", three_seg_content, slack=16)

    # 40000 bytes via BigData on version 5 ...
    vk_wide = add_big_value(builder, "Wide", wide_content)

    hive5_bytes = finalize_hive(builder, [vk_pair_inline, vk_pair_normal, vk_one_seg, vk_two_seg, vk_three_seg, vk_wide])
    values5 = values_by_name(regf.RegistryHive(io.BytesIO(hive5_bytes)))

    # ... and the same 40000 bytes in a single plain cell on version 4.
    builder4 = HiveBuilder(minor=4)
    data_wide4 = builder4.add_cell(wide_content, "data:WideLegacy")
    vk_wide4 = builder4.add_cell(key_value_payload("WideLegacy", len(wide_content), data_wide4), "vk:WideLegacy")
    hive4_bytes = finalize_hive(builder4, [vk_wide4])
    values4 = values_by_name(regf.RegistryHive(io.BytesIO(hive4_bytes)))

    a.eq("pair: inline storage", values5["PairInline"].data, pair_content)
    a.eq("pair: plain cell storage", values5["PairNormal"].data, pair_content)
    a.eq("pair: identical logical bytes across storage methods", values5["PairInline"].data, values5["PairNormal"].data)

    one_seg = values5["OneSeg"]
    a.eq("one-seg: not big at exactly 0x3FD8 bytes", one_seg.is_big_value, False)
    a.eq("one-seg: data", one_seg.data, one_seg_content)

    two_seg = values5["TwoSegTiny"]
    a.eq("two-seg: big at 0x3FD8 + 1 bytes", two_seg.is_big_value, True)
    a.eq("two-seg: length is exactly DataLength, padding excluded", len(two_seg.data), len(two_seg_content))
    a.eq("two-seg: data", two_seg.data, two_seg_content)

    three_seg = values5["ThreeSeg"]
    a.eq("three-seg: big", three_seg.is_big_value, True)
    a.eq("three-seg: length is exactly DataLength", len(three_seg.data), len(three_seg_content))
    a.eq("three-seg: data", three_seg.data, three_seg_content)
    a.eq(
        "three-seg: first byte after segment boundary 1",
        three_seg.data[CM_KEY_VALUE_BIG],
        three_seg_content[CM_KEY_VALUE_BIG],
    )
    a.eq(
        "three-seg: first byte after segment boundary 2",
        three_seg.data[2 * CM_KEY_VALUE_BIG],
        three_seg_content[2 * CM_KEY_VALUE_BIG],
    )

    wide5 = values5["Wide"]
    a.eq("wide: big on version 5", wide5.is_big_value, True)
    a.eq("wide: data via BigData", wide5.data, wide_content)
    a.eq("wide-legacy: not big on version 4", values4["WideLegacy"].is_big_value, False)
    a.eq(
        "wide: identical logical bytes across BigData and legacy single-cell storage",
        wide5.data,
        values4["WideLegacy"].data,
    )

    # Fixture self-checks: the 0xCC padding really exists in the raw image
    # (so the assertions above prove padding is excluded, not absent).
    last_segment = builder.record_for("segment:TwoSegTiny:1")
    pad_start = last_segment.file_offset + 4 + last_segment.payload_length
    a.eq(
        "fixture: last segment cell carries 0xCC slack after the payload",
        hive5_bytes[pad_start : pad_start + 8],
        bytes([PADDING_BYTE]) * 8,
    )
    mid_segment = builder.record_for("segment:ThreeSeg:1")
    a.eq("fixture: middle segment cell is larger than one segment", mid_segment.size - 4 > CM_KEY_VALUE_BIG, True)
    a.eq("fixture: middle segment payload is a full segment", mid_segment.payload_length, CM_KEY_VALUE_BIG)

    samples = [record.as_dict() for record in builder.records + builder4.records]
    return _result(
        "C02",
        "segment boundaries, last-segment padding, exact length and cross-storage consistency",
        a,
        seed=SEED_C02,
        samples=samples,
        meta={
            "segment_size": CM_KEY_VALUE_BIG,
            "value_lengths": [4, CM_KEY_VALUE_BIG, CM_KEY_VALUE_BIG + 1, 2 * CM_KEY_VALUE_BIG + 12, 40000],
            "padding_marker": PADDING_BYTE,
        },
    )


# ---------------------------------------------------------------------------
# C03: forensic provenance (raw offsets, read sources, concatenation order)
# and parser capability boundaries.
# ---------------------------------------------------------------------------


def _boundary_hive(data_target_offset_role: str) -> tuple[bytes, HiveBuilder]:
    """Hive whose big vk points its Data field at a non-BigData cell."""
    builder = HiveBuilder(minor=5)
    if data_target_offset_role == "key-node":
        target = builder.add_cell(key_node_payload("NotDB", [], 0xFFFFFFFF), "boundary:nk")
    else:  # unknown signature
        target = builder.add_cell(b"zz" + b"\x00" * 30, "boundary:unknown-sig")
    vk = builder.add_cell(key_value_payload("Broken", CM_KEY_VALUE_BIG + 10, target), "vk:Broken")
    return finalize_hive(builder, [vk]), builder


def case_c03() -> dict:
    a = Asserter()

    # Three segments with distinct tag bytes. Physical placement is ascending
    # (s0 < s1 < s2) but the segment list orders them [s2, s0, s1], so the
    # parsed output proves the parser follows list order, not offset order.
    builder = HiveBuilder(minor=5)
    tag_contents = {0: bytes([0x11]) * CM_KEY_VALUE_BIG, 1: bytes([0x22]) * CM_KEY_VALUE_BIG, 2: bytes([0x33]) * CM_KEY_VALUE_BIG}
    seg_offsets = {index: builder.add_cell(content, f"segment:{index}") for index, content in tag_contents.items()}
    list_order = [2, 0, 1]
    data_length = 2 * CM_KEY_VALUE_BIG + 100
    list_offset = builder.add_cell(uint32_list_payload([seg_offsets[i] for i in list_order]), "segment-list")
    db_offset = builder.add_cell(big_data_payload(list_offset, len(list_order)), "db")
    vk = builder.add_cell(key_value_payload("Shuffled", data_length, db_offset), "vk:Shuffled")
    hive_bytes = finalize_hive(builder, [vk])

    expected = tag_contents[2] + tag_contents[0] + tag_contents[1][:100]

    provenance = ProvenanceIO(hive_bytes)
    hive = regf.RegistryHive(provenance)
    value = values_by_name(hive)["Shuffled"]

    a.eq("shuffled: big value", value.is_big_value, True)
    a.eq("shuffled: concatenation follows segment-list order", value.data, expected)
    a.eq("shuffled: exact length", len(value.data), data_length)

    file_offset_of = {index: seg_offsets[index] + HBASE_BLOCK_SIZE for index in seg_offsets}
    segment_read_sequence = [off for off in provenance.read_offsets() if off in set(file_offset_of.values())]
    a.eq(
        "provenance: segments are read in list order, not offset order",
        segment_read_sequence,
        [file_offset_of[2], file_offset_of[0], file_offset_of[1]],
    )
    a.eq(
        "provenance: db cell was read from its recorded offset",
        db_offset + HBASE_BLOCK_SIZE in provenance.read_offsets(),
        True,
    )
    a.eq(
        "provenance: segment list cell was read from its recorded offset",
        list_offset + HBASE_BLOCK_SIZE in provenance.read_offsets(),
        True,
    )
    a.eq(
        "provenance: physical placement differs from logical order",
        [seg_offsets[0], seg_offsets[1], seg_offsets[2]] != [seg_offsets[i] for i in list_order],
        True,
    )

    # --- capability boundaries (recorded separately, asserted as observed)
    boundaries = []

    def truncated_list() -> None:
        builder_t = HiveBuilder(minor=5)
        s1 = builder_t.add_cell(b"A" * 100, "segment:0")
        s2 = builder_t.add_cell(b"B" * 100, "segment:1")
        # db declares 3 segments but the list cell only carries 2 entries;
        # the 0xCC in-cell padding is dereferenced as the third offset.
        list_off = builder_t.add_cell(uint32_list_payload([s1, s2]), "segment-list")
        db_off = builder_t.add_cell(big_data_payload(list_off, 3), "db")
        vk_off = builder_t.add_cell(key_value_payload("Trunc", CM_KEY_VALUE_BIG + 10, db_off), "vk:Trunc")
        image = finalize_hive(builder_t, [vk_off])
        values_by_name(regf.RegistryHive(io.BytesIO(image)))["Trunc"].data

    boundary = _probe_boundary("truncated-segment-list", truncated_list)
    boundaries.append(boundary)
    a.check("boundary: truncated segment list raises", boundary["raised"], expected=True, observed=boundary["raised"])
    a.eq("boundary: truncated segment list error type", boundary["error_type"], "builtins.ValueError")

    def wrong_type_key_node() -> None:
        image, _ = _boundary_hive("key-node")
        values_by_name(regf.RegistryHive(io.BytesIO(image)))["Broken"].data

    boundary = _probe_boundary("data-target-is-key-node", wrong_type_key_node)
    boundaries.append(boundary)
    a.check("boundary: nk target raises", boundary["raised"], expected=True, observed=boundary["raised"])
    a.eq("boundary: nk target error type", boundary["error_type"], "dissect.regf.exceptions.Error")

    def wrong_type_unknown() -> None:
        image, _ = _boundary_hive("unknown-sig")
        values_by_name(regf.RegistryHive(io.BytesIO(image)))["Broken"].data

    boundary = _probe_boundary("data-target-unknown-signature", wrong_type_unknown)
    boundaries.append(boundary)
    a.check("boundary: unknown signature target raises", boundary["raised"], expected=True, observed=boundary["raised"])
    a.eq("boundary: unknown signature error type", boundary["error_type"], "builtins.NotImplementedError")

    samples = [record.as_dict() for record in builder.records]
    return _result(
        "C03",
        "raw offset provenance, read sources, concatenation order and capability boundaries",
        a,
        samples=samples,
        boundaries=boundaries,
        meta={
            "segment_list_order": list_order,
            "segment_file_offsets": {str(k): f"0x{v:x}" for k, v in file_offset_of.items()},
            "segment_read_sequence": [f"0x{off:x}" for off in segment_read_sequence],
        },
    )


# ---------------------------------------------------------------------------
# C04: seeded random legal layouts, byte-exact replay, and branch-preserving
# shrink. The production implementation is left untouched.
# ---------------------------------------------------------------------------


def build_randomized_big_hive(seed: int) -> tuple[HiveBuilder, bytes, bytes, dict]:
    """Build a big-value hive whose legal layout is randomised by ``seed``.

    Returns ``(builder, hive_bytes, logical_content, layout)`` where ``layout``
    records the randomised placement for replay and audit.
    """
    rng = random.Random(seed)
    segment_count = rng.randint(2, 5)
    last_length = rng.randint(1, CM_KEY_VALUE_BIG)
    data_length = CM_KEY_VALUE_BIG * (segment_count - 1) + last_length
    content = rng.randbytes(data_length)

    builder = HiveBuilder(minor=5)
    segments = [content[i * CM_KEY_VALUE_BIG : (i + 1) * CM_KEY_VALUE_BIG] for i in range(segment_count)]

    placement = list(range(segment_count))
    rng.shuffle(placement)
    segment_offsets: dict[int, int] = {}
    gaps: dict[int, int] = {}
    slacks: dict[int, int] = {}
    for index in placement:
        gap = rng.choice([0, 8, 24, 64])
        slack = rng.choice([0, 8, 16])
        builder.gap(gap)
        segment_offsets[index] = builder.add_cell(segments[index], f"segment:{index}", slack=slack)
        gaps[index] = gap
        slacks[index] = slack

    list_offset = builder.add_cell(
        uint32_list_payload([segment_offsets[i] for i in range(segment_count)]), "segment-list"
    )
    db_offset = builder.add_cell(big_data_payload(list_offset, segment_count), "db")
    vk_offset = builder.add_cell(key_value_payload("Random", data_length, db_offset), "vk:Random")
    hive_bytes = finalize_hive(builder, [vk_offset])

    layout = {
        "segment_count": segment_count,
        "last_length": last_length,
        "data_length": data_length,
        "placement": placement,
        "segment_offsets": {str(k): v for k, v in segment_offsets.items()},
        "gaps": {str(k): v for k, v in gaps.items()},
        "slacks": {str(k): v for k, v in slacks.items()},
    }
    return builder, hive_bytes, content, layout


def build_shrunk_big_hive(content: bytes) -> tuple[HiveBuilder, bytes]:
    """Minimal two-segment layout preserving the BigData storage branch."""
    builder = HiveBuilder(minor=5)
    first = builder.add_cell(content[:CM_KEY_VALUE_BIG], "segment:0")
    second = builder.add_cell(content[CM_KEY_VALUE_BIG:], "segment:1")
    list_offset = builder.add_cell(uint32_list_payload([first, second]), "segment-list")
    db_offset = builder.add_cell(big_data_payload(list_offset, 2), "db")
    vk_offset = builder.add_cell(key_value_payload("Shrunk", len(content), db_offset), "vk:Shrunk")
    return builder, finalize_hive(builder, [vk_offset])


def case_c04() -> dict:
    a = Asserter()

    builder, hive_bytes, content, layout = build_randomized_big_hive(SEED_C04)
    hive = regf.RegistryHive(io.BytesIO(hive_bytes))
    value = values_by_name(hive)["Random"]

    a.eq("random: hive version is 5", hive.version, 5)
    a.eq("random: storage branch is BigData", value.is_big_value, True)
    a.eq("random: exact length", len(value.data), layout["data_length"])
    a.eq("random: data matches the logical content", value.data, content)

    ordered_offsets = [layout["segment_offsets"][str(i)] for i in range(layout["segment_count"])]
    a.eq(
        "random: physical placement is not plain ascending order",
        ordered_offsets != sorted(ordered_offsets),
        True,
    )

    # Replay: the same seed must reproduce the identical image and layout.
    builder2, hive_bytes2, content2, layout2 = build_randomized_big_hive(SEED_C04)
    a.eq("replay: identical hive image for the same seed", hive_bytes2, hive_bytes)
    a.eq("replay: identical logical content", content2, content)
    a.eq("replay: identical layout", layout2, layout)
    a.eq(
        "replay: identical cell offset records",
        [r.as_dict() for r in builder2.records],
        [r.as_dict() for r in builder.records],
    )

    # Shrink: minimal layout that still triggers the original storage branch,
    # keeping the forensic provenance (offsets) of every segment.
    shrunk_content = content[: CM_KEY_VALUE_BIG + 1]
    shrunk_builder, shrunk_bytes = build_shrunk_big_hive(shrunk_content)
    shrunk_value = values_by_name(regf.RegistryHive(io.BytesIO(shrunk_bytes)))["Shrunk"]

    a.eq("shrink: still the BigData storage branch", shrunk_value.is_big_value, True)
    a.eq("shrink: exact length", len(shrunk_value.data), CM_KEY_VALUE_BIG + 1)
    a.eq("shrink: data matches the shrunk logical content", shrunk_value.data, shrunk_content)
    shrunk_roles = {record.role for record in shrunk_builder.records}
    a.eq(
        "shrink: forensic provenance retained for every segment",
        {"segment:0", "segment:1", "segment-list", "db", "vk:Shrunk"} <= shrunk_roles,
        True,
    )

    samples = [record.as_dict() for record in builder.records + shrunk_builder.records]
    return _result(
        "C04",
        "seeded random legal layouts, replay, and branch-preserving shrink",
        a,
        seed=SEED_C04,
        samples=samples,
        meta={
            "layout": layout,
            "shrunk_data_length": CM_KEY_VALUE_BIG + 1,
            "production_file": str(Path(regf.__file__).name),
            "production_sha256": _production_sha256(),
        },
    )


CASES: dict[str, dict] = {
    "C01": {"title": "storage branch selection (inline / cell / BigData)", "fn": case_c01},
    "C02": {"title": "segment boundaries, padding, exact length, cross-storage consistency", "fn": case_c02},
    "C03": {"title": "offset provenance, read order, capability boundaries", "fn": case_c03},
    "C04": {"title": "seeded random layouts, replay, branch-preserving shrink", "fn": case_c04},
}


def run_case(case_id: str) -> dict:
    """Run one QA case and return its JSON-serialisable result record."""
    try:
        case = CASES[case_id]
    except KeyError:
        raise ValueError(f"unknown case {case_id!r}, expected one of {sorted(CASES)}") from None
    return case["fn"]()
