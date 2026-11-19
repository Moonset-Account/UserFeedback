"""Synthetic document fixtures shared by the acceptance tests.

The layout mirrors the reported scenario: one editable text object is reused by
several pages through shared instances and a shared container.  A private page
and an orphaned text object exercise export reachability.

Reference paths (``page: path -> object``)::

    p1: ("top",)        -> inst_head   (txt_head)
    p1: ("main","lead") -> inst_head   (txt_head, shared with p2/p3)
    p1: ("main","body") -> inst_copy   (txt_copy, shared with p2/p3)
    p2: ("main","lead") -> inst_head
    p2: ("main","body") -> inst_copy
    p3: ("duplicate","lead") -> inst_head
    p3: ("duplicate","body") -> inst_copy
"""

from __future__ import annotations

from typing import Any

HEAD_TEXT = "内部公告：代号「曙光」将于周五发布"
COPY_TEXT = "Project Aurora launches in Q3. Owner: alice@example.com"
SECRET_TEXT = "董事会密钥：ALPHA-9"
ORPHAN_TEXT = "未挂载的草稿：backup@example.com"

# Adjacent on purpose: "Owner: " ends exactly where the address starts, at 38.
COPY_SEGMENTS: list[tuple[str, str]] = [
    ("Project Aurora", "Project Aurora"),
    ("launches in Q3.", "launches in Q3."),
    ("Owner: ", "Owner"),
    ("alice@example.com", "alice at example dot com"),
]

HEAD_SEGMENTS: list[tuple[str, str]] = [
    ("内部公告", "内部公告"),
    ("代号「曙光」", "代号曙光"),
]


def span(text: str, fragment: str) -> tuple[int, int]:
    start = text.index(fragment)
    return start, start + len(fragment)


def _segments(text: str, specs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    result = []
    last_end = -1
    for fragment, utterance in specs:
        start, end = span(text, fragment)
        if start < last_end:
            raise AssertionError(f"fixture segments overlap at {fragment!r}")
        last_end = end
        result.append({"start": start, "end": end, "utterance": utterance})
    return result


COPY_SPANS = {fragment: span(COPY_TEXT, fragment) for fragment, _ in COPY_SEGMENTS}
HEAD_SPANS = {fragment: span(HEAD_TEXT, fragment) for fragment, _ in HEAD_SEGMENTS}

SEGMENTS_BY_TEXT = {
    "txt_copy": _segments(COPY_TEXT, COPY_SEGMENTS),
    "txt_head": _segments(HEAD_TEXT, HEAD_SEGMENTS),
}


def build_objects() -> dict[str, dict[str, Any]]:
    return {
        "txt_head": {"kind": "text", "text": HEAD_TEXT, "speech": _segments(HEAD_TEXT, HEAD_SEGMENTS)},
        "txt_copy": {"kind": "text", "text": COPY_TEXT, "speech": _segments(COPY_TEXT, COPY_SEGMENTS)},
        "txt_secret": {"kind": "text", "text": SECRET_TEXT, "speech": []},
        "txt_orphan": {"kind": "text", "text": ORPHAN_TEXT, "speech": []},
        "inst_head": {"kind": "instance", "ref": "txt_head"},
        "inst_copy": {"kind": "instance", "ref": "txt_copy"},
        "inst_secret": {"kind": "instance", "ref": "txt_secret"},
        "sec_shared": {"kind": "container", "children": {"lead": "inst_head", "body": "inst_copy"}},
        "pg1": {"kind": "container", "children": {"top": "inst_head", "main": "sec_shared"}},
        "pg2": {"kind": "container", "children": {"main": "sec_shared"}},
        "pg3": {"kind": "container", "children": {"duplicate": "sec_shared"}},
        "pg_secret": {"kind": "container", "children": {"lock": "inst_secret"}},
    }


def build_pages() -> dict[str, dict[str, Any]]:
    return {
        "p1": {"root": "pg1", "public": True},
        "p2": {"root": "pg2", "public": True},
        "p3": {"root": "pg3", "public": True},
        "secret": {"root": "pg_secret", "public": False},
    }


def build_document() -> dict[str, Any]:
    return {"format": "docredact/1", "objects": build_objects(), "pages": build_pages()}


def head_without(fragment: str) -> str:
    return HEAD_TEXT.replace(fragment, "")


def copy_without(fragment: str) -> str:
    return COPY_TEXT.replace(fragment, "")


def p1_text(copy: str = COPY_TEXT, head: str = HEAD_TEXT) -> str:
    """p1 renders the head at "top", the head at "main.lead", then the copy."""

    return head + head + copy


def p2_text(copy: str = COPY_TEXT, head: str = HEAD_TEXT) -> str:
    """p2 (and p3) render one head and one copy through the shared container."""

    return head + copy
