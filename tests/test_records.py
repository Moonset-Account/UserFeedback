"""记录级校验单元测试：对象版本、完整性、实验 ID 一致性。"""

from __future__ import annotations

import json

import pytest

from sampler.experiment_data import EXPERIMENT_ID, build_member_document, encode_member, member_name
from selective_zip.errors import MemberRejected
from selective_zip.records import (
    expected_version_from_name,
    parse_and_validate_member,
)


def _payload(**overrides):
    doc = build_member_document(2, 1, 5)
    doc.update(overrides)
    return encode_member(doc)


def test_valid_member_accepted():
    payload = _payload()
    parsed = parse_and_validate_member(member_name(2, 1), payload, EXPERIMENT_ID)
    assert parsed.schema_version == 2
    assert len(parsed.observations) == 5


def test_filename_version_mismatch_rejected():
    # 文件名 v1，内容 v3
    payload = encode_member(build_member_document(3, 1, 5))
    with pytest.raises(MemberRejected, match="版本不一致"):
        parse_and_validate_member(member_name(1, 1), payload, EXPERIMENT_ID)


def test_disallowed_schema_version_rejected():
    payload = _payload(schema_version=9)
    with pytest.raises(MemberRejected, match="不在允许范围"):
        parse_and_validate_member("members/exp_v9_part01.json", payload, EXPERIMENT_ID)


def test_incomplete_status_rejected():
    payload = encode_member(build_member_document(2, 1, 5, status="partial"))
    with pytest.raises(MemberRejected, match="不完整"):
        parse_and_validate_member(member_name(2, 1), payload, EXPERIMENT_ID)


def test_declared_count_mismatch_rejected():
    payload = encode_member(build_member_document(2, 1, 5, declared_count=8))
    with pytest.raises(MemberRejected, match="声明 8 条"):
        parse_and_validate_member(member_name(2, 1), payload, EXPERIMENT_ID)


def test_wrong_experiment_id_rejected():
    payload = encode_member(build_member_document(2, 1, 3, experiment_id="other-exp"))
    with pytest.raises(MemberRejected, match="experiment_id"):
        parse_and_validate_member(member_name(2, 1), payload, EXPERIMENT_ID)


def test_invalid_json_rejected():
    with pytest.raises(MemberRejected, match="合法 JSON"):
        parse_and_validate_member(member_name(2, 1), b"{not json", EXPERIMENT_ID)


def test_bad_measurement_rejected():
    doc = build_member_document(2, 1, 2)
    doc["observations"][0]["measurement"] = "N/A"
    with pytest.raises(MemberRejected, match="measurement"):
        parse_and_validate_member(member_name(2, 1), encode_member(doc), EXPERIMENT_ID)


def test_missing_observation_id_rejected():
    doc = build_member_document(2, 1, 2)
    del doc["observations"][0]["observation_id"]
    with pytest.raises(MemberRejected, match="observation_id"):
        parse_and_validate_member(member_name(2, 1), encode_member(doc), EXPERIMENT_ID)


def test_version_name_extraction():
    assert expected_version_from_name("members/exp_v3_part02.json") == 3
    assert expected_version_from_name("plain.json") is None
