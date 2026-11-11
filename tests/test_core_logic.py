"""纯逻辑单元测试：协议解析与五种语义值的区分。不启动浏览器。"""
from __future__ import annotations

import pytest

from rpa_grid.tsv_parser import TsvParseError, parse_delimited
from rpa_grid.value_model import (
    ValueKind,
    classify_value,
    is_dangerous_formula,
    values_equal,
)


# ---------- TSV 解析协议子集 ----------
class TestTsvParser:
    def test_basic_columns_and_rows(self):
        t = parse_delimited("a\tb\tc\n1\t2\t3\n")
        assert t.headers == ["a", "b", "c"]
        assert t.rows == [["1", "2", "3"]]

    def test_quoted_field_keeps_tab_and_newline(self):
        t = parse_delimited('h\n"x\ty\nz"\n')
        assert t.rows[0][0] == "x\ty\nz"

    def test_escaped_quote_and_carriage_return(self):
        t = parse_delimited('h\n"a""b\rc"\n')
        assert t.rows[0][0] == 'a"b\rc'

    def test_crlf_lf_cr_line_endings_accepted(self):
        assert parse_delimited("a\tb\r\n1\t2\r\n").rows == [["1", "2"]]
        assert parse_delimited("a\tb\n1\t2\n").rows == [["1", "2"]]
        assert parse_delimited("a\tb\r1\t2\r").rows == [["1", "2"]]

    def test_empty_string_field_vs_trailing_truncation(self):
        # 两个相邻制表符 => 空字符串字段
        t = parse_delimited("a\tb\tc\n1\t\t3\n")
        assert t.rows[0] == ["1", "", "3"]
        # 行尾截断 => 字段数更少（缺失语义由上层处理）
        t2 = parse_delimited("a\tb\tc\n1\n")
        assert t2.rows[0] == ["1"]

    def test_bom_stripped(self):
        t = parse_delimited("﻿h\nv\n")
        assert t.headers == ["h"]

    def test_unterminated_quote_rejected(self):
        with pytest.raises(TsvParseError):
            parse_delimited('h\n"abc\n')

    def test_bare_quote_midfield_rejected(self):
        with pytest.raises(TsvParseError):
            parse_delimited('h\nab"cd\n')

    def test_empty_input_rejected(self):
        with pytest.raises(TsvParseError):
            parse_delimited("")

    def test_blank_header_rejected(self):
        with pytest.raises(TsvParseError):
            parse_delimited("a\t\tc\n1\t2\t3\n")

    def test_csv_comma_delimiter_supported(self):
        t = parse_delimited('a,b\n"x,y",2\n', delimiter=",")
        assert t.headers == ["a", "b"]
        assert t.rows[0] == ["x,y", "2"]


# ---------- 值模型 ----------
class TestValueModel:
    def test_leading_zero_id_preserved(self):
        v = classify_value("007", ValueKind.ID_TEXT.value)
        assert v.kind is ValueKind.ID_TEXT
        assert v.raw_text == "007"

    def test_decimal_exact_compare_ignores_trailing_zero_display(self):
        v = classify_value("1.20", ValueKind.DECIMAL.value)
        assert v.decimal_value is not None
        ok, _ = values_equal(v, "1.2", "decimal")
        assert ok

    def test_date_text_is_string_not_date(self):
        v = classify_value("2026-11-09", ValueKind.DATE_TEXT.value)
        assert v.kind is ValueKind.DATE_TEXT
        assert v.raw_text == "2026-11-09"

    def test_empty_string_distinct_from_missing(self):
        empty = classify_value("", ValueKind.TEXT.value)
        missing = classify_value(None, ValueKind.TEXT.value)
        assert empty.kind is ValueKind.EMPTY and empty.raw_text == ""
        assert missing.kind is ValueKind.MISSING and missing.raw_text is None

    def test_missing_requires_untouched_target(self):
        missing = classify_value(None, ValueKind.TEXT.value)
        ok, _ = values_equal(missing, None, "missing")
        assert ok
        ok2, diff = values_equal(missing, "", "empty")
        assert not ok2 and "缺失" in diff

    def test_empty_string_readback_rules(self):
        empty = classify_value("", ValueKind.TEXT.value)
        assert values_equal(empty, "", "empty")[0]
        assert values_equal(empty, "", "text")[0]
        assert not values_equal(empty, None, "missing")[0]

    def test_dangerous_formula_detection(self):
        assert is_dangerous_formula("=1+1")
        assert is_dangerous_formula("=cmd|' /C x'!A1")
        assert is_dangerous_formula("@SUM(1,2)")
        assert is_dangerous_formula("+1-2")
        # 普通负数十进制不算公式
        assert not is_dangerous_formula("-2.75")
        assert not is_dangerous_formula("1.20")
        assert not is_dangerous_formula("007")

    def test_dangerous_string_stays_text(self):
        v = classify_value("=1+1", ValueKind.TEXT.value)
        ok, _ = values_equal(v, "=1+1", "text")
        assert ok
        # 若被错误求值成数字，必须判失败
        ok2, _ = values_equal(v, "2", "decimal")
        assert not ok2

    def test_invalid_decimal_rejected_at_modeling(self):
        with pytest.raises(ValueError):
            classify_value("abc", ValueKind.DECIMAL.value)

    def test_invalid_date_rejected(self):
        with pytest.raises(ValueError):
            classify_value("11/09/2026", ValueKind.DATE_TEXT.value)

    def test_text_character_exact_mismatch_detected(self):
        v = classify_value("批次A\t第一行\n第二行", ValueKind.TEXT.value)
        ok, diff = values_equal(v, "批次A第一行第二行", "text")
        assert not ok and "不一致" in diff
