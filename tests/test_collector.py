"""独立预期断言：采集器输出 vs 生成器独立真值。

真值文件 samples/truth/*.json 只在 tests/ 内被读取；
collector 包的任何模块都不导入 generator、不打开 truth 目录。
"""

from __future__ import annotations

import json
import math
import os

import pytest

from collector import collect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
TRUTH = os.path.join(SAMPLES, "truth")


def _run(key):
    with open(os.path.join(SAMPLES, f"{key}.svg"), encoding="utf-8") as fh:
        svg = fh.read()
    with open(os.path.join(TRUTH, f"{key}.json"), encoding="utf-8") as fh:
        truth = json.load(fh)
    result = collect(svg, source_name=f"{key}.svg")
    return result, truth


def _match_observations(result, truth):
    """按画布坐标最近邻把观测点与真值点配对（容差 2px）。"""
    pairs = []
    for tp in truth["points"]:
        if not tp.get("visible", True):
            continue
        best, best_d = None, 2.0
        for ob in result["observations"]:
            d = math.hypot(ob["canvas"]["x"] - tp["canvas"]["x"],
                           ob["canvas"]["y"] - tp["canvas"]["y"])
            if d < best_d:
                best, best_d = ob, d
        if best is None:
            pytest.fail(f"真值点 {tp['canvas']} 在采集结果中找不到对应观测")
        pairs.append((tp, best))
    return pairs


def _within_interval(value, interval, scale):
    lo, hi = interval
    if scale == "log":
        # 对数轴允许相对 8% 的偏差（像素先验 + 生成器抖动）
        tol = max(abs(value) * 0.08, 1e-9)
    else:
        tol = max(abs(value) * 0.03, 0.05)
    assert lo - tol <= value <= hi + tol, (
        f"真值 {value} 落在采集区间 [{lo}, {hi}]±tol={tol} 之外"
    )


# ---------- 验收：四类轴变换 ----------
def test_linear_basic_axes_and_values():
    result, truth = _run("linear_basic")
    assert result["status"] == "ok"
    assert result["axes"]["x"]["scale"] == "linear"
    assert result["axes"]["y"]["scale"] == "linear"
    assert result["summary"]["recoverable_points"] == len(truth["points"])
    for tp, ob in _match_observations(result, truth):
        assert ob["recoverable"]
        _within_interval(tp["x"], ob["values"]["x"]["interval"], "linear")
        _within_interval(tp["y"], ob["values"]["y"]["interval"], "linear")


def test_log_y_axis():
    result, truth = _run("log_y")
    assert result["axes"]["y"]["scale"] == "log"
    assert result["axes"]["x"]["scale"] == "linear"
    for tp, ob in _match_observations(result, truth):
        _within_interval(tp["y"], ob["values"]["y"]["interval"], "log")
        _within_interval(tp["x"], ob["values"]["x"]["interval"], "linear")
        # 对数轴误差区间必须非对称且为正
        lo, hi = ob["values"]["y"]["interval"]
        assert 0 < lo < ob["values"]["y"]["value"] < hi


def test_log_log_axes():
    result, truth = _run("log_log")
    assert result["axes"]["x"]["scale"] == "log"
    assert result["axes"]["y"]["scale"] == "log"
    for tp, ob in _match_observations(result, truth):
        _within_interval(tp["x"], ob["values"]["x"]["interval"], "log")
        _within_interval(tp["y"], ob["values"]["y"]["interval"], "log")


def test_reversed_x_axis():
    result, truth = _run("reversed_x")
    ax = result["axes"]["x"]
    assert ax["scale"] == "linear"
    assert ax["reversed"] is True
    assert ax["slope_sign"] < 0
    for tp, ob in _match_observations(result, truth):
        _within_interval(tp["x"], ob["values"]["x"]["interval"], "linear")
    # 反向轴：画布 X 最小的点对应最大数据值，画布 X 最大的点对应最小值
    obs = sorted(result["observations"], key=lambda o: o["canvas"]["x"])
    assert obs[0]["values"]["x"]["value"] > obs[-1]["values"]["x"]["value"]


# ---------- 嵌套 transform / viewBox ----------
def test_nested_transform_recovers_values():
    result, truth = _run("nested_transform")
    assert result["status"] == "ok"
    # 画布坐标必须与经过同一矩阵链变换的独立真值一致
    for tp, ob in _match_observations(result, truth):
        assert abs(ob["canvas"]["x"] - tp["canvas"]["x"]) < 1.5
        assert abs(ob["canvas"]["y"] - tp["canvas"]["y"]) < 1.5
        _within_interval(tp["x"], ob["values"]["x"]["interval"], "linear")
        _within_interval(tp["y"], ob["values"]["y"]["interval"], "linear")
    # 观测点应携带嵌套变换链证据
    assert any(
        len(o["transform_chain"]) >= 2
        for o in result["observations"]
    )


