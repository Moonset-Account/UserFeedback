"""采集器测试：只经公开 HTTP / 文件接口，传输失败显式化。"""

from __future__ import annotations

import pytest

from classifier.engine import classify_response
from collector import FetchError, fetch, fetch_with_browser, fetch_with_file
from tests.conftest import EXPECTED


def test_http_collect_then_classify(source_server):
    """真实 HTTP 采集（正常页）后判定为 VALID。"""
    result_page = fetch(f"{source_server}/scenarios/normal")
    assert result_page.ok is True
    assert result_page.status_code == 200
    decision = classify_response(result_page)
    assert decision.category == "VALID"


def test_http_collect_multilingual_empty(source_server):
    for sid in ("empty-zh", "empty-en", "empty-ja"):
        page = fetch(f"{source_server}/scenarios/{sid}")
        assert classify_response(page).category == "EMPTY"


def test_transport_reset_is_fetch_failure_not_empty(source_server):
    """验收点：连接重置必须产生 FETCH_FAILED，绝不能落入 EMPTY。"""
    with pytest.raises(FetchError):
        fetch(f"{source_server}/__transport/reset", timeout=5)

    # 与合法空结果并排比较，类别必须不同
    empty_page = fetch(f"{source_server}/scenarios/empty-zh")
    assert classify_response(empty_page).category == "EMPTY"


def test_file_mode_uses_exported_samples(samples_dir):
    """文件模式：读取生成器导出的 HTML。"""
    page = fetch_with_file(samples_dir / "maintenance-zh.html")
    assert page.fetch_mode == "file"
    assert classify_response(page).category == "MAINTENANCE"


def test_file_mode_missing_path_raises(tmp_path):
    with pytest.raises(FetchError):
        fetch_with_file(tmp_path / "not-exist.html")


def test_unknown_mode_rejected():
    with pytest.raises(FetchError):
        fetch("http://example.com", mode="carrier-pigeon")


def test_browser_mode_rejects_file_target(tmp_path):
    with pytest.raises(FetchError):
        fetch_with_browser(tmp_path.as_uri())


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("playwright") is None,
    reason="未安装 Playwright，浏览器用例跳过",
)
def test_browser_collect_rendered_dom(source_server):
    """Playwright 无头浏览器采集：拿到渲染后 DOM 并正确判定。"""
    page = fetch_with_browser(f"{source_server}/scenarios/error-en", timeout=15)
    assert page.fetch_mode == "browser"
    assert page.status_code == 200
    decision = classify_response(page)
    assert decision.category == "ERROR_PAGE"


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("playwright") is None,
    reason="未安装 Playwright，浏览器用例跳过",
)
def test_browser_and_http_agree_on_all(source_server):
    """浏览器与 HTTP 两种模式在全部语义场景上结论一致。"""
    for sid, expected in EXPECTED.items():
        http_page = fetch(f"{source_server}/scenarios/{sid}")
        browser_page = fetch_with_browser(
            f"{source_server}/scenarios/{sid}", timeout=15
        )
        assert classify_response(http_page).category == expected
        assert classify_response(browser_page).category == expected
