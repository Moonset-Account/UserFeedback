"""端到端验收测试：采集器只经 HTTP 公开接口工作，真值仅用于断言。

覆盖验收要求：
1. 只取两个成员时不下载整个大包（流量断言）；
2. 目录与本地头冲突被拒绝；
3. CRC 错误被拒绝；
4. 采集中源包变化被检测并中止；
并补充 ZIP64、bit3、stored、加密/分卷拒绝、记录级拒绝等场景。
"""

from __future__ import annotations

import json

import pytest

from sampler.experiment_data import EXPERIMENT_ID, member_name
from selective_zip.fetcher import run_selective_fetch


def _run(base_url, package, wanted):
    url = f"{base_url}/packages/{package}.zip"
    return run_selective_fetch(url, wanted, EXPERIMENT_ID)


# ----------------------------------------------------------------------
# 1) 正常路径 + "不下载整包"
# ----------------------------------------------------------------------
def test_normal_small_two_members_success(base_url, truth):
    wanted = [member_name(1, 1), member_name(3, 3)]
    report = _run(base_url, "normal_small", wanted)
    assert report["status"] == "success"
    assert report["error"] is None
    assert len(report["records"]) == 2
    assert report["rejected_members"] == []
    # 每个成员的 CRC 都与独立真值一致
    for rec in report["records"]:
        tv = truth["packages"]["normal_small"]["members"][rec["member_name"]]
        evidence = next(e for e in report["member_evidence"] if e["member_name"] == rec["member_name"])
        assert evidence["crc32_actual"] == evidence["crc32_declared"] == tv["crc32"]
        assert evidence["uncompressed_size"] == tv["uncompressed_size"]
        assert rec["accepted_observation_count"] == tv["observation_count"]
    # 所有 Range 请求都必须是 206，且没有任何一次请求覆盖整个文件
    total = report["remote"]["total_size"]
    for req in report["range_requests"]:
        assert req["response_status"] == 206
        assert req["content_range"] and req["content_range"].startswith("bytes ")
        assert req["etag"], "每次 Range 响应都应带回 ETag 指纹"
        assert not (req["start"] == 0 and req["end_inclusive"] == total - 1), "不得整包请求"


def test_big_package_two_members_does_not_download_whole_archive(base_url, truth):
    # 大包 20 个成员，只取前两个
    wanted = [member_name(2, 1), member_name(3, 2)]
    report = _run(base_url, "normal_big", wanted)
    assert report["status"] == "success"
    assert len(report["records"]) == 2
    traffic = report["traffic"]
    # 核心验收：实际下载字节远小于整包（留出宽松但有意义的阈值）
    assert traffic["downloaded_bytes"] < traffic["remote_total_bytes"] * 0.25
    assert traffic["saved_bytes"] > traffic["remote_total_bytes"] * 0.75
    # 同一字节不会被重复下载
    assert traffic["downloaded_bytes"] == traffic["unique_bytes_touched"]
    # 与真值核对内容
    for rec in report["records"]:
        tv = truth["packages"]["normal_big"]["members"][rec["member_name"]]
        assert rec["accepted_observation_count"] == tv["observation_count"]


def test_stored_package(base_url):
    report = _run(base_url, "normal_stored", [member_name(2, 1), member_name(3, 2)])
    assert report["status"] == "success"
    assert all("stored" in e["compression"] for e in report["member_evidence"])


def test_zip64_package(base_url):
    report = _run(base_url, "normal_zip64", [member_name(3, 1), member_name(1, 2)])
    assert report["status"] == "success"
    assert report["remote"]["is_zip64"] is True
    assert len(report["records"]) == 2


def test_bit3_data_descriptor_package(base_url):
    report = _run(base_url, "bit3_descriptor", [member_name(2, 1), member_name(3, 2)])
    assert report["status"] == "success"
    assert len(report["records"]) == 2


# ----------------------------------------------------------------------
# 2) 目录与本地头冲突
# ----------------------------------------------------------------------
def test_header_conflict_fails_with_no_records(base_url):
    wanted = [member_name(1, 1), member_name(3, 3)]
    report = _run(base_url, "header_conflict", wanted)
    assert report["status"] == "failed"
    assert report["error"]["code"] == "header_mismatch"
    assert report["records"] == []
    assert "冲突" in report["error"]["message"]


# ----------------------------------------------------------------------
# 3) CRC 错误
# ----------------------------------------------------------------------
def test_crc_error_fails_with_no_records(base_url):
    wanted = [member_name(1, 1), member_name(3, 3)]
    report = _run(base_url, "crc_bad", wanted)
    assert report["status"] == "failed"
    assert report["error"]["code"] == "crc_mismatch"
    assert report["records"] == []


# ----------------------------------------------------------------------
# 4) 采集中源包变化
# ----------------------------------------------------------------------
def test_source_change_during_collection_aborts(base_url):
    url = f"{base_url}/packages/mutation.zip"
    wanted = [member_name(2, 1), member_name(3, 2)]
    report = run_selective_fetch(url, wanted, EXPERIMENT_ID)
    assert report["status"] == "failed"
    assert report["error"]["code"] == "source_changed"
    assert report["records"] == []
    # 失败时仍保留已发生的 Range 请求证据
    assert len(report["range_requests"]) >= 1


# ----------------------------------------------------------------------
# 不支持的子集：加密 / 分卷
# ----------------------------------------------------------------------
def test_encrypted_archive_rejected(base_url):
    report = _run(base_url, "encrypted", [member_name(1, 1)])
    assert report["status"] == "failed"
    assert report["error"]["code"] == "encrypted_archive"
    assert report["records"] == []


def test_multivolume_archive_rejected(base_url):
    report = _run(base_url, "multivolume", [member_name(1, 1)])
    assert report["status"] == "failed"
    assert report["error"]["code"] == "multi_volume_archive"
    assert report["records"] == []


# ----------------------------------------------------------------------
# 记录级拒绝：不完整/跨版本成员不得进入正式记录
# ----------------------------------------------------------------------
def test_mixed_versions_good_kept_bad_rejected(base_url):
    wanted = [
        member_name(2, 1),  # 正常
        member_name(1, 3),  # 文件名 v1 / 内容 v3 → 跨版本
        member_name(2, 4),  # partial → 不完整
        member_name(3, 5),  # 计数不符
        member_name(1, 6),  # 实验 ID 不符
    ]
    report = _run(base_url, "mixed_versions", wanted)
    assert report["status"] == "completed_with_rejections"
    accepted_names = {r["member_name"] for r in report["records"]}
    rejected_names = {r["member_name"] for r in report["rejected_members"]}
    assert accepted_names == {member_name(2, 1)}
    assert rejected_names == set(wanted[1:])
    # 任何被拒绝成员都不在正式记录里
    assert accepted_names.isdisjoint(rejected_names)


def test_member_not_found(base_url):
    from selective_zip.errors import MemberNotFound

    url = f"{base_url}/packages/normal_small.zip"
    report = run_selective_fetch(url, ["members/does_not_exist.json"], EXPERIMENT_ID)
    assert report["status"] == "failed"
    assert report["error"]["code"] == "member_not_found"


def test_range_evidence_is_complete_and_ordered(base_url):
    wanted = [member_name(1, 1), member_name(3, 3)]
    report = _run(base_url, "normal_small", wanted)
    requests = report["range_requests"]
    assert requests
    # 序号连续、区间非空、状态全部 206
    for i, req in enumerate(requests, start=1):
        assert req["index"] == i
        assert req["end_inclusive"] >= req["start"]
        assert req["bytes_returned"] == req["requested_bytes"]
        assert req["response_status"] == 206
