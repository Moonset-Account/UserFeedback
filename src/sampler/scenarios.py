"""验收场景生成：在一个输出目录里产出全部虚构样例 ZIP 与真值清单。

场景列表
--------
- ``normal_small``      小型常规包（演示正常路径，选两个成员）
- ``normal_big``        大包（证明只取两个成员时不下载整包）
- ``normal_stored``     全 stored 压缩
- ``normal_zip64``      强制 ZIP64 结构
- ``bit3_descriptor``   使用 bit3 数据描述符
- ``crc_bad``           压缩数据被篡改 → CRC 错误
- ``header_conflict``   本地头与中央目录冲突
- ``mixed_versions``    含跨版本/不完整成员（记录级拒绝）
- ``encrypted``         加密标志包（整包拒绝）
- ``multivolume``       分卷标志包（整包拒绝）
- ``mutation_a/b``      同名资源在请求间切换 → 源包变化检测

真值文件 ``truth.json`` 仅由测试使用，采集器不会读取它。
"""

from __future__ import annotations

import dataclasses
import json
import os
import struct
from dataclasses import dataclass
from typing import Any

from selective_zip.protocol import (
    COMPRESSION_STORED,
    FLAG_ENCRYPTED,
    SIG_END_OF_CENTRAL_DIR,
    SIZE_EOCD_FIXED,
)

from .experiment_data import (
    EXPERIMENT_ID,
    build_member_document,
    encode_member,
    member_name,
)
from .patcher import (
    corrupt_local_header_crc,
    flip_compressed_data_byte,
    rewrite_local_compression,
)
from .zip_writer import ZipMember, ZipWriter

SCENARIO_PACKAGES = [
    "normal_small",
    "normal_big",
    "normal_stored",
    "normal_zip64",
    "bit3_descriptor",
    "crc_bad",
    "header_conflict",
    "mixed_versions",
    "encrypted",
    "multivolume",
]
# /packages/mutation.zip 在两次请求间轮换
MUTATION_VARIANTS = ["mutation_a", "mutation_b"]


@dataclass
class ScenarioSpec:
    key: str
    filename: str
    description: str
    wanted_members: list[str]
    expect: str  # success / failed / rejected
    expect_error_code: str | None = None
    demo: bool = False


def _member(version: int, part: int, count: int, *, compression: int | None = None,
            status="complete", declared_count=None, experiment_id=EXPERIMENT_ID) -> ZipMember:
    doc = build_member_document(
        version, part, count, status=status,
        declared_count=declared_count, experiment_id=experiment_id,
    )
    chosen = 8 if compression is None else compression
    return ZipMember(member_name(version, part), encode_member(doc), compression=chosen)


def _build_normal(parts: list[tuple[int, int]]) -> list[ZipMember]:
    return [_member(version, part, count) for part, (version, count) in enumerate(parts, start=1)]


