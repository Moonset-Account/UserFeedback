"""样例源站生成器（与采集器分离）。

生成虚构的“青岚大学材料表征实验室 · 样品测量台账”页面及其独立真值文件。
所有数据均为虚构，不依赖真实账户或外部业务服务。

目录：
  generated/
    samples/<scenario>/index.html   供浏览器访问的页面（采集器只允许通过浏览器/HTTP 读它）
    truth/<scenario>.json           独立预期真值，仅供测试断言使用，
                                    采集器代码路径中不存在任何对 truth 目录的读取。

六个场景（前四个为验收要求场景）：
  baseline   正常基线：选择器、标签、单位、限定说明、类型全部符合 v1 契约
  restyle    纯样式改版：CSS 与装饰文本变化，选择器与语义完全不变（不得误报）
  label      标签含义改变：干重 -> 湿重（选择器保留）
  unit       单位倍数改变：g -> mg，数值按 1000 倍改写（选择器保留）
  no-qual    缺少必要限定说明：列头去掉“平均”（选择器保留）
  median     统计口径改变：平均 -> 中位数（选择器保留）
  type-error 数据类型组成改变：某行干重单元格变为文本（额外场景）
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# 虚构基线数据（单位见列头；页面数值即这些字符串）
BASELINE_ROWS: list[dict[str, str]] = [
    {"sample_id": "QL-2026-001", "dry_weight": "2.450", "crude_protein": "18.6", "moisture": "7.2"},
    {"sample_id": "QL-2026-002", "dry_weight": "3.105", "crude_protein": "20.1", "moisture": "6.8"},
    {"sample_id": "QL-2026-003", "dry_weight": "1.872", "crude_protein": "17.4", "moisture": "8.1"},
    {"sample_id": "QL-2026-004", "dry_weight": "2.660", "crude_protein": "19.3", "moisture": "7.5"},
]

# 每个场景的列头覆写（选择器保持不变）
HEADERS: dict[str, dict[str, str]] = {
    "baseline": {
        "sample_id": "样品编号",
        "dry_weight": "样品干重（平均，g）",
        "crude_protein": "粗蛋白含量（平均，质量分数 %）",
        "moisture": "含水率（平均，%）",
    },
    "restyle": {
        "sample_id": "样品编号",
        "dry_weight": "样品干重（平均，g）",
        "crude_protein": "粗蛋白含量（平均，质量分数 %）",
        "moisture": "含水率（平均，%）",
    },
    "label": {
        "sample_id": "样品编号",
        "dry_weight": "样品湿重（平均，g）",
        "crude_protein": "粗蛋白含量（平均，质量分数 %）",
        "moisture": "含水率（平均，%）",
    },
    "unit": {
        "sample_id": "样品编号",
        "dry_weight": "样品干重（平均，mg）",
        "crude_protein": "粗蛋白含量（平均，质量分数 %）",
        "moisture": "含水率（平均，%）",
    },
    "no-qual": {
        "sample_id": "样品编号",
        "dry_weight": "样品干重（g，烘箱法测定）",
        "crude_protein": "粗蛋白含量（平均，质量分数 %）",
        "moisture": "含水率（平均，%）",
    },
    "median": {
        "sample_id": "样品编号",
        "dry_weight": "样品干重（中位数，g）",
        "crude_protein": "粗蛋白含量（平均，质量分数 %）",
        "moisture": "含水率（平均，%）",
    },
    "type-error": {
        "sample_id": "样品编号",
        "dry_weight": "样品干重（平均，g）",
        "crude_protein": "粗蛋白含量（平均，质量分数 %）",
        "moisture": "含水率（平均，%）",
    },
}

SCENARIO_TITLES = {
    "baseline": "样品测量台账（基线页面）",
    "restyle": "样品测量台账（新版式）",
    "label": "样品测量台账（湿重口径）",
    "unit": "样品测量台账（毫克单位）",
    "no-qual": "样品测量台账（方法改版）",
    "median": "样品测量台账（中位数口径）",
    "type-error": "样品测量台账（录入异常）",
}

# 测试预期：每个场景应触发的列头级/记录级违规码（独立真值的一部分）
EXPECTED_VIOLATIONS: dict[str, dict[str, list[str]]] = {
    "baseline": {"column": [], "record": []},
    "restyle": {"column": [], "record": []},
    "label": {"column": ["LABEL_MISMATCH"], "record": []},
    "unit": {"column": ["UNIT_SCALE"], "record": []},
    "no-qual": {"column": ["QUALIFIER_MISSING"], "record": []},
    "median": {"column": ["STAT_MISMATCH"], "record": []},
    "type-error": {"column": [], "record": ["TYPE_MISMATCH"]},
}

SCENARIOS = tuple(HEADERS.keys())

_CSS_CLASS = {
    "sample_id": "col-sample",
    "dry_weight": "col-dry",
    "crude_protein": "col-protein",
    "moisture": "col-moisture",
}

# 场景级单元格覆写：(scenario, row_index, field) -> 文本
CELL_OVERRIDES: dict[tuple[str, int, str], str] = {
    ("type-error", 2, "dry_weight"): "仪器离线",
}


@dataclass(frozen=True)
class GeneratedSample:
    scenario: str
    html_path: Path
    truth_path: Path


def _rows_for(scenario: str) -> list[dict[str, str]]:
    rows = [dict(r) for r in BASELINE_ROWS]
    if scenario == "unit":
        for r in rows:
            r["dry_weight"] = f"{float(r['dry_weight']) * 1000:.1f}"
    if scenario == "label":
        # 湿重虚构数值：含水率更高，湿重明显大于干重
        wet = (2.640, 3.332, 2.037, 2.876)
        for r, value in zip(rows, wet):
            r["dry_weight"] = f"{value:.3f}"
    for (sc, idx, fld), text in CELL_OVERRIDES.items():
        if sc == scenario:
            rows[idx][fld] = text
    return rows


def _render_html(scenario: str) -> str:
    headers = HEADERS[scenario]
    rows = _rows_for(scenario)
    banner = (
        '<p class="deco-banner">实验室信息系统 · 测量数据公开接口（虚构演示数据）</p>'
        if scenario == "restyle"
        else '<p class="deco-banner">实验室信息系统 · 测量数据页面（虚构演示数据）</p>'
    )
    th = "".join(
        f'<th class="{_CSS_CLASS[key]}" scope="col">{headers[key]}</th>'
        for key in ("sample_id", "dry_weight", "crude_protein", "moisture")
    )
    trs = []
    for row in rows:
        tds = "".join(
            f'<td class="{_CSS_CLASS[key]}">{row[key]}</td>'
            for key in ("sample_id", "dry_weight", "crude_protein", "moisture")
        )
        trs.append(f"      <tr>{tds}</tr>")
    theme = "theme-new" if scenario == "restyle" else "theme-classic"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{SCENARIO_TITLES[scenario]}</title>
  <style>
    body {{ font-family: "PingFang SC", "Microsoft YaHei", sans-serif; margin: 24px;
           background: {("#f4f7fb" if scenario == "restyle" else "#ffffff")}; }}
    h1 {{ font-size: 20px; color: {("#1f5fbf" if scenario == "restyle" else "#222")}; }}
    .deco-banner {{""" + ("""
      background: linear-gradient(90deg, #1f5fbf, #58a6ff); color: #fff;
      padding: 8px 12px; border-radius: 8px; font-size: 13px;
""" if scenario == "restyle" else """
      color: #666; font-size: 13px;
""") + f"""}}
    table#measure {{ border-collapse: collapse; width: 100%; max-width: 900px;
                     box-shadow: {("0 2px 10px rgba(0,0,0,.12)" if scenario == "restyle" else "none")}; }}
    table#measure th, table#measure td {{ border: 1px solid #bbb; padding: 8px 10px; text-align: right; }}
    table#measure th {{ background: {("#e8f0fe" if scenario == "restyle" else "#f0f0f0")}; }}
    table#measure td.col-sample, table#measure th.col-sample {{ text-align: left; }}
    .{theme} {{ padding: 4px; }}
  </style>
</head>
<body class="{theme}">
  <h1>{SCENARIO_TITLES[scenario]}</h1>
  {banner}
  <table id="measure" aria-label="样品测量台账">
    <thead>
      <tr>{th}</tr>
    </thead>
    <tbody>
{chr(10).join(trs)}
    </tbody>
  </table>
  <p class="footnote">数据来源：青岚大学材料表征实验室（本页全部为虚构演示数据）</p>
</body>
</html>
"""


