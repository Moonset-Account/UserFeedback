"""Path-template parsing, matching, and pairwise set analysis.

Grammar
-------
A template is a sequence of slash-separated segments:

* a *literal* segment is any non-empty text without braces;
* ``{name}`` is a single-segment parameter and matches any non-empty segment;
* a final ``**`` matches zero or more further segments.

The empty sequence (the template ``"/"``) is the root path.  A trailing
slash, a leading empty segment, and consecutive slashes are not valid in
templates or request paths.

Because ``**`` may only appear as the last segment, every template language
is determined by a fixed prefix of literal/parameter matchers and a flag that
allows any number of additional arbitrary segments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from .errors import PathError, TemplateError

__all__ = [
    "Template",
    "TemplatePair",
    "parse_template",
    "split_path",
    "analyze_templates",
]

_PARAM_EXAMPLE = "x"


@dataclass(frozen=True)
class _Literal:
    value: str

    def accepts(self, segment: str) -> bool:
        return segment == self.value

    def example(self) -> str:
        return self.value


@dataclass(frozen=True)
class _Param:
    name: str

    def accepts(self, segment: str) -> bool:
        return segment != ""

    def example(self) -> str:
        return _PARAM_EXAMPLE


_Segment = Union[_Literal, _Param]


def split_path(path: str) -> tuple[str, ...]:
    """Split a canonical request path into non-empty segments.

    ``"/"`` is the root and corresponds to an empty segment tuple.  Paths
    without a leading slash, paths with empty segments (including a trailing
    slash), and non-string values are rejected.
    """

    if not isinstance(path, str):
        raise PathError(path, "path must be a string")
    if not path.startswith("/"):
        raise PathError(path, "path must start with '/'")
    if path == "/":
        return ()
    segments = tuple(path[1:].split("/"))
    if any(segment == "" for segment in segments):
        raise PathError(path, "path must not contain an empty segment")
    return segments


@dataclass(frozen=True)
class Template:
    """A parsed path template.

    ``prefix`` contains the fixed literal/parameter matchers.  When
    ``has_star`` is true, a trailing ``**`` accepts any number (including
    zero) of additional segments after that prefix.
    """

    text: str
    prefix: tuple[_Segment, ...]
    has_star: bool

    @property
    def prefix_length(self) -> int:
        return len(self.prefix)

    def __str__(self) -> str:
        return self.text

    def match_segments(self, segments: tuple[str, ...]) -> dict[str, str] | None:
        """Match already-split path segments.

        Returns captured parameter values on success and ``None`` on failure.
        If a parameter name occurs more than once in a template, the last
        occurrence wins in the returned mapping.
        """

        if self.has_star:
            if len(segments) < len(self.prefix):
                return None
        elif len(segments) != len(self.prefix):
            return None

        params: dict[str, str] = {}
        for matcher, segment in zip(self.prefix, segments):
            if not matcher.accepts(segment):
                return None
            if isinstance(matcher, _Param):
                params[matcher.name] = segment
        return params

    def match_path(self, path: str) -> dict[str, str] | None:
        """Match a request path string; see :func:`split_path`."""

        return self.match_segments(split_path(path))


def parse_template(text: str) -> Template:
    """Parse and validate one path template."""

    if not isinstance(text, str):
        raise TemplateError(text, "template must be a string")
    if not text.startswith("/"):
        raise TemplateError(text, "template must start with '/'")
    if text == "/":
        return Template(text=text, prefix=(), has_star=False)

    raw_segments = text[1:].split("/")
    if any(segment == "" for segment in raw_segments):
        raise TemplateError(text, "template must not contain an empty segment")

    prefix: list[_Segment] = []
    has_star = False
    for index, segment in enumerate(raw_segments):
        if segment == "**":
            if index != len(raw_segments) - 1:
                raise TemplateError(text, "trailing '**' is only allowed as the final segment")
            has_star = True
            continue

        if "{" in segment or "}" in segment:
            if not (segment.startswith("{") and segment.endswith("}")):
                raise TemplateError(
                    text,
                    f"segment {segment!r} must be a literal or a whole '{{name}}' parameter",
                )
            name = segment[1:-1]
            if not name:
                raise TemplateError(text, "parameter name must not be empty")
            if "{" in name or "}" in name:
                raise TemplateError(text, f"parameter segment {segment!r} contains a brace")
            if "*" in name:
                raise TemplateError(text, f"parameter name {name!r} must not contain '*'")
            if any(character.isspace() for character in name):
                raise TemplateError(text, f"parameter name {name!r} must not contain whitespace")
            prefix.append(_Param(name))
            continue

        prefix.append(_Literal(segment))

    return Template(text=text, prefix=tuple(prefix), has_star=has_star)


def _as_template(value: str | Template) -> Template:
    if isinstance(value, Template):
        return value
    if isinstance(value, str):
        return parse_template(value)
    raise TemplateError(value, "expected a Template or a template string")


def _segment_subset(left: _Segment, right: _Segment) -> bool:
    if isinstance(right, _Param):
        return True
    return isinstance(left, _Literal) and left.value == right.value


def _segment_witness(left: _Segment, right: _Segment) -> str | None:
    if isinstance(left, _Literal) and isinstance(right, _Literal):
        return left.value if left.value == right.value else None
    if isinstance(left, _Literal):
        return left.value
    if isinstance(right, _Literal):
        return right.value
    return _PARAM_EXAMPLE


def _is_subset(left: Template, right: Template) -> bool:
    left_len = left.prefix_length
    right_len = right.prefix_length

    if not left.has_star:
        if not right.has_star:
            if left_len != right_len:
                return False
        elif left_len < right_len:
            return False
    else:
        # An infinite language cannot be a subset of a single fixed length.
        if not right.has_star:
            return False
        if left_len < right_len:
            return False

    return all(
        _segment_subset(left.prefix[index], right.prefix[index])
        for index in range(right_len)
    )


def _intersection_witness(left: Template, right: Template) -> str | None:
    left_len = left.prefix_length
    right_len = right.prefix_length

    if not left.has_star and not right.has_star:
        if left_len != right_len:
            return None
        length = left_len
    elif left.has_star and not right.has_star:
        if right_len < left_len:
            return None
        length = right_len
    elif not left.has_star and right.has_star:
        if left_len < right_len:
            return None
        length = left_len
    else:
        length = max(left_len, right_len)

    common = min(left_len, right_len)
    for index in range(common):
        if _segment_witness(left.prefix[index], right.prefix[index]) is None:
            return None

    witness_segments: list[str] = []
    for index in range(length):
        if index < common:
            value = _segment_witness(left.prefix[index], right.prefix[index])
            assert value is not None
            witness_segments.append(value)
        elif index < left_len:
            witness_segments.append(left.prefix[index].example())
        else:
            witness_segments.append(right.prefix[index].example())

    return "/" + "/".join(witness_segments) if witness_segments else "/"


@dataclass(frozen=True)
class TemplatePair:
    """Result of comparing the path sets of two templates."""

    a: Template
    b: Template
    intersects: bool
    witness_path: str | None
    a_subset_b: bool
    b_subset_a: bool

    @property
    def equal(self) -> bool:
        return self.a_subset_b and self.b_subset_a

    @property
    def kind(self) -> str:
        if not self.intersects:
            return "disjoint"
        if self.equal:
            return "equal"
        if self.a_subset_b:
            return "a_subset_b"
        if self.b_subset_a:
            return "b_subset_a"
        return "overlap"

    @property
    def conflict(self) -> bool:
        """True when both rules may not be registered together."""

        return self.kind in {"equal", "overlap"}


def analyze_templates(a: str | Template, b: str | Template) -> TemplatePair:
    """Compare two templates' languages exactly.

    The decision procedure is exact for the supported grammar (no
    backtracking, no interior ``**``).  When the languages intersect, a
    concrete witness path matching both templates is included.
    """

    left = _as_template(a)
    right = _as_template(b)
    witness = _intersection_witness(left, right)
    return TemplatePair(
        a=left,
        b=right,
        intersects=witness is not None,
        witness_path=witness,
        a_subset_b=_is_subset(left, right),
        b_subset_a=_is_subset(right, left),
    )