def test_viewbox_scale_and_rotated_labels():
    result, truth = _run("viewbox_rotated")
    assert result["status"] == "ok"
    scale = truth["viewBox_scale"]
    # 采集器识别的刻度像素间距应体现 viewBox 缩放
    coords = [t["canvas_coord"] for t in result["axes"]["x"]["ticks"]]
    gap = coords[1] - coords[0]
    inner_gap = 380 / 5
    assert abs(gap - inner_gap * scale) < 1.0
    for tp, ob in _match_observations(result, truth):
        _within_interval(tp["x"], ob["values"]["x"]["interval"], "linear")


# ---------- 断点 ----------
def test_broken_line_reports_breaks():
    result, truth = _run("broken_line")
    assert result["status"] == "ok"
    assert len(result["breaks"]) == truth["expected_break_count"]
    # 断点两端的点分段编号必须不同，且全部仍可恢复
    seg_ids = {o["segment_index"] for o in result["observations"]}
    assert len(seg_ids) == truth["expected_break_count"] + 1
    assert all(o["recoverable"] for o in result["observations"])


# ---------- 裁剪 ----------
def test_clipped_points_explained_but_not_collected():
    result, truth = _run("clipped")
    clipped_truth = [p for p in truth["points"] if not p["visible"]]
    visible_truth = [p for p in truth["points"] if p["visible"]]
    assert len(result["clipped_points"]) == len(clipped_truth)
    assert result["summary"]["recoverable_points"] == len(visible_truth)
    # 被裁剪点不得出现在观测值里，且必须带可见解释
    clipped_canvas = {(round(c["canvas"]["x"], 1), round(c["canvas"]["y"], 1))
                      for c in result["clipped_points"]}
    for c in result["clipped_points"]:
        assert c["explanation"] and "裁剪" in c["explanation"]
        assert c["recoverable"] is False
    for ob in result["observations"]:
        key = (round(ob["canvas"]["x"], 1), round(ob["canvas"]["y"], 1))
        assert key not in clipped_canvas


# ---------- 隐藏诱饵 ----------
def test_hidden_decoy_series_ignored():
    result, truth = _run("hidden_decoys")
    assert result["status"] == "ok"
    # 采集点数等于可见系列点数，隐藏的一批点全部忽略
    assert result["summary"]["visible_points"] == len(truth["points"])
    flat = json.dumps(result, ensure_ascii=False)
    assert "data_secret_values" not in flat
    assert "48.0" not in [
        o["values"]["y"]["value"] for o in result["observations"]
    ]


# ---------- 刻度不足：拒绝假精确 ----------
def test_insufficient_ticks_refuses():
    result, truth = _run("insufficient_ticks")
    assert result["status"] == "partial"
    assert result["axes"]["x"]["available"] is False
    assert truth["expected_error_keyword"] in result["axes"]["x"]["error"]
    # 没有任何点能给出 X 值，Y 轴仍可独立标定
    for ob in result["observations"]:
        assert "x" not in ob["values"]
        assert ob["recoverable"] is False
        assert any("标定失败" in e or "拒绝" in e for e in ob["errors"])
    assert result["axes"]["y"]["available"] is True
    # 详细的“拒绝猜测”原因在顶层错误与轴证据中可见
    assert any("拒绝" in e for e in result["errors"])


# ---------- 非法对数/域外点 ----------
def test_illegal_log_point_blocked_with_explanation():
    result, truth = _run("illegal_log")
    assert result["status"] == "partial"
    blocked = [o for o in result["observations"] if not o["recoverable"]]
    assert len(blocked) == 1
    err_text = " ".join(blocked[0]["errors"])
    assert "拒绝" in err_text
    assert ("对数" in err_text) or ("刻度覆盖范围" in err_text)
    # 同图正常点不受影响
    good = [o for o in result["observations"] if o["recoverable"]]
    assert len(good) == len([p for p in truth["points"] if p["y"] is not None])


# ---------- 纯几何单元：transform 数学独立验证 ----------
def test_transform_math_independent_check():
    from collector.geometry import apply, multiply, rotation, translation
    # 手写 2D 仿射期望：T(10,20) * R(90°) * T(1,0) 作用于 (0,0)
    # (0,0) 先 T(1,0)->(1,0)，R90 -> (0,1)，T(10,20) -> (10,21)
    m = multiply(translation(10, 20), multiply(rotation(90), translation(1, 0)))
    x, y = apply(m, 0, 0)
    assert abs(x - 10) < 1e-9
    assert abs(y - 21) < 1e-9


def test_path_parser_rejects_curves():
    from collector.series import parse_path_d
    from collector.transforms import UnsupportedFeature
    with pytest.raises(UnsupportedFeature):
        parse_path_d("M0 0 C1 1 2 2 3 3")
    subs = parse_path_d("M0 0 L5 5 H10 V2 Z M20 20 L25 25")
    assert len(subs) == 2  # 两个子路径 = 一个断点
