"""采集器边界单元：标签解析、裁剪语义、协议外特性拒绝。"""

from __future__ import annotations

import pytest

from collector.axis import parse_label_number
from collector.series import parse_path_d
from collector.transforms import UnsupportedFeature, parse_transform
from collector import collect


def test_label_number_formats():
    assert parse_label_number("1,234.5")[0] == 1234.5
    assert parse_label_number("50%")[0] == pytest.approx(0.5)
    assert parse_label_number("12ms")[0] == 12
    assert parse_label_number("-3.2e2")[0] == -320
    value, note = parse_label_number("10³")  # 上标形式不在子集内
    assert value is None and note
    value, note = parse_label_number("2024-01")  # 日期不支持
    assert value is None and note


def test_skew_transform_rejected():
    with pytest.raises(UnsupportedFeature):
        parse_transform("skewX(10)")


def test_nested_transform_order():
    from collector.geometry import apply
    from collector.transforms import combine, parse_transform
    mats = parse_transform("translate(5,0) scale(2)")
    # 合并后 (1,1) 应先缩放再平移 => (7,2)
    x, y = apply(combine(mats), 1, 1)
    assert (x, y) == (7, 2)


def test_unsupported_clip_path_marks_skipped():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">
      <defs><clipPath id="c" clipPathUnits="objectBoundingBox">
        <rect x="0" y="0" width="0.5" height="1"/></clipPath></defs>
      <line x1="20" y1="200" x2="380" y2="200" stroke="#000"/>
      <line x1="40" y1="200" x2="40" y2="208" stroke="#000"/>
      <line x1="120" y1="200" x2="120" y2="208" stroke="#000"/>
      <text x="40" y="230">0</text><text x="120" y="230">10</text>
      <line x1="20" y1="40" x2="20" y2="200" stroke="#000"/>
      <line x1="12" y1="80" x2="20" y2="80" stroke="#000"/>
      <line x1="12" y1="160" x2="20" y2="160" stroke="#000"/>
      <text x="10" y="84" text-anchor="end">5</text>
      <text x="10" y="164" text-anchor="end">10</text>
      <polyline clip-path="url(#c)" points="60,100 100,120" stroke="#00f"/>
    </svg>"""
    result = collect(svg)
    assert result["skipped_elements"]
    assert any("clipPath" in s["reason"] for s in result["skipped_elements"])


def test_curve_path_skipped_not_crashed():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">
      <line x1="20" y1="200" x2="380" y2="200" stroke="#000"/>
      <line x1="40" y1="200" x2="40" y2="208" stroke="#000"/>
      <line x1="120" y1="200" x2="120" y2="208" stroke="#000"/>
      <text x="40" y="230">0</text><text x="120" y="230">10</text>
      <line x1="20" y1="40" x2="20" y2="200" stroke="#000"/>
      <line x1="12" y1="80" x2="20" y2="80" stroke="#000"/>
      <line x1="12" y1="160" x2="20" y2="160" stroke="#000"/>
      <text x="10" y="84" text-anchor="end">5</text>
      <text x="10" y="164" text-anchor="end">10</text>
      <path d="M40 100 C60 20, 120 20, 140 100" stroke="#00f"/>
    </svg>"""
    result = collect(svg)
    assert result["skipped_elements"]
    assert any("曲线" in s["reason"] for s in result["skipped_elements"])


def test_broken_xml_returns_error_status():
    result = collect("<svg><polyline points='0,0'></svg>")
    assert result["status"] == "error"
    assert result["errors"]


def test_path_parser_relative_commands():
    subs = parse_path_d("m10 10 l5 5 h10 v-5 z")
    assert len(subs) == 1
    # 校验终点绝对坐标 (25, 10)
    coords = [p for cmd, pts in subs[0] for p in pts]
    assert coords[-1] == (25.0, 10.0)


def test_degenerate_duplicate_tick_values_rejected():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="500" height="360">
      <line x1="40" y1="260" x2="460" y2="260" stroke="#000"/>
      <line x1="80" y1="260" x2="80" y2="268"/>
      <line x1="240" y1="260" x2="240" y2="268"/>
      <text x="80" y="288" text-anchor="middle">10</text>
      <text x="240" y="288" text-anchor="middle">10</text>
      <line x1="40" y1="40" x2="40" y2="260" stroke="#000"/>
      <line x1="32" y1="100" x2="40" y2="100"/>
      <line x1="32" y1="180" x2="40" y2="180"/>
      <text x="28" y="104" text-anchor="end">20</text>
      <text x="28" y="184" text-anchor="end">40</text>
      <polyline points="120,150 200,120" stroke="#00f"/>
    </svg>"""
    result = collect(svg)
    assert result["status"] == "partial"
    assert result["axes"]["x"]["available"] is False
    assert "数值完全相同" in result["axes"]["x"]["error"]
    # Y 轴不受影响
    assert result["axes"]["y"]["available"] is True
