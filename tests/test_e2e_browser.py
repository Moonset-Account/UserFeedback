"""端到端测试：真实 Chromium 采集生成的页面，结果对照独立真值断言。

采集路径：Engine -> BrowserDriver -> Playwright 打开 file:// 页面 -> 读 DOM。
测试只在断言阶段读取 generated/truth 下的真值，采集器代码不访问真值目录。
"""

import json
from pathlib import Path

import pytest

from site_gen.generator import SCENARIOS


def _run_scenario(engine, generated_dir: Path, scenario: str):
    url = (generated_dir / "samples" / scenario / "index.html").as_uri()
    return engine.run(scenario=scenario, url=url, contract_version="v1")


def _truth(generated_dir: Path, scenario: str) -> dict:
    return json.loads((generated_dir / "truth" / f"{scenario}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_browser_collection_matches_truth(engine, generated_dir, scenario):
    result = _run_scenario(engine, generated_dir, scenario)
    truth = _truth(generated_dir, scenario)

    # 浏览器真实采集到的行数与真值一致（不是预填数据）
    assert result["evidence"]["table_found"] is True
    assert result["evidence"]["row_count"] == truth["expected_row_count"]
    assert result["evidence"]["access"] == "file"
    # 截图与 HTML 原始证据已落盘
    run_dir = engine.storage.runs_dir / result["run_id"]
    assert (run_dir / "screenshot.png").stat().st_size > 0
    assert 'id="measure"' in (run_dir / "table.html").read_text(encoding="utf-8")

    # 违规码与独立真值完全一致
    column_codes = sorted(v["code"] for v in result["violations"] if v["level"] == "column")
    record_codes = sorted(v["code"] for v in result["violations"] if v["level"] == "record")
    assert column_codes == sorted(truth["expected_violations"]["column"])
    assert record_codes == sorted(truth["expected_violations"]["record"])

    # 发布/隔离数量与真值一致
    assert len(result["published"]) == truth["expected_published_rows"]
    assert len(result["published"]) + len(result["quarantined"]) == truth["expected_row_count"]


def test_restyle_no_false_positive(engine, generated_dir):
    """验收①：纯样式改版不得误报，全部记录正常发布。"""
    result = _run_scenario(engine, generated_dir, "restyle")
    assert result["status"] == "passed"
    assert result["violations"] == []
    assert len(result["published"]) == 4
    assert result["quarantined"] == []


def test_label_change_blocks_unapproved_semantics(engine, generated_dir):
    """验收②：干重->湿重，未批准时相关记录一律不得进入正式结果。"""
    result = _run_scenario(engine, generated_dir, "label")
    assert {v["code"] for v in result["violations"]} == {"LABEL_MISMATCH"}
    assert result["published"] == []  # 列头级违规隔离整列全部记录
    assert len(result["quarantined"]) == 4
    # 正式结果里不允许出现湿重数值
    blocked = result["quarantined"][0]
    assert "dry_weight" in blocked["blocked_fields"]
    assert "湿重" in blocked["reason"]


def test_unit_scale_change_blocks_and_reports_ratio(engine, generated_dir):
    """验收③：g->mg 倍数改变必须拦截，并给出换算倍数证据。"""
    result = _run_scenario(engine, generated_dir, "unit")
    violation = next(v for v in result["violations"] if v["code"] == "UNIT_SCALE")
    assert violation["observed"]["unit"] == "mg"
    assert violation["observed"]["scale_ratio_to_expected"] == pytest.approx(1e-3)
    assert result["published"] == []
    assert len(result["quarantined"]) == 4


def test_missing_qualifier_blocks(engine, generated_dir):
    """验收④：缺少“平均”限定说明必须拦截。"""
    result = _run_scenario(engine, generated_dir, "no-qual")
    assert {v["code"] for v in result["violations"]} == {"QUALIFIER_MISSING"}
    assert result["published"] == []
    assert len(result["quarantined"]) == 4


def test_median_change_blocks(engine, generated_dir):
    result = _run_scenario(engine, generated_dir, "median")
    assert {v["code"] for v in result["violations"]} == {"STAT_MISMATCH"}
    assert len(result["quarantined"]) == 4


def test_record_level_type_error_only_quarantines_one_row(engine, generated_dir):
    """数据类型问题只隔离对应行，其余记录仍可发布。"""
    result = _run_scenario(engine, generated_dir, "type-error")
    assert len(result["published"]) == 3
    assert len(result["quarantined"]) == 1
    bad = result["quarantined"][0]
    assert bad["row_index"] == 2
    assert "dry_weight" in bad["blocked_fields"]
    assert bad["raw"]["dry_weight"] == "仪器离线"


def test_baseline_published_values_equal_truth(engine, generated_dir):
    result = _run_scenario(engine, generated_dir, "baseline")
    truth = _truth(generated_dir, "baseline")
    for rec, trow in zip(result["published"], truth["rows"]):
        assert rec["raw"]["sample_id"] == trow["sample_id"]
        assert rec["values"]["dry_weight"] == pytest.approx(float(trow["dry_weight"]))
        assert rec["values"]["crude_protein"] == pytest.approx(float(trow["crude_protein"]))
