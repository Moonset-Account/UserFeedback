"""Render the diagnostics page.

Rendering rules that matter for the security contract:

* Every dataset string is inserted as *text*, never as markup.
* Search highlighting splits the **original** string into adjacent slices and
  wraps some slices in ``<mark>``; concatenating the slices reproduces the
  original string byte-for-byte (no case folding, no entity re-encoding of the
  visible text).
* A rejected documentation link is rendered as plain text inside a ``<span>``,
  never as an ``<a>``.  The rest of the page is unaffected.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from .link_policy import LinkDecision, LinkPolicy
from .model import Dataset, Record

_ESCAPE_TABLE = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#x27;",
}


def escape_text(value: str) -> str:
    """Escape text for an HTML text node or quoted attribute."""
    return "".join(_ESCAPE_TABLE.get(char, char) for char in value)


@dataclass(frozen=True)
class Segment:
    text: str
    match: bool


def split_highlights(text: str, query: str | None) -> list[Segment]:
    """Split ``text`` into segments around case-insensitive literal matches.

    All slices are taken from ``text`` itself, so the concatenation of the
    segment texts is exactly ``text``.  ``re.finditer`` reports spans in the
    original string, which keeps this true even when a query character folds to
    a different-length form (e.g. ``İ``).
    """
    if not query or not query.strip() or text == "":
        return [Segment(text=text, match=False)] if text else []

    try:
        pattern = re.compile(re.escape(query), re.IGNORECASE)
    except re.error:  # pragma: no cover - re.escape output is always valid
        return [Segment(text=text, match=False)]

    segments: list[Segment] = []
    cursor = 0
    for match in pattern.finditer(text):
        start, end = match.span()
        if start == end:  # zero-width; cannot happen for a non-empty query
            continue
        if start > cursor:
            segments.append(Segment(text=text[cursor:start], match=False))
        segments.append(Segment(text=text[start:end], match=True))
        cursor = end
    if cursor < len(text):
        segments.append(Segment(text=text[cursor:], match=False))
    return segments or [Segment(text=text, match=False)]


def _render_segments(text: str, query: str | None) -> str:
    out: list[str] = []
    for segment in split_highlights(text, query):
        escaped = escape_text(segment.text)
        if segment.match:
            out.append(f'<mark class="hit">{escaped}</mark>')
        else:
            out.append(escaped)
    return "".join(out)


def _display_label(record: Record) -> str:
    assert record.doc is not None
    return record.doc.label if record.doc.label != "" else record.doc.url


def _render_doc(record: Record, decision: LinkDecision, query: str | None) -> str:
    doc = record.doc
    if doc is None:
        return ""
    label = _display_label(record)
    if decision.allowed:
        label_html = _render_segments(label, query)
        url_html = escape_text(doc.url)
        return (
            '<p class="doc doc-allowed">'
            '<span class="doc-kind">文档</span> '
            f'<a class="doc-link" href="{url_html}" rel="noopener noreferrer" '
            f'data-doc-host="{escape_text(decision.host or "")}">{label_html}</a> '
            f'<span class="doc-url-inline">{url_html}</span>'
            "</p>"
        )
    # Rejected: plain text only, with the machine-readable reason code.
    return (
        f'<p class="doc doc-blocked" data-doc-reason="{escape_text(decision.reason)}">'
        f'<span class="doc-kind">文档链接不可用</span> '
        f'<span class="doc-url">{_render_segments(doc.url, query)}</span> '
        f'<span class="doc-note">{escape_text(decision.note)}</span>'
        "</p>"
    )


def _render_record(record: Record, decision: LinkDecision | None, query: str | None) -> str:
    parts: list[str] = [
        f'<article class="record" id="record-{escape_text(record.record_id)}" '
        f'data-record-id="{escape_text(record.record_id)}">',
        f'<p class="record-id">{escape_text(record.record_id)}</p>',
        f'<p class="summary">{_render_segments(record.summary, query)}</p>',
    ]
    if record.details is not None:
        # Browsers drop one newline immediately after a <pre> start tag; emit a
        # guard newline when the payload itself starts with one so that the
        # rendered text still matches the dataset exactly.
        guard = "\n" if record.details.startswith("\n") else ""
        parts.append(
            f'<pre class="details">{guard}{_render_segments(record.details, query)}</pre>'
        )
    if decision is not None:
        parts.append(_render_doc(record, decision, query))
    parts.append("</article>")
    return "".join(parts)


STYLE = """
:root { color-scheme: light dark; }
body { font: 16px/1.55 -apple-system, "Segoe UI", "Noto Sans CJK SC", sans-serif;
       margin: 0 auto; max-width: 62rem; padding: 1.5rem 1.25rem 4rem; }
