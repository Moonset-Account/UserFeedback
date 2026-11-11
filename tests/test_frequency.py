"""小总体多种子频率检验（验收项）及覆盖性免责声明检查。"""
from __future__ import annotations

from webapp.frequency import run_frequency_check


def test_chi_square_uniformity_across_seeds():
    report = run_frequency_check(population_size=25, sample_size=5, trials=2000, base_seed=20261111)
    # df=24，α=0.05 临界值 36.415；正常实现的卡方统计量应远低于该值
    assert report["chi_square"] < 36.415
    assert report["passed_at_0_05"] is True
    # 每个 ID 都被抽中过，且频率围绕理论概率 p=0.2 小幅波动
    for f in report["frequencies"]:
        assert f["count"] > 0
        assert abs(f["frequency"] - 0.2) < 0.09
    assert len({tuple(p["sample"]) for p in report["pinned_runs"]}) >= 2


def test_frequency_check_is_seed_reproducible():
    a = run_frequency_check(trials=500)
    b = run_frequency_check(trials=500)
    assert a["chi_square"] == b["chi_square"]
    assert a["frequencies"] == b["frequencies"]


def test_disclaimer_explicitly_rejects_sitewide_coverage_claim():
    report = run_frequency_check(trials=200)
    text = report["disclaimer"]
    assert "不构成全站覆盖证明" in text
    assert "已观察集合" in text
    # 边界参数校验
    import pytest
    with pytest.raises(ValueError):
        run_frequency_check(population_size=25, sample_size=30, trials=200)
    with pytest.raises(ValueError):
        run_frequency_check(trials=10)
