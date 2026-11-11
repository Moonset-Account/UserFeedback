"""目录页解析器。

支持的协议子集（明确声明，超出子集直接抛 UnsupportedProtocolError）：

1. HTTP/HTTPS GET，响应体为以下两种形态之一：
   a) application/json：顶层对象，含
        - "items":      数组，元素至少含 "id"（字符串），可含 "title"/"url"/"detail_url"；
        - "next_cursor"：不透明字符串，非空表示存在下一页，空串/缺省表示末页。
      客户端不依赖任何 "total"/总数字段——目录规模预先未知，仅靠 next_cursor 翻页。
   b) text/html：页面含 ``<div data-role="catalog">``，其下每个条目为
        ``<article class="specimen" data-id="...">``，可含
        ``<a class="detail-link" href="...">``；
      翻页链接为 ``<a data-role="next-page" href="...">``，缺省该元素表示末页。
2. 字符编码以响应头/UTF-8 为准；不执行 JavaScript（需要 JS 时改用 collector.browser）。
3. 不支持：XML、嵌套分页、POST 翻页、需要鉴权的接口、非 200 响应（由 scanner 处理重试/报错）。

HTML 解析使用成熟开源库 BeautifulSoup4（内置 html.parser，无需系统级依赖）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

from bs4 import BeautifulSoup

SUPPORTED_CONTENT_TYPES = ("application/json", "text/html")


class UnsupportedProtocolError(ValueError):
    """响应不在上述受支持协议子集内。"""


@dataclass(frozen=True)
class ParsedRecord:
    specimen_id: str
    detail_url: str | None
    title: str | None


@dataclass(frozen=True)
class ParsedPage:
    records: list[ParsedRecord]
    next_url: str | None  # 绝对 URL；None 表示末页


def _coerce_record(raw: Any, base_url: str) -> ParsedRecord:
    if not isinstance(raw, dict) or "id" not in raw:
        raise UnsupportedProtocolError(f"JSON 条目缺少字符串 id 字段: {raw!r:.120}")
    sid = raw["id"]
    if not isinstance(sid, str) or not sid:
        raise UnsupportedProtocolError(f"JSON 条目 id 必须为非空字符串: {raw!r:.120}")
    detail = raw.get("detail_url") or raw.get("url")
    if detail is not None:
        if not isinstance(detail, str):
            raise UnsupportedProtocolError("detail_url/url 必须为字符串")
        detail = urljoin(base_url, detail)
    title = raw.get("title")
    if title is not None and not isinstance(title, str):
        title = str(title)
    return ParsedRecord(specimen_id=sid, detail_url=detail, title=title)


def parse_json_page(payload: bytes | str, base_url: str) -> ParsedPage:
    import json

    try:
        doc = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise UnsupportedProtocolError(f"响应不是合法 JSON: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        raise UnsupportedProtocolError('JSON 顶层必须为对象且含数组字段 "items"')

    records = [_coerce_record(item, base_url) for item in doc["items"]]
    cursor = doc.get("next_cursor")
    if cursor is None or cursor == "":
        next_url = None
    elif not isinstance(cursor, str):
        raise UnsupportedProtocolError("next_cursor 必须为字符串或为空")
    else:
        # 游标语义：不透明字符串。允许三种形态：
        #   绝对/相对 URL；"cursor=页码"；"cursor=页码&k=v" 形式的查询串片段。
        # 合并时替换（而非堆叠）base_url 中已有的同名参数，避免 cursor 重复。
        if cursor.startswith(("http://", "https://", "/")):
            next_url = urljoin(base_url, cursor)
        elif cursor.startswith("cursor="):
            next_url = _merge_query(base_url, cursor)
        else:
            raise UnsupportedProtocolError("无法识别的 next_cursor 形态")
    return ParsedPage(records=records, next_url=next_url)


def _merge_query(base_url: str, query_fragment: str) -> str:
    """把查询片段合并进 URL，同名参数以片段为准（其余参数如 dataset 保留）。"""
    parsed = urlparse(base_url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params.update(dict(parse_qsl(query_fragment, keep_blank_values=True)))
    return urlunparse(parsed._replace(query=urlencode(params)))


def parse_html_page(payload: bytes | str, base_url: str) -> ParsedPage:
    soup = BeautifulSoup(payload, "html.parser")
    catalog = soup.find(attrs={"data-role": "catalog"})
    if catalog is None:
        raise UnsupportedProtocolError('HTML 缺少 <div data-role="catalog"> 目录容器')

    records: list[ParsedRecord] = []
    for node in catalog.find_all("article", class_="specimen"):
        sid = node.get("data-id")
        if not sid or not isinstance(sid, str):
            raise UnsupportedProtocolError("HTML 条目 article.specimen 缺少 data-id")
        link = node.find("a", class_="detail-link")
        detail = urljoin(base_url, link["href"]) if link and link.get("href") else None
        # 标题优先取条目内 h3，缺失时退回 ID，避免误用链接自身文字
        heading = node.find("h3")
        title = heading.get_text(strip=True) if heading else sid
        records.append(ParsedRecord(specimen_id=sid, detail_url=detail, title=title))

    next_node = soup.find(attrs={"data-role": "next-page"})
    next_url = None
    if next_node is not None and next_node.get("href"):
        next_url = urljoin(base_url, next_node["href"])
    return ParsedPage(records=records, next_url=next_url)


def parse_page(body: bytes, content_type: str, url: str) -> ParsedPage:
    """按 Content-Type 分派到 JSON / HTML 解析器。"""
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype == "application/json":
        return parse_json_page(body, url)
    if ctype == "text/html":
        return parse_html_page(body, url)
    raise UnsupportedProtocolError(
        f"不支持的 Content-Type: {content_type!r}；仅支持 {SUPPORTED_CONTENT_TYPES}"
    )
