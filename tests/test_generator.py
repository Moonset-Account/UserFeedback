"""样例生成器与独立真值测试：确保页面内容与真值一致、选择器在各场景保留。"""

import json
from pathlib import Path

import pytest

from site_gen.generator import (
    EXPECTED_VIOLATIONS,
    SCENARIOS,
    generate_all,
)


def test_all_scenarios_generated(generated_dir: Path):
    items = generate_all(generated_dir)
    assert {i.scenario for i in items} == set(SCENARIOS)
    for item in items:
        html = item.html_path.read_text(encoding="utf-8")
        # 选择器在所有改版场景中保持不变
        assert 'table id="measure"' in html
        for cls in ("col-sample", "col-dry", "col-protein", "col-moisture"):
            assert f'class="{cls}"' in html
        truth = json.loads(item.truth_path.read_text(encoding="utf-8"))
        assert truth["selectors_preserved"] is True


def test_truth_rows_match_rendered_cells(generated_dir: Path):
    for scenario in SCENARIOS:
        truth = json.loads((generated_dir / "truth" / f"{scenario}.json").read_text(encoding="utf-8"))
        html = (generated_dir / "samples" / scenario / "index.html").read_text(encoding="utf-8")
        for row in truth["rows"]:
            for value in row.values():
                assert f">{value}<" in html


def test_unit_scenario_values_are_1000x_baseline(generated_dir: Path):
    base = json.loads((generated_dir / "truth" / "baseline.json").read_text(encoding="utf-8"))
    unit = json.loads((generated_dir / "truth" / "unit.json").read_text(encoding="utf-8"))
    for b, u in zip(base["rows"], unit["rows"]):
        assert float(u["dry_weight"]) == pytest.approx(float(b["dry_weight"]) * 1000)


def test_expected_violations_cover_acceptance_scenarios():
    # 验收四类场景 + 额外两类
    assert EXPECTED_VIOLATIONS["restyle"] == {"column": [], "record": []}
    assert EXPECTED_VIOLATIONS["label"]["column"] == ["LABEL_MISMATCH"]
    assert EXPECTED_VIOLATIONS["unit"]["column"] == ["UNIT_SCALE"]
    assert EXPECTED_VIOLATIONS["no-qual"]["column"] == ["QUALIFIER_MISSING"]
    assert EXPECTED_VIOLATIONS["median"]["column"] == ["STAT_MISMATCH"]
    assert EXPECTED_VIOLATIONS["type-error"]["record"] == ["TYPE_MISMATCH"]
