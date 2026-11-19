"""数据模型：插件原始建议、归一后的建议与合并报告。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .coordinates import SpanError, line_utf16_to_indices, split_lines_preserving, utf8_offsets_to_indices

PLUGIN_UTF8_BYTES = "utf8_bytes"
PLUGIN_LINE_UTF16 = "line_utf16"
_KNOWN_PLUGINS = (PLUGIN_UTF8_BYTES, PLUGIN_LINE_UTF16)


@dataclass(frozen=True)
class RawEdit:
    """单条替换建议（插件原始坐标）。

    两种坐标的字段约定：

    * ``utf8_bytes``：使用 ``start_byte`` / ``end_byte``；
    * ``line_utf16``：使用 ``line`` / ``start_utf16`` / ``end_utf16``，
      行号 0 基，列为行内 UTF-16 码元偏移。
    """

    file: str
    plugin: str
    old_text: str
    new_text: str
    version: str
    start_byte: int | None = None
    end_byte: int | None = None
    line: int | None = None
    start_utf16: int | None = None
    end_utf16: int | None = None


@dataclass(frozen=True)
class RawGroup:
    """一组原子替换建议：任一条目校验失败则整组撤下。"""

    group_id: str
    alert_id: str
    edits: tuple[RawEdit, ...]
    version: str | None = None


@dataclass(frozen=True)
class ResolvedEdit:
    """坐标归一到字符索引后的替换区间，区间为半开 [start, end)。"""

    file: str
    start: int
    end: int
    old_text: str
    new_text: str
    version: str
    plugin: str
    group_id: str
    alert_id: str

    @property
    def key(self) -> tuple[str, int, int, str, str]:
        """同一区间且替换完全相同的编辑共用此键。"""

        return (self.file, self.start, self.end, self.old_text, self.new_text)


@dataclass
class SuggestionGroup:
    """解析成功后的组。"""

    group_id: str
    alert_id: str
    version: str | None
    edits: list[ResolvedEdit] = field(default_factory=list)


@dataclass(frozen=True)
class Withdrawal:
    """撤下记录：校验失败或断言失效都会产生。"""

    group_id: str
    alert_id: str | None
    reason: str
    files: tuple[str, ...]
    stage: str
    triggered_alert: str | None = None


@dataclass(frozen=True)
class ConflictRecord:
    """被搁置的直接冲突（同文件区间重叠且改动不同）。"""

    file: str
    left_group: str
    right_group: str
    left_key: tuple[Any, ...]
    right_key: tuple[Any, ...]


@dataclass(frozen=True)
class DedupRecord:
    """去重记录：独立组在同一区间给出完全相同的替换。"""

    key: tuple[Any, ...]
    kept_group: str
    merged_groups: tuple[str, ...]
    file: str


def _require_str(mapping: dict[str, Any], key: str, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{context} 缺少字符串字段 {key!r}")
    return value


def _optional_int(mapping: dict[str, Any], key: str) -> int | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"字段 {key!r} 必须是整数")
    return value


def parse_edit(data: dict[str, Any]) -> RawEdit:
    """解析单条插件建议字典，结构错误抛 ``ValueError``。"""

    if not isinstance(data, dict):
        raise ValueError("edit 必须是对象")
    plugin = _require_str(data, "plugin", "edit")
    if plugin not in _KNOWN_PLUGINS:
        raise ValueError(f"未知插件坐标类型 {plugin!r}")
    base = dict(
        file=_require_str(data, "file", "edit"),
        plugin=plugin,
        old_text=_require_str(data, "old_text", "edit"),
        new_text=_require_str(data, "new_text", "edit"),
        version=_require_str(data, "version", "edit"),
    )
    if plugin == PLUGIN_UTF8_BYTES:
        start_byte = _optional_int(data, "start_byte")
        end_byte = _optional_int(data, "end_byte")
        if start_byte is None or end_byte is None:
            raise ValueError("utf8_bytes 建议需要 start_byte/end_byte")
        return RawEdit(start_byte=start_byte, end_byte=end_byte, **base)
    line = _optional_int(data, "line")
    start_unit = _optional_int(data, "start_utf16")
    end_unit = _optional_int(data, "end_utf16")
    if line is None or start_unit is None or end_unit is None:
        raise ValueError("line_utf16 建议需要 line/start_utf16/end_utf16")
    return RawEdit(line=line, start_utf16=start_unit, end_utf16=end_unit, **base)


def parse_group(data: dict[str, Any]) -> RawGroup:
    """解析一组建议字典。"""

    if not isinstance(data, dict):
        raise ValueError("group 必须是对象")
    group_id = _require_str(data, "group_id", "group")
    alert_id = _require_str(data, "alert_id", "group")
    raw_edits = data.get("edits")
    if not isinstance(raw_edits, list) or not raw_edits:
        raise ValueError(f"组 {group_id} 的 edits 必须是非空数组")
    edits = tuple(parse_edit(item) for item in raw_edits)
    version = data.get("version")
    if version is not None and not isinstance(version, str):
        raise ValueError(f"组 {group_id} 的 version 必须是字符串")
    return RawGroup(group_id=group_id, alert_id=alert_id, edits=edits, version=version)


def coerce_groups(data: Any) -> list[RawGroup]:
    """接受字典列表或已构造的 :class:`RawGroup` 列表。"""

    if not isinstance(data, list) or not data:
        raise ValueError("建议必须是非空组数组")
    groups: list[RawGroup] = []
    for item in data:
        if isinstance(item, RawGroup):
            groups.append(item)
        else:
            groups.append(parse_group(item))
    return groups


def resolve_edit(edit: RawEdit, group_id: str, alert_id: str, text: str, line_table) -> ResolvedEdit:
    """把原始坐标解析为字符索引区间，坐标非法抛 :class:`SpanError`。"""

    if edit.plugin == PLUGIN_UTF8_BYTES:
        start, end = utf8_offsets_to_indices(text, edit.start_byte, edit.end_byte)
    else:
        start, end = line_utf16_to_indices(
            text, edit.line, edit.start_utf16, edit.end_utf16, lines=line_table
        )
    return ResolvedEdit(
        file=edit.file,
        start=start,
        end=end,
        old_text=edit.old_text,
        new_text=edit.new_text,
        version=edit.version,
        plugin=edit.plugin,
        group_id=group_id,
        alert_id=alert_id,
    )


def build_line_tables(files: dict[str, str]) -> dict[str, list]:
    return {path: split_lines_preserving(text) for path, text in files.items()}
