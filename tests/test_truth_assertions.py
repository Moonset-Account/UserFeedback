"""独立预期断言：以生成器产出的 truth.json 为唯一期望来源。

这些测试不把采集结果当真值，而是用独立文件核对规划与建模；
并验证采集边界——truth.json 不通过任何 HTTP 接口暴露。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from rpa_grid.collector import CollectorError, collect
from rpa_grid.matrix import build_matrix_from_tsv
from rpa_grid.planner import PlanStatus, plan_execution
from rpa_grid.value_model import ValueKind

SCENARIOS = ["demo-normal", "demo-edge", "acceptance-full"]


class TestCollectorBoundary:
    def test_truth_not_served_over_http(self, server):
        for scen in SCENARIOS:
            url = f"{server}/samples/{scen}/truth.json"
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(url)
            assert ei.value.code == 403

    def test_matrix_and_schema_are_served(self, server):
        for scen in SCENARIOS:
            tsv = urllib.request.urlopen(
                f"{server}/samples/{scen}/matrix.tsv").read()
            schema = json.loads(urllib.request.urlopen(
                f"{server}/samples/{scen}/schema.json").read())
            assert tsv.startswith(("实验编号".encode("utf-8"), b"\xef\xbb\xbf"))
            assert all("raw_text" not in c for c in schema["columns"])

    def test_collector_only_uses_registered_sources(self):
        with pytest.raises(CollectorError):
            collect("gopher://127.0.0.1/x", "demo-normal")

    def test_collect_over_http_builds_typed_matrix(self, server):
        bundle = collect(server, "demo-normal")
        assert bundle.matrix.n_data_rows == 3
        kinds = {c.value.kind for c in bundle.matrix.rows[0]}
        assert ValueKind.ID_TEXT in kinds
        assert ValueKind.DECIMAL in kinds
        # 证据里只记录接口与指纹，不含真值
        assert "matrix_sha256_12" in bundle.evidence


class TestTruthAssertions:
    """每个场景逐格核对：类型、空串/缺失、可写性与规划状态。"""

    @pytest.mark.parametrize("scenario", SCENARIOS)
    def test_matrix_matches_truth_values(self, scenario, matrix_text, truth):
        expected = truth(scenario)
        matrix = build_matrix_from_tsv(
            matrix_text(scenario), scenario, f"http://test/{scenario}")
        got = {
            (c.row_index, c.column.key): c.value
            for _, c in matrix.walk_cells()
        }
        assert len(got) == len(expected["cells"])
        for cell in expected["cells"]:
            coord = (cell["row"], cell["column"])
            tv = got[coord]
            assert tv.kind.value == cell["kind"], coord
            assert tv.raw_text == cell["raw_text"], coord

    @pytest.mark.parametrize("scenario", SCENARIOS)
    def test_planner_rejects_hidden_and_readonly(self, scenario,
                                                  matrix_text, truth,
                                                  samples_dir):
        from rpa_grid.matrix import GridConfig

        grid = GridConfig.load(samples_dir / scenario / "grid_config.json")
        matrix = build_matrix_from_tsv(
            matrix_text(scenario), scenario, "x")
        plan = plan_execution(matrix, grid)
        by_coord = {(p.row_index, p.column_key): p for p in plan.cell_plans}

        for cell in truth(scenario)["cells"]:
            p = by_coord[(cell["row"], cell["column"])]
            if cell["expect_status"] == "rejected":
                assert p.status is PlanStatus.REJECTED
                assert p.reason  # 必须给出可解释的理由
            else:
                assert p.status is PlanStatus.PLANNED

    def test_hidden_column_never_in_visible_list(self, matrix_text, samples_dir):
        from rpa_grid.matrix import GridConfig

        grid = GridConfig.load(samples_dir / "demo-edge" / "grid_config.json")
        matrix = build_matrix_from_tsv(
            matrix_text("demo-edge"), "demo-edge", "x")
        plan = plan_execution(matrix, grid)
        assert "操作员" not in plan.visible_columns
        assert "操作员" in plan.hidden_columns

    def test_missing_cells_are_skipped_not_written(self, matrix_text,
                                                   samples_dir, truth):
        from rpa_grid.matrix import GridConfig

        scenario = "demo-normal"
        grid = GridConfig.load(samples_dir / scenario / "grid_config.json")
        matrix = build_matrix_from_tsv(
            matrix_text(scenario), scenario, "x")
        plan = plan_execution(matrix, grid)
        # 期望跳过数 = 真值里「可写且缺失」的格（隐藏列缺失应先被拒绝）
        exp_skip = sum(
            1 for c in truth(scenario)["cells"]
            if c["writable"] and c["kind"] == "missing")
        skip = [p for p in plan.cell_plans
                if p.strategy is not None
                and p.strategy.value == "skip_missing"]
        assert len(skip) == exp_skip
        # 隐藏列中的缺失格走拒绝而非跳过
        hidden_missing = [
            p for p in plan.cell_plans
            if p.column_key in plan.hidden_columns and p.kind == "missing"]
        assert all(p.status is PlanStatus.REJECTED for p in hidden_missing)
