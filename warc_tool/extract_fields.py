"""HTML 研究资料字段提取。

仅依赖标准库（html.parser），针对受控样例站点的虚构页面。
每个字段同时返回取值证据（CSS 风格定位与原始片段），供结果卡片展示来源。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from .digests import sha256_hex

META_AUTHOR_KEYS = ("author", "dc.creator", "dcterms.creator", "article:author")
META_DATE_KEYS = (
    "dc.date", "dcterms.date", "article:published_time",
    "publication_date", "publish-date", "date",
)
META_DESC_KEYS = ("description", "dc.description", "dcterms.abstract", "summary")
META_KEYWORDS_KEYS = ("keywords", "article:tag", "dc.subject", "dcterms.subject")


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self._in_title = False
        self.meta: list[dict] = []
        self.h1: list[str] = []
        self.h2: list[str] = []
        self._heading_stack: list[str] = []
        self._skip_depth = 0
        self.body_parts: list[str] = []
        self.time_tags: list[dict] = []
        self.links: list[dict] = []
        self._current_href = None
        self._a_text: list[str] = []
        self._p_depth = 0
        self.paragraphs: list[str] = []
        self._p_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_d = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            self.meta.append({
                "name": (attrs_d.get("name") or attrs_d.get("property") or "").lower(),
                "content": attrs_d.get("content", ""),
                "itemprop": (attrs_d.get("itemprop") or "").lower(),
            })
        elif tag == "h1":
            self._heading_stack.append("h1")
        elif tag == "h2":
            self._heading_stack.append("h2")
        elif tag in ("script", "style", "noscript"):
            self._skip_depth += 1
        elif tag == "time":
            self.time_tags.append({"datetime": attrs_d.get("datetime", ""), "text": ""})
        elif tag == "a":
            self._current_href = attrs_d.get("href")
            self._a_text = []
        elif tag == "p":
            self._p_depth += 1
            self._p_text = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in ("h1", "h2") and self._heading_stack:
            self._heading_stack.pop()
        elif tag in ("script", "style", "noscript") and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "time" and self.time_tags:
            pass  # 文本在 handle_data 中累加
        elif tag == "a" and self._current_href is not None:
            self.links.append({
                "href": self._current_href,
                "text": "".join(self._a_text).strip(),
            })
            self._current_href = None
        elif tag == "p" and self._p_depth:
            self._p_depth -= 1
            text = "".join(self._p_text).strip()
            if text:
                self.paragraphs.append(re.sub(r"\s+", " ", text))
            self._p_text = []

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        if self._skip_depth:
            return
        if self._heading_stack and self._heading_stack[-1] == "h1":
            self.h1.append(data)
        elif self._heading_stack and self._heading_stack[-1] == "h2":
            self.h2.append(data)
        if self.time_tags and not self.time_tags[-1]["text"]:
            self.time_tags[-1]["text"] += data.strip()
        if self._current_href is not None:
            self._a_text.append(data)
        if self._p_depth:
            self._p_text.append(data)
        stripped = data.strip()
        if stripped:
            self.body_parts.append(stripped)


def _meta_value(parser: _PageParser, keys: tuple[str, ...]) -> tuple[str | None, str | None]:
    for meta in parser.meta:
        if meta["name"] in keys or meta["itemprop"] in keys:
            if meta["content"].strip():
                locator = f'meta[name="{meta["name"] or meta["itemprop"]}"]'
                return meta["content"].strip(), locator
    return None, None


def extract_fields(html_bytes: bytes, base_url: str, content_type: str | None = None) -> dict:
    """从 HTML 字节提取研究资料字段。

    返回结构::

        {"fields": {字段名: {value, evidence}}, "payload_sha256": ...,
         "body_text": ..., "links": [...], "parse_errors": [...]}
    """
    parse_errors: list[str] = []
    encoding = "utf-8"
    if content_type and "charset=" in content_type:
        encoding = content_type.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
    try:
        html = html_bytes.decode(encoding, errors="replace")
    except LookupError:
        parse_errors.append(f"未知字符集 {encoding!r}，回退 UTF-8")
        html = html_bytes.decode("utf-8", errors="replace")

    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # 样例页面受控，仍保证不抛出到流水线
        parse_errors.append(f"HTML 解析异常：{exc}")

    fields: dict[str, dict] = {}

    title = " ".join(t.strip() for t in parser.title_parts if t.strip()).strip()
    if title:
        fields["title"] = {
            "value": re.sub(r"\s+", " ", title),
            "evidence": {"locator": "title", "snippet": title[:120]},
        }
    elif parser.h1:
        h1_text = " ".join(t.strip() for t in parser.h1 if t.strip()).strip()
        fields["title"] = {
            "value": re.sub(r"\s+", " ", h1_text),
            "evidence": {"locator": "h1", "snippet": h1_text[:120]},
        }

    author, author_loc = _meta_value(parser, META_AUTHOR_KEYS)
    if author:
        fields["author"] = {
            "value": author,
            "evidence": {"locator": author_loc, "snippet": author[:120]},
        }

    published, date_loc = _meta_value(parser, META_DATE_KEYS)
    if not published and parser.time_tags:
        first_time = parser.time_tags[0]
        published = first_time["datetime"] or first_time["text"] or None
        date_loc = "time[datetime]" if first_time["datetime"] else "time"
    if published:
        fields["published"] = {
            "value": published,
            "evidence": {"locator": date_loc, "snippet": str(published)[:120]},
        }

    summary, summary_loc = _meta_value(parser, META_DESC_KEYS)
    if not summary and parser.paragraphs:
        summary = parser.paragraphs[0]
        summary_loc = "p:first-of-type"
    if summary:
        fields["summary"] = {
            "value": summary,
            "evidence": {"locator": summary_loc, "snippet": summary[:160]},
        }

    keywords, kw_loc = _meta_value(parser, META_KEYWORDS_KEYS)
    if keywords:
        tags = [t.strip() for t in re.split(r"[,，;；]", keywords) if t.strip()]
        fields["keywords"] = {
            "value": tags,
            "evidence": {"locator": kw_loc, "snippet": keywords[:160]},
        }

    body_text = "\n".join(parser.paragraphs) if parser.paragraphs else \
        " ".join(parser.body_parts)
    body_text = re.sub(r"[ \t]+", " ", body_text).strip()
    char_count = len(body_text)
    fields["body_char_count"] = {
        "value": char_count,
        "evidence": {"locator": "body>p", "snippet": body_text[:80]},
    }

    links = []
    seen = set()
    for link in parser.links:
        absolute = urljoin(base_url, link["href"])
        key = (absolute, link["text"])
        if key not in seen:
            seen.add(key)
            links.append({"url": absolute, "text": link["text"]})

    return {
        "fields": fields,
        "payload_sha256": sha256_hex(html_bytes),
        "body_text": body_text,
        "links": links,
        "headings": [re.sub(r"\s+", " ", " ".join(parser.h1).strip())],
        "parse_errors": parse_errors,
    }
