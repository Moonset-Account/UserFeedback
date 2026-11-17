"""Label based domain allow rules.

A rule is one of:

* ``"example.com"``   - exact match of that single name;
* ``".example.com"``  - dotted suffix: matches ``example.com`` and every
  sub-domain such as ``cdn.example.com``;
* ``"*.example.com"`` - wildcard: matches a *single* sub-domain label, e.g.
  ``a.example.com`` but neither ``example.com`` nor ``a.b.example.com``.

Matching always works on whole DNS labels (case insensitive, trailing dot
ignored). Partial-label wildcards such as ``*cdn.example.com`` or
``foo.*.com`` are rejected at rule construction time.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import InvalidRuleError

# DNS label: 1-63 chars, letters/digits/hyphen, not starting/ending with '-'.
_ALLOWED_LABEL_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
)


def normalize_hostname(hostname: str) -> str:
    """Lower-case, strip a single trailing dot and reject obvious non-DNS names.

    IDNA/punycode encoding is deliberately not performed: callers pass already
    encoded ASCII names. Embedded dots, empty labels, overlong labels and
    non-label characters raise :class:`InvalidRuleError`.
    """
    if not isinstance(hostname, str) or not hostname:
        raise InvalidRuleError("hostname must be a non-empty string")
    name = hostname.strip().lower()
    if name.endswith("."):
        name = name[:-1]
    if not name or "." not in name:
        # A bare single label is not an acceptable public target for this proxy.
        raise InvalidRuleError(f"hostname {hostname!r} must contain at least two labels")
    labels = name.split(".")
    for label in labels:
        if not label or len(label) > 63:
            raise InvalidRuleError(f"invalid DNS label in {hostname!r}")
        if label.startswith("-") or label.endswith("-"):
            raise InvalidRuleError(f"invalid DNS label in {hostname!r}")
        if any(ch not in _ALLOWED_LABEL_CHARS for ch in label):
            raise InvalidRuleError(f"invalid character in hostname {hostname!r}")
    if len(name) > 253:
        raise InvalidRuleError(f"hostname {hostname!r} exceeds 253 characters")
    return name


def _validate_label(name: str) -> None:
    normalize_hostname(name)


@dataclass(frozen=True)
class DomainRule:
    """A single compiled domain allow rule."""

    raw: str
    mode: str  # "exact" | "suffix" | "wildcard"
    base: str  # normalized base domain, e.g. "example.com"

    @classmethod
    def parse(cls, rule: str) -> "DomainRule":
        if not isinstance(rule, str) or not rule.strip():
            raise InvalidRuleError("domain rule must be a non-empty string")
        text = rule.strip()
        if text.startswith("*."):
            base = text[2:]
            _validate_label(base)
            return cls(raw=text, mode="wildcard", base=normalize_hostname(base))
        if text.startswith("."):
            base = text[1:]
            _validate_label(base)
            return cls(raw=text, mode="suffix", base=normalize_hostname(base))
        # A literal '*' anywhere else, or a '*' inside a label, is not supported.
        if "*" in text:
            raise InvalidRuleError(
                f"unsupported wildcard rule {rule!r}: only a leading '*.' is allowed"
            )
        return cls(raw=text, mode="exact", base=normalize_hostname(text))

    def matches(self, hostname: str) -> bool:
        try:
            name = normalize_hostname(hostname)
        except InvalidRuleError:
            return False
        if self.mode == "exact":
            return name == self.base
        if self.mode == "suffix":
            if name == self.base:
                return True
            return name.endswith("." + self.base)
        # wildcard: exactly one label beneath the base
        suffix = "." + self.base
        if not name.endswith(suffix):
            return False
        left = name[: -len(suffix)]
        # Left side must be a single, non-empty label.
        return bool(left) and "." not in left


class DomainAllowList:
    """A collection of :class:`DomainRule` objects."""

    def __init__(self, rules: list[str] | tuple[str, ...]) -> None:
        self._rules: tuple[DomainRule, ...] = tuple(
            DomainRule.parse(rule) for rule in rules
        )

    @property
    def rules(self) -> tuple[DomainRule, ...]:
        return self._rules

    def allows(self, hostname: str) -> bool:
        return any(rule.matches(hostname) for rule in self._rules)

    def describe(self) -> list[str]:
        return [rule.raw for rule in self._rules]
