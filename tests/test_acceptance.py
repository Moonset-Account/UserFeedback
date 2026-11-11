# -*- coding: utf-8 -*-
"""完整验收场景：格式覆盖、地区切换重解释、缺失/检测限语义、采集接口、真值对照。"""
import json
import os

import pytest

from collector import tasks
from collector.fetchers import collect_file, collect_http, CollectorError
from collector.locales import NBSP, NNBSP


# 验收要求：至少四种格式 + 空白 + 破折号缺失 + 区间 + 非法单位
ACCEPTANCE_TEXTS = {
    "en_US": [
        "1,234.56 mg/L",       # 逗号分组 + 点小数
        "7.8E-3 mg/L",         # 科学计数法
        "0.042(7) mg/L",       # 括号不确定度
        "18.6 ± 0.4 mg/L",     # 对称不确定度
        "9.81 +0.12 -0.08 mg/L",  # 非对称不确定度
    ],
    "de_DE": ["1.234,56 mg/L", "0,0078 mg/L", "12,5 ± 0,2 mg/L", "−3,5 °C"],
    "fr_FR": [f"1{NBSP}234,56 mg/L", "7,8e-3 mg/L", f"42{NBSP}mg/L"],
    "sv_SE": [f"12{NNBSP}345,67 mg/L"],
}
MISSING_TEXTS = ["-", "—", "N/A"]
RANGE_TEXTS = ["6.8 – 7.2 pH", "0.10 ~ 0.15 mg/L"]
ILLEGAL_UNIT_TEXTS = ["25.3 zz", "3.7 parsec"]


def test_at_least_four_distinct_formats_all_parse():
    parsed = []
    for loc, texts in ACCEPTANCE_TEXTS.items():
        for t in texts:
            r = tasks.run_text_input([t], loc)
            assert r.error_count == 0, (t, r.records[0]["error"])
            parsed.append(r.records[0])
    # 明确覆盖 5 类格式
    statuses = {(p["raw_text"], p["status"]) for p in parsed}
    assert any("E" in s[0] or "e" in s[0] for s in statuses)        # 科学计数法
    assert any("±" in s[0] for s in statuses)                       # 不确定度
    assert any("(" in s[0] for s in statuses)                       # 括号记法
    assert any(NBSP in s[0] or NNBSP in s[0] for s in statuses)     # 不换行空格
    assert any("," in s[0] for s in statuses)                       # 分组/逗号小数


def test_dash_missing_values_never_zero():
    for loc in ["en_US", "de_DE", "und"]:
        r = tasks.run_text_input(MISSING_TEXTS, loc)
        assert r.missing_count == len(MISSING_TEXTS)
        for rec in r.records:
            assert rec["status"] == "missing"
            assert rec["value"] is None
            assert rec["qualifier"] == "missing"


def test_ranges_preserved_with_endpoint_values():
    r = tasks.run_text_input(RANGE_TEXTS, "en_US")
    assert all(rec["status"] == "range" for rec in r.records)
    assert r.records[0]["value"] == "6.8" and r.records[0]["value_end"] == "7.2"


def test_illegal_units_reported_not_silently_accepted():
    r = tasks.run_text_input(ILLEGAL_UNIT_TEXTS, "en_US")
    assert r.error_count == 2
    for rec in r.records:
        assert rec["error"]["code"] == "ILLEGAL_UNIT"
        assert rec["value"] is not None  # 数值保留，单位拒绝


def test_below_lod_never_treated_as_exact_equality():
    r = tasks.run_text_input(["< 0.01 mg/L", "> 50.0 ppb"], "en_US")
    below, above = r.records
    assert below["status"] == "below_LOD"
    assert below["qualifier"] == "below_detection_limit"
    assert any("禁止当作精确" in s or "不是精确等值" in s for s in below["steps"])
    assert above["status"] == "above_LOD"


def test_reinterpret_same_raw_text_across_locales():
    raw = ["1,234"]
    und = tasks.reinterpret(raw, "und")
    assert und.records[0]["status"] == "error"
    assert und.records[0]["error"]["code"] == "AMBIGUOUS_LOCALE"

    us = tasks.reinterpret(raw, "en_US")
    de = tasks.reinterpret(raw, "de_DE")
    assert us.records[0]["value"] == "1234"
    assert de.records[0]["value"] == "1.234"
    # 原文在两种解释下完全一致
    assert us.records[0]["raw_text"] == de.records[0]["raw_text"] == "1,234"


