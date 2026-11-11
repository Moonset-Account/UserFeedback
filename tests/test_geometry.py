"""核心纯逻辑单元测试（独立断言，不启动浏览器）。"""
from __future__ import annotations

import math

import pytest

from curve_rpa.geometry import (
    AffineMap,
    TickSample,
    clamp,
    expected_snap_error,
    fit_affine,
    is_mid_grid,
    out_of_bounds,
    snap_to_grid,
    violates_x_order,
    within_tolerance,
)


def test_fit_affine_standard_axis():
    # 数值 0->像素 100，数值 100->像素 500：slope=4, intercept=100
    samples = [TickSample(0.0, 100.0), TickSample(50.0, 300.0), TickSample(100.0, 500.0)]
    m = fit_affine(samples)
    assert m.slope == pytest.approx(4.0)
    assert m.intercept == pytest.approx(100.0)
    assert m.to_value(300.0) == pytest.approx(50.0)


def test_fit_affine_reversed_axis_negative_slope():
    # 反向纵轴：数值越大像素越靠上（像素值越小），斜率必须为负
    samples = [TickSample(0.0, 496.0), TickSample(50.0, 268.0), TickSample(100.0, 40.0)]
    m = fit_affine(samples)
    assert m.slope < 0
    assert m.to_value(40.0) == pytest.approx(100.0)
    assert m.to_value(496.0) == pytest.approx(0.0)


def test_fit_affine_least_squares_resists_noise():
    # 中间刻度带 1px 噪声，拟合结果仍接近真值 slope=2
    samples = [TickSample(0.0, 10.0), TickSample(10.0, 31.0), TickSample(20.0, 50.0), TickSample(30.0, 70.0)]
    m = fit_affine(samples)
    assert m.slope == pytest.approx(2.0, abs=0.05)
    assert m.intercept == pytest.approx(10.0, abs=0.8)


def test_fit_affine_requires_two_samples():
    with pytest.raises(ValueError):
        fit_affine([TickSample(1.0, 5.0)])


def test_snap_to_grid_and_mid_grid():
    # 网格步长 5：网格线在 ..., 25, 30, 35, ...
    assert snap_to_grid(30.0, 5.0) == pytest.approx(30.0)
    assert snap_to_grid(32.4, 5.0) == pytest.approx(30.0)
    assert snap_to_grid(33.0, 5.0) == pytest.approx(35.0)
    # 32.5 恰落在两条网格线 30/35 正中间：与 JS Math.round 一致，半值向上吸附到 35，误差 2.5
    assert snap_to_grid(32.5, 5.0) == pytest.approx(35.0)
    assert is_mid_grid(32.4, 5.0)
    assert is_mid_grid(32.5, 5.0)
    assert not is_mid_grid(30.0, 5.0)
    assert expected_snap_error(32.5, 5.0) == pytest.approx(2.5)


def test_out_of_bounds_and_clamp():
    assert out_of_bounds(118.0, 0.0, 100.0)
    assert out_of_bounds(-0.1, 0.0, 100.0)
    assert not out_of_bounds(100.0, 0.0, 100.0)
    assert clamp(118.0, 0.0, 100.0) == 100.0
    assert clamp(-3.0, 0.0, 100.0) == 0.0


def test_order_constraint():
    assert not violates_x_order([10.0, 20.0, 30.0])
    assert violates_x_order([60.0, 40.0])  # 交叉
    assert violates_x_order([10.0, 10.0])  # 相等也违反严格递增


def test_within_tolerance():
    assert within_tolerance(27.5, 27.5, 0.55)
    assert within_tolerance(30.0, 32.5, 2.5)
    assert not within_tolerance(30.0, 32.5, 0.1)


def test_affine_roundtrip_with_zoom_pan():
    # 模拟 zoom=1.35、平移后的映射：像素 = slope*value + intercept
    m = AffineMap(slope=10.7, intercept=-42.3)
    for v in [0.0, 27.5, 62.5, 100.0]:
        assert m.to_value(m.to_pixel(v)) == pytest.approx(v)
    assert math.isfinite(m.to_pixel(50.0))
