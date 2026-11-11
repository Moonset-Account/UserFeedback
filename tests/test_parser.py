# -*- coding: utf-8 -*-
"""数值字面量解析器独立断言：四种以上格式、空白、科学计数法、地区歧义。"""
import pytest

from collector.locales import get_rule, NBSP, NNBSP
from collector.parser import parse_number, ParseError, ERR_AMBIGUOUS, ERR_BAD_GROUP


def test_en_us_dot_decimal_and_comma_grouping():
    r = parse_number("1,234.56", get_rule("en_US"))
    assert format(r.value, "f") == "1234.56"
    assert r.grouping_removed is True
    assert r.precision if hasattr(r, "precision") else True


def test_zh_cn_same_as_en_us_numeric_grammar():
    r = parse_number("9,876.5", get_rule("zh_CN"))
    assert format(r.value, "f") == "9876.5"


def test_de_de_comma_decimal_dot_grouping():
    r = parse_number("1.234,56", get_rule("de_DE"))
    assert format(r.value, "f") == "1234.56"
    r2 = parse_number("0,0078", get_rule("de_DE"))
    assert format(r2.value, "f") == "0.0078"


def test_fr_fr_non_breaking_space_grouping():
    r = parse_number(f"1{NBSP}234,56", get_rule("fr_FR"))
    assert format(r.value, "f") == "1234.56"


def test_sv_se_narrow_non_breaking_space_grouping():
    r = parse_number(f"12{NNBSP}345,67", get_rule("sv_SE"))
    assert format(r.value, "f") == "12345.67"


@pytest.mark.parametrize("text,locale,expected", [
    ("7.8E-3", "en_US", "0.0078"),
    ("7.8e-3", "en_US", "0.0078"),
    ("1.2E+4", "en_US", "12000"),
    ("7,8e-3", "fr_FR", "0.0078"),
])
def test_scientific_notation(text, locale, expected):
    r = parse_number(text, get_rule(locale))
    assert r.scientific is True
    assert format(r.value, "f") == expected


def test_unicode_minus_normalized():
    r = parse_number("−3,5", get_rule("de_DE"))
    assert format(r.value, "f") == "-3.5"


def test_ambiguous_comma_under_undetermined_locale_rejected():
    with pytest.raises(ParseError) as exc:
        parse_number("1,234", get_rule("und"))
    assert exc.value.code == ERR_AMBIGUOUS
    assert any("显式选择地区" in s for s in exc.value.steps)


def test_same_literal_different_locales_different_values():
    us = parse_number("1,234", get_rule("en_US"))
    de = parse_number("1,234", get_rule("de_DE"))
    assert format(us.value, "f") == "1234"
    assert format(de.value, "f") == "1.234"


def test_bad_grouping_detected():
    with pytest.raises(ParseError) as exc:
        parse_number("12,34.5", get_rule("en_US"))
    assert exc.value.code == ERR_BAD_GROUP


def test_grouping_separator_not_allowed_after_decimal():
    with pytest.raises(ParseError):
        parse_number("1.23,4", get_rule("en_US"))


def test_unsupported_character_rejected():
    with pytest.raises(ParseError):
        parse_number("12.a4", get_rule("en_US"))


def test_malformed_exponent_rejected():
    with pytest.raises(ParseError):
        parse_number("1.2e", get_rule("en_US"))
    with pytest.raises(ParseError):
        parse_number("1.2ex", get_rule("en_US"))


def test_raw_text_preserved_and_steps_emitted():
    raw = f"1{NBSP}234,56"
    r = parse_number(raw, get_rule("fr_FR"))
    assert r.raw_text == raw
    assert len(r.steps) >= 3
    assert any("分组" in s for s in r.steps)


def test_decimal_places_and_significant_digits():
    from collector.parser import precision_info
    assert precision_info("1234.56") == {"decimal_places": 2, "significant_digits": 6}
    assert precision_info("0.0078") == {"decimal_places": 4, "significant_digits": 2}
    # 不臆断整数末尾 0 为占位符：12000 按 5 位有效数字记录
    assert precision_info("12000") == {"decimal_places": 0, "significant_digits": 5}
