"""HTML 字段提取单元测试（含证据定位器与字符集回退）。"""
from __future__ import annotations

from warc_tool.extract_fields import extract_fields


HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>测试标题 A</title>
<meta name="author" content="作者乙">
<meta name="dc.date" content="2025-06-01">
<meta name="description" content="这是摘要文本">
<meta name="keywords" content="关键词一,关键词二，关键词三">
</head>
<body>
<article>
<h1>测试标题 A</h1>
<p><time datetime="2025-06-01">六月一日</time></p>
<p>第一段正文内容，用于字数统计。</p>
<p>第二段正文内容，<a href="relative/link">相对链接</a>与
<a href="https://other.test/x">绝对链接</a>。</p>
</article>
</body></html>""".encode("utf-8")


def test_all_fields_and_evidence():
    result = extract_fields(HTML, "http://x.test/article/1", "text/html; charset=utf-8")
    fields = result["fields"]
    assert fields["title"]["value"] == "测试标题 A"
    assert fields["title"]["evidence"]["locator"] == "title"
    assert fields["author"]["value"] == "作者乙"
    assert fields["published"]["value"] == "2025-06-01"
    assert fields["summary"]["value"] == "这是摘要文本"
    assert fields["keywords"]["value"] == ["关键词一", "关键词二", "关键词三"]
    assert fields["body_char_count"]["value"] > 0


def test_relative_link_resolved_with_base_url():
    result = extract_fields(HTML, "http://x.test/dir/page")
    urls = {link["url"] for link in result["links"]}
    assert "http://x.test/dir/relative/link" in urls
    assert "https://other.test/x" in urls


def test_payload_sha256_present():
    result = extract_fields(HTML, "http://x.test/a")
    assert len(result["payload_sha256"]) == 64


def test_unknown_charset_falls_back_to_utf8():
    result = extract_fields(HTML, "http://x.test/a", "text/html; charset=bogus-encoding")
    assert any("字符集" in e for e in result["parse_errors"])
    assert result["fields"]["title"]["value"] == "测试标题 A"


def test_minimal_html_does_not_raise():
    result = extract_fields("<html><body>仅正文</body></html>".encode("utf-8"),
                            "http://x.test/m")
    assert result["fields"]["body_char_count"]["value"] >= 2
    assert "title" not in result["fields"]


def test_script_style_content_excluded_from_body():
    html = ("<html><head><style>.x{color:red}</style></head>"
            "<body><p>可见正文</p><script>var a=1;</script></body></html>").encode("utf-8")
    result = extract_fields(html, "http://x.test/s")
    assert "var a=1" not in result["body_text"]
    assert "color:red" not in result["body_text"]
    assert "可见正文" in result["body_text"]
