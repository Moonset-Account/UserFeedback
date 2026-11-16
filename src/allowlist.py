"""Allowlist parser for the Secret/Config Diff Scanner.

Supports .secretsallowlist files with pattern, value, and path-based suppression rules.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from .safeio import read_regular_bounded

MAX_ALLOWLIST_BYTES = 256 * 1024
MAX_ALLOWLIST_LINES = 4096
MAX_ALLOWLIST_LINE_CHARS = 1024
MAX_ALLOWLIST_RULES = 1024


@dataclass
class AllowlistRule:
    """A single allowlist rule."""
    rule_type: str  # "pattern", "value", "path"
    pattern: str = ""   # Pattern name to suppress
    path: str = ""      # File path glob to suppress in
    value: str = ""     # Exact value to suppress
    severity: str = ""   # Optional severity filter
    compiled_path: re.Pattern | None = None  # Compiled glob pattern

    def __post_init__(self):
        if self.path and self.compiled_path is None:
            self.compiled_path = _compile_glob(self.path)


def parse_allowlist(text: str) -> list[AllowlistRule]:
    """Parse an allowlist file content into rules.

    Format:
    # Comments start with #
    # Blank lines are ignored

    # Suppress by pattern name (optionally with path or severity)
    pattern:PATTERN_NAME path:glob/pattern
    pattern:PATTERN_NAME severity:low

    # Suppress by exact value
    value:exact_secret_text

    # Suppress by file path
    path:tests/**
    path:examples/**
    """
    lines = text.split("\n")
    if len(lines) > MAX_ALLOWLIST_LINES or any(len(line) > MAX_ALLOWLIST_LINE_CHARS for line in lines):
        raise ValueError("Allowlist exceeds line limits")
    rules = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("pattern:"):
            rule = AllowlistRule(rule_type="pattern")
            parts = line.split()
            for part in parts:
                if part.startswith("pattern:"):
                    rule.pattern = part[8:]
                elif part.startswith("path:"):
                    rule.path = part[5:]
                    rule.compiled_path = _compile_glob(rule.path)
                elif part.startswith("severity:"):
                    rule.severity = part[9:]
            rules.append(rule)

        elif line.startswith("value:"):
            rule = AllowlistRule(rule_type="value", value=line[6:])
            rules.append(rule)

        elif line.startswith("path:"):
            path_glob = line[5:]
            rule = AllowlistRule(rule_type="path", path=path_glob)
            rule.compiled_path = _compile_glob(path_glob)
            rules.append(rule)

    if len(rules) > MAX_ALLOWLIST_RULES:
        raise ValueError("Allowlist exceeds rule count limit")
    return rules


def load_allowlist(path: str) -> list[AllowlistRule]:
    """Load and parse a bounded regular non-symlink allowlist file."""
    filepath = Path(path)
    if not filepath.exists() and not filepath.is_symlink():
        return []
    try:
        raw = read_regular_bounded(filepath, MAX_ALLOWLIST_BYTES, "allowlist")
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Invalid or unsafe allowlist file") from exc
    return parse_allowlist(text)


def is_suppressed(
    finding_pattern_name: str,
    finding_severity: str,
    finding_file: str,
    finding_matched_text: str,
    rules: list[AllowlistRule],
) -> bool:
    """Check if a finding should be suppressed based on safe placeholders or allowlist rules."""
    placeholder = re.search(
        r"\$\{[A-Za-z_][A-Za-z0-9_]*(?:(?:\}|$)|:-(?:placeholder|default|example|dummy|changeme|replace[_-]?me)(?:\}|$))",
        finding_matched_text or "",
        re.IGNORECASE,
    )
    if placeholder:
        return True

    for rule in rules:
        if rule.rule_type == "pattern":
            # Pattern name must match
            if rule.pattern and rule.pattern != finding_pattern_name:
                continue
            # Severity must match if specified
            if rule.severity and rule.severity != finding_severity:
                continue
            # Path must match if specified
            if rule.compiled_path and not _glob_match(rule.compiled_path, finding_file):
                continue
            return True

        elif rule.rule_type == "value":
            if finding_matched_text and rule.value in finding_matched_text:
                return True

        elif rule.rule_type == "path":
            if rule.compiled_path and _glob_match(rule.compiled_path, finding_file):
                return True

    return False


def _compile_glob(glob_pattern: str) -> re.Pattern:
    """Convert a glob pattern to a compiled regex.

    Supports * (any chars except /), ** (any chars including /), and ? (single char).
    """
    regex = ""
    i = 0
    while i < len(glob_pattern):
        c = glob_pattern[i]
        if c == "*":
            if i + 1 < len(glob_pattern) and glob_pattern[i + 1] == "*":
                regex += ".*"
                i += 2
                # Skip trailing /
                if i < len(glob_pattern) and glob_pattern[i] == "/":
                    regex += "/?"
                    i += 1
            else:
                regex += "[^/]*"
                i += 1
        elif c == "?":
            regex += "[^/]"
            i += 1
        elif c in ".+^${}()|[]":
            regex += "\\" + c
            i += 1
        else:
            regex += c
            i += 1

    return re.compile("^" + regex + "$")


def _glob_match(pattern: re.Pattern, path: str) -> bool:
    """Check if a path matches a compiled glob pattern."""
    return bool(pattern.match(path))