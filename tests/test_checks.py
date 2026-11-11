"""四类语义检查的单元测试：标签 / 单位 / 限定说明 / 数据类型。"""

import pytest

from collector import checks
from collector.contracts import BASELINE_CONTRACT

DRY = BASELINE_CONTRACT.field("dry_weight")
PROTEIN = BASELINE_CONTRACT.field("crude_protein")


def codes(violations):
    return [v.code for v in violations]


def test_baseline_header_passes():
    obs = checks.build_observed(DRY, "样品干重（平均，g）")
    assert codes(checks.check_header(DRY, obs)) == []


def test_restyle_same_semantics_passes():
    # 纯样式改版不改文本；即使有空白差异也应通过
    obs = checks.build_observed(DRY, " 样品干重 （平均，g） ")
    assert codes(checks.check_header(DRY, obs)) == []


def test_label_change_detected():
    obs = checks.build_observed(DRY, "样品湿重（平均，g）")
    violations = checks.check_header(DRY, obs)
    assert codes(violations) == ["LABEL_MISMATCH"]
    assert violations[0].level == "column"
    assert "湿重" in violations[0].evidence_text


def test_unit_scale_change_detected():
    obs = checks.build_observed(DRY, "样品干重（平均，mg）")
    violations = checks.check_header(DRY, obs)
    assert codes(violations) == ["UNIT_SCALE"]
    assert violations[0].observed["scale_ratio_to_expected"] == pytest.approx(1e-3)


def test_unit_incompatible_dimension_detected():
    obs = checks.build_observed(DRY, "样品干重（平均，g/g）")
    assert codes(checks.check_header(DRY, obs)) == ["UNIT_INCOMPATIBLE"]


def test_unsupported_unit_flagged_not_silently_accepted():
    obs = checks.build_observed(DRY, "样品干重（平均，lb）")
    assert codes(checks.check_header(DRY, obs)) == ["UNIT_UNSUPPORTED"]


def test_missing_unit_flagged():
    obs = checks.build_observed(DRY, "样品干重（烘箱法测定）")
    # 同时缺少单位与“平均”限定说明，两个问题都应如实报告
    assert set(codes(checks.check_header(DRY, obs))) == {"UNIT_MISSING", "QUALIFIER_MISSING"}


def test_qualifier_missing_detected():
    obs = checks.build_observed(DRY, "样品干重（g，烘箱法测定）")
    assert codes(checks.check_header(DRY, obs)) == ["QUALIFIER_MISSING"]


def test_stat_switch_mean_to_median_detected():
    obs = checks.build_observed(DRY, "样品干重（中位数，g）")
    assert codes(checks.check_header(DRY, obs)) == ["STAT_MISMATCH"]


def test_header_selector_miss_returns_header_missing():
    assert codes(checks.check_header(DRY, None)) == ["HEADER_MISSING"]


def test_cell_number_type_ok_and_text_fails():
    assert checks.check_cell(DRY, 0, "2.450") == []
    assert checks.check_cell(DRY, 0, "1,234.5") == []  # 千分位数字可解析
    violations = checks.check_cell(DRY, 2, "仪器离线")
    assert codes(violations) == ["TYPE_MISMATCH"]
    assert violations[0].row_index == 2 and violations[0].level == "record"


def test_text_field_empty_fails():
    sample = BASELINE_CONTRACT.field("sample_id")
    assert checks.check_cell(sample, 0, "QL-2026-001") == []
    assert codes(checks.check_cell(sample, 0, "  ")) == ["TYPE_MISMATCH"]


def test_percent_alias_header_passes():
    obs = checks.build_observed(PROTEIN, "粗蛋白含量（平均，质量分数 %）")
    assert checks.check_header(PROTEIN, obs) == []