def _build_truth(scenario: str) -> dict:
    rows = _rows_for(scenario)
    expected = EXPECTED_VIOLATIONS[scenario]
    if expected["column"]:
        expected_published = 0                       # 列头级违规隔离整列全部记录
    elif expected["record"]:
        expected_published = len(rows) - 1          # 记录级违规只隔离对应行
    else:
        expected_published = len(rows)
    return {
        "scenario": scenario,
        "title": SCENARIO_TITLES[scenario],
        "note": "独立预期真值，仅供测试断言；采集器不得读取本文件。",
        "selectors_preserved": True,
        "rows": rows,
        "expected_violations": expected,
        "expected_row_count": len(rows),
        "expected_published_rows": expected_published,
    }


def generate_all(output_dir: str | Path) -> list[GeneratedSample]:
    out = Path(output_dir)
    samples_dir = out / "samples"
    truth_dir = out / "truth"
    samples_dir.mkdir(parents=True, exist_ok=True)
    truth_dir.mkdir(parents=True, exist_ok=True)

    generated: list[GeneratedSample] = []
    for scenario in SCENARIOS:
        sdir = samples_dir / scenario
        sdir.mkdir(parents=True, exist_ok=True)
        html_path = sdir / "index.html"
        html_path.write_text(_render_html(scenario), encoding="utf-8")

        truth_path = truth_dir / f"{scenario}.json"
        truth_path.write_text(
            json.dumps(_build_truth(scenario), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        generated.append(GeneratedSample(scenario, html_path, truth_path))
    return generated


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="生成样例源站页面与独立真值（虚构数据）")
    parser.add_argument("--out", default="generated", help="输出目录（默认 generated）")
    args = parser.parse_args()
    items = generate_all(args.out)
    for item in items:
        print(f"[generated] {item.scenario}: {item.html_path} | truth: {item.truth_path}")


if __name__ == "__main__":
    main()
