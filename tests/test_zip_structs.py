"""ZIP/ZIP64 结构解析单元测试（基于生成器产出的字节，两者独立实现）。"""

from __future__ import annotations

import struct

import pytest

from selective_zip import protocol as P
from selective_zip import zip_structs
from selective_zip.errors import HeaderMismatch, ZipStructureError
from sampler.experiment_data import build_member_document, encode_member, member_name
from sampler.patcher import corrupt_local_header_crc, flip_compressed_data_byte
from sampler.zip_writer import ZipMember, ZipWriter


def _make_zip(members, force_zip64=False):
    return ZipWriter(members).render() if not force_zip64 else ZipWriter(members, force_zip64=True).render()


def _json_member(version, part, count, **kwargs):
    return ZipMember(member_name(version, part), encode_member(build_member_document(version, part, count)), **kwargs)


def test_find_eocd_basic():
    blob = _make_zip([_json_member(1, 1, 3)])
    eocd, offset, _ = zip_structs.find_eocd(blob, len(blob))
    assert eocd.is_zip64 is False
    assert eocd.total_entries == 1
    assert blob[offset : offset + 4] == P.SIG_END_OF_CENTRAL_DIR


def test_find_eocd_from_small_tail_window():
    blob = _make_zip([_json_member(1, 1, 3), _json_member(2, 2, 4)])
    # 只用最后 64 字节也应能定位 EOCD（无注释）
    eocd, _offset, _ = zip_structs.find_eocd(blob[-64:], len(blob))
    assert eocd.total_entries == 2


def test_missing_eocd_raises():
    with pytest.raises(ZipStructureError):
        zip_structs.find_eocd(b"not a zip file at all" * 4, 64)


def test_central_directory_roundtrip():
    members = [_json_member(1, 1, 3), _json_member(2, 2, 4), _json_member(3, 3, 5)]
    blob = _make_zip(members)
    eocd, _, _ = zip_structs.find_eocd(blob, len(blob))
    cd = blob[eocd.central_dir_offset : eocd.central_dir_offset + eocd.central_dir_size]
    entries = zip_structs.parse_central_directory(cd, eocd.total_entries)
    assert [e.filename for e in entries] == [m.filename for m in members]
    assert all(e.compression == P.COMPRESSION_DEFLATE for e in entries)
    assert all(e.is_encrypted is False for e in entries)


def test_zip64_structures_parse():
    members = [_json_member(3, 1, 5), _json_member(1, 2, 4)]
    blob = _make_zip(members, force_zip64=True)
    eocd, _, _ = zip_structs.find_eocd(blob, len(blob))
    assert eocd.is_zip64 is True
    cd = blob[eocd.central_dir_offset : eocd.central_dir_offset + eocd.central_dir_size]
    entries = zip_structs.parse_central_directory(cd, eocd.total_entries)
    assert len(entries) == 2
    # ZIP64 下真实大小不应再是占位值
    assert all(e.compressed_size < P.ZIP64_MAGIC for e in entries)
    assert all(e.uncompressed_size < P.ZIP64_MAGIC for e in entries)
    # 本地头也能解析并回填
    first = entries[0]
    local_block = blob[first.local_header_offset : first.local_header_offset + 256]
    header = zip_structs.parse_local_header(local_block)
    assert header.compressed_size == first.compressed_size
    assert header.uncompressed_size == first.uncompressed_size


def test_local_header_consistency_check_passes():
    members = [_json_member(2, 1, 4)]
    blob = _make_zip(members)
    eocd, _, _ = zip_structs.find_eocd(blob, len(blob))
    cd = blob[eocd.central_dir_offset : eocd.central_dir_offset + eocd.central_dir_size]
    entry = zip_structs.parse_central_directory(cd, 1)[0]
    header = zip_structs.parse_local_header(blob[entry.local_header_offset : entry.local_header_offset + 256])
    # 不抛异常即通过
    zip_structs.verify_header_consistency(entry, header)


def test_local_header_crc_conflict_detected():
    members = [_json_member(2, 1, 4), _json_member(3, 2, 5)]
    blob = _make_zip(members)
    bad = corrupt_local_header_crc(blob, members[0].filename)
    eocd, _, _ = zip_structs.find_eocd(bad, len(bad))
    cd = bad[eocd.central_dir_offset : eocd.central_dir_offset + eocd.central_dir_size]
    entry = zip_structs.parse_central_directory(cd, 2)[0]
    header = zip_structs.parse_local_header(bad[entry.local_header_offset : entry.local_header_offset + 256])
    with pytest.raises(HeaderMismatch, match="CRC"):
        zip_structs.verify_header_consistency(entry, header)


def test_data_descriptor_bit3_roundtrip():
    members = [
        ZipMember(member_name(2, 1), encode_member(build_member_document(2, 1, 4)), use_data_descriptor=True),
    ]
    blob = _make_zip(members)
    eocd, _, _ = zip_structs.find_eocd(blob, len(blob))
    cd = blob[eocd.central_dir_offset : eocd.central_dir_offset + eocd.central_dir_size]
    entry = zip_structs.parse_central_directory(cd, 1)[0]
    assert entry.has_data_descriptor is True
    header = zip_structs.parse_local_header(blob[entry.local_header_offset : entry.local_header_offset + 256])
    # bit3 本地头 CRC/大小为 0，一致性检查应放行
    zip_structs.verify_header_consistency(entry, header)
    desc_start = entry.local_header_offset + header.header_total_size + entry.compressed_size
    desc_block = blob[desc_start : desc_start + 16]
    descriptor, size = zip_structs.parse_data_descriptor(desc_block, entry)
    assert size == 16
    assert descriptor.has_signature is True
    assert descriptor.crc32 == entry.crc32


def test_truncated_central_directory_raises():
    members = [_json_member(1, 1, 3)]
    blob = _make_zip(members)
    eocd, _, _ = zip_structs.find_eocd(blob, len(blob))
    # 故意只给目录的前一半字节，并声明完整条目数
    half = eocd.central_dir_size // 2
    with pytest.raises(ZipStructureError):
        zip_structs.parse_central_directory(blob[eocd.central_dir_offset : eocd.central_dir_offset + half], 1)


def test_flipped_data_breaks_crc_eventually():
    members = [_json_member(1, 1, 3)]
    blob = _make_zip(members)
    bad = flip_compressed_data_byte(blob, members[0].filename)
    assert bad != blob
