"""集成测试：经本地源站 HTTP 公开接口与样例文件两条入口采集，对照独立预期真值。"""
from __future__ import annotations

import pytest

from labsoap import sources
from labsoap.collector import collect

from tests.expected import expected_values as expected


def _run_http(instrument_server, scenario: str) -> dict:
    return collect({"kind": "http", "ref": f"{instrument_server}/soap?scenario={scenario}"})


def _run_file(samples_dir, scenario: str) -> dict:
    sources.ALLOWED_FILE_ROOT = samples_dir
    return collect({"kind": "file", "ref": f"{scenario}.mime"})


@pytest.mark.parametrize("entry", ["http", "file"])
def test_all_scenarios_status_matches_independent_truth(instrument_server, samples_dir, entry):
    for scenario, want_status in expected.EXPECTED_STATUS.items():
        result = _run_http(instrument_server, scenario) if entry == "http" else _run_file(samples_dir, scenario)
        assert result["status"] == want_status, f"{entry}/{scenario} 状态不符：{result['status']} != {want_status}"


def test_complete_values_and_provenance(instrument_server):
    result = _run_http(instrument_server, "complete")
    assert result["status"] == "complete"
    assert len(result["measurement_groups"]) == 2
    assert result["raw_evidence"]["total_bytes"] > 0

    got = {g["measurement_id"]: g for g in result["measurement_groups"]}
    for want_group in expected.EXPECTED_COMPLETE["groups"]:
        group = got[want_group["measurement_id"]]
        assert group["instrument_id"] == expected.EXPECTED_COMPLETE["instrument_id"]
        got_channels = {c["code"]: c for c in group["channels"]}
        for code, want_ch in want_group["channels"].items():
            ch = got_channels[code]
            assert ch["unit"] == want_ch["unit"]
            assert ch["sample"]["values"] == want_ch["values"]  # 数值与独立字面量完全一致
            assert ch["summary_check"]["match"] is True
            # 数值来源证据：XML 字段 + 附件字节双来源齐备
            assert ch["value_provenance"]["code"]["source"] == "xml_field"
            att = ch["value_provenance"]["attachment"]
            assert att["byte_length"] > 0 and len(att["sha256"]) == 64
            assert att["content_type"] == "application/x-fictional-lab-sample"
            assert ch["sample"]["embedded_measurement_id"] == want_group["measurement_id"]


def test_reordered_parts_associate_by_cid_not_order(instrument_server):
    # 附件先于 XML 根 part 到达：数值仍按 Content-ID 正确关联
    result = _run_http(instrument_server, "parts_reordered")
    assert result["status"] == "complete"
    group = result["measurement_groups"][0]
    channels = {c["code"]: c for c in group["channels"]}
    for code, want in expected.EXPECTED_REORDERED["groups"][0]["channels"].items():
        assert channels[code]["sample"]["values"] == want["values"]
        assert channels[code]["summary_check"]["match"] is True
    # 原始 part 顺序里根 part 确实在最后，证明关联没有依赖出现顺序
    indexes = [p["part_index"] for p in result["raw_evidence"]["parts"]]
    root_evidence = next(p for p in result["raw_evidence"]["parts"] if "xml" in p["content_type"])
    assert root_evidence["part_index"] == max(indexes)


def test_fault_is_structured_not_crash(instrument_server):
    result = _run_http(instrument_server, "fault")
    assert result["status"] == "fault"
    assert result["error"]["code"] == expected.EXPECTED_ERROR_CODE["fault"]
    assert result["fault"]["code"] == expected.EXPECTED_FAULT["code"]
    assert result["fault"]["detail"]["ErrorKind"] == expected.EXPECTED_FAULT["error_kind"]
    assert result["fault"]["detail"]["InstrumentId"] == expected.EXPECTED_FAULT["instrument_id"]
    assert result["fault"]["soap_version"] == "1.1"
    assert "校准证书已过期" in result["fault"]["reason"]
    assert result["measurement_groups"] == []  # Fault 不产生正式结果


def test_missing_reference_names_exact_cid(instrument_server):
    result = _run_http(instrument_server, "missing_reference")
    assert result["status"] == "rejected"
    assert result["error"]["code"] == expected.EXPECTED_ERROR_CODE["missing_reference"]
    refs = result["error"]["evidence"]["missing_references"]
    assert any(r["missing_cid"] == expected.EXPECTED_MISSING_CID for r in refs)
    assert result["measurement_groups"] == []  # 有缺失引用时整批不进正式结果


def test_duplicate_cid_conflict_blocks_association(instrument_server):
    result = _run_http(instrument_server, "duplicate_cid")
    assert result["status"] == "rejected"
    assert result["error"]["code"] == expected.EXPECTED_ERROR_CODE["duplicate_cid"]
    dups = result["error"]["evidence"]["duplicates"]
    assert expected.EXPECTED_DUPLICATE_CID in dups
    assert len(dups[expected.EXPECTED_DUPLICATE_CID]) == 2


def test_digest_mismatch_rejects_group_but_keeps_evidence(instrument_server):
    result = _run_http(instrument_server, "digest_mismatch")
    assert result["status"] == "rejected"
    assert result["error"]["code"] == expected.EXPECTED_ERROR_CODE["digest_mismatch"]
    rejected = result["error"]["evidence"]["rejected_groups"]
    flat = [(g["measurement_id"], e) for g in rejected for e in g["errors"]]
    hit = next((e for mid, e in flat
                if mid == expected.EXPECTED_MISMATCH_MEASUREMENT
                and e["channel_code"] == expected.EXPECTED_MISMATCH_CHANNEL), None)
    assert hit is not None and hit["code"] == "SUMMARY_DIGEST_MISMATCH"
    # 原始响应证据仍可展开核对
    assert result["raw_evidence"]["raw_sha256"] and len(result["raw_evidence"]["parts"]) == 3


def test_only_complete_groups_enter_formal_results(instrument_server):
    # 验收口径：正式结果里只允许出现 status=complete 的测量组
    for scenario in expected.EXPECTED_STATUS:
        result = _run_http(instrument_server, scenario)
        if result["status"] != "complete":
            assert result["measurement_groups"] == []
        else:
            assert result["measurement_groups"]
            for g in result["measurement_groups"]:
                assert g["accepted"] is True
                assert all(c["summary_check"]["match"] for c in g["channels"])


def test_file_source_path_traversal_blocked(samples_dir):
    sources.ALLOWED_FILE_ROOT = samples_dir
    result = collect({"kind": "file", "ref": "../../../etc/passwd"})
    assert result["status"] in ("rejected", "fetch_error")
    assert result["error"]["code"] in ("UNKNOWN_SOURCE", "HTTP_FETCH_ERROR")


def test_http_failure_is_structured():
    # 指向一个关闭的端口，验证获取失败被结构化而非抛裸异常
    result = collect({"kind": "http", "ref": "http://127.0.0.1:1/soap?scenario=complete"})
    assert result["status"] == "fetch_error"
    assert result["error"]["code"] == "HTTP_FETCH_ERROR"
