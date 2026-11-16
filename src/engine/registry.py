"""Compiled rule registry used by the scanner evaluator."""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import re
from typing import Iterable

from ..patterns import DetectionRule


@dataclass(frozen=True)
class CompiledRule:
    """Immutable compiled representation of one declarative detection rule."""
    rule: DetectionRule
    compiled: re.Pattern[str]
    literals: tuple[str, ...]
    order: int


@dataclass(frozen=True)
class FilePlan:
    """Candidate rule plan for one path and line."""
    path: str
    candidates: tuple[CompiledRule, ...]
    literal_prefiltered: bool


class RuleRegistry:
    """Precompile rules and produce bounded candidate plans."""

    def __init__(self, rules: Iterable[DetectionRule]):
        self._compiled = tuple(
            CompiledRule(rule, rule.compiled, self._extract_literals(rule.regex), order)
            for order, rule in enumerate(rules)
        )
        self._by_extension: dict[str, tuple[CompiledRule, ...]] = {}
        for item in self._compiled:
            for glob in item.rule.file_globs:
                suffix = glob.rsplit(".", 1)[-1].lower() if "." in glob else "*"
                self._by_extension.setdefault(suffix, tuple())
                self._by_extension[suffix] += (item,)

    @staticmethod
    def _extract_literals(regex: str) -> tuple[str, ...]:
        """Extract safe literal runs for a conservative prefilter."""
        # A literal prefilter is only safe for plain regexes. Alternation,
        # character classes, escapes and quantifiers may make every apparent
        # literal optional; those rules remain in the candidate plan.
        if any(marker in regex for marker in ("[", "]", "(", ")", "{", "}", "?", "*", "+", "|", "\\\\", "^", "$")):
            return ()
        runs = re.findall(r"[A-Za-z0-9_]{4,}", regex)
        return tuple(sorted(set(run.lower() for run in runs), key=lambda value: (-len(value), value)))

    @property
    def rules(self) -> tuple[CompiledRule, ...]:
        return self._compiled

    def plan(self, path: str, line: str) -> FilePlan:
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.lower()
        suffix = normalized.rsplit(".", 1)[-1] if "." in normalized else "*"
        candidates: list[CompiledRule] = []
        for item in self._compiled:
            globs = item.rule.file_globs
            if globs and not any(fnmatch.fnmatchcase(normalized, glob.lower()) or (glob.startswith("**/") and fnmatch.fnmatchcase(normalized, glob[3:].lower())) for glob in globs):
                continue
            if item.literals and not any(literal in line.lower() for literal in item.literals):
                continue
            candidates.append(item)
        return FilePlan(path=path, candidates=tuple(candidates), literal_prefiltered=True)
