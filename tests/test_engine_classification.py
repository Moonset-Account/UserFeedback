"""引擎分类测试：独立预期表 vs 实际判定（文件模式，离线可跑）。"""

from __future__ import annotations

import json
from pathlib import Path

from classifier.engine import FetchInput, classify_response
from tests.conftest import EXPECTED, TRANSPORT_EXPECTED


def _classify_file(path: Path):
    html = path.read_text(encoding="utf-8")
    return classify_response(FetchInput(html=html, url=path.name))


def test_all_semantic_scenarios_match_independent_expectations(samples_dir):
    """21 个语义场景全部命中独立预期（含多语言空结果、残缺、歧义）。"""
    seen = set()
    for path in sorted(samples_dir.glob("*.html")):
        sid = path.stem
        assert sid in EXPECTED, f"场景 {sid} 缺少独立预期声明"
        result = _classify_file(path)
        assert result.category == EXPECTED[sid], (
            f"{sid}: 得到 {result.category}，预期 {EXPECTED[sid]}，"
            f"隔离原因={result.isolation_reasons}"
        )
        seen.add(sid)
    assert seen == set(EXPECTED), f"预期覆盖不全，缺少：{set(EXPECTED) - seen}"


def test_maintenance_keyword_in_normal_body_not_misclassified(samples_dir):
    """验收点：含“维护”一词的正常正文不得被判为维护页。"""
    result = _classify_file(samples_dir / "normal-keyword.html")
    assert result.category == "VALID"
    # 且必须留下“偶然命中不算数”的证据，证明判定不是碰巧正确
    incidental = [e for e in result.evidences if e.code == "marker-maintenance-incidental"]
    assert incidental, "应记录维护词的偶然命中证据"
    assert incidental[0].weight < 0


def test_normal_page_with_maintenance_notice_stays_valid(samples_dir):
    """公告区含维护通知的正常资料页仍为 VALID。"""
    result = _classify_file(samples_dir / "normal.html")
    assert result.category == "VALID"
    assert result.confidence >= 0.8


def test_empty_results_are_never_fetch_failure(samples_dir):
    """验收点：合法空结果与采集失败必须分开 —— 空结果一律不是 FETCH_FAILED。"""
    for sid in ("empty-zh", "empty-en", "empty-ja"):
        result = _classify_file(samples_dir / f"{sid}.html")
        assert result.category == "EMPTY"
        assert result.category != "FETCH_FAILED"
        assert result.isolated is False


def test_multilingual_empty_markers(samples_dir):
    """多语言空结果：zh/en/ja 各自的明确标记都在空状态区域内被识别。"""
    for sid in ("empty-zh", "empty-en", "empty-ja"):
        result = _classify_file(samples_dir / f"{sid}.html")
        scoped = [e for e in result.evidences if e.code == "marker-empty"]
        assert scoped, f"{sid} 缺少区域化空结果标记证据"


def test_error_page_similar_to_normal_template(samples_dir):
    """验收点：与正常模板相似的错误页（共享 header/nav/footer）仍判 ERROR_PAGE。"""
    result = _classify_file(samples_dir / "error-zh.html")
    assert result.category == "ERROR_PAGE"
    # 结构地标完整，分类依据来自错误面板区域而非模板缺失
    struct_ok = [e for e in result.evidences if e.code == "struct-landmarks-ok"]
    assert struct_ok


def test_broken_truncation_has_hard_gate(samples_dir):
    """截断页：硬门控触发，证据含截断声明与未闭合标签。"""
    result = _classify_file(samples_dir / "broken-truncated.html")
    assert result.category == "BROKEN_TEMPLATE"
    assert result.broken_gate is True
    codes = {e.code for e in result.evidences}
    assert "gate-broken-template" in codes
    assert "struct-truncation-note" in codes
    # 置信封顶，保留人工复核空间
    assert result.confidence <= 0.90


def test_broken_unclosed_without_truncation_note(samples_dir):
    """无截断声明但标签未闭合，结构契约失败仍应判残缺。"""
    result = _classify_file(samples_dir / "broken-unclosed.html")
    assert result.category == "BROKEN_TEMPLATE"
    assert any(e.code == "struct-unclosed-tags" for e in result.evidences)


def test_broken_skeleton_ratio(samples_dir):
    result = _classify_file(samples_dir / "broken-skeleton.html")
    assert result.category == "BROKEN_TEMPLATE"
    assert any(e.code.startswith("struct-skeleton") for e in result.evidences)


def test_ambiguous_page_is_isolated_not_forced(samples_dir):
    """验收点：空标记与资料卡片同时存在时必须隔离，不能强行归类。"""
    result = _classify_file(samples_dir / "ambiguous.html")
    assert result.category == "UNKNOWN"
    assert result.isolated is True
    assert any("互斥" in r for r in result.isolation_reasons)
    assert any(e.code == "gate-semantic-contradiction" for e in result.evidences)


def test_thin_signal_page_isolated_by_confidence(samples_dir):
    """信号稀薄页：置信度低于下限时隔离。"""
    result = _classify_file(samples_dir / "uncertain-busy.html")
    assert result.category == "UNKNOWN"
    assert result.isolated is True


def test_http_200_alone_never_decides(samples_dir):
    """所有样例都是 HTTP 200（除 500 场景），但分类各不相同。"""
    categories = {
        sid: _classify_file(samples_dir / f"{sid}.html").category
        for sid in ("normal", "empty-zh", "maintenance-zh", "error-zh",
                    "access-login-zh", "broken-truncated")
    }
    assert len(set(categories.values())) >= 5


def test_500_status_with_maintenance_semantics(samples_dir):
    """非 200 状态码下语义判定仍以页面内容为准。"""
    result = _classify_file(samples_dir / "maintenance-500.html")
    assert result.category == "MAINTENANCE"


def test_every_decision_carries_evidence_and_bounds(samples_dir):
    """每个判定都必须带证据链与置信边界。"""
    for path in samples_dir.glob("*.html"):
        result = _classify_file(path)
        assert result.evidences, f"{path.name} 无证据"
        payload = result.to_dict()
        assert 0.0 <= payload["confidence_lower_bound"] <= payload["confidence"]
        assert payload["confidence"] <= payload["confidence_upper_bound"] <= 1.0


def test_results_are_deterministic(samples_dir):
    """同输入同规则，两次判定必须完全一致。"""
    path = samples_dir / "ambiguous.html"
    html = path.read_text(encoding="utf-8")
    r1 = classify_response(FetchInput(html=html))
    r2 = classify_response(FetchInput(html=html))
    assert r1.category == r2.category
    assert r1.to_dict()["scores"] == r2.to_dict()["scores"]


def test_ground_truth_matches_independent_expectations(runtime_dir):
    """交叉核对：独立预期表与生成器真值一致（真值仅在本测试中被读取）。"""
    truth_path = runtime_dir / "source_truth" / "ground_truth.json"
    truth = {r["scenario_id"]: r["expected_category"]
             for r in json.loads(truth_path.read_text(encoding="utf-8"))}
    for sid, expected in EXPECTED.items():
        assert truth[sid] == expected, f"独立预期与真值在 {sid} 上不一致"
    for sid, expected in TRANSPORT_EXPECTED.items():
        assert truth[sid] == expected
