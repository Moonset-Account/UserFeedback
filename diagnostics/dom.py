"""Minimal DOM parser and query helpers (standard library only).

The tests use this to assert what a browser would see: textContent after entity
decoding, element nesting, attributes and the absence/presence of anchors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterator


VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)

# Subtrees whose text is not rendered as document text.
NON_TEXT_TAGS = frozenset({"script", "style", "template", "noscript"})


@dataclass
class Text:
    data: str


@dataclass
class Element:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Element | Text"] = field(default_factory=list)
    parent: "Element | None" = None

    def get(self, name: str) -> str | None:
        return self.attrs.get(name)

    def classes(self) -> tuple[str, ...]:
        value = self.attrs.get("class", "")
        return tuple(part for part in value.split() if part)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#document", {}, [])
        self._stack: list[Element] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(tag.lower(), {name.lower(): (value or "") for name, value in attrs})
        element.parent = self._stack[-1]
        self._stack[-1].children.append(element)
        if tag.lower() not in VOID_TAGS:
            self._stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(tag.lower(), {name.lower(): (value or "") for name, value in attrs})
        element.parent = self._stack[-1]
        self._stack[-1].children.append(element)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if not data:
            return
        parent = self._stack[-1]
        # HTML parsers ignore a single newline right after <pre> / <textarea>.
        if parent.tag in ("pre", "textarea") and not parent.children and data.startswith("\n"):
            data = data[1:]
            if not data:
                return
        parent.children.append(Text(data))


def parse_html(markup: str) -> Element:
    """Parse markup into an element tree rooted at ``#document``."""
    builder = _TreeBuilder()
    builder.feed(markup)
    builder.close()
    return builder.root


def text_content(node: Element | Text) -> str:
    """Approximate ``Node.textContent`` for rendered text."""
    if isinstance(node, Text):
        return node.data
    if node.tag in NON_TEXT_TAGS:
        return ""
    return "".join(text_content(child) for child in node.children)


def iter_elements(node: Element, tag: str | None = None) -> Iterator[Element]:
    for child in node.children:
        if isinstance(child, Text):
            continue
        if tag is None or child.tag == tag:
            yield child
        yield from iter_elements(child, tag)


def find_all(
    root: Element,
    tag: str | None = None,
    *,
    class_: str | None = None,
    attrs: dict[str, str] | None = None,
) -> list[Element]:
    matches: list[Element] = []
    for element in iter_elements(root, tag):
        if class_ is not None and class_ not in element.classes():
            continue
        if attrs is not None and any(element.attrs.get(k) != v for k, v in attrs.items()):
            continue
        matches.append(element)
    return matches


def find_by_id(root: Element, element_id: str) -> Element | None:
    for element in iter_elements(root):
        if element.attrs.get("id") == element_id:
            return element
    return None
