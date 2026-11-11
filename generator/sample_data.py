# -*- coding: utf-8 -*-
"""虚构测量数据样例生成器（与采集器分离）。

产出：
- samples/source/  采集器可经规定接口读取的源文件（JSON/CSV/TXT）；
- samples/truth/   独立预期真值，仅测试断言使用，采集器代码不导入、不读取。

所有机构、样品编号、数值均为虚构。
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys

# 允许作为脚本直接运行（python generator/sample_data.py [target_dir]）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector.locales import NBSP, NNBSP

# 虚构机构与数据集标识
DATASET_ID = "FIC-LAB-2026-NONPROD"

# 主数据集：覆盖验收要求的全部形态
# 字段：id, analyte, sample_code, raw_value（原文，手工按各地区格式构造）
MAIN_ROWS = [
    # ---- en_US：点小数 + 逗号分组 + 普通文本 ----
    ("m01", "Lead (Pb)",       "FIC-S-001", "12.5 mg/L"),
    ("m02", "Cadmium (Cd)",    "FIC-S-002", "1,234.56 mg/L"),
    ("m03", "Arsenic (As)",    "FIC-S-003", "7.8E-3 mg/L"),
    ("m04", "Mercury (Hg)",    "FIC-S-004", "0.042(7) mg/L"),
    ("m05", "Chromium (Cr)",   "FIC-S-005", "18.6 ± 0.4 mg/L"),
    ("m06", "Nickel (Ni)",     "FIC-S-006", "9.81 +0.12 -0.08 mg/L"),
    ("m07", "Copper (Cu)",     "FIC-S-007", "-"),
    ("m08", "Zinc (Zn)",       "FIC-S-008", "< 0.01 mg/L"),
    ("m09", "pH",              "FIC-S-009", "6.8 – 7.2 pH"),
    ("m10", "Nitrate (NO3)",   "FIC-S-010", "25.3 zz"),
    # ---- de_DE：逗号小数 + 点分组（源声明 locale=de_DE） ----
    ("m11", "Lead (Pb)",       "FIC-S-011", "1.234,56 mg/L"),
    ("m12", "Cadmium (Cd)",    "FIC-S-012", "0,0078 mg/L"),
    ("m13", "Arsenic (As)",    "FIC-S-013", "1,234"),
    ("m14", "Mercury (Hg)",    "FIC-S-014", "12,5 ± 0,2 mg/L"),
    ("m15", "Chromium (Cr)",   "FIC-S-015", "−3,5 °C"),
    # ---- fr_FR：逗号小数 + 不换行空格分组 ----
    ("m16", "Nitrate (NO3)",   "FIC-S-016", f"1{NBSP}234,56 mg/L"),
    ("m17", "Nitrite (NO2)",   "FIC-S-017", f"7,8e-3 mg/L"),
    ("m18", "Sulfate (SO4)",   "FIC-S-018", f"42{NNBSP}mg/L"),
    # ---- sv_SE：逗号小数 + 窄不换行空格分组 ----
    ("m19", "Iron (Fe)",       "FIC-S-019", f"12{NNBSP}345,67 mg/L"),
    # ---- 更多缺失值形态 ----
    ("m20", "Manganese (Mn)",  "FIC-S-020", "N/A"),
    ("m21", "Boron (B)",       "FIC-S-021", "—"),
    # ---- 区间与检测限 ----
    ("m22", "Fluoride (F)",    "FIC-S-022", "0.10 ~ 0.15 mg/L"),
    ("m23", "Selenium (Se)",   "FIC-S-023", "> 50.0 ppb"),
    # ---- 非法单位与缺失混合 ----
    ("m24", "Barium (Ba)",     "FIC-S-024", "3.7 parsec"),
]

# 歧义重解释样例（und 报错；en_US / de_DE 给出不同结果）
AMBIGUITY_ROWS = ["1,234", "1.234", "1,234.5", "12.5", "1 234,5"]

# 演示按钮使用的小样本（正常 + 边界，目标 15 秒内完成）
DEMO_NORMAL = ["1,234.56 mg/L", "7.8E-3 mg/L", "18.6 ± 0.4 mg/L"]
DEMO_BOUNDARY = ["-", "< 0.01 mg/L", "25.3 zz", "1,234"]

# 更完整的验收场景（网页上的选择入口使用）
ACCEPTANCE_LOCALES = ["en_US", "de_DE", "fr_FR", "sv_SE", "und"]


def _source_dirs(base_dir: str) -> tuple[str, str]:
    src = os.path.join(base_dir, "source")
    truth = os.path.join(base_dir, "truth")
    os.makedirs(src, exist_ok=True)
    os.makedirs(truth, exist_ok=True)
    return src, truth


def generate(base_dir: str) -> dict[str, list[str]]:
    src, truth = _source_dirs(base_dir)
    written: dict[str, list[str]] = {"source": [], "truth": []}

    # 1) 主 JSON 源（声明 locale=en_US；de/fr 行通过独立分语言文件提供，
    #    主文件内保留全部原文，HTML 页面用 meta 声明 en_US）
    json_payload = {
        "dataset": DATASET_ID,
        "locale": "en_US",
        "note": "虚构数据，仅用于非生产环境采集演示",
        "rows": [
            {"id": rid, "analyte": analyte, "sample_code": code, "value": raw}
            for rid, analyte, code, raw in MAIN_ROWS
        ],
    }
    p = os.path.join(src, "measurements_en.json")
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(json_payload, f, ensure_ascii=False, indent=2)
    written["source"].append(p)

    # 2) 分语言源：de_DE（JSON 内显式声明 locale）
    de_rows = [r for r in MAIN_ROWS if r[0] in {"m11", "m12", "m13", "m14", "m15"}]
    p = os.path.join(src, "measurements_de.json")
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump({
            "dataset": DATASET_ID, "locale": "de_DE",
            "rows": [{"id": r[0], "analyte": r[1], "sample_code": r[2], "value": r[3]}
                     for r in de_rows],
        }, f, ensure_ascii=False, indent=2)
    written["source"].append(p)

    # 3) CSV 源（表头 locale 列声明 en_US；含不换行空格单元格）
    p = os.path.join(src, "measurements.csv")
    with io.open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "analyte", "sample_code", "value", "locale"])
        for rid, analyte, code, raw in MAIN_ROWS[:18]:
            w.writerow([rid, analyte, code, raw, "en_US"])
    written["source"].append(p)

    # 4) TXT 源（每行一条，无地区声明 → und 规则）
    p = os.path.join(src, "measurements_undeclared.txt")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(f"# {DATASET_ID} 虚构数据；本文件不声明地区，采集器按 und 处理\n")
        for line in AMBIGUITY_ROWS:
            f.write(line + "\n")
    written["source"].append(p)

    # 5) 歧义重解释输入集
    p = os.path.join(src, "ambiguity_lines.txt")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(AMBIGUITY_ROWS) + "\n")
    written["source"].append(p)

    # 6) 独立真值（仅测试使用；采集器不得读取本目录）
    truth_payload = {
        "dataset": DATASET_ID,
        "warning": "独立预期真值，仅用于测试断言；采集器代码禁止读取本文件",
        "main_rows": [
            {"id": rid, "raw_value": raw} for rid, _a, _c, raw in MAIN_ROWS
        ],
        "ambiguity_rows": AMBIGUITY_ROWS,
        "demo": {"normal": DEMO_NORMAL, "boundary": DEMO_BOUNDARY},
        "acceptance_locales": ACCEPTANCE_LOCALES,
    }
    p = os.path.join(truth, "expected_index.json")
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(truth_payload, f, ensure_ascii=False, indent=2)
    written["truth"].append(p)

    return written


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    base = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
    out = generate(base)
    print(json.dumps(out, ensure_ascii=False, indent=2))
