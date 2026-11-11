"""协议解析器测试：声明子集内合法、子集外拒绝。"""
import pytest

from core.parser import PROTOCOL_ID, ProtocolError, parse_obj, parse_text


def _base(**over):
    payload = {
        "protocol": PROTOCOL_ID,
        "scenario_id": "p",
        "title": "协议测试",
        "steps": [{"id": "a", "name": "甲"}, {"id": "b", "name": "乙"}],
        "constraints": {"depends_on": {"b": ["a"]}, "fixed": {"a": 0}},
    }
    payload.update(over)
    return payload


def test_parse_valid_and_group_expansion():
    payload = _base(
        steps=[{"id": "a", "name": "甲", "group": "g"},
               {"id": "b", "name": "乙", "group": "g"},
               {"id": "c", "name": "丙"}]
    )
    spec = parse_obj(payload)
    # 同组约束被展开为 b 依赖 a
    assert "a" in spec.depends_on["b"]


def test_unknown_top_level_field_rejected():
    with pytest.raises(ProtocolError):
        parse_obj(_base(extra_field=1))


def test_wrong_protocol_id_rejected():
    with pytest.raises(ProtocolError):
        parse_obj(_base(protocol="rpa-order/v2"))


def test_duplicate_step_id_rejected():
    payload = _base(steps=[{"id": "a", "name": "甲"}, {"id": "a", "name": "重"}])
    with pytest.raises(ProtocolError, match="唯一"):
        parse_obj(payload)


def test_fixed_reference_unknown_step_rejected():
    payload = _base(constraints={"fixed": {"zzz": 0}})
    with pytest.raises(ProtocolError, match="不存在"):
        parse_obj(payload)


def test_fixed_slot_out_of_range_rejected():
    payload = _base(constraints={"fixed": {"a": 9}})
    with pytest.raises(ProtocolError, match="超出范围"):
        parse_obj(payload)


def test_dependency_unknown_step_rejected():
    payload = _base(constraints={"depends_on": {"b": ["nope"]}})
    with pytest.raises(ProtocolError, match="不存在"):
        parse_obj(payload)


def test_bad_json_text_rejected():
    with pytest.raises(ProtocolError, match="JSON"):
        parse_text("{not json")


def test_unsupported_encoding_features_absent():
    # 协议子集不支持软约束/权重等字段 -> additionalProperties:false 拒绝
    payload = _base(constraints={"fixed": {"a": 0}, "soft_constraints": []})
    with pytest.raises(ProtocolError):
        parse_obj(payload)
