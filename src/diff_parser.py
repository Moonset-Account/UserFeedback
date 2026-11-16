"""Fail-closed adapter from the strict diff parser to Scanner models."""

from dataclasses import dataclass, field

from .strict_diff import DiffParseError, parse_diff as parse_strict_diff


@dataclass
class DiffLine:
    """A single line in a diff."""

    content: str
    line_type: str
    line_number: int = 0
    hunk_id: int = 0


@dataclass
class DiffFile:
    """A file changed in a validated unified diff."""

    source_path: str
    target_path: str
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False
    added_lines: list[DiffLine] = field(default_factory=list)
    removed_lines: list[DiffLine] = field(default_factory=list)
    target_lines: list[DiffLine] = field(default_factory=list)


def _line(value) -> DiffLine:
    return DiffLine(
        content=value.content,
        line_type=value.line_type,
        line_number=value.line_number,
        hunk_id=value.hunk_id,
    )


def parse_diff(diff_text: str) -> list[DiffFile]:
    """Parse a bounded diff or raise ``DiffParseError`` on unsafe/malformed input."""

    strict_files = parse_strict_diff(diff_text)
    result = []
    for item in strict_files:
        result.append(DiffFile(
            source_path=item.source_path,
            target_path=item.target_path,
            is_new=item.operation == "create",
            is_deleted=item.operation == "delete",
            is_binary=item.is_binary,
            added_lines=[_line(line) for line in item.added_lines],
            removed_lines=[_line(line) for line in item.removed_lines],
            target_lines=[_line(line) for line in item.target_lines],
        ))
    return result


def get_target_path(diff_file: DiffFile) -> str:
    """Get the effective validated path from a ``DiffFile``."""

    if diff_file.is_new:
        return diff_file.target_path
    if diff_file.is_deleted:
        return diff_file.source_path
    return diff_file.target_path if diff_file.target_path != "/dev/null" else diff_file.source_path


__all__ = ["DiffFile", "DiffLine", "DiffParseError", "parse_diff", "get_target_path"]
