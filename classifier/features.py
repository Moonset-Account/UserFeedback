"""页面结构与内容特征提取。

提取结果只描述“页面上客观存在什么”，不做类别结论；
结论与权重由 engine.py 组合，保证每条证据可回溯到具体区域与片段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from . import markers

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
# 参与闭合检查的主要容器标签
BLOCK_TAGS = {
    "html", "head", "body", "header", "footer", "nav", "main",
    "section", "article", "div", "ul", "ol", "li", "table",
    "thead", "tbody", "tr", "td", "th", "form", "aside", "span",
    "p", "h1", "h2", "h3", "h4",
}
_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]*)[^>]*?(/?)>")


@dataclass
class MarkerHit:
    phrase: str
    region: str          # 命中的区域标识；"incidental" 表示仅在正文内容中偶然出现
    excerpt: str


@dataclass
class PageFeatures:
    url: str
    title: str
    lang: str
    nav_source: str
    landmarks: dict[str, bool]
    doc_card_count: int
    table_data_rows: int
    main_text_len: int
    skeleton_count: int
    total_block_count: int
    unclosed_tags: list[str]
    truncation_marker: str
    footer_present: bool
    region_texts: dict[str, str]
    marker_hits: dict[str, list[MarkerHit]] = field(default_factory=dict)
    incidental_hits: dict[str, list[MarkerHit]] = field(default_factory=dict)
    visible_text: str = ""
    content_type: str = "text/html; charset=utf-8"

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "lang": self.lang,
            "nav_source": self.nav_source,
            "landmarks": self.landmarks,
            "doc_card_count": self.doc_card_count,
            "table_data_rows": self.table_data_rows,
            "main_text_len": self.main_text_len,
            "skeleton_count": self.skeleton_count,
            "total_block_count": self.total_block_count,
            "unclosed_tags": self.unclosed_tags,
            "truncation_marker": self.truncation_marker,
            "footer_present": self.footer_present,
            "region_texts": {k: _clip(v) for k, v in self.region_texts.items()},
            "marker_hits": {
                k: [vars(h) for h in v] for k, v in self.marker_hits.items()
            },
            "incidental_hits": {
                k: [vars(h) for h in v] for k, v in self.incidental_hits.items()
            },
        }


def _clip(text: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _scan_unclosed(raw_html: str) -> list[str]:
    """用简单栈扫描器统计未闭合的容器标签（不依赖渲染器，对截断页有效）。"""
    stack: list[str] = []
    for m in _TAG_RE.finditer(raw_html):
        closing, name, self_closing = m.group(1), m.group(2).lower(), m.group(3)
        if name not in BLOCK_TAGS:
            continue
        if closing:
            # 容错：向上找最近的同名开标签，弹出其间元素
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == name:
                    del stack[i:]
                    break
            continue
        if name in VOID_TAGS or self_closing:
            continue
        stack.append(name)
    return stack


def _region_of(tag) -> str:
    """沿祖先链查找最近的可识别区域 id / class 标识。

    不能在第一个带 class 的元素就返回 —— 例如段落本身带 doc-card 类时，
    会遮蔽外层真正的 access/error 区域容器。
    """
    node = tag
    seen = 0
    while node is not None and seen < 12 and getattr(node, "name", None):
        nid = (node.get("id") or "").lower()
        classes = " ".join(node.get("class") or []).lower()
        token = nid or classes
        if token and _classify_region(token) != "incidental":
            return token
        node = node.parent
        seen += 1
    # 回退：返回第一个非空标识（保持 excerpt 区域信息可读）
    node = tag
    seen = 0
    while node is not None and seen < 12 and getattr(node, "name", None):
        nid = (node.get("id") or "").lower()
        classes = " ".join(node.get("class") or []).lower()
        token = nid or classes
        if token:
            return token
        node = node.parent
        seen += 1
    return "incidental"


def _classify_region(region_token: str) -> str:
    for hint in markers.MAINTENANCE_REGION_HINTS:
        if hint in region_token:
            return "maintenance"
    for hint in markers.EMPTY_REGION_HINTS:
        if hint in region_token:
            return "empty"
    for hint in markers.ACCESS_REGION_HINTS:
        if hint in region_token:
            return "access"
    for hint in markers.ERROR_REGION_HINTS:
        if hint in region_token:
            return "error"
    if "doc-card" in region_token or "doc-list" in region_token or "doc-table" in region_token:
        return "content"
    if "main" in region_token or "content" in region_token or "article" in region_token:
        return "main"
    return "incidental"


def _scan_marker_table(
    soup: BeautifulSoup,
    table: dict[str, list[str]],
    semantic: str,
    scope_regions: set[str],
) -> tuple[list[MarkerHit], list[MarkerHit]]:
    """区域命中与偶然命中分开记录 —— 单关键词不能决定分类的关键机制。"""
    scoped: list[MarkerHit] = []
    incidental: list[MarkerHit] = []
    phrases = markers.all_markers(table)
    for phrase in phrases:
        for node in soup.find_all(string=re.compile(re.escape(phrase), re.IGNORECASE)):
            parent = node.parent
            if parent is None:
                continue
            raw_region = _region_of(parent)
            region_kind = _classify_region(raw_region)
            hit = MarkerHit(
                phrase=phrase,
                region=raw_region,
                excerpt=_clip(node),
            )
            if region_kind in scope_regions:
                scoped.append(hit)
            else:
                incidental.append(hit)
    return scoped, incidental


def extract_features(
    html: str,
    url: str = "",
    content_type: str = "text/html; charset=utf-8",
    nav_source_hint: str = "",
) -> PageFeatures:
    soup = BeautifulSoup(html or "", "html.parser")

    title = soup.title.get_text(strip=True) if soup.title else ""
    lang = (soup.html.get("lang", "") if soup.html else "") or "unknown"

    nav_source = nav_source_hint
    meta_nav = soup.find("meta", attrs={"name": "x-nav-source"})
    if meta_nav and meta_nav.get("content"):
        nav_source = meta_nav["content"].strip()

    landmarks = {
        "header": bool(soup.find("header")),
        "nav": bool(soup.find("nav")),
        "breadcrumb": bool(soup.select_one(".breadcrumb, #breadcrumb, nav[aria-label=breadcrumb]")),
        "main": bool(soup.find("main")),
        "footer": bool(soup.find("footer")),
        "doc_list": bool(soup.select_one("#doc-list, .doc-list")),
    }

    doc_cards = soup.select(".doc-card")
    table_rows = soup.select("table.doc-table tbody tr") or soup.select("table tbody tr")

    main_node = soup.find("main") or soup.body or soup
    main_text = re.sub(r"\s+", " ", main_node.get_text(" ", strip=True))
    visible_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    skeletons = soup.select(
        "[class*=skeleton], .placeholder-block, .loading-placeholder"
    )
    blocks = soup.find_all(["div", "section", "article", "main", "ul", "table", "form"])

    unclosed = _scan_unclosed(html or "")

    truncation = ""
    for note in markers.TRUNCATION_MARKERS:
        if note in (html or ""):
            truncation = note
            break

    region_texts: dict[str, str] = {}
    for key, selector in (
        ("header", "header"),
        ("breadcrumb", ".breadcrumb"),
        ("maintenance_banner", ".maintenance-banner, #maintenance, .maintain-banner"),
        ("empty_state", "#empty-state, .empty-block, .no-result"),
        ("access_notice", ".login-gate, .access-notice, .permission-tip, .captcha"),
        ("error_panel", ".error-panel, .error-page, .app-error"),
        ("footer", "footer"),
    ):
        node = soup.select_one(selector)
        if node:
            region_texts[key] = _clip(node.get_text(" ", strip=True))

    marker_hits: dict[str, list[MarkerHit]] = {}
    incidental_hits: dict[str, list[MarkerHit]] = {}
    scan_specs = [
        ("empty", markers.EMPTY_MARKERS, {"empty"}),
        ("maintenance", markers.MAINTENANCE_MARKERS, {"maintenance"}),
        ("access", markers.ACCESS_MARKERS, {"access"}),
        ("error", markers.ERROR_MARKERS, {"error"}),
    ]
    for semantic, table, scope in scan_specs:
        scoped, incident = _scan_marker_table(soup, table, semantic, scope)
        if scoped:
            marker_hits[semantic] = scoped
        if incident:
            incidental_hits[semantic] = incident

    parsed = urlparse(url)
    return PageFeatures(
        url=url,
        title=title,
        lang=lang,
        nav_source=nav_source or (parsed.path if parsed.path else ""),
        landmarks=landmarks,
        doc_card_count=len(doc_cards),
        table_data_rows=len(table_rows),
        main_text_len=len(main_text),
        skeleton_count=len(skeletons),
        total_block_count=max(1, len(blocks)),
        unclosed_tags=unclosed,
        truncation_marker=truncation,
        footer_present=landmarks["footer"],
        region_texts=region_texts,
        marker_hits=marker_hits,
        incidental_hits=incidental_hits,
        visible_text=visible_text,
        content_type=content_type,
    )
