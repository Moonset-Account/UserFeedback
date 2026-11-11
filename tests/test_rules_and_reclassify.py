"""自定义规则与复判历史测试。"""

from __future__ import annotations

import json

import pytest

from classifier import RuleError
from classifier.custom_rules import load_rules
from classifier.engine import FetchInput, classify_response


def test_invalid_rule_rejected(tmp_path):
    rule_file = tmp_path / "rules.json"
    rule_file.write_text('[{"code": "x"}]', encoding="utf-8")
    with pytest.raises(RuleError, match="phrase"):
        load_rules(rule_file)


def test_bad_json_rejected(tmp_path):
    rule_file = tmp_path / "rules.json"
    rule_file.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuleError):
        load_rules(rule_file)


def test_illegal_category_rejected(tmp_path):
    rule_file = tmp_path / "rules.json"
    rule_file.write_text(json.dumps([
        {"code": "r1", "phrase": "x", "category": "FETCH_FAILED", "weight": 0.5}
    ]), encoding="utf-8")
    with pytest.raises(RuleError):
        load_rules(rule_file)


def test_weight_bounds_enforced(tmp_path):
    rule_file = tmp_path / "rules.json"
    rule_file.write_text(json.dumps([
        {"code": "r1", "phrase": "x", "category": "EMPTY", "weight": 5.0}
    ]), encoding="utf-8")
    with pytest.raises(RuleError):
        load_rules(rule_file)


def test_rule_with_region_miss_does_not_fire(samples_dir, tmp_path):
    """规则的 region_contains 不匹配时不得产生证据。"""
    rule_file = tmp_path / "rules.json"
    rule_file.write_text(json.dumps([{
        "code": "r-region", "name": "区域不匹配",
        "phrase": "正在为您检索相关资料",
        "region_contains": "maintenance-banner",
        "category": "MAINTENANCE", "weight": 0.6,
    }]), encoding="utf-8")
    rules = load_rules(rule_file)
    html = (samples_dir / "uncertain-busy.html").read_text(encoding="utf-8")
    result = classify_response(FetchInput(html=html), rules=rules)
    assert "r-region" not in result.custom_rule_codes


def test_negative_rule_can_vote_against_category(samples_dir, tmp_path):
    rule_file = tmp_path / "rules.json"
    rule_file.write_text(json.dumps([{
        "code": "r-neg", "name": "反对维护误判",
        "phrase": "维护",
        "category": "MAINTENANCE", "weight": -0.5,
    }]), encoding="utf-8")
    rules = load_rules(rule_file)
    html = (samples_dir / "normal-keyword.html").read_text(encoding="utf-8")
    result = classify_response(FetchInput(html=html), rules=rules)
    assert result.category == "VALID"
    assert "r-neg" in result.custom_rule_codes
    neg = next(e for e in result.evidences if e.code == "r-neg")
    assert neg.weight == -0.5


def test_reclassify_keeps_old_judgment_and_adds_new_basis(job_store, source_server):
    """验收点：复判保留旧判断，追加新依据与变更轨迹。"""
    # 初次运行隔离场景
    job = job_store.run(
        f"{source_server}/scenarios/uncertain-busy",
        label="忙碌页复验",
    )
    assert job["history"][-1]["category"] == "UNKNOWN"
    assert len(job["history"]) == 1

    # 保存补充规则后复判
    job_store.save_rules(json.dumps([{
        "code": "rule-busy-001",
        "name": "忙碌/未就绪页面识别",
        "phrase": "正在为您检索相关资料",
        "region_contains": "pending-tip",
        "category": "BROKEN_TEMPLATE",
        "weight": 0.6,
    }], ensure_ascii=False))
    rejudged = job_store.reclassify(job["job_id"], note="人工补充规则")

    assert len(rejudged["history"]) == 2
    v1, v2 = rejudged["history"]
    assert v1["category"] == "UNKNOWN"            # 旧判断原样保留
    assert v2["category"] == "BROKEN_TEMPLATE"    # 新依据生效
    assert v2["previous_category"] == "UNKNOWN"
    assert "rule-busy-001" in v2["rule_codes"]
    assert v2["note"] == "人工补充规则"

    # 复判不重新采集：快照与首次一致
    assert rejudged["fetch"]["html_snapshot"] == job["fetch"]["html_snapshot"]


def test_reclassify_fetch_failure_is_rejected(job_store, source_server):
    job = job_store.run(f"{source_server}/__transport/reset")
    assert job["history"][-1]["category"] == "FETCH_FAILED"
    with pytest.raises(RuleError):
        job_store.reclassify(job["job_id"])


def test_full_suite_separates_empty_and_fetch_failure(job_store, source_server):
    """完整跑一遍，EMPTY 与 FETCH_FAILED 是互不相交的两个集合。"""
    empty_job = job_store.run(f"{source_server}/scenarios/empty-zh")
    failed_job = job_store.run(f"{source_server}/__transport/reset")
    assert empty_job["history"][-1]["category"] == "EMPTY"
    assert failed_job["history"][-1]["category"] == "FETCH_FAILED"
    assert empty_job["fetch"]["ok"] is True
    assert failed_job["fetch"]["ok"] is False