def _write(path: str, blob: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(blob)


def _mark_encrypted(blob: bytes) -> bytes:
    """把所有本地头与中央目录条目的通用位标志 bit0 置位（仅用于拒绝测试）。"""
    out = bytearray(blob)
    for signature, fixed_size, flag_offset in (
        (b"PK\x03\x04", 30, 6),
        (b"PK\x01\x02", 46, 8),
    ):
        pos = 0
        while True:
            idx = out.find(signature, pos)
            if idx < 0:
                break
            flags = struct.unpack_from("<H", out, idx + flag_offset)[0]
            struct.pack_into("<H", out, idx + flag_offset, flags | FLAG_ENCRYPTED)
            pos = idx + 4
    return bytes(out)


def _mark_multivolume(blob: bytes) -> bytes:
    """把 EOCD 的盘号字段改为分卷语义（仅用于拒绝测试）。"""
    out = bytearray(blob)
    eocd = out.rfind(SIG_END_OF_CENTRAL_DIR)
    if eocd < 0 or eocd + SIZE_EOCD_FIXED > len(out):
        raise RuntimeError("找不到 EOCD，无法制造分卷包")
    struct.pack_into("<H", out, eocd + 4, 0)   # 当前盘号 0
    struct.pack_into("<H", out, eocd + 6, 1)   # 中央目录所在盘 1
    return bytes(out)


def build_all(output_dir: str) -> dict[str, Any]:
    """生成全部样例，返回写盘的场景清单（dict）。"""
    pkg_dir = os.path.join(output_dir, "packages")
    os.makedirs(pkg_dir, exist_ok=True)
    truth: dict[str, Any] = {"experiment_id": EXPERIMENT_ID, "packages": {}}

    # 1) normal_small：6 个成员，演示只取其中两个
    small_members = [
        _member(1, 1, 4), _member(2, 2, 5), _member(3, 3, 6),
        _member(2, 4, 3), _member(3, 5, 7), _member(1, 6, 2),
    ]
    small_blob = ZipWriter(small_members).render()
    _write(os.path.join(pkg_dir, "normal_small.zip"), small_blob)
    truth["packages"]["normal_small"] = _truth_for(small_members)

    # 2) normal_big：20 个成员、每成员约 150~250 条高熵观测，整包约 1.5MB 以上
    big_members = []
    for part in range(1, 21):
        version = (part % 3) + 1
        big_members.append(_member(version, part, 150 + part * 5))
    big_blob = ZipWriter(big_members).render()
    _write(os.path.join(pkg_dir, "normal_big.zip"), big_blob)
    truth["packages"]["normal_big"] = _truth_for(big_members)

    # 3) normal_stored
    stored_members = [_member(2, 1, 5, compression=COMPRESSION_STORED),
                      _member(3, 2, 6, compression=COMPRESSION_STORED)]
    _write(os.path.join(pkg_dir, "normal_stored.zip"), ZipWriter(stored_members).render())
    truth["packages"]["normal_stored"] = _truth_for(stored_members)

    # 4) normal_zip64（强制 ZIP64）
    z64_members = [_member(3, 1, 5), _member(1, 2, 4)]
    _write(os.path.join(pkg_dir, "normal_zip64.zip"),
           ZipWriter(z64_members, force_zip64=True).render())
    truth["packages"]["normal_zip64"] = _truth_for(z64_members)

    # 5) bit3_descriptor
    bit3_members = [
        ZipMember(member_name(2, 1), encode_member(build_member_document(2, 1, 5).copy()),
                  use_data_descriptor=True),
        ZipMember(member_name(3, 2), encode_member(build_member_document(3, 2, 6).copy()),
                  use_data_descriptor=True),
    ]
    bit3_blob = ZipWriter(bit3_members).render()
    _write(os.path.join(pkg_dir, "bit3_descriptor.zip"), bit3_blob)
    truth["packages"]["bit3_descriptor"] = _truth_for(bit3_members)

    # 6) crc_bad：在 normal_small 基础上翻转第一个成员的压缩数据
    crc_bad = flip_compressed_data_byte(small_blob, small_members[0].filename)
    _write(os.path.join(pkg_dir, "crc_bad.zip"), crc_bad)
    truth["packages"]["crc_bad"] = {"base": "normal_small",
                                    "tampered_member": small_members[0].filename}

    # 7) header_conflict：本地头 CRC 与中央目录冲突（再加一个压缩方式冲突成员）
    conflict = corrupt_local_header_crc(small_blob, small_members[1].filename)
    conflict = rewrite_local_compression(conflict, small_members[2].filename, 12)
    _write(os.path.join(pkg_dir, "header_conflict.zip"), conflict)
    truth["packages"]["header_conflict"] = {
        "base": "normal_small",
        "crc_conflict_member": small_members[1].filename,
        "compression_conflict_member": small_members[2].filename,
    }

    # 8) mixed_versions：正常成员 + 跨版本成员 + 不完整成员 + 错误 experiment_id 成员
    good_a = _member(2, 1, 5)
    good_b = _member(3, 2, 6)
    cross_version = ZipMember(
        member_name(1, 3),
        encode_member(build_member_document(3, 3, 4)),  # 文件名 v1，内容 v3
    )
    incomplete_doc = build_member_document(2, 4, 6, status="partial")
    incomplete = ZipMember(member_name(2, 4), encode_member(incomplete_doc))
    count_mismatch_doc = build_member_document(3, 5, 5, declared_count=9)
    count_mismatch = ZipMember(member_name(3, 5), encode_member(count_mismatch_doc))
    wrong_exp_doc = build_member_document(1, 6, 3, experiment_id="some-other-experiment")
    wrong_exp = ZipMember(member_name(1, 6), encode_member(wrong_exp_doc))
    mixed_members = [good_a, good_b, cross_version, incomplete, count_mismatch, wrong_exp]
    _write(os.path.join(pkg_dir, "mixed_versions.zip"), ZipWriter(mixed_members).render())
    truth["packages"]["mixed_versions"] = {
        "accepted": [good_a.filename, good_b.filename],
        "rejected": {
            cross_version.filename: "文件名版本与 schema_version 不一致",
            incomplete.filename: "status=partial 不完整",
            count_mismatch.filename: "declared_count 与实际观测数不符",
            wrong_exp.filename: "experiment_id 不匹配",
        },
    }

    # 9) encrypted：加密标志
    _write(os.path.join(pkg_dir, "encrypted.zip"), _mark_encrypted(small_blob))
    truth["packages"]["encrypted"] = {"base": "normal_small", "flag": "encrypted"}

    # 10) multivolume：分卷标志
    _write(os.path.join(pkg_dir, "multivolume.zip"), _mark_multivolume(small_blob))
    truth["packages"]["multivolume"] = {"base": "normal_small", "flag": "multivolume"}

    # 11) mutation_a / mutation_b：同结构、不同内容（观测条数不同 → CRC/大小全变）
    mut_a_members = [_member(2, 1, 5), _member(3, 2, 6)]
    mut_b_members = [_member(2, 1, 8), _member(3, 2, 9)]
    _write(os.path.join(pkg_dir, "mutation_a.zip"), ZipWriter(mut_a_members).render())
    _write(os.path.join(pkg_dir, "mutation_b.zip"), ZipWriter(mut_b_members).render())
    truth["packages"]["mutation_a"] = _truth_for(mut_a_members)
    truth["packages"]["mutation_b"] = _truth_for(mut_b_members)

    with open(os.path.join(output_dir, "truth.json"), "w", encoding="utf-8") as fh:
        json.dump(truth, fh, ensure_ascii=False, indent=2)

    manifest = build_manifest(output_dir)
    return manifest


def _truth_for(members: list[ZipMember]) -> dict[str, Any]:
    result = {}
    for member in members:
        doc = json.loads(member.data.decode("utf-8"))
        result[member.filename] = {
            "crc32": zlib_crc32(member.data),
            "uncompressed_size": len(member.data),
            "compressed_size": len(_deflated(member)),
            "schema_version": doc["schema_version"],
            "observation_count": len(doc["observations"]),
            "status": doc["status"],
        }
    return {"members": result}


def _deflated(member: ZipMember) -> bytes:
    import zlib

    if member.compression == COMPRESSION_STORED:
        return member.data
    co = zlib.compressobj(6, zlib.DEFLATED, -15)
    return co.compress(member.data) + co.flush()


def zlib_crc32(data: bytes) -> int:
    import zlib

    return zlib.crc32(data) & 0xFFFFFFFF


def build_manifest(output_dir: str) -> dict[str, Any]:
    """生成源站公开清单 index.json（采集器可通过公开 HTTP 接口读取）。"""
    pkg_dir = os.path.join(output_dir, "packages")
    packages = []
    for name in sorted(os.listdir(pkg_dir)):
        if name.endswith(".zip"):
            packages.append(
                {
                    "name": name,
                    "size": os.path.getsize(os.path.join(pkg_dir, name)),
                    "url_path": f"/packages/{name}",
                }
            )
    wanted_small = [member_name(1, 1), member_name(3, 3)]
    wanted_big = [member_name((1 % 3) + 1, 1), member_name((2 % 3) + 1, 2)]
    scenarios = [
        ScenarioSpec("normal_small", "normal_small.zip",
                     "常规小包：6 个成员中只采集 2 个，正常入册",
                     wanted_small, "success", demo=True),
        ScenarioSpec("crc_bad", "crc_bad.zip",
                     "边界：成员压缩数据被篡改，CRC-32 校验失败，任务失败且无正式记录",
                     wanted_small, "failed", "crc_mismatch", demo=True),
        ScenarioSpec("normal_big", "normal_big.zip",
                     "大包选择性采集：20 个成员只取 2 个，实际下载远小于整包",
                     wanted_big, "success"),
        ScenarioSpec("normal_stored", "normal_stored.zip",
                     "全 stored（不压缩）成员的正常采集",
                     [member_name(2, 1), member_name(3, 2)], "success"),
        ScenarioSpec("normal_zip64", "normal_zip64.zip",
                     "强制 ZIP64 结构：经 ZIP64 Locator/Record 定位中央目录",
                     [member_name(3, 1), member_name(1, 2)], "success"),
        ScenarioSpec("bit3_descriptor", "bit3_descriptor.zip",
                     "bit3 数据描述符：本地头 CRC/大小为 0，靠描述符核对",
                     [member_name(2, 1), member_name(3, 2)], "success"),
        ScenarioSpec("header_conflict", "header_conflict.zip",
                     "边界：本地头与中央目录冲突（CRC 与压缩方式），任务失败",
                     wanted_small, "failed", "header_mismatch"),
        ScenarioSpec("mixed_versions", "mixed_versions.zip",
                     "记录级边界：跨版本/不完整/计数不符/实验不符成员被拒绝，好成员正常入册",
                     [member_name(2, 1), member_name(1, 3), member_name(2, 4),
                      member_name(3, 5), member_name(1, 6)],
                     "rejected"),
        ScenarioSpec("encrypted", "encrypted.zip",
                     "边界：加密包在子集检查阶段被整包拒绝",
                     wanted_small, "failed", "encrypted_archive"),
        ScenarioSpec("multivolume", "multivolume.zip",
                     "边界：分卷包在 EOCD 检查阶段被整包拒绝",
                     wanted_small, "failed", "multi_volume_archive"),
        ScenarioSpec("mutation", "mutation.zip",
                     "边界：采集过程中源包切换，ETag 变化触发中止，无正式记录",
                     [member_name(2, 1), member_name(3, 2)],
                     "failed", "source_changed"),
    ]
    manifest = {
        "experiment_id": EXPERIMENT_ID,
        "packages": packages,
        "scenarios": [dataclasses.asdict(s) for s in scenarios],
        "mutation_endpoint": "/packages/mutation.zip",
        "mutation_variants": MUTATION_VARIANTS,
    }
    with open(os.path.join(output_dir, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return manifest
