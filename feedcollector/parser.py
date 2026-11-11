"""RSS 2.0 / Atom 1.0 解析器。

协议子集（详见 README）：
- RSS 2.0：channel/title/link/description；item 的 title/link/guid/pubDate/
  description/author，enclosure，以及 itunes:image、media:content、
  media:thumbnail、content:encoded 扩展。
- Atom 1.0（RFC 4287 子集）：feed/title/id/updated/logo/icon/link；
  entry/title/id/updated/published/summary/author/link。
- xml:base（XML Base）：支持在根、条目、链接/附件元素上嵌套声明，
  逐级解析相对地址，并保留完整 base 链作为证据。

设计原则：
- 不按标题、不按媒体 URL 唯一化条目；身份判定见 identity.py。
- 每个元素保留原始 XML 片段，作为页面“来源证据”。
"""
from __future__ import annotations

import hashlib
from urllib.parse import urljoin

from lxml import etree

from .errors import FeedParseError
from .models import (
    CoverRecord,
    EnclosureRecord,
    IdentityEvidence,
    LINK_PURPOSE_LABELS,
    LinkRecord,
    ParsedEntry,
    ParsedFeed,
)

ATOM_NS = "http://www.w3.org/2005/Atom"
MEDIA_NS = "http://search.yahoo.com/mrss/"
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
XML_BASE_ATTR = "{http://www.w3.org/XML/1998/namespace}base"


def _qn(prefix_ns: str | None, local: str) -> str:
    return f"{{{prefix_ns}}}{local}" if prefix_ns else local


