"""Version parsing and version intervals used by the module planner."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering
from typing import Iterable, Sequence


class VersionParseError(ValueError):
    """Raised when a version or version interval cannot be parsed."""


_RELEASE_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def _compare_numeric_sequences(a: Sequence[int], b: Sequence[int]) -> int:
    length = max(len(a), len(b))
    for index in range(length):
        left = a[index] if index < len(a) else 0
        right = b[index] if index < len(b) else 0
        if left != right:
            return -1 if left < right else 1
    return 0


@total_ordering
@dataclass(frozen=True)
class Version:
    """A small semantic-version-like value.

    Missing trailing components are treated as zero so ``1.2`` equals
    ``1.2.0`` for interval matching.
    """

    parts: tuple[int, ...]
    prerelease: tuple[object, ...] | None = None
    text: str | None = None

    @classmethod
    def parse(cls, value: "Version | str") -> "Version":
        if isinstance(value, Version):
            return value
        if not isinstance(value, str):
            raise VersionParseError(f"version must be a string, got {type(value)!r}")
        match = _RELEASE_RE.match(value.strip())
        if not match:
            raise VersionParseError(f"invalid version: {value!r}")
        parts = tuple(int(part) for part in match.group(1).split("."))
        prerelease_text = match.group(2)
        prerelease: tuple[object, ...] | None = None
        if prerelease_text:
            prerelease = tuple(
                int(identifier) if identifier.isdigit() else identifier
                for identifier in prerelease_text.split(".")
                if identifier
            )
        return cls(parts=parts, prerelease=prerelease, text=value.strip())

    def __str__(self) -> str:
        if self.text is not None:
            return self.text
        release = ".".join(str(part) for part in self.parts)
        if self.prerelease:
            identifiers = ".".join(str(part) for part in self.prerelease)
            return f"{release}-{identifiers}"
        return release

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return (
            _compare_numeric_sequences(self.parts, other.parts) == 0
            and Version._compare_prerelease(self.prerelease, other.prerelease) == 0
        )

    def __hash__(self) -> int:
        return hash((self.parts, self.prerelease))

    @staticmethod
    def _compare_prerelease(
        left: tuple[object, ...] | None, right: tuple[object, ...] | None
    ) -> int:
        # A stable release is newer than a prerelease of the same release.
        if left is None and right is None:
            return 0
        if left is None:
            return 1
        if right is None:
            return -1
        for l_item, r_item in zip(left, right):
            l_is_int = isinstance(l_item, int)
            r_is_int = isinstance(r_item, int)
            if l_is_int and r_is_int:
                assert isinstance(l_item, int) and isinstance(r_item, int)
                if l_item != r_item:
                    return -1 if l_item < r_item else 1
            elif l_is_int:
                return -1
            elif r_is_int:
                return 1
            else:
                assert isinstance(l_item, str) and isinstance(r_item, str)
                if l_item != r_item:
                    return -1 if l_item < r_item else 1
        return len(left) - len(right)

    def __lt__(self, other: "Version") -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        release_cmp = _compare_numeric_sequences(self.parts, other.parts)
        if release_cmp != 0:
            return release_cmp < 0
        return self._compare_prerelease(self.prerelease, other.prerelease) < 0

    @property
    def major(self) -> int:
        return self.parts[0] if self.parts else 0

    @property
    def minor(self) -> int:
        return self.parts[1] if len(self.parts) > 1 else 0

    @property
    def patch(self) -> int:
        return self.parts[2] if len(self.parts) > 2 else 0


@dataclass(frozen=True)
class VersionRange:
    """An intersection of version comparators.

    Accepts ordinary comparator strings (``>=1.2,<2.0``), npm-like caret and
    tilde ranges, and mathematical intervals such as ``[1.0,2.0)``.
    """

    text: str
    comparators: tuple[tuple[str, Version], ...] = ()
    exact: Version | None = None
    any_version: bool = False

    @classmethod
    def parse(cls, value: "VersionRange | str | None") -> "VersionRange":
        if value is None:
            return cls.any()
        if isinstance(value, VersionRange):
            return value
        if not isinstance(value, str):
            raise VersionParseError(f"version range must be a string, got {type(value)!r}")

        raw = value.strip()
        if raw in ("", "*", "x", "X"):
            return cls(text=raw or "*", any_version=True)

        if raw[0] in "[(]" and raw[-1] in "[])]":
            return cls._parse_interval(raw)

        comparators: list[tuple[str, Version]] = []
        for token in _split_range_tokens(raw):
            parsed = _parse_comparator_token(token)
            comparators.extend(parsed)

        exact = None
        if len(comparators) == 1 and comparators[0][0] in ("=", "=="):
            exact = comparators[0][1]
        return cls(text=raw, comparators=tuple(comparators), exact=exact)

    @classmethod
    def any(cls) -> "VersionRange":
        return cls(text="*", any_version=True)

    @classmethod
    def exact_version(cls, version: Version | str) -> "VersionRange":
        parsed = Version.parse(version)
        return cls(text=f"={parsed}", comparators=(("=", parsed),), exact=parsed)

    @classmethod
    def _parse_interval(cls, raw: str) -> "VersionRange":
        lower_inclusive = raw[0] == "["
        upper_inclusive = raw[-1] == "]"
        body = raw[1:-1]
        if "," not in body:
            raise VersionParseError(f"invalid version interval: {raw!r}")
        lower_text, upper_text = (part.strip() for part in body.split(",", 1))
        comparators: list[tuple[str, Version]] = []
        if lower_text:
            lower = Version.parse(lower_text)
            comparators.append((">=" if lower_inclusive else ">", lower))
        if upper_text:
            upper = Version.parse(upper_text)
            comparators.append(("<=" if upper_inclusive else "<", upper))
        return cls(text=raw, comparators=tuple(comparators))

    def contains(self, version: Version | str) -> bool:
        parsed = Version.parse(version)
        if self.any_version:
            return True
        for operator, expected in self.comparators:
            if operator == ">=" and not parsed >= expected:
                return False
            if operator == "<=" and not parsed <= expected:
                return False
            if operator == ">" and not parsed > expected:
                return False
            if operator == "<" and not parsed < expected:
                return False
            if operator in ("=", "==") and parsed != expected:
                return False
            if operator == "!=" and parsed == expected:
                return False
        return True

    def filter(self, versions: Iterable[Version | str]) -> list[Version]:
        return [Version.parse(v) for v in versions if self.contains(v)]

    def best(
        self,
        versions: Iterable[Version | str],
        preferred: Version | str | None = None,
    ) -> Version | None:
        matching = self.filter(versions)
        if not matching:
            return None
        if preferred is not None:
            preferred_version = Version.parse(preferred)
            if preferred_version in matching:
                return preferred_version
        return max(matching)

    def __str__(self) -> str:
        return self.text


def _split_range_tokens(raw: str) -> list[str]:
    # Commas and whitespace both act as AND separators.  Keeping this small
    # avoids introducing a dependency on a full semver library.
    tokens: list[str] = []
    current: list[str] = []
    operators = {">=", "<=", "==", "!="}
    index = 0
    normalized = raw.replace(",", " ")
    while index < len(normalized):
        char = normalized[index]
        if char.isspace():
            if current:
                tokens.append("".join(current).strip())
                current = []
            index += 1
            continue
        if normalized[index : index + 2] in operators:
            if current:
                tokens.append("".join(current).strip())
                current = []
            current.append(normalized[index : index + 2])
            index += 2
            continue
        if char in "><=^~":
            if current and current[-1] not in "><=":
                tokens.append("".join(current).strip())
                current = []
            current.append(char)
            index += 1
            continue
        current.append(char)
        index += 1
    if current:
        tokens.append("".join(current).strip())
    return [token for token in tokens if token]


def _parse_comparator_token(token: str) -> list[tuple[str, Version]]:
    token = token.strip()
    if not token:
        return []
    for op in (">=", "<=", "==", "!=", ">", "<", "=", "^", "~"):
        if token.startswith(op):
            version_text = token[len(op) :].strip()
            if op == "^":
                return list(_caret_range(Version.parse(version_text)))
            if op == "~":
                return list(_tilde_range(Version.parse(version_text)))
            normalized_op = "=" if op == "=" else op
            return [(normalized_op, Version.parse(version_text))]
    if token in ("*", "x", "X"):
        return []
    if token.endswith(".x") or token.endswith(".X"):
        prefix = token[:-2].strip(".")
        parts = prefix.split(".")
        if len(parts) == 1:
            major = int(parts[0])
            return [(">=", Version.parse(f"{major}.0.0")), ("<", Version.parse(f"{major + 1}.0.0"))]
        major, minor = int(parts[0]), int(parts[1])
        return [
            (">=", Version.parse(f"{major}.{minor}.0")),
            ("<", Version.parse(f"{major}.{minor + 1}.0")),
        ]
    return [("=", Version.parse(token))]


def _caret_range(version: Version) -> tuple[tuple[str, Version], ...]:
    major, minor, patch = version.major, version.minor, version.patch
    lower = version
    if major > 0:
        upper = Version.parse(f"{major + 1}.0.0")
    elif minor > 0:
        upper = Version.parse(f"0.{minor + 1}.0")
    else:
        upper = Version.parse(f"0.0.{patch + 1}")
    return ((">=", lower), ("<", upper))


def _tilde_range(version: Version) -> tuple[tuple[str, Version], ...]:
    major, minor = version.major, version.minor
    lower = Version.parse(f"{major}.{minor}.{version.patch}")
    upper = Version.parse(f"{major}.{minor + 1}.0")
    return ((">=", lower), ("<", upper))
