"""从解析后的 MIME 结构中提取实验批号与附件数值。

可追溯性原则：每个提取结果都带 source_part_id，指向解析器分配的 MIME part；
替代版本冲突、附件解码错误均结构化返回，由上层决定如何展示。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .parser import ParsedMessage, PartNode

# 实验批号：EXP- 开头，后接 4 位年份与至少 3 位序号，允许连字符/点分隔
BATCH_RE = re.compile(r"EXP[-_.]?\d{4}[-_.]?\d{3,}", re.IGNORECASE)
# 附件中的数值：支持负数、小数、科学计数法
NUMBER_RE = re.compile(r"[-+]?(?:\d+\.\d+|\.\d+|\d+)(?:[eE][-+]?\d+)?")

PREVIEW_LIMIT_CSV = 200


class _HtmlStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def text(self) -> str:
        return " ".join(self._chunks)


def strip_html(html: str) -> str:
    parser = _HtmlStripper()
    parser.feed(html)
    return parser.text()


@dataclass
class AlternativeVersion:
    part_id: str
    content_type: str
    batch: str | None
    selected: bool


@dataclass
class BatchExtraction:
    value: str | None
    source_part_id: str | None
    source_content_type: str | None
    versions: list[AlternativeVersion] = field(default_factory=list)
    conflict: bool = False
    conflict_detail: str | None = None


@dataclass
class AttachmentExtraction:
    part_id: str
    filename: str | None
    content_type: str
    numbers: list[float] = field(default_factory=list)
    decode_error: str | None = None
    skipped_reason: str | None = None
    source_preview: str | None = None


@dataclass
class ExtractionResult:
    batch: BatchExtraction
    attachments: list[AttachmentExtraction]
    warnings: list[str]

    def to_dict(self) -> dict:
        return {
            "batch": {
                "value": self.batch.value,
                "source_part_id": self.batch.source_part_id,
                "source_content_type": self.batch.source_content_type,
                "conflict": self.batch.conflict,
                "conflict_detail": self.batch.conflict_detail,
                "versions": [v.__dict__ for v in self.batch.versions],
            },
            "attachments": [a.__dict__ for a in self.attachments],
            "warnings": self.warnings,
        }


def _iter_nodes(node: PartNode):
    yield node
    for child in node.children:
        yield from _iter_nodes(child)


def _find_alternative_groups(node: PartNode) -> list[PartNode]:
    groups = []
    for n in _iter_nodes(node):
        if n.content_type == "multipart/alternative":
            groups.append(n)
    return groups


def extract_batch(parsed: ParsedMessage) -> BatchExtraction:
    groups = _find_alternative_groups(parsed.tree)
    versions: list[AlternativeVersion] = []
    plain_choice: tuple[str, str, str] | None = None   # (part_id, ctype, batch)
    html_choice: tuple[str, str, str] | None = None

    if groups:
        # 取第一个 alternative 组（邮件正文组）；每个叶子按 MIME 顺序记录
        group = groups[0]
        plain_leaves = [c for c in group.children if c.content_type == "text/plain"]
        html_leaves = [c for c in group.children if c.content_type == "text/html"]

        for leaf in plain_leaves:
            batch = BATCH_RE.search(leaf.text or "")
            hit = batch.group(0).upper() if batch else None
            versions.append(AlternativeVersion(leaf.part_id, "text/plain", hit, False))
            if hit and plain_choice is None:
                plain_choice = (leaf.part_id, "text/plain", hit)
        for leaf in html_leaves:
            visible = strip_html(leaf.text or "")
            batch = BATCH_RE.search(visible)
            hit = batch.group(0).upper() if batch else None
            versions.append(AlternativeVersion(leaf.part_id, "text/html", hit, False))
            if hit and html_choice is None:
                html_choice = (leaf.part_id, "text/html", hit)
    else:
        # 没有 alternative 组时，退化为扫描全部 text 叶子
        for n in _iter_nodes(parsed.tree):
            if n.media_type != "text" or n.text is None:
                continue
            text = strip_html(n.text) if n.content_type == "text/html" else n.text
            m = BATCH_RE.search(text)
            hit = m.group(0).upper() if m else None
            versions.append(AlternativeVersion(n.part_id, n.content_type, hit, False))
            if n.content_type == "text/plain" and hit and plain_choice is None:
                plain_choice = (n.part_id, n.content_type, hit)
            if n.content_type == "text/html" and hit and html_choice is None:
                html_choice = (n.part_id, n.content_type, hit)

    # 选用规则：text/plain 优先；不存在时退回 text/html
    chosen = plain_choice or html_choice
    for v in versions:
        v.selected = chosen is not None and v.part_id == chosen[0]

    result = BatchExtraction(
        value=chosen[2] if chosen else None,
        source_part_id=chosen[0] if chosen else None,
        source_content_type=chosen[1] if chosen else None,
        versions=versions,
    )

    # 冲突检测：两个替代版本都提取到批号且不一致
    if plain_choice and html_choice and plain_choice[2] != html_choice[2]:
        result.conflict = True
        result.conflict_detail = (
            f"text/plain ({plain_choice[0]}) 批号 {plain_choice[2]} 与 "
            f"text/html ({html_choice[0]}) 批号 {html_choice[2]} 不一致；"
            f"按规则选用 text/plain 的 {plain_choice[2]}"
        )
    return result


def _numbers_from_csv(text: str) -> list[float]:
    numbers: list[float] = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        for cell in row:
            token = cell.strip()
            if NUMBER_RE.fullmatch(token):
                try:
                    numbers.append(float(token))
                except ValueError:
                    continue
    return numbers


def _numbers_from_text(text: str) -> list[float]:
    return [float(m.group(0)) for m in NUMBER_RE.finditer(text)]


def extract_attachments(parsed: ParsedMessage) -> list[AttachmentExtraction]:
    results: list[AttachmentExtraction] = []
    for node in _iter_nodes(parsed.tree):
        if not node.is_attachment:
            continue
        item = AttachmentExtraction(
            part_id=node.part_id,
            filename=node.filename,
            content_type=node.content_type,
        )
        if node.error:  # 坏附件：解码阶段已失败
            item.decode_error = node.error
            results.append(item)
            continue
        if node.text is None:
            item.skipped_reason = "非文本附件，仅记录结构与大小，不提取数值"
            item.source_preview = node.preview
            results.append(item)
            continue
        text = node.text
        item.source_preview = text[:PREVIEW_LIMIT_CSV]
        is_csv = (
            node.content_type in ("text/csv", "application/csv")
            or (node.filename or "").lower().endswith(".csv")
        )
        if is_csv:
            item.numbers = _numbers_from_csv(text)
        else:
            item.numbers = _numbers_from_text(text)
        results.append(item)
    return results


def extract(parsed: ParsedMessage) -> ExtractionResult:
    warnings: list[str] = []
    batch = extract_batch(parsed)
    if batch.conflict:
        warnings.append(batch.conflict_detail or "替代版本批号冲突")
    attachments = extract_attachments(parsed)
    for att in attachments:
        if att.decode_error:
            warnings.append(f"附件 {att.part_id} ({att.filename}) 解码失败: {att.decode_error}")
    if batch.value is None:
        warnings.append("未在任何文本 part 中匹配到实验批号")
    return ExtractionResult(batch=batch, attachments=attachments, warnings=warnings)