def _text(el: etree._Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    value = el.text.strip()
    return value or None


def _fragment(el: etree._Element) -> str:
    """元素原始 XML 片段（证据），去掉多余缩进空白但保留属性与嵌套。"""
    raw = etree.tostring(el, encoding="unicode", pretty_print=True)
    return raw.strip()


def _base_chain(el: etree._Element) -> list[str]:
    """收集从根到当前元素路径上所有 xml:base 声明（含自身）。"""
    chain: list[str] = []
    node = el
    while node is not None:
        base = node.get(XML_BASE_ATTR)
        if base:
            chain.append(base.strip())
        node = node.getparent()
    chain.reverse()
    return chain


def _resolve(raw_href: str, chain: list[str], document_base: str) -> str:
    """按 XML Base 规则逐级 urljoin；最后以订阅源地址兜底。"""
    base = document_base
    for b in chain:
        base = urljoin(base, b)
    return urljoin(base, raw_href)


def _purpose(rel: str | None) -> tuple[str, str]:
    if rel is None:
        return "unknown", LINK_PURPOSE_LABELS["unknown"]
    label = LINK_PURPOSE_LABELS.get(rel)
    if label is None:
        return rel, f"{rel}（非本采集器专门处理的关系，仅记录不跟随）"
    return rel, label


def _fingerprint(parts: list[str]) -> str:
    digest = hashlib.sha256("\u001f".join(parts).encode("utf-8")).hexdigest()
    return "sha256:" + digest[:16]


# ---------------------------------------------------------------------------
# 链接与附件
# ---------------------------------------------------------------------------

def _parse_atom_link(el: etree._Element, doc_base: str) -> LinkRecord:
    href = el.get("href")
    if href is None:
        raise FeedParseError(
            "Atom <link> 缺少必需的 href 属性，不符合 RFC 4287 子集要求"
        )
    chain = _base_chain(el)
    rel_raw = el.get("rel")
    rel, label = _purpose(rel_raw)
    return LinkRecord(
        raw_href=href.strip(),
        resolved_url=_resolve(href.strip(), chain, doc_base),
        rel=rel,
        rel_raw=rel_raw,
        link_type=el.get("type"),
        title=el.get("title"),
        lang=el.get("{http://www.w3.org/XML/1998/namespace}lang"),
        is_permalink=False,
        base_chain=chain,
        purpose_label=label,
        source_fragment=_fragment(el),
    )


def _parse_rss_link_element(el: etree._Element, doc_base: str) -> LinkRecord:
    """RSS 里的命名空间 <atom:link>（常见 rel=self/hub 等）。"""
    href = el.get("href")
    if not href:
        raise FeedParseError("RSS 中的 atom:link 缺少 href 属性")
    chain = _base_chain(el)
    rel_raw = el.get("rel", "alternate")
    rel, label = _purpose(rel_raw)
    return LinkRecord(
        raw_href=href.strip(),
        resolved_url=_resolve(href.strip(), chain, doc_base),
        rel=rel,
        rel_raw=rel_raw,
        link_type=el.get("type"),
        title=el.get("title"),
        lang=None,
        is_permalink=False,
        base_chain=chain,
        purpose_label=label,
        source_fragment=_fragment(el),
    )


def _rss_enclosure(el: etree._Element, doc_base: str) -> EnclosureRecord:
    url = el.get("url")
    if not url:
        raise FeedParseError("RSS <enclosure> 缺少必需的 url 属性")
    chain = _base_chain(el)
    length_raw = el.get("length")
    try:
        length = int(length_raw) if length_raw not in (None, "") else None
    except ValueError:
        length = None
    return EnclosureRecord(
        raw_url=url.strip(),
        resolved_url=_resolve(url.strip(), chain, doc_base),
        media_type=el.get("type"),
        length=length,
        origin="rss:enclosure",
        base_chain=chain,
        source_fragment=_fragment(el),
    )


def _atom_enclosure(el: etree._Element, doc_base: str) -> EnclosureRecord:
    href = el.get("href")
    if not href:
        raise FeedParseError("Atom enclosure 链接缺少 href 属性")
    chain = _base_chain(el)
    length_raw = el.get("length")
    try:
        length = int(length_raw) if length_raw not in (None, "") else None
    except ValueError:
        length = None
    return EnclosureRecord(
        raw_url=href.strip(),
        resolved_url=_resolve(href.strip(), chain, doc_base),
        media_type=el.get("type"),
        length=length,
        origin="atom:link[rel=enclosure]",
        base_chain=chain,
        source_fragment=_fragment(el),
    )


def _media_content(el: etree._Element, doc_base: str) -> EnclosureRecord:
    url = el.get("url")
    if not url:
        raise FeedParseError("media:content 缺少 url 属性")
    chain = _base_chain(el)
    try:
        length = int(el.get("fileSize")) if el.get("fileSize") else None
    except ValueError:
        length = None
    return EnclosureRecord(
        raw_url=url.strip(),
        resolved_url=_resolve(url.strip(), chain, doc_base),
        media_type=el.get("type"),
        length=length,
        origin="media:content",
        base_chain=chain,
        source_fragment=_fragment(el),
    )


# ---------------------------------------------------------------------------
# 身份线索
# ---------------------------------------------------------------------------

def _entry_identity(
    *,
    atom_id: str | None,
    guid_text: str | None,
    guid_is_permalink: bool | None,
    permalinks: list[str],
    fingerprint_parts: list[str],
    atom_id_fragment: str,
    guid_fragment: str,
) -> list[IdentityEvidence]:
    """按 P1..P5 生成身份线索（排序后即优先级）。

    P1 Atom <id>；P2 RSS <guid>（区分 permalink 与不透明标识）；
    P3 订阅源作用域内的 permalink；P4 首个 alternate 链接；
    P5 结构指纹（仅兜底，弱标识）。
    标题与媒体 URL 永不参与身份判定。
    """
    candidates: list[IdentityEvidence] = []
    if atom_id:
        candidates.append(IdentityEvidence(
            tier=1, key=f"atom-id::{atom_id.strip()}", raw_value=atom_id.strip(),
            element="atom:id", is_permalink=None,
            explanation="P1：Atom 规范规定 <id> 是条目的永久唯一标识，优先级最高",
            source_fragment=atom_id_fragment,
        ))
    if guid_text is not None:
        if guid_is_permalink:
            explanation = ("P2：RSS <guid isPermaLink=\"true\">，规范指明其值为永久链接；"
                           "注意 permalink 可能与 <link> 不一致，两者都保留为证据")
        elif guid_is_permalink is False:
            explanation = ("P2：RSS <guid isPermaLink=\"false\">，是发布者指定的不透明唯一标识，"
                           "不是 URL，优先级仍高于链接")
        else:
            explanation = ("P2：RSS <guid> 未声明 isPermaLink，按 RSS 2.0 规范默认视为 true，"
                           "但本采集器不把它当 URL 打开，仅作标识字符串")
        candidates.append(IdentityEvidence(
            tier=2, key=f"guid::{guid_text.strip()}", raw_value=guid_text.strip(),
            element="guid", is_permalink=guid_is_permalink,
            explanation=explanation, source_fragment=guid_fragment,
        ))
    if permalinks:
        for idx, url in enumerate(permalinks):
            candidates.append(IdentityEvidence(
                tier=3, key=f"permalink::{url}", raw_value=url,
                element="link[rel=alternate]" if idx == 0 else "guid(permalink)",
                is_permalink=True,
                explanation=("P3：永久链接只在同一订阅源作用域内有效，"
                             "不同订阅源可能使用相同链接，因此不能作为全局唯一键"),
                source_fragment="",
            ))
    fp = _fingerprint(fingerprint_parts)
    candidates.append(IdentityEvidence(
        tier=5, key=f"fp::{fp}", raw_value=fp,
        element="structural-fingerprint", is_permalink=None,
        explanation=("P5：条目缺少任何显式标识时的兜底结构指纹"
                     "（标题+日期+作者+链接地址+附件地址的哈希），"
                     "仅在同一订阅源内参与归并，属弱标识"),
        source_fragment="",
    ))
    candidates.sort(key=lambda c: c.tier)
    return candidates


# ---------------------------------------------------------------------------
# Atom 解析
# ---------------------------------------------------------------------------

def _parse_atom(root: etree._Element, doc_base: str, raw_doc: str) -> ParsedFeed:
    feed_id_el = root.find(_qn(ATOM_NS, "id"))
    feed_id = _text(feed_id_el)
    feed_links = [_parse_atom_link(el, doc_base)
                  for el in root.findall(_qn(ATOM_NS, "link"))]
    self_link = next((l.resolved_url for l in feed_links if l.rel == "self"), None)
    site_link = next((l.resolved_url for l in feed_links if l.rel == "alternate"), None)

    feed_evidence: list[IdentityEvidence] = []
    if feed_id:
        feed_evidence.append(IdentityEvidence(
            tier=1, key=f"atom-feed-id::{feed_id}", raw_value=feed_id,
            element="atom:feed/id", is_permalink=None,
            explanation="F-P1：Atom 订阅源 <id> 是订阅源的永久标识",
            source_fragment=_fragment(feed_id_el),
        ))
    if self_link:
        feed_evidence.append(IdentityEvidence(
            tier=2, key=f"self::{self_link}", raw_value=self_link,
            element="atom:link[rel=self]", is_permalink=None,
            explanation="F-P2：rel=self 的链接规范地指向订阅源自身",
            source_fragment="",
        ))
    if site_link:
        feed_evidence.append(IdentityEvidence(
            tier=3, key=f"site::{site_link}", raw_value=site_link,
            element="atom:link[rel=alternate]", is_permalink=None,
            explanation="F-P3：订阅源 alternate 链接指向站点页面，可能与其他源重复",
            source_fragment="",
        ))
    feed_evidence.append(IdentityEvidence(
        tier=4, key=f"url::{doc_base}", raw_value=doc_base,
        element="fetched-url", is_permalink=None,
        explanation="F-P4：抓取地址只是传输位置，镜像/迁移会变化，优先级最低",
        source_fragment="",
    ))
    feed_evidence.sort(key=lambda c: c.tier)
    feed_key = feed_evidence[0].key
    feed_tier = feed_evidence[0].tier

    covers: list[CoverRecord] = []
    for tag in ("logo", "icon"):
        el = root.find(_qn(ATOM_NS, tag))
        if el is not None and _text(el):
            chain = _base_chain(el)
            covers.append(CoverRecord(
                owner="feed", origin=f"atom:{tag}",
                raw_url=_text(el),  # type: ignore[arg-type]
                resolved_url=_resolve(_text(el), chain, doc_base),  # type: ignore[arg-type]
                base_chain=chain, source_fragment=_fragment(el),
            ))

    entries: list[ParsedEntry] = []
    for idx, entry_el in enumerate(root.findall(_qn(ATOM_NS, "entry"))):
        entries.append(_parse_atom_entry(entry_el, doc_base, feed_key, feed_tier, idx))

    return ParsedFeed(
        feed_url=doc_base,
        format="Atom 1.0",
        feed_identity_key=feed_key,
        feed_identity_tier=feed_tier,
        feed_identity_evidence=feed_evidence,
        title=_text(root.find(_qn(ATOM_NS, "title"))),
        self_link=self_link,
        site_link=site_link,
        declared_base=root.get(XML_BASE_ATTR),
        links=feed_links,
        covers=covers,
        entries=entries,
        raw_document=raw_doc,
    )


def _parse_atom_entry(entry_el: etree._Element, doc_base: str,
                      feed_key: str, feed_tier: int, idx: int) -> ParsedEntry:
    links = [_parse_atom_link(el, doc_base)
             for el in entry_el.findall(_qn(ATOM_NS, "link"))]
    enclosures = [_atom_enclosure(el, doc_base)
                  for el in entry_el.findall(_qn(ATOM_NS, "link"))
                  if el.get("rel", "alternate") == "enclosure" and el.get("href")]
    # media 扩展（Atom 文档里也允许出现）
    enclosures += [_media_content(el, doc_base)
                   for el in entry_el.findall(_qn(MEDIA_NS, "content"))
                   if el.get("url")]
    covers: list[CoverRecord] = []
    for el in entry_el.findall(_qn(MEDIA_NS, "thumbnail")):
        if el.get("url"):
            chain = _base_chain(el)
            covers.append(CoverRecord(
                owner="entry", origin="media:thumbnail",
                raw_url=el.get("url"),  # type: ignore[arg-type]
                resolved_url=_resolve(el.get("url"), chain, doc_base),  # type: ignore[arg-type]
                base_chain=chain, source_fragment=_fragment(el),
            ))

    permalinks = [l.resolved_url for l in links if l.rel == "alternate"]
    atom_id_el = entry_el.find(_qn(ATOM_NS, "id"))
    atom_id = _text(atom_id_el)
    title = _text(entry_el.find(_qn(ATOM_NS, "title")))
    authors = [_text(a.find(_qn(ATOM_NS, "name"))) or ""
               for a in entry_el.findall(_qn(ATOM_NS, "author"))]
    authors = [a for a in authors if a]
    published = _text(entry_el.find(_qn(ATOM_NS, "published")))
    updated = _text(entry_el.find(_qn(ATOM_NS, "updated")))
    summary = _text(entry_el.find(_qn(ATOM_NS, "summary")))

    fp_parts = [title or "", published or "", updated or "",
                "|".join(authors), "|".join(permalinks),
                "|".join(sorted(e.resolved_url for e in enclosures))]
    candidates = _entry_identity(
        atom_id=atom_id,
        guid_text=None,
        guid_is_permalink=None,
        permalinks=permalinks,
        fingerprint_parts=fp_parts,
        atom_id_fragment=_fragment(atom_id_el) if atom_id_el is not None else "",
        guid_fragment="",
    )
    chosen = candidates[0]
    return ParsedEntry(
        feed_url=doc_base,
        feed_identity_key=feed_key,
        feed_identity_tier=feed_tier,
        entry_index=idx,
        title=title,
        published=published,
        updated=updated,
        authors=authors,
        summary=summary,
        links=links,
        enclosures=enclosures,
        covers=covers,
        identity_candidates=candidates,
        chosen_identity_key=chosen.key,
        chosen_identity_tier=chosen.tier,
        permalinks=permalinks,
        structural_fingerprint=chosen.key if chosen.tier == 5 else
        next(c.key for c in candidates if c.tier == 5),
        raw_fragment=_fragment(entry_el),
        xml_base=entry_el.get(XML_BASE_ATTR),
    )


# ---------------------------------------------------------------------------
# RSS 解析
# ---------------------------------------------------------------------------

def _parse_rss(root: etree._Element, doc_base: str, raw_doc: str) -> ParsedFeed:
    channel = root.find("channel")
    if channel is None:
        raise FeedParseError("RSS 文档缺少 channel 根元素")

    feed_links = [_parse_rss_link_element(el, doc_base)
                  for el in channel.findall(_qn(ATOM_NS, "link"))]
    self_link = next((l.resolved_url for l in feed_links if l.rel == "self"), None)
    site_link_el = channel.find("link")
    site_link = _text(site_link_el)
    if site_link:
        site_link = _resolve(site_link, _base_chain(site_link_el), doc_base)

    feed_evidence: list[IdentityEvidence] = []
    if self_link:
        feed_evidence.append(IdentityEvidence(
            tier=2, key=f"self::{self_link}", raw_value=self_link,
            element="atom:link[rel=self]", is_permalink=None,
            explanation="F-P2：RSS 中 atom:link rel=self 规范指向订阅源自身",
            source_fragment="",
        ))
    if site_link:
        feed_evidence.append(IdentityEvidence(
            tier=3, key=f"site::{site_link}", raw_value=site_link,
            element="rss:channel/link", is_permalink=None,
            explanation="F-P3：channel link 是站点页面，多个订阅源可能指向同一站点",
            source_fragment=_fragment(site_link_el),
        ))
    feed_evidence.append(IdentityEvidence(
        tier=4, key=f"url::{doc_base}", raw_value=doc_base,
        element="fetched-url", is_permalink=None,
        explanation="F-P4：抓取地址只是传输位置，镜像/迁移会变化，优先级最低",
        source_fragment="",
    ))
    feed_evidence.sort(key=lambda c: c.tier)
    feed_key = feed_evidence[0].key
    feed_tier = feed_evidence[0].tier

    covers: list[CoverRecord] = []
    img_el = channel.find(_qn(ITUNES_NS, "image"))
    if img_el is not None and img_el.get("href"):
        chain = _base_chain(img_el)
        covers.append(CoverRecord(
            owner="feed", origin="itunes:image",
            raw_url=img_el.get("href"),  # type: ignore[arg-type]
            resolved_url=_resolve(img_el.get("href"), chain, doc_base),  # type: ignore[arg-type]
            base_chain=chain, source_fragment=_fragment(img_el),
        ))

    entries: list[ParsedEntry] = []
    for idx, item_el in enumerate(channel.findall("item")):
        entries.append(_parse_rss_item(item_el, doc_base, feed_key, feed_tier, idx))

    return ParsedFeed(
        feed_url=doc_base,
        format="RSS 2.0",
        feed_identity_key=feed_key,
        feed_identity_tier=feed_tier,
        feed_identity_evidence=feed_evidence,
        title=_text(channel.find("title")),
        self_link=self_link,
        site_link=site_link,
        declared_base=root.get(XML_BASE_ATTR) or channel.get(XML_BASE_ATTR),
        links=feed_links,
        covers=covers,
        entries=entries,
        raw_document=raw_doc,
    )


def _parse_rss_item(item_el: etree._Element, doc_base: str,
                    feed_key: str, feed_tier: int, idx: int) -> ParsedEntry:
    links: list[LinkRecord] = []
    # 经典 RSS <link> 文本元素 => 视为 alternate
    link_el = item_el.find("link")
    item_link = _text(link_el)
    if item_link:
        chain = _base_chain(link_el)
        resolved = _resolve(item_link, chain, doc_base)
        rel, label = _purpose("alternate")
        links.append(LinkRecord(
            raw_href=item_link, resolved_url=resolved, rel=rel, rel_raw=None,
            link_type=None, title=None, lang=None, is_permalink=False,
            base_chain=chain, purpose_label=label,
            source_fragment=_fragment(link_el),
        ))
    # 命名空间 atom:link（多关系链接）
    for el in item_el.findall(_qn(ATOM_NS, "link")):
        links.append(_parse_rss_link_element(el, doc_base))

    enclosures = [_rss_enclosure(el, doc_base)
                  for el in item_el.findall("enclosure") if el.get("url")]
    enclosures += [_media_content(el, doc_base)
                   for el in item_el.findall(_qn(MEDIA_NS, "content"))
                   if el.get("url")]

    covers: list[CoverRecord] = []
    img_el = item_el.find(_qn(ITUNES_NS, "image"))
    if img_el is not None and img_el.get("href"):
        chain = _base_chain(img_el)
        covers.append(CoverRecord(
            owner="entry", origin="itunes:image",
            raw_url=img_el.get("href"),  # type: ignore[arg-type]
            resolved_url=_resolve(img_el.get("href"), chain, doc_base),  # type: ignore[arg-type]
            base_chain=chain, source_fragment=_fragment(img_el),
        ))
    for el in item_el.findall(_qn(MEDIA_NS, "thumbnail")):
        if el.get("url"):
            chain = _base_chain(el)
            covers.append(CoverRecord(
                owner="entry", origin="media:thumbnail",
                raw_url=el.get("url"),  # type: ignore[arg-type]
                resolved_url=_resolve(el.get("url"), chain, doc_base),  # type: ignore[arg-type]
                base_chain=chain, source_fragment=_fragment(el),
            ))

    guid_el = item_el.find("guid")
    guid_text = _text(guid_el)
    if guid_text is not None:
        perm = guid_el.get("isPermaLink")
        if perm is None:
            guid_is_permalink = None  # 规范默认 true，但不臆断
        else:
            guid_is_permalink = perm.strip().lower() == "true"
    else:
        guid_is_permalink = None

    permalinks: list[str] = []
    if guid_text is not None and guid_is_permalink is not False:
        # 无论显式 true 还是缺省，guid 的 permalink 都单独记录，
        # 且与 <link> 不同时不做覆盖，两个地址都保留。
        chain = _base_chain(guid_el)
        permalinks.append(_resolve(guid_text, chain, doc_base))
    for l in links:
        if l.rel == "alternate" and l.resolved_url not in permalinks:
            permalinks.append(l.resolved_url)

    title = _text(item_el.find("title"))
    author = _text(item_el.find("author")) or _text(
        item_el.find(_qn("http://purl.org/dc/elements/1.1/", "creator")))
    published = _text(item_el.find("pubDate"))
    summary = _text(item_el.find("description")) or _text(
        item_el.find(_qn("http://purl.org/rss/1.0/modules/content/", "encoded")))

    fp_parts = [title or "", published or "", author or "",
                "|".join(permalinks),
                "|".join(sorted(e.resolved_url for e in enclosures))]
    candidates = _entry_identity(
        atom_id=None,
        guid_text=guid_text,
        guid_is_permalink=guid_is_permalink,
        permalinks=permalinks,
        fingerprint_parts=fp_parts,
        atom_id_fragment="",
        guid_fragment=_fragment(guid_el) if guid_el is not None else "",
    )
    chosen = candidates[0]
    return ParsedEntry(
        feed_url=doc_base,
        feed_identity_key=feed_key,
        feed_identity_tier=feed_tier,
        entry_index=idx,
        title=title,
        published=published,
        updated=None,
        authors=[author] if author else [],
        summary=summary,
        links=links,
        enclosures=enclosures,
        covers=covers,
        identity_candidates=candidates,
        chosen_identity_key=chosen.key,
        chosen_identity_tier=chosen.tier,
        permalinks=permalinks,
        structural_fingerprint=next(c.key for c in candidates if c.tier == 5),
        raw_fragment=_fragment(item_el),
        xml_base=item_el.get(XML_BASE_ATTR),
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_feed(raw_xml: bytes | str, feed_url: str) -> ParsedFeed:
    """解析订阅源文档。feed_url 用于相对地址兜底与 feed 身份末位线索。"""
    if isinstance(raw_xml, str):
        data = raw_xml.encode("utf-8")
    else:
        data = raw_xml
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True,
                                 load_dtd=False, remove_blank_text=False)
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise FeedParseError(f"XML 语法不合法：{exc.msg}") from exc

    # 重新 pretty 一份完整文档作为证据（属性/文本均未改动）
    raw_doc = etree.tostring(root, encoding="unicode", pretty_print=True).strip()

    if root.tag == "rss":
        return _parse_rss(root, feed_url, raw_doc)
    if root.tag == _qn(ATOM_NS, "feed"):
        return _parse_atom(root, feed_url, raw_doc)
    raise FeedParseError(
        f"根元素 <{root.tag}> 不是 RSS 2.0 <rss> 或 Atom 1.0 <feed>，"
        "超出本采集器声明支持的协议子集"
    )