h1 { font-size: 1.5rem; margin: 0 0 .35rem; }
h2 { font-size: 1.1rem; margin: 2rem 0 .6rem; border-bottom: 1px solid #8884; padding-bottom: .3rem; }
.policy-note { font-size: .85rem; opacity: .75; margin: 0 0 1rem; }
.record { border: 1px solid #8884; border-radius: .5rem; padding: .75rem .9rem; margin: .75rem 0; }
.record-id { font: .75rem/1.2 ui-monospace, monospace; opacity: .6; margin: 0 0 .35rem; }
.summary { margin: 0 0 .5rem; white-space: pre-wrap; overflow-wrap: anywhere; }
.details { margin: 0 0 .5rem; padding: .6rem .7rem; background: #8881; border-radius: .35rem;
           white-space: pre-wrap; overflow-wrap: anywhere; font: .875rem/1.45 ui-monospace, monospace; }
.doc { margin: 0; font-size: .85rem; overflow-wrap: anywhere; }
.doc-kind { font-weight: 600; }
.doc-allowed .doc-url-inline { opacity: .55; font: .78rem ui-monospace, monospace; margin-left: .4rem; }
.doc-blocked { border-left: 3px solid #d33; padding-left: .55rem; }
.doc-blocked .doc-url { font: .8rem ui-monospace, monospace; }
.doc-note { opacity: .75; margin-left: .35rem; }
.build-note { font-size: .8rem; opacity: .8; margin: 0 0 1rem; }
.build-note code { font-family: ui-monospace, monospace; background: #8881; padding: .1rem .3rem;
                   border-radius: .2rem; overflow-wrap: anywhere; }
mark.hit { background: #ffd54a; color: #111; border-radius: .15rem; }
.empty { opacity: .7; font-style: italic; }
""".strip()


_HIGHLIGHT_HINT = "大小写不敏感，字符保持原样"

_HIGHLIGHT_NOTE = (
    '<p class="build-note">高亮由构建参数生成：'
    f"<code>{escape_text('python3 app.py --data <json> --allowlist <json> --query <text>')}</code>"
    "（页面为静态文件，不依赖脚本）。</p>"
)


def _highlight_banner(query: str | None) -> str:
    if not query:
        return _HIGHLIGHT_NOTE
    return (
        f'<p class="build-note">当前高亮词：<code>{escape_text(query)}</code>'
        f"（{escape_text(_HIGHLIGHT_HINT)}）</p>"
    )


_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


def render_page(dataset: Dataset, policy: LinkPolicy, query: str | None = None) -> str:
    """Return the full HTML document for ``dataset``."""
    decisions: dict[str, LinkDecision] = {}
    for record in dataset.records:
        if record.doc is not None:
            decisions[record.record_id] = policy.check(record.doc.url)

    body: list[str] = []
    groups = dataset.grouped()
    if not groups:
        body.append('<p class="empty">数据集为空。</p>')
    for index, (group_name, records) in enumerate(groups):
        body.append(f'<section class="group" data-group-index="{index}">')
        body.append(f"  <h2>{_render_segments(group_name, query)}</h2>")
        for record in records:
            body.append(_render_record(record, decisions.get(record.record_id), query))
        body.append("</section>")

    allowed = ", ".join(policy.allowed_hosts)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{escape_text(_CSP)}">\n'
        '<meta name="referrer" content="no-referrer">\n'
        f"<title>{escape_text(dataset.title)}</title>\n"
        f"<style>\n{STYLE}\n</style>\n</head>\n<body>\n"
        f"<header>\n<h1>{_render_segments(dataset.title, query)}</h1>\n"
        f'<p class="policy-note">页面内容一律按文本显示；搜索仅做高亮，不改变字符。'
        f"文档链接仅允许 HTTPS 且主机属于白名单：{escape_text(allowed)}；"
        "含凭据的链接与白名单外链接按普通文本展示。</p>\n</header>\n"
        f"{_highlight_banner(query)}\n"
        "<main>\n" + "\n".join(body) + "\n</main>\n"
        "</body>\n</html>\n"
    )


# ---------------------------------------------------------------------------
# DOM-derived report
# ---------------------------------------------------------------------------


def _record_report_from_dom(record_el) -> dict[str, object]:
    """Extract facts from a parsed ``<article class="record">`` element."""
    from . import dom

    record_id = record_el.get("data-record-id") or ""
    summary_el = dom.find_all(record_el, "p", class_="summary")
    details_el = dom.find_all(record_el, "pre", class_="details")
    anchor_el = dom.find_all(record_el, "a", class_="doc-link")
    blocked_el = dom.find_all(record_el, "p", class_="doc-blocked")

    summary_text = dom.text_content(summary_el[0]) if summary_el else None
    details_text = dom.text_content(details_el[0]) if details_el else None
    summary_marks = dom.find_all(summary_el[0], "mark") if summary_el else []
    details_marks = dom.find_all(details_el[0], "mark") if details_el else []

    doc: dict[str, object] | None = None
    if anchor_el:
        anchor = anchor_el[0]
        doc = {
            "kind": "anchor",
            "href": anchor.get("href"),
            "text": dom.text_content(anchor),
            "host": anchor.get("data-doc-host"),
            "rel": anchor.get("rel"),
        }
    elif blocked_el:
        paragraph = blocked_el[0]
        url_el = dom.find_all(paragraph, "span", class_="doc-url")
        note_el = dom.find_all(paragraph, "span", class_="doc-note")
        doc = {
            "kind": "plain-text",
            "reason": paragraph.get("data-doc-reason"),
            "url_text": dom.text_content(url_el[0]) if url_el else None,
            "note": dom.text_content(note_el[0]) if note_el else None,
        }

    return {
        "id": record_id,
        "summary": summary_text,
        "details": details_text,
        "doc": doc,
        "highlight": {
            "summary_matches": len(summary_marks),
            "details_matches": len(details_marks),
        },
    }


def build_report(markup: str) -> dict[str, object]:
    """Parse ``markup`` and report what a DOM consumer would observe."""
    from . import dom

    root = dom.parse_html(markup)
    title_el = dom.find_all(root, "h1")
    sections = []
    for section in dom.find_all(root, "section", class_="group"):
        heading = dom.find_all(section, "h2")
        records = [
            _record_report_from_dom(article)
            for article in dom.find_all(section, "article", class_="record")
        ]
        sections.append(
            {
                "group": dom.text_content(heading[0]) if heading else None,
                "records": records,
            }
        )
    return {
        "title": dom.text_content(title_el[0]) if title_el else None,
        "groups": sections,
        "anchors": [element.get("href") for element in dom.find_all(root, "a")],
        "record_count": len(dom.find_all(root, "article", class_="record")),
    }