def test_reinterpret_full_ambiguity_set_all_locales(samples_dir):
    txt = os.path.join(samples_dir, "source", "ambiguity_lines.txt")
    outcomes = {}
    for loc in ["und", "en_US", "de_DE", "fr_FR", "sv_SE"]:
        r = tasks.run_file(txt, None if loc == "und" else loc)
        outcomes[loc] = r
    # und 下含逗号/空格的行必须报错，无任何猜测
    und_records = outcomes["und"].records
    assert und_records[0]["error"]["code"] == "AMBIGUOUS_LOCALE"
    assert all(rec["status"] in ("error",) for rec in und_records
               if any(ch in rec["raw_text"] for ch in (",", NBSP, NNBSP)))
    # 无歧义的点小数在 und 下仍可解析
    plain = next(rec for rec in und_records if rec["raw_text"] == "12.5")
    assert plain["status"] == "ok" and plain["value"] == "12.5"


def test_ascii_space_grouping_accepted_only_for_space_locales():
    # fr/sv 协议子集接受普通空格分组；数字-单位之间的空格仍正确作为边界
    fr = tasks.run_text_input(["1 234,5", "42 mg/L"], "fr_FR")
    assert fr.records[0]["value"] == "1234.5"
    assert fr.records[1]["unit"] == "mg/L"
    # de/und/en_US 不允许空格分组，必须显式报错而非猜测拼接
    for loc in ["de_DE", "und", "en_US"]:
        r = tasks.run_text_input(["1 234,5"], loc)
        rec = r.records[0]
        assert rec["status"] == "error"
        assert rec["error"]["code"] in (
            "SPACE_GROUP_NOT_ALLOWED", "AMBIGUOUS_LOCALE")


def test_file_collection_json_and_truth_alignment(samples_dir, truth):
    path = os.path.join(samples_dir, "source", "measurements_en.json")
    records = collect_file(path)
    # 采集到的原文必须与独立真值逐行一致（真值只在测试里读取）
    truth_map = {row["id"]: row["raw_value"] for row in truth["main_rows"]}
    assert len(records) == len(truth["main_rows"])
    for rec in records:
        assert rec.raw_value == truth_map[rec.row_id]
        assert rec.declared_locale == "en_US"  # 源声明被采集层采纳


def test_file_collection_csv_and_txt(samples_dir):
    csv_records = collect_file(os.path.join(samples_dir, "source", "measurements.csv"))
    assert len(csv_records) == 18
    txt_records = collect_file(
        os.path.join(samples_dir, "source", "measurements_undeclared.txt"))
    # TXT 无声明 → und
    assert all(r.declared_locale == "und" for r in txt_records)


def test_german_source_declaration_used_without_guessing(samples_dir):
    r = tasks.run_file(os.path.join(samples_dir, "source", "measurements_de.json"), None)
    by_id = {rec["source"]["row_id"]: rec for rec in r.records}
    assert by_id["m11"]["value"] == "1234.56"     # 1.234,56
    assert by_id["m12"]["value"] == "0.0078"      # 0,0078
    assert by_id["m13"]["value"] == "1.234"       # 1,234 按德语小数解释
    assert r.locale_code == "de_DE"


def test_full_main_dataset_expected_semantics(samples_dir):
    r = tasks.run_file(os.path.join(samples_dir, "source", "measurements_en.json"), None)
    by_id = {rec["source"]["row_id"]: rec for rec in r.records}
    assert by_id["m01"]["value"] == "12.5"
    assert by_id["m02"]["value"] == "1234.56"
    assert by_id["m03"]["value"] == "0.0078"
    assert by_id["m07"]["status"] == "missing" and by_id["m07"]["value"] is None
    assert by_id["m08"]["status"] == "below_LOD"
    assert by_id["m09"]["status"] == "range"
    assert by_id["m10"]["error"]["code"] == "ILLEGAL_UNIT"
    # 来源位置被保存
    assert by_id["m01"]["source"]["location"]["json_path"].startswith("$.rows[")


def test_http_collection_against_local_source(server_base_url):
    records = collect_http(f"{server_base_url}/source/measurements.json")
    assert records and records[0].declared_locale == "en_US"
    html_records = collect_http(f"{server_base_url}/source/measurements.html")
    assert html_records[0].source_type == "http_html"
    assert "selector" in html_records[0].location
    csv_records = collect_http(f"{server_base_url}/source/measurements.csv")
    assert len(csv_records) == len(records)


def test_collection_error_is_structured_for_missing_file():
    r = tasks.run_file("/nonexistent/fictional/path.json", "en_US")
    assert r.collection_error["code"] == "COLLECT_FAILED"


def test_collector_never_reads_truth_directory(samples_dir):
    """结构性保证：采集器模块代码不引用 samples/truth 路径。"""
    collector_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "collector")
    for name in os.listdir(collector_dir):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(collector_dir, name), encoding="utf-8") as f:
            text = f.read()
        assert "truth" not in text.lower(), f"采集器代码 {name} 不得引用真值目录"
