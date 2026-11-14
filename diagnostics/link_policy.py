"""Documentation-link policy.

Rules enforced here:

* Only ``https`` URLs are eligible.
* The **parsed hostname** (``urllib.parse`` hostname component, lowercased and
  IDNA-canonicalised) must exactly match an entry in the configured allowlist.
* URLs carrying credentials (``user:pass@`` / ``user@``) are rejected.
* Raw control characters, whitespace, backslashes and bidi/format characters
  are rejected before parsing, because browsers silently strip or reinterpret
  them (``https://good.example\\n.evil.example`` style bypasses).

Anything rejected is rendered as plain text by the page renderer; a rejected
link never removes the record or its neighbours from the page.
"""

from __future__ import annotations

import ipaddress
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Raised when the allowlist configuration is malformed."""


# Machine-readable decision codes.
OK = "ok"
EMPTY_URL = "empty_url"
CONTROL_CHARS = "control_chars"
BACKSLASH = "backslash"
MALFORMED_URL = "malformed_url"
SCHEME_NOT_HTTPS = "scheme_not_https"
MISSING_HOST = "missing_host"
CREDENTIALS = "credentials"
INVALID_PORT = "invalid_port"
MALFORMED_HOST = "malformed_host"
HOST_NOT_ALLOWED = "host_not_allowed"

_HUMAN_NOTES = {
    EMPTY_URL: "地址为空",
    CONTROL_CHARS: "包含空白、控制或不可见格式字符",
    BACKSLASH: "包含反斜杠（浏览器会把它当作路径分隔符）",
    MALFORMED_URL: "地址无法解析",
    SCHEME_NOT_HTTPS: "不是 HTTPS 地址",
    MISSING_HOST: "缺少主机名",
    CREDENTIALS: "包含凭据（用户名或密码）",
    INVALID_PORT: "端口无效",
    MALFORMED_HOST: "主机名格式无效",
    HOST_NOT_ALLOWED: "主机不在文档链接白名单内",
}

# Hostname labels: letters/digits/hyphen, no leading/trailing hyphen.
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)


@dataclass(frozen=True)
class LinkDecision:
    """Outcome of checking one documentation URL."""

    url: str
    allowed: bool
    reason: str
    host: str | None

    @property
    def note(self) -> str:
        return _HUMAN_NOTES.get(self.reason, self.reason)

    def as_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "allowed": self.allowed,
            "reason": self.reason,
            "host": self.host,
            "note": self.note,
        }


def _reject(code: str, url: str, host: str | None = None) -> LinkDecision:
    return LinkDecision(url=url, allowed=False, reason=code, host=host)


def _allow(url: str, host: str) -> LinkDecision:
    return LinkDecision(url=url, allowed=True, reason=OK, host=host)


def canonicalize_host(raw_host: str) -> tuple[str | None, str | None]:
    """Return ``(canonical_host, error_code)`` for a parsed hostname.

    Canonical form is lowercase, no trailing root dot, ASCII/IDNA.  IPv6
    literals are accepted in canonical compressed-less form (standard library
    ``ipaddress`` normalisation).
    """
    host = raw_host.strip().lower()
    if host == "":
        return None, MISSING_HOST
    if ":" in host:  # IPv6 literal
        try:
            return str(ipaddress.ip_address(host)), None
        except ValueError:
            return None, MALFORMED_HOST
    host = host.rstrip(".")
    if host == "":
        return None, MALFORMED_HOST
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None, MALFORMED_HOST
    if not _HOST_RE.fullmatch(host):
        return None, MALFORMED_HOST
    return host, None


class LinkPolicy:
    """Allowlist-backed documentation link checker."""

    def __init__(self, allowed_hosts: Iterable[str]) -> None:
        canonical: list[str] = []
        for entry in allowed_hosts:
            if not isinstance(entry, str):
                raise ConfigError(f"allowlist entry must be a string, got {type(entry).__name__}")
            if entry.strip() != entry or entry == "":
                raise ConfigError(f"allowlist entry {entry!r} has surrounding whitespace or is empty")
            host, error = canonicalize_host(entry)
            if error is not None:
                raise ConfigError(f"allowlist entry {entry!r} is not a valid host: {error}")
            assert host is not None
            canonical.append(host)
        if not canonical:
            raise ConfigError("allowlist must contain at least one host")
        self._allowed: frozenset[str] = frozenset(canonical)
        self._display: tuple[str, ...] = tuple(sorted(self._allowed))

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        return self._display

    def check(self, url: str) -> LinkDecision:
        """Classify one URL.  Never raises for user-controlled input."""
        if not isinstance(url, str):
            return _reject(MALFORMED_URL, "")
        if url == "":
            return _reject(EMPTY_URL, url)

        for char in url:
            if char.isspace() or unicodedata.category(char) in ("Cc", "Cf"):
                return _reject(CONTROL_CHARS, url)
        if "\\" in url:
            return _reject(BACKSLASH, url)

        try:
            parts = urlsplit(url)
        except ValueError:
            return _reject(MALFORMED_URL, url)

        if parts.scheme.lower() != "https":
            return _reject(SCHEME_NOT_HTTPS, url)
        if "@" in parts.netloc or parts.username is not None or parts.password is not None:
            return _reject(CREDENTIALS, url)

        raw_host = parts.hostname
        if raw_host is None or raw_host == "":
            return _reject(MISSING_HOST, url)

        try:
            parts.port  # noqa: B018 - raises ValueError for bad ports
        except ValueError:
            return _reject(INVALID_PORT, url)

        host, error = canonicalize_host(raw_host)
        if error is not None or host is None:
            return _reject(error or MALFORMED_HOST, url, host)
        if host not in self._allowed:
            return _reject(HOST_NOT_ALLOWED, url, host)
        return _allow(url, host)


def load_policy(path: str | Path) -> LinkPolicy:
    """Load an allowlist from JSON.

    Accepted shapes::

        {"allowed_doc_hosts": ["docs.example.com"]}
        ["docs.example.com"]

    Unknown keys in the object form are ignored, but a missing/empty list is an
    error rather than an implicit "allow everything".
    """
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"cannot read allowlist {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in allowlist {path}: {exc}") from exc

    if isinstance(data, dict):
        if "allowed_doc_hosts" not in data:
            raise ConfigError(f"allowlist {path} must contain 'allowed_doc_hosts'")
        hosts = data["allowed_doc_hosts"]
    elif isinstance(data, list):
        hosts = data
    else:
        raise ConfigError(f"allowlist {path} must be a JSON object or array")

    if not isinstance(hosts, list):
        raise ConfigError(f"allowlist {path}: 'allowed_doc_hosts' must be an array")
    return LinkPolicy(hosts)
