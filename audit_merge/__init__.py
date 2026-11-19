"""审计插件修复建议合并模块。

两款审计插件针对同一版源码提交修复建议：

* ``utf8_bytes`` 插件按 UTF-8 字节偏移标注区间；
* ``line_utf16`` 插件按行号 + 行内 UTF-16 码元列标注区间。

模块负责坐标归一、旧文本/版本校验、同改去重、冲突搁置、
临时副本上的保护断言调用以及分簇回滚。
"""

from .coordinates import (
    SpanError,
    line_utf16_to_indices,
    split_lines_preserving,
    utf8_offsets_to_indices,
)
from .merger import AssertionContext, MergeReport, Workspace, merge_suggestions
from .model import (
    ConflictRecord,
    DedupRecord,
    RawEdit,
    RawGroup,
    ResolvedEdit,
    SuggestionGroup,
    Withdrawal,
    coerce_groups,
)

PLUGIN_UTF8_BYTES = "utf8_bytes"
PLUGIN_LINE_UTF16 = "line_utf16"

__all__ = [
    "PLUGIN_UTF8_BYTES",
    "PLUGIN_LINE_UTF16",
    "SpanError",
    "AssertionContext",
    "ConflictRecord",
    "DedupRecord",
    "MergeReport",
    "RawEdit",
    "RawGroup",
    "ResolvedEdit",
    "SuggestionGroup",
    "Withdrawal",
    "Workspace",
    "coerce_groups",
    "line_utf16_to_indices",
    "merge_suggestions",
    "split_lines_preserving",
    "utf8_offsets_to_indices",
]
