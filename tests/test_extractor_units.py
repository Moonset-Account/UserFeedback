# -*- coding: utf-8 -*-
"""单位白名单与测量记录语义独立断言。"""
import pytest

from collector.extractor import parse_measurement
from collector.locales import get_rule, NBSP


def test_illegal_unit_keeps_value_but_errors():
    m = parse_measurement("25.3 zz", get_rule("en_US"))
    assert m.status == "error"
    assert format(m.value, "f") == "25.3"   # 数值仍保留
    assert m.error["code"] == "ILLEGAL_UNIT"
    assert m.unit is None


def test_another_illegal_unit():
    m = parse_measurement("3.7 parsec", get_rule("en_US"))
    assert m.status == "error"
    assert m.error["code"] == "ILLEGAL_UNIT"


def test_whitelisted_unit_aliases_canonicalized():
    m = parse_measurement("42 ug/L", get_rule("en_US"))
    assert m.status == "ok"
    assert m.unit == "μg/L"
    m2 = parse_measurement("36.6 °C", get_rule("en_US"))
    assert m2.unit == "°C"


def test_nbsp_between_number_and_unit():
    m = parse_measurement(f"42{NBSP}mg/L", get_rule("fr_FR"))
    assert m.status == "ok"
    assert format(m.value, "f") == "42"
    assert m.unit == "mg/L"


def test_missing_values_never_become_zero():
    for token in ["-", "—", "N/A", "n/a", "无数据", ""]:
        m = parse_measurement(token, get_rule("en_US"))
        assert m.status == "missing"
        assert m.value is None
        assert m.qualifier == "missing"


def test_below_lod_is_bound_not_equality():
    m = parse_measurement("< 0.01 mg/L", get_rule("en_US"))
    assert m.status == "below_LOD"
    assert m.qualifier == "below_detection_limit"
    assert format(m.value, "f") == "0.01"
    assert any("不是精确等值" in s or "禁止当作精确" in s for s in m.steps)


def test_above_lod():
    m = parse_measurement("> 50.0 ppb", get_rule("en_US"))
    assert m.status == "above_LOD"
    assert m.qualifier == "above_detection_limit"


def test_range_kept_as_interval():
    m = parse_measurement("6.8 – 7.2 pH", get_rule("en_US"))
    assert m.status == "range"
    assert format(m.value, "f") == "6.8"
    assert format(m.value_end, "f") == "7.2"
    m2 = parse_measurement("0.10 ~ 0.15 mg/L", get_rule("en_US"))
    assert m2.status == "range"


def test_inverted_range_errors():
    m = parse_measurement("9.0 – 1.0 mg/L", get_rule("en_US"))
    assert m.status == "error"
    assert m.error["code"] == "BAD_RANGE"


def test_symmetric_uncertainty():
    m = parse_measurement("18.6 ± 0.4 mg/L", get_rule("en_US"))
    assert m.uncertainty.kind == "symmetric"
    assert m.uncertainty.plus == "0.4" and m.uncertainty.minus == "0.4"


def test_parenthesis_uncertainty_alignment():
    m = parse_measurement("0.042(7) mg/L", get_rule("en_US"))
    assert m.uncertainty.kind == "parenthesis"
    assert m.uncertainty.plus == "0.007"
    m2 = parse_measurement("1.234(5) mg/L", get_rule("en_US"))
    assert m2.uncertainty.plus == "0.005"


def test_asymmetric_uncertainty():
    m = parse_measurement("9.81 +0.12 -0.08 mg/L", get_rule("en_US"))
    assert m.uncertainty.kind == "asymmetric"
    assert m.uncertainty.plus == "0.12"
    assert m.uncertainty.minus == "0.08"


def test_precision_recorded():
    m = parse_measurement("1,234.56 mg/L", get_rule("en_US"))
    assert m.precision == {"decimal_places": 2, "significant_digits": 6}


def test_steps_always_present_for_ok_records():
    m = parse_measurement("12.5 mg/L", get_rule("en_US"))
    assert m.steps and any("规范化数字" in s for s in m.steps)


def test_german_uncertainty_uses_german_decimal():
    m = parse_measurement("12,5 ± 0,2 mg/L", get_rule("de_DE"))
    assert format(m.value, "f") == "12.5"
    assert m.uncertainty.plus == "0.2"
